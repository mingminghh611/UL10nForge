"""识别率报告：分母普查清册 vs 提取池差集 + 证明链分母 + 形态统计。

这是「看不见」问题的收官组件：每个游戏跑完得到——
1. 普查清册（census.py）：全量字节文本（盲区可见化）；
2. 提取池：三个提取通道（asset/mono/il2cpp）的全部条目原文集合；
3. 缺口清单：清册有、池没有的文本，按字符串级规则归因
   （该跳=已解释计数；无法解释=盲区样本，按可疑度排序，等待
   新载体登记或新规则——哑信号变工作队列）；
4. 证明链分母：程序集 #US 堆全集 / 已证明 UI 串 / 含空格串
   （代码侧文本的精确分母，见 mono_dll._verified_ui_user_string_tokens）；
5. 形态级 skipped 率：按形态 × reason 分解（dense 形态高跳过率 → 告警）。

Phase 3 还将接入：per-game 识别率下限 = 证明链条目 / (清册总量 +
#US 堆总量)；typetree 覆盖率需 TypeTreeGenerator（Mono 游戏由
project._build_typetree_generator 构建，报告模块暂不构建——raw scan
兜底不影响缺口分析的完整性）。
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from hanhua.core.census import CensusResult, sweep_game
from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.unity import extractor as asset_ex
from hanhua.core.unity import il2cpp as il2cpp_ex
from hanhua.core.unity import mono_dll
from hanhua.core.tooling.morphology import (Morphology, classify_morphology,
                                            morphology as morphology_by_name)

# dense 形态（文本先验=几乎全是显示文本）的 skipped 率告警阈值：
# 超过即意味着「该形态整类被规则跳过」——0.14.0 的 UnityScript 825 条
# 遗漏案例（881 skipped 94%）应在此触发告警而非等待用户实测。
_DENSE_SKIP_RATE_ALARM = 0.85


@dataclass
class GapItem:
    rel_path: str
    offset: int
    encoding: str
    text: str
    disposition: str      # explained:<reason> | unexplained


@dataclass
class RecognitionReport:
    game_dir: str
    game_name: str
    # 普查
    census_hits: int = 0
    census_files: int = 0
    census_bytes: int = 0
    census_skipped: dict[str, int] = field(default_factory=dict)
    census_truncated: tuple[int, int] = (0, 0)
    # 提取池
    pool_entries: int = 0
    pool_display: int = 0          # 可译条目（status≠skipped）
    pool_skipped: int = 0
    pool_morphology: dict[str, tuple[int, int]] = field(default_factory=dict)
    pool_originals: frozenset = frozenset()  # 全部条目原文集合（召回测量用）
    pool_actionable: frozenset = frozenset()  # 可译条目原文（disposition=translate）
    # 缺口
    gaps: list[GapItem] = field(default_factory=list)
    gap_explained: dict[str, int] = field(default_factory=dict)
    # 证明链分母（mono）
    us_heap_total: int = 0
    us_heap_verified: int = 0
    us_heap_structural: int = 0      # 结构证明（按名查找键确定性跳过）
    us_heap_with_space: int = 0
    mono_files: int = 0
    # 脚本类队列（识别 L9：未登记类名 = 类注册表下一条登记候选）
    script_classes: dict[str, int] = field(default_factory=dict)
    # 未证明含空格堆串样本（证明率告警的配套工作队列）
    us_unverified_samples: list[str] = field(default_factory=list)
    # 告警
    alarms: list[str] = field(default_factory=list)

    @property
    def gap_unexplained(self) -> int:
        return sum(1 for g in self.gaps if g.disposition == "unexplained")

    def top_unexplained(self, limit: int = 100) -> list[GapItem]:
        """按可疑度排序的未解释缺口：长、含空格的排前。"""
        return sorted(
            (g for g in self.gaps if g.disposition == "unexplained"),
            key=lambda g: (" " in g.text and len(g.text) >= 8,
                           len(g.text)),
            reverse=True)[:limit]


def _string_disposition(text: str) -> str:
    """字符串级归因：能被既有规则解释的缺口不计入盲区。"""
    from hanhua.core.engine_strings import is_engine_string_core
    from hanhua.core.placeholders import is_hard_structural, should_skip
    from hanhua.core.tmp_tags import is_pure_tags
    from hanhua.core.unity.extractor import _is_script_code_line
    if not any(ch.isalpha() for ch in text):
        return "explained:no_letters"
    # F48（holo-dungeon 实证）：混淆/加密数据——≥60 字符、无空格、
    # 纯字母（或字母+数字）随机串（'jefimiz.gh' 的 148 字符纯字母
    # 混淆流）——非自然语言形态（自然语言必有空格/标点/重复结构）
    if (len(text) >= 60 and " " not in text
            and all(ch.isalpha() or ch.isdigit() for ch in text)):
        return "explained:obfuscated_data"
    if is_pure_tags(text):
        return "explained:tmp_pure_tags"
    if should_skip(text):
        return "explained:key_identifier"
    if is_hard_structural(text):
        return "explained:hard_structural"
    if is_engine_string_core(text):
        return "explained:engine_core"
    if _is_script_code_line(text):
        return "explained:code_line"
    return "unexplained"


# 覆盖率接线（0.38.0）：scan_all 末尾的轻量 census 差集上限——
# census 全树扫描本身有预算（_MAX_RUNS_TOTAL=200k），差集逐条归因
# 也受此上限约束；超限截断并如实计数，报告不假装扫完。
_MAX_COVERAGE_GAPS = 20_000


def coverage_gaps(game_dir: str | Path, pool_originals: Iterable[str],
                  *, exclude_roots: Iterable[str | Path] = ()
                  ) -> CensusResult:
    """轻量覆盖率缺口：census 全树普查 − 提取池 = 未覆盖文本。

    scan_all 专用（build_report 已内置同口径差集，不要重复调用）：
    提取池直接复用 store 已落库条目原文（零额外提取开销），census
    沿用独立字节级双通道。返回的 CensusResult.hits 已剔除进池命中，
    每个残差带 _string_disposition 归因（disposition 属性；hit 是
    frozen dataclass，归因在残差构造时计算并挂在扩展字段上）。
    """
    from hanhua.core.census import sweep_game as _sweep
    pool = frozenset(pool_originals)
    result = _sweep(game_dir, exclude_roots=exclude_roots)
    gaps: list[GapItem] = []
    for hit in result.hits:
        if hit.text in pool:
            continue
        gaps.append(GapItem(hit.rel_path, hit.offset, hit.encoding,
                            hit.text, _string_disposition(hit.text)))
        if len(gaps) >= _MAX_COVERAGE_GAPS:
            result.runs_truncated_total += max(
                0, len(result.hits) - len(pool) - len(gaps))
            break
    result.hits = gaps  # CensusResult.hits 是可变 list
    return result


def coverage_summary(gaps: Iterable[GapItem]) -> dict:
    """缺口归因聚合 → AnalysisReport 字段可序列化摘要。

    unexplained 是盲区工作队列（等待新载体登记或新规则），单列
    而非折算进覆盖率（归因规则宽严不应影响覆盖率口径）；samples
    按可疑度排序（含空格长句优先）供 UI/日志展示。"""
    gaps = list(gaps)
    explained: dict[str, int] = {}
    unexplained: list[GapItem] = []
    for gap in gaps:
        if gap.disposition.startswith("explained:"):
            reason = gap.disposition.split(":", 1)[1]
            explained[reason] = explained.get(reason, 0) + 1
        else:
            unexplained.append(gap)
    return {
        "gap_total": len(gaps),
        "explained": explained,
        "unexplained": len(unexplained),
        "unexplained_samples": [
            g.text for g in sorted(
                unexplained,
                key=lambda g: (" " in g.text and len(g.text) >= 8,
                               len(g.text)),
                reverse=True)[:20]],
    }


def _run_extractor(fn: Callable, path: Path, rel: str,
                   entries: list, morph_counts: dict,
                   script_classes: dict | None = None) -> None:
    try:
        pf = fn(path)
    except Exception:  # noqa: BLE001 单文件失败不阻断报告
        return
    morph = classify_morphology(rel)
    if morph is not None:
        total, skipped = morph_counts.get(morph, (0, 0))
        morph_counts[morph] = (
            total + len(pf.entries),
            skipped + sum(1 for e in pf.entries
                          if e.status == STATUS_SKIPPED))
    if script_classes is not None:
        for name in (pf.meta or {}).get("script_classes", ()):
            script_classes[name] = script_classes.get(name, 0) + 1
    entries.extend(pf.entries)


def _mono_cross_sinks(dll_files: list[Path]) -> frozenset:
    """多程序集 UI sink 闭包（提取池与分母共用同一口径）。"""
    if len(dll_files) < 2:
        return frozenset()
    try:
        import dnfile
        return mono_dll._cross_assembly_ui_sinks([
            dnfile.dnPE(str(f)) for f in dll_files])
    except Exception:  # noqa: BLE001
        return frozenset()


def _mono_denominators(dll_files: list[Path], report: RecognitionReport,
                       cross_sinks: frozenset) -> None:
    """程序集 #US 堆分母：堆全集（编译器枚举）与已证明 UI 串数。

    跨程序集闭包在多 DLL 游戏上联合计算，与提取侧同口径
    （deadbeat 实证 +10 条真实 UI 文本证明）。
    """
    report.mono_files = len(dll_files)
    for f in dll_files:
        try:
            import dnfile
            pe = dnfile.dnPE(str(f))
            us = pe.net.user_strings
            if us is None:
                continue
            data = us.get_data_at_offset(0, us.sizeof())
            records = mono_dll._walk_us_heap_records(data)
            report.us_heap_total += len(records)
            structural: set = set()
            verified = mono_dll._verified_ui_user_string_tokens(
                pe, cross_sinks=cross_sinks,
                structural_out=structural)
            report.us_heap_verified += len(verified)
            report.us_heap_structural += len(structural)
            report.us_heap_with_space += sum(
                1 for _, _, raw in records if b" " in raw)
            # 未证明含空格串样本（≤12 条/游戏，告警的可行动配套）
            if len(report.us_unverified_samples) < 12:
                for token, _, raw in records:
                    if (b" " not in raw and b"\t" not in raw
                            and b"\n" not in raw):
                        continue
                    if token in verified or token in structural:
                        continue
                    text = raw[:-1].decode("utf-16-le", errors="replace")
                    if len(text) >= 8 and text not in report.us_unverified_samples:
                        report.us_unverified_samples.append(text)
                    if len(report.us_unverified_samples) >= 12:
                        break
        except Exception:  # noqa: BLE001
            continue


def build_report(game_dir: str | Path, *,
                 progress_cb: Callable | None = None) -> RecognitionReport:
    root = Path(game_dir)
    report = RecognitionReport(
        game_dir=str(root), game_name=root.name)

    # ── 提取池 ──
    entries: list[TextEntry] = []
    morph_counts: dict[str, tuple[int, int]] = {}
    # F51b（shellcore 实证）：识别报告必须包含松散文本文件扫描
    # （scanner.discover）——否则 .corescript 对话/.txt/.json 等文本
    # 载体不进提取池，缺口报告把真盲区当"census 假盲区"跳过（原
    # build_report 只扫 Unity 资源 + DLL + census，文本文件缺失）。
    try:
        from hanhua.core.scanner import discover
        text_files = discover(root)
    except Exception:  # noqa: BLE001
        text_files = []
    from hanhua.core.extractor import parse_file as parse_text_file
    for i, f in enumerate(text_files):
        if progress_cb:
            progress_cb("text", i + 1, len(text_files))
        rel = str(f.relative_to(root)).replace("\\", "/")
        try:
            pf = parse_text_file(f)
        except Exception:  # noqa: BLE001 单文件失败不阻断报告
            continue
        for e in pf.entries:
            entries.append(e)
            morph_counts.setdefault("text_file", (0, 0))
            morph_counts["text_file"] = (
                morph_counts["text_file"][0] + 1,
                morph_counts["text_file"][1]
                + (1 if e.status == STATUS_SKIPPED else 0))
    try:
        asset_files = asset_ex.find_asset_files(root)
    except Exception:  # noqa: BLE001
        asset_files = []
    for i, f in enumerate(asset_files):
        if progress_cb:
            progress_cb("asset", i + 1, len(asset_files))
        rel = str(f.relative_to(root)).replace("\\", "/")
        _run_extractor(asset_ex.extract_asset_file, f, rel,
                       entries, morph_counts, report.script_classes)
    try:
        dll_files = mono_dll.find_dll_files(root)
    except Exception:  # noqa: BLE001
        dll_files = []
    cross_sinks = _mono_cross_sinks(dll_files)
    for i, f in enumerate(dll_files):
        if progress_cb:
            progress_cb("mono", i + 1, len(dll_files))
        rel = str(f.relative_to(root)).replace("\\", "/")
        _run_extractor(
            functools.partial(mono_dll.extract_dll_user_strings,
                              cross_sinks=cross_sinks),
            f, rel, entries, morph_counts)
    meta = next(root.rglob("global-metadata.dat"), None)
    if meta is not None:
        if progress_cb:
            progress_cb("il2cpp", 1, 1)
        rel = str(meta.relative_to(root)).replace("\\", "/")
        _run_extractor(il2cpp_ex.extract_metadata_strings, meta, rel,
                       entries, morph_counts)

    report.pool_entries = len(entries)
    report.pool_display = sum(
        1 for e in entries if e.status != STATUS_SKIPPED)
    report.pool_skipped = sum(
        1 for e in entries if e.status == STATUS_SKIPPED)
    report.pool_morphology = morph_counts
    report.pool_originals = frozenset(e.original for e in entries)
    from hanhua.core.models import is_actionable_translation
    report.pool_actionable = frozenset(
        e.original for e in entries if is_actionable_translation(e))

    # ── 普查 + 缺口差集 ──
    pool_originals = {e.original for e in entries}
    census: CensusResult = sweep_game(root)
    report.census_hits = len(census.hits)
    report.census_files = census.files_scanned
    report.census_bytes = census.bytes_scanned
    report.census_skipped = census.files_skipped
    report.census_truncated = (census.runs_truncated_file,
                               census.runs_truncated_total)
    for hit in census.hits:
        if hit.text in pool_originals:
            continue
        disposition = _string_disposition(hit.text)
        report.gaps.append(GapItem(
            hit.rel_path, hit.offset, hit.encoding, hit.text, disposition))
        if disposition.startswith("explained:"):
            reason = disposition.split(":", 1)[1]
            report.gap_explained[reason] = \
                report.gap_explained.get(reason, 0) + 1

    # ── 证明链分母 ──
    _mono_denominators(dll_files, report, cross_sinks)

    # ── 形态级 skipped 率告警（dense 形态高跳过率 = 整类遗漏信号）──
    for name, (total, skipped) in morph_counts.items():
        if total == 0:
            continue
        morph = morphology_by_name(name)
        if morph is None:
            continue
        rate = skipped / total
        if morph.prior == "dense" and rate > _DENSE_SKIP_RATE_ALARM:
            report.alarms.append(
                f"形态 {name}（dense 先验）skipped 率 {rate:.0%} 超阈值"
                f"{_DENSE_SKIP_RATE_ALARM:.0%}：该形态文本可能整类被跳过"
                f"（{skipped}/{total}）")

    # ── 证明率告警 ──
    if report.us_heap_total and report.us_heap_with_space:
        verified_rate = report.us_heap_verified / report.us_heap_total
        if verified_rate < 0.01 and report.us_heap_with_space >= 10:
            report.alarms.append(
                f"程序集 #US 堆证明率 {verified_rate:.1%} 过低：堆中"
                f"{report.us_heap_with_space} 条含空格串仅 "
                f"{report.us_heap_verified} 条被证明流入 UI——代码侧显示"
                f"文本可能大面积未识别")
    return report


def format_report(report: RecognitionReport, *, gap_limit: int = 60) -> str:
    """人类可读报告文本（CLI/GUI 共用）。"""
    lines: list[str] = []
    lines.append(f"识别报告：{report.game_name}")
    lines.append("")
    lines.append(f"提取池：{report.pool_entries} 条目"
                 f"（可译 {report.pool_display} / 跳过 {report.pool_skipped}）")
    for morph, (total, skipped) in sorted(report.pool_morphology.items()):
        rate = skipped / total if total else 0
        lines.append(f"  形态 {morph}: {total} 条（skipped {rate:.0%}）")
    lines.append("")
    lines.append(f"普查清册：{report.census_hits} 文本命中"
                 f"（{report.census_files} 文件）")
    lines.append(f"缺口：{len(report.gaps)} 条未进池"
                 f"（未解释 {report.gap_unexplained}）")
    for reason, count in sorted(report.gap_explained.items()):
        lines.append(f"  已解释 {reason}: {count}")
    if report.census_truncated != (0, 0):
        lines.append(f"  截断：每文件 {report.census_truncated[0]} / "
                     f"总量 {report.census_truncated[1]}")
    lines.append("")
    if report.mono_files:
        lines.append(f"代码侧分母（#US 堆）：{report.us_heap_total} 全集"
                     f" | 已证明 UI {report.us_heap_verified}"
                     f" | 已证明结构 {report.us_heap_structural}"
                     f" | 含空格 {report.us_heap_with_space}")
    from hanhua.core.unity.class_registry import disposition
    unknown = {name: count for name, count in report.script_classes.items()
               if disposition(name) is None}
    if unknown:
        lines.append("")
        lines.append(f"待登记类队列（{len(unknown)} 个未登记脚本类，"
                     f"按出现次数排序，人工裁决后进 class_registry）：")
        for name, count in sorted(unknown.items(),
                                  key=lambda kv: (-kv[1], kv[0]))[:30]:
            lines.append(f"  {name} ×{count}")
    if report.alarms:
        lines.append("")
        for alarm in report.alarms:
            lines.append(f"[告警] {alarm}")
        if report.us_unverified_samples:
            lines.append(f"  未证明含空格串样本（sink 扩容/规则裁决的工作队列）：")
            for sample in report.us_unverified_samples:
                text = sample.replace("\n", "␤").replace("\r", "")
                lines.append(f"    {text!r}")
    top = report.top_unexplained(gap_limit)
    if top:
        lines.append("")
        lines.append(f"未解释缺口样本（前 {len(top)}，按可疑度排序）：")
        for g in top:
            text = g.text.replace("\n", "␤").replace("\r", "")
            lines.append(f"  {g.rel_path}@{g.offset} [{g.encoding}] {text!r}")
    return "\n".join(lines)
