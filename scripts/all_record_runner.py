#!/usr/bin/env python3
"""地毯式排查单游戏 runner：真实完整流程 + 全环节记录。

与 GUI 走完全相同的代码路径（真实启动 llama-server、真实模型翻译、
真实文件写回），产出 docs/all record/<游戏名>/{summary.md, text/*, writeback/}。

用法:
  python scripts/all_record_runner.py <游戏目录> [--batch N] [--no-translate]
      [--no-writeback] [--keep-library] [--app-dir ~/.hanhua_sweep]

记录结构（docs/all record/<游戏名>/）:
  summary.md              # 排查总结：统计/发现的问题/修复项/闭环状态
  text/translated.txt     # 成功文本：来源/键位/原文/译文/置信度/原因/质量评分
  text/failed.txt         # 失败文本：来源/键位/原文/译文/失败原因/详情
  text/skipped.txt        # 跳过文本：来源/键位/原文/跳过原因/判定结论
  text/blocked.txt        # 阻断文本：语义审核重译未收敛（坏译文/审核理由/轮次）
  writeback/writeback.txt # 写回逐文件记录：文件/成功失败/详情/验证结果
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台 GBK 下 print 含 ⚠ 等非 GBK 字符（写回汇总等）会
# UnicodeEncodeError 崩溃（hickory 实证，2026-08-13）——强制 UTF-8
# 输出，errors=replace 兜底（不影响逻辑，只影响控制台显示）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from hanhua.core.agent_memory import AgentMemory  # noqa: E402
from hanhua.core.glossary import GlossaryStore  # noqa: E402
from hanhua.core.knowledge import KnowledgeBase  # noqa: E402
from hanhua.core.local_model import LocalModelManager  # noqa: E402
from hanhua.core.memory import settle_translation_memory  # noqa: E402
from hanhua.core.models import TextEntry  # noqa: E402
from hanhua.core.reviewer import (ReviewResult,  # noqa: E402
                                  SemanticReviewer, review_entries)
from hanhua.core.project import Project  # noqa: E402
from hanhua.core.prompts import (build_system_prompt,  # noqa: E402
                                 collect_known_names)
from hanhua.core.settings import SettingsStore  # noqa: E402
from hanhua.core.translator import create_client  # noqa: E402
from hanhua.core.batch_translator import BatchTranslator  # noqa: E402

_SEPARATOR = "─" * 64
DEFAULT_OUT_BASE = PROJECT_ROOT / "docs" / "all record"
REAL_USER_DIR = Path.home() / ".hanhua"

# 语言分布预检（多语言游戏识别盲区，faerie-afterlight 实证）：法语/日语/
# 印尼语条目混在英文游戏里，翻译把外语段当英文翻（12 kana 失败 + 印尼语
# 段保留误判）。扫描后统计原文语言特征写入 summary「语言分布」——分析者
# 据此知道本游戏含非英语文本，翻译需保留外语段（F22-3 已系统性豁免）。
_CJK_IDEOGRAPH = re.compile(r"[㐀-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ヿㇰ-ㇿ]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_ACCENT_LATIN = re.compile(
    r"[àáâäãåæçèéêëìíîïñòóôöõøùúûüýÿœšž]")
_ASCII_LETTER = re.compile(r"[A-Za-z]")
_LANG_CATEGORIES = (
    ("日语", _KANA),
    ("中文", _CJK_IDEOGRAPH),
    ("俄语/西里尔", _CYRILLIC),
    ("重音拉丁（法/西/德等）", _ACCENT_LATIN),
    ("英文/ASCII", _ASCII_LETTER),
)


def _language_profile(rows, sample_limit: int = 30000) -> list[tuple[str, int]]:
    """统计原文语言分布（按行抽样，特征优先级：假名>中文>西里尔>重音
    拉丁>ASCII）。返回 [(类别, 条数), ...]（仅非零类别，按条数降序）。"""
    counts = {name: 0 for name, _ in _LANG_CATEGORIES}
    other = 0
    total = 0
    for row in rows:
        original = str(row.get("original") or "")
        if not original:
            continue
        if total >= sample_limit:
            break
        total += 1
        for name, pattern in _LANG_CATEGORIES:
            if pattern.search(original):
                counts[name] += 1
                break
        else:
            other += 1
    out = [(name, n) for name, n in counts.items() if n]
    if other:
        out.append(("其他/无字母", other))
    out.sort(key=lambda item: item[1], reverse=True)
    return out


def _detect_builtin_chinese_pack(game_dir: Path) -> bool:
    """F52（8morelives 实证 2026-08-16）：游戏自带中文语言包检测。

    8morelives 是 10 语言游戏（TextAsset 每语言一个对象，obj=2145 为
    307/317 条纯中文包）——玩家选中文即完整汉化，翻译写回反而破坏
    语言选项（俄语包值被翻成中文）。判定：任一 TextAsset 对象 ≥50 条
    且 ≥70% 含 CJK 字符 → 自带中文。electric-trains 同规则（用户指令
    手动跳过）此前靠人工，现自动化。
    """
    from hanhua.core.unity import extractor as asset_ex
    try:
        cjk = _CJK_IDEOGRAPH
        has_chinese_obj = False
        total_entries = 0
        latin_entries = 0
        for f in asset_ex.find_asset_files(game_dir):
            try:
                pf = asset_ex.extract_asset_file(f)
            except Exception:  # noqa: BLE001 单文件失败不阻断
                continue
            objs: dict = {}
            for e in pf.entries:
                obj = (e.meta or {}).get("obj")
                if obj is not None:
                    objs.setdefault(obj, []).append(e.original)
                if e.original.strip():
                    total_entries += 1
                    if any(ord(c) < 0x2E80 for c in e.original):
                        latin_entries += 1
            for vals in objs.values():
                if len(vals) < 50:
                    continue
                zh = sum(1 for v in vals if cjk.search(v))
                if zh / len(vals) >= 0.7:
                    has_chinese_obj = True
        # 完整中文包判据 v2（Rendezvous 实证修正）：对象级 ≥50 条 ≥70%
        # 中文只说明「有中文数据」——Rendezvous 只有 Chapter1_CHN 一个
        # 对话中文版 + CSV CHN 部分行，其余 98% 文本仍英文，不是完整
        # 中文包（拦截会漏掉 14 个对话/数千条文本的汉化）。完整包 =
        # 中文对象存在 **且游戏文本主体不是英文**（8morelives 10 语言
        # 完整包：英文对象只占总量 1/10；Rendezvous 英文占 ~98%）
        if has_chinese_obj and total_entries > 0:
            if latin_entries / total_entries < 0.5:
                return True
    except Exception:  # noqa: BLE001 检测失败不阻断
        return False
    return False


def _safe_name(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "unnamed"


def _load_meta(row: dict) -> dict:
    raw = row.get("meta", {})
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _entry_from_row(row: dict) -> TextEntry:
    meta = _load_meta(row)
    reasons = meta.get("quality_reasons", [])
    return TextEntry(
        file_id=row["file_id"], key_path=row["key_path"],
        original=row["original"], translation=row.get("translation", ""),
        status=row.get("status", "pending"),
        locked=bool(row.get("locked", 0)),
        id=row.get("id"), meta=meta,
        confidence=str(meta.get("confidence", "medium")),
        quality_reasons=tuple(str(r) for r in reasons)
        if isinstance(reasons, list) else (),
    )


def _export_text_records(project, out_text: Path, profile,
                         model_name: str = "",
                         writeback_status: dict[str, str] | None = None,
                         review_results: dict[str, ReviewResult] | None = None) -> None:
    """导出 translated/failed/skipped/blocked 四类文本全字段记录。

    2026-08-22 记录升级：文本导出委托 record_writer 共享实现（GUI 与
    runner 同构——对象行/审核：行/处置行/哨兵分布/重译档案统一），
    本地保留薄签名转发，调用方无需改动。
    """
    from hanhua.core.record_writer import (
        _export_retranslated_records,
        _export_text_records as _rw_export_text_records,
    )
    _rw_export_text_records(
        project, out_text, profile,
        model_name=model_name,
        writeback_status=writeback_status,
        review_results=review_results)
    _export_retranslated_records(
        project, out_text, profile,
        model_name=model_name,
        writeback_status=writeback_status,
        review_results=review_results)


def _export_writeback_record(project, out_writeback: Path, profile,
                             result: dict | None, error_title: str = "",
                             error_detail: str = "") -> None:
    path = out_writeback / "writeback.txt"
    blocks = [
        f"游戏：{profile.game_name or Path(project.game_dir).name}",
        f"写回时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"输出目录：{project.out_dir}", "",
    ]
    # 逐文件记录（含翻译条目数）
    files = project.store.get_files()
    blocks += [f"文件清单：{len(files)} 个", ""]
    all_rows = project.store.get_entries()
    per_file: dict[str, int] = {}
    for row in all_rows:
        per_file[row["file_id"]] = per_file.get(row["file_id"], 0) + 1
    for f in files:
        blocks.append(
            f"- {f['rel_path']}（{per_file.get(f['id'], 0)} 条）")
    blocks.append("")
    if error_title:
        blocks += [_SEPARATOR, f"写回失败：{error_title}"]
        if error_detail:
            from hanhua.core.record_writer import _format_detail
            blocks.append(f"详情：{_format_detail(error_detail)}")
        blocks.append("")
    elif result is not None:
        verification = result.get("verification", {})
        gates = verification.get("gates", {})
        gate_lines = [
            f"  {name}={item.get('status', '?')}"
            for name, item in gates.items()
            if isinstance(item, dict) and name != "overall"
        ]
        blocks += [
            _SEPARATOR,
            "写回结果",
            f"文本文件：{result.get('text_files', '—')}",
            f"输入保护：{verification.get('input_protected')}",
            f"重开验证：{verification.get('reopen_verified')}",
            f"变更文件：{verification.get('changed_files')}",
            f"写入译文：{verification.get('written_translations')}",
            f"总体闸门：{verification.get('overall')}",
            f"字体层级：{verification.get('font_level')}",
            f"清单：{verification.get('manifest')}",
            f"备份：{verification.get('backup')}",
            "",
            "四态闸门明细",
            *gate_lines,
            "",
        ]
        gate = verification.get("font_gate")
        if gate:
            blocks += [
                "字体发布门",
                f"状态：{gate.get('status')}",
                f"说明：{gate.get('detail')}",
                "",
            ]
        bitmap = verification.get("font_bitmap")
        if bitmap:
            blocks += [
                "位图字体注入",
                f"provider：{', '.join(bitmap.get('providers') or []) or '—'}",
                f"注入：{bitmap.get('injected')} · "
                f"审计：{bitmap.get('audited')} · "
                f"未注入：{bitmap.get('pending')}",
                "",
            ]
        coverage = verification.get("font_coverage")
        if coverage:
            stacks = coverage.get("stack_counts") or {}
            stack_text = " · ".join(
                f"{kind}: {n}" for kind, n in sorted(stacks.items()))
            blocks += [
                "字体覆盖摘要",
                f"终态：{coverage.get('overall')}"
                f"（{stack_text or '无消费者'}）",
            ]
            missing = coverage.get("missing") or []
            if missing:
                blocks.append("缺字：")
                blocks += [
                    f"- {row.get('scalar')} → {row.get('consumer')}"
                    f"（{row.get('kind')}）"
                    for row in missing[:16]]
            blocks.append("")
        # 知识库案例转规则：writeback_case 5 条理论案例 → 可执行规则
        # （规则实现清单见 knowledge.writeback_case_rules，写回链路已启用）
        from hanhua.core.knowledge import writeback_case_rules
        rules = writeback_case_rules()
        blocks += [
            _SEPARATOR,
            f"知识库案例转规则：{len(rules)} 条已启用（writeback_case → 可执行检测）", ""]
        for rule in rules:
            blocks.append(
                f"- [{rule['rule']}] {rule['case'][:34]}（实现：{rule['impl'][:66]}）")
        blocks.append("")
        # 逻辑层审计（§写回逻辑层检查）：写回前敏感形态 / rawstr 扩容 /
        # 反向语义审计回退 / 互斥一致性 / 重开逻辑验证失败。warn 级全列，
        # note 级只列统计与抽样。
        logic_audit = verification.get("logic_audit") or []
        raw_expansions = verification.get("raw_expansions") or []
        logic_mismatches = verification.get("logic_mismatches") or []
        logic_reverted = verification.get("logic_reverted") or 0
        if (logic_audit or raw_expansions or logic_mismatches or logic_reverted):
            blocks += [_SEPARATOR, "逻辑层审计（写回逻辑敏感形态 / 扩容 / 语义回退 / 重开验证）", ""]
        if logic_mismatches:
            blocks += [f"重开逻辑验证失败：{len(logic_mismatches)} 项（写回整体拒绝）", ""]
            for item in logic_mismatches:
                blocks.append(f"- {item}")
            blocks.append("")
        # 反向语义审计：确定性逻辑键自动回退译文（保留原文）——知识库
        # 案例「UnityEvent 绑定断裂」「显示文本当逻辑键」转规则
        semantic_reverts = [
            a for a in logic_audit if a.get("stage") == "semantic_revert"]
        if semantic_reverts:
            blocks += [
                f"逻辑键自动回退（译文保留原文，防断链）：{logic_reverted} 条", ""]
            for item in semantic_reverts[:30]:
                blocks.append(
                    f"- [{item.get('reason')}] {item.get('original', '')[:40]}"
                    f" → {item.get('translation', '')[:40]}"
                    f"（{item.get('locator', '')[:70]}）")
            if len(semantic_reverts) > 30:
                blocks.append(f"…（其余 {len(semantic_reverts) - 30} 条）")
            blocks.append("")
        semantic_reports = [
            a for a in logic_audit if a.get("stage") == "semantic_report"]
        written_total = verification.get("written") or 0
        if semantic_reports:
            blocks += [
                f"疑似逻辑键（report，已写回需复核）：{len(semantic_reports)} 条", ""]
            for item in semantic_reports[:30]:
                blocks.append(
                    f"- [{item.get('reason')}] {item.get('original', '')[:40]}"
                    f" → {item.get('translation', '')[:40]}"
                    f"（{item.get('locator', '')[:70]}）")
            if len(semantic_reports) > 30:
                blocks.append(f"…（其余 {len(semantic_reports) - 30} 条）")
            # 召回率监控（防识别层哑信号）：疑似逻辑键占比超阈值 → 告警。
            # 高占比说明识别层放行了大量「对象角色不明」的标识符/比较词，
            # 游戏按名查找有断链风险，须人工复核而不是默默写回。
            if written_total and len(semantic_reports) / written_total > 0.05:
                blocks.append(
                    f"⚠ 疑似逻辑键占比 {len(semantic_reports)}/{written_total}"
                    f"（{len(semantic_reports) / written_total:.0%}）> 5%——"
                    f"识别层可能漏判逻辑键，建议复核上述条目后决定回退")
            blocks.append("")
        consistencies = [
            a for a in logic_audit if a.get("stage") == "consistency"]
        if consistencies:
            blocks += [f"同原文互斥一致性：{len(consistencies)} 组（全组保留原文防混排）", ""]
            for item in consistencies[:20]:
                blocks.append(
                    f"- {item.get('original', '')[:40]}"
                    f"（对象 {item.get('obj')}，出现 {item.get('count')} 次）"
                    f"：{item.get('reason', '')}")
            if len(consistencies) > 20:
                blocks.append(f"…（其余 {len(consistencies) - 20} 组）")
            blocks.append("")
        form_audits = [a for a in logic_audit if not a.get("stage")]
        warns = [a for a in form_audits if a.get("severity") == "warn"]
        if warns:
            blocks += [f"疑似逻辑字符串（warn，已写回需人工复核）：{len(warns)} 条", ""]
            for item in warns[:30]:
                blocks.append(
                    f"- [{item.get('pattern')}] {item.get('original', '')[:40]}"
                    f" → {item.get('translation', '')[:40]}"
                    f"（{item.get('locator', '')[:70]}）")
            if len(warns) > 30:
                blocks.append(f"…（其余 {len(warns) - 30} 条见 translated.txt 全量对照）")
            blocks.append("")
        notes = [a for a in form_audits if a.get("severity") != "warn"]
        if notes:
            blocks.append(
                f"短词/常见按钮文本（note，正常可译）：{len(notes)} 条"
                f"（抽样：{[n['original'] for n in notes[:8]]}）")
            blocks.append("")
        if raw_expansions:
            blocks += [f"rawstr 扩容写入：{len(raw_expansions)} 条（译文 UTF-8 字节 > 原文）", ""]
            for item in raw_expansions[:20]:
                blocks.append(
                    f"- {item.get('original', '')[:36]} → {item.get('translation', '')[:36]}"
                    f"（{item.get('src_bytes')} → {item.get('dst_bytes')} 字节，"
                    f"+{item.get('delta_bytes')}）")
            if len(raw_expansions) > 20:
                blocks.append(f"…（其余 {len(raw_expansions) - 20} 条）")
            blocks.append("")
        # 逐条明细：rejected/truncated 全量 + 回显跳过清单（written 条数
        # 大时只列统计与抽样，全文对照由 text/translated.txt 的「写回」字段承担）
        rejected = verification.get("rejected_entries", [])
        if rejected:
            blocks += [f"拒绝条目：{len(rejected)} 条", ""]
            for item in rejected:
                blocks.append(
                    f"- {item.get('locator', '?')}: {item.get('reason', '')}")
            blocks.append("")
        truncated = verification.get("truncated_entries", [])
        if truncated:
            blocks += [f"截断条目：{len(truncated)} 条（容量内部分翻译已写入）", ""]
            for item in truncated:
                blocks.append(
                    f"- {item.get('locator', '?')}: {item.get('reason', '')}"
                    if isinstance(item, dict) else f"- {item}")
            blocks.append("")
        # 回显跳过：译文与原文相同（模型保留原文，写回无变化被正确过滤）
        echoed = [
            row for row in all_rows
            if row.get("status") == "translated"
            and row.get("translation") == row.get("original")
        ]
        if echoed:
            blocks += [f"回显跳过（译文==原文，未写入）：{len(echoed)} 条", ""]
            for row in echoed:
                blocks.append(
                    f"- {row['file_id']}:{row.get('key_path', '')}："
                    f"{str(row.get('original', ''))[:60]}")
            blocks.append("")
            blocks.append("")
        warnings = verification.get("warnings", [])
        if warnings:
            blocks += [f"警告：{len(warnings)} 条", ""]
            for w in warnings:
                blocks.append(f"- {w}")
            blocks.append("")
    path.write_text("\n".join(blocks), encoding="utf-8")


def _register_unity_structure(kb: KnowledgeBase, game_name: str,
                              game_dir: Path, report) -> None:
    """登记 unity_structure（Unity 结构库闭环沉淀 §0.4.4-5）。

    每款游戏闭环登记：Unity 版本/runtime（fingerprint）+ 识别形态清单。
    后续遇到结构相似的新游戏 → 六库检索直接命中先验结构方案。"""
    from hanhua.core.tooling.fingerprint import fingerprint_game  # noqa: PLC0415
    try:
        fp = fingerprint_game(Path(game_dir))
        if fp and fp.unity_version and fp.unity_version != "unknown":
            kb.store.upsert(
                "unity_structure", "unity_version",
                f"游戏 {game_name}：Unity {fp.unity_version} · {fp.runtime}",
                action="info",
                map_to="该版本特征/风险见 unity_version 知识；"
                       f"runtime={fp.runtime} 决定写回路径"
                       "（mono→DLL #US，il2cpp→global-metadata）",
                source="auto", game=game_name)
    except Exception:  # noqa: BLE001
        pass  # 指纹识别失败不阻断流程
    for morph, files, entries in report.morphology_stats:
        kb.store.upsert(
            "unity_structure", "detect_method",
            f"游戏 {game_name} 形态 {morph}：{files} 文件 / {entries} 条",
            action="info", map_to=f"{morph} 形态（先验见识别形态清单）",
            source="auto", game=game_name)


def _register_writeback(kb: KnowledgeBase, game_name: str,
                        result: dict | None, error: str = "") -> None:
    """登记 writeback（写回验证库闭环沉淀 §0.4.4-5）。

    每次写回自动登记结果与关键验证指标——同类写回失败 → 六库检索
    直接命中历史写回方案（含四态闸门/字体层级/备份验证要点）。"""
    if error:
        kb.store.upsert(
            "writeback", "writeback_case",
            f"游戏 {game_name} 写回失败：{error[:60]}",
            action="check", map_to="按写回失败分类定位 writer 代码路径，"
                                   "根因修复 + 回归测试（§4 问题分类表）",
            source="auto", game=game_name)
        return
    if result is None:
        return
    verification = result.get("verification", {})
    gates = verification.get("gates", {})
    gate_fails = [name for name, item in gates.items()
                  if isinstance(item, dict) and name != "overall"
                  and item.get("status") == "fail"]
    overall = verification.get("overall")
    if gate_fails:
        map_to = ("验证要点：输入保护/重开验证/四态闸门/字体层级/备份齐全"
                  f"；未通过闸门：{'、'.join(gate_fails)}")
    else:
        map_to = "四态闸门全绿，按 test_flow 流程实测游戏验证"
    kb.store.upsert(
        "writeback", "writeback_case",
        f"游戏 {game_name} 写回完成：总体 {overall} · "
        f"译文 {verification.get('written_translations')} 条 · "
        f"变更文件 {verification.get('changed_files')} 个",
        action="verify", map_to=map_to,
        source="auto", game=game_name)


def _run_semantic_review(project, entries, out_dir: Path, game_name: str,
                         glossary: GlossaryStore,
                         skip: bool = False,
                         translator=None, app_dir: Path | None = None,
                         model_name: str = "", lang: str = "",
                         max_send_rate: float = 0.15) \
        -> tuple[dict[str, ReviewResult], dict]:
    """翻译后语义审核（翻译质量升级核心，2026-08-12）。

    对全部已翻译条目做语义级审核（术语/语境/专名/语义/风格五维），
    不合格条目标记「需要优化」并写 review 报告；术语词对自动沉淀
    全局术语库（后续游戏翻译按词对约束模型输出）。

    Phase A（2026-08-13 架构审计）：通过 review_entries 统一管线把终态
    原子落回 project.store——MAJOR/CRITICAL/blocked/审核错误不可写回
    （project.write_all → is_write_ready 读库生效），重启后状态仍正确。

    返回 (review_results: {locator: ReviewResult}, summary)。
    审核服务不可用/失败 → 返回空结果（不阻断写回，控制台告警）。
    条目构建/审核/词对沉淀复用 hanhua.core.reviewer.review_entries
    （GUI 主路径同源，翻译 C6 闭口——两入口行为一致）。

    max_send_rate：discretionary 送审率上限（P1-11 与 GUI 同一策略
    映射：fast 5% / balanced 15% / strict 30%）。
    """
    summary = {"reviewed": 0, "flagged": 0, "pairs": 0, "skipped": skip,
               "blocked": 0, "errors": 0, "cancelled": 0,
               "deferred_due_to_budget": 0}
    if skip:
        return {}, summary
    reviewer = SemanticReviewer(app_dir=app_dir or PROJECT_ROOT)
    if not reviewer.usable:
        print("  [审核] 跳过：本地审核服务不可用（模型缺失或启动失败）")
        return {}, summary
    # 设计文档 §16：Game Context 注入审校——runner 与 GUI 同一份游戏
    # 语境（背景/风格/角色/术语/注意事项）进审校 prompt，语境判定
    # 有据可依（GUI 翻译流程 profile= 已带 context_* 字段，runner
    # 此处同样透传）。审核提示注入失败不阻断主流程。
    try:
        _profile = project.profile
    except Exception:  # noqa: BLE001 - 语境注入失败不阻断审核主流程
        _profile = None
    core = review_entries(
        entries, glossary, game_name=game_name,
        on_note=lambda s: print(f"  [审核] {s}"),
        translator=translator, memory=project.store, store=project.store,
        app_dir=app_dir or PROJECT_ROOT, model_name=model_name, lang=lang,
        max_send_rate=max_send_rate, profile=_profile)
    if not core["used"]:
        print("  [审核] 无可审核条目（0 条已翻译）")
        return {}, summary
    flagged = core["flagged"]
    results = core["results"]
    summary["reviewed"] = core["reviewed"]
    summary["flagged"] = len(flagged)
    summary["blocked"] = core["blocked"]
    summary["errors"] = core["errors"]
    summary["cancelled"] = core["cancelled"]
    summary["deferred_due_to_budget"] = core["deferred_due_to_budget"]
    added = core["pairs_added"]
    rejected = core["pairs_rejected"]
    summary["pairs"] = added
    if core["blocked"]:
        print(f"  [审核] 重译未收敛阻塞 {core['blocked']} 条"
              f"（已从发布槽移除，需人工复核）")
    if core["errors"]:
        print(f"  [审核] 审核错误 {core['errors']} 条（不可发布）")
    if core["cancelled"]:
        print(f"  [审核] 取消 {core['cancelled']} 条")
    if core["deferred_due_to_budget"]:
        print(f"  [审核] 预算截断 {core['deferred_due_to_budget']} 条"
              f"（人工队列）")
    if added:
        print(f"  [审核] 术语沉淀 {added} 条词对 → 全局术语库"
              f"（后续游戏自动按词对约束翻译）")
    if rejected:
        print(f"  [审核] C5 门禁拒绝 {len(rejected)} 条污染风险词对"
              f"（高频普通词单 token，无语境区分）→ 不写入全局术语库")
    # 审核报告（2026-08-14 用户要求「审校后输出记录」：统一走
    # write_review_report——汇总 + CRITICAL 明细 + 全量送审明细，
    # 每条待审核文本的原文（保留富文本标签）/译文/AI 判定/未通过
    # 原因/终态完整留档，PASS 也记录，可逐条追溯）
    originals = core["originals"]
    locators = core["locators"]
    review_dir = out_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    try:
        from hanhua.core.reviewer import write_review_report
        write_review_report(core, review_dir / "review-report.md",
                            game_name=game_name)
    except Exception:  # noqa: BLE001 - 报告失败不阻断主流程
        pass
    try:
        (review_dir / "review.json").write_text(
            json.dumps([{
                "locator": locators.get(r.entry_id, r.entry_id),
                "original": originals.get(r.entry_id, ""),
                "verdict": r.verdict, "issue": r.issue,
                "reason": r.reason, "suggestion": r.suggestion,
            } for r in results.values()], ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    # Phase B-5（审计 P1-7）：审核失败结构化落库（fail_case 域）——
    # CRITICAL/MAJOR 语义错误与 REVIEW_ERROR 管线错误全部记录（收敛与
    # 未收敛均记）；正确例仅终态 APPROVED 系译文（二审收敛/人工确认）。
    # 幂等（game:locator pattern）：同条目重审只 hits+1。知识库可按原文
    # 召回同类失败作为反例（match_case 解析 note JSON original 字段）。
    review_failures = core.get("review_failures") or []
    if review_failures:
        case_kb = KnowledgeBase(REAL_USER_DIR / "knowledge.db")
        failure_added = 0
        for failure in review_failures:
            if case_kb.record_review_failure(failure):
                failure_added += 1
        case_kb.close()
        if failure_added:
            print(f"  [审核] 失败案例沉淀：{failure_added} 条审核失败"
                  f"结构化入库（错误译文/正确译文/理由留档，"
                  f"收敛 {sum(1 for f in review_failures if f['converged'])}"
                  f" 条）")
    # 映射回 locator（导出标注用）
    mapped: dict[str, ReviewResult] = {}
    for r in flagged:
        mapped[locators.get(r.entry_id, r.entry_id)] = r
    return mapped, summary


def _apply_official_zh(entries: list) -> int:
    """CSV 官方中文搬运 + ink ^ 前缀保护。

    CSV 条目 meta official_zh（目标语言列 CHN 的官方中文）→ translation
    直接用官方译文（覆盖模型译文）；ink 对话行 ^ 前缀是 ink 文本标记
    （模型可能丢失）→ 写回前补回。
    """
    moved = 0
    for e in entries:
        zh = e.meta.get("official_zh") if getattr(e, "meta", None) else None
        if zh and e.status != "skipped":
            e.translation = zh
            e.meta["translation_note"] = "official_zh"
            moved += 1
            continue
        if (e.meta.get("kind") == "ink" and e.translation
                and e.original.startswith("^")
                and not e.translation.startswith("^")):
            e.translation = "^" + e.translation
    return moved


def _apply_ink_official_zh(game_dir: Path, entries: list) -> int:
    """ink 对话语言版搬运：游戏目录中 `X_CHN` 对话存在时，`X_EN` 条目的
    同块同行用官方中文（块内行序一致——同源编译的 ink 语言版）。

    按 ink_base（Chapter1）+ 块名对齐：CHN 版块行数 == EN 版块行数 →
    逐行搬运（官方中文，含 ^ 前缀）；行数不同/块缺失（官方翻译未覆盖）
    → 保留模型译文（宁漏勿坏）。
    """
    from hanhua.core.unity import extractor as asset_ex
    import json as _json
    import re as _re
    import UnityPy
    chn: dict[str, dict[str, list[str]]] = {}
    for f in asset_ex.find_asset_files(game_dir):
        try:
            env = UnityPy.load(str(f))
        except Exception:  # noqa: BLE001
            continue
        for obj in env.objects:
            if obj.type.name != "TextAsset":
                continue
            try:
                t = obj.read()
                name = str(getattr(t, "m_Name", "") or "")
                text = t.m_Script
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(text, str) or not text.lstrip(
                    "﻿").startswith('{"inkVersion"'):
                continue
            m = _re.match(r"^(.*?)_([A-Z]{2,3})$", name)
            if not m or m.group(2) != "CHN":
                continue
            base = m.group(1)
            try:
                data = _json.loads(text.lstrip("﻿"))
            except Exception:  # noqa: BLE001
                continue
            blocks: dict[str, list[str]] = {}

            def walk(node: Any, cur: str) -> None:
                if isinstance(node, dict):
                    for k, v in node.items():
                        if (isinstance(k, str) and k.startswith("#")) or (
                                isinstance(k, str) and k == "->"):
                            continue
                        nb = cur
                        if (isinstance(k, str) and k not in ("root",)
                                and not k.startswith("^")
                                and not k[0].isdigit() and len(k) > 1):
                            nb = k
                        walk(v, nb)
                elif isinstance(node, list):
                    for v in node:
                        walk(v, cur)
                elif isinstance(node, str) and node.strip():
                    v = node.strip()
                    if v in ("done", "end") or not any(
                            c.isalpha() for c in v):
                        return
                    blocks.setdefault(cur, []).append(v)

            walk(data, "")
            chn.setdefault(base, {})
            for blk, lines in blocks.items():
                chn[base][blk] = lines
    if not chn:
        return 0
    # EN 版块条目数（ink_base + 块）——与 CHN 版块行数一致才搬运
    en_count: dict[tuple[str, str], int] = {}
    for e in entries:
        base = e.meta.get("ink_base")
        blk = e.meta.get("ink_block") or ""
        if not base or not blk or e.meta.get("reason") != "ink_dialogue_line":
            continue
        key = (base, blk)
        en_count[key] = en_count.get(key, 0) + 1
    moved = 0
    for e in entries:
        if e.meta.get("reason") != "ink_dialogue_line":
            continue
        base = e.meta.get("ink_base") or ""
        block = e.meta.get("ink_block") or ""
        seq = e.meta.get("ink_seq")
        if not isinstance(seq, int) or not base or not block:
            continue
        lines = (chn.get(base) or {}).get(block)
        if lines is None:
            continue
        # 块行数一致才对齐（官方翻译未覆盖的块宁漏勿坏）
        if en_count.get((base, block)) != len(lines):
            continue
        if seq < len(lines):
            e.translation = lines[seq]
            e.meta["translation_note"] = "official_ink_zh"
            moved += 1
    return moved


def run_game(game_dir: Path, *, batch: int | None = None,
             do_translate: bool = True, do_writeback: bool = True,
             keep_library: bool = False,
             do_review: bool | None = None,
             app_dir: Path | None = None,
             resume: bool = False,
             no_cleanup: bool = False,
             csv_overwrite_source: bool = False) -> int:
    """单游戏完整流程。返回退出码：0=流程完成（待分析），2=扫描阻断。

    resume=True：项目库已存在时跳过扫描+翻译，从语义审核/写回/导出继续
    （runner 中断恢复——faerie 实证：翻译完成 18634 条后卡在内嵌审核
    超时循环，重跑要重扫重翻 4.5h）。库不存在则报错退出。

    no_cleanup=True：写回成功后保留 `_汉化` 发布目录（默认闭环即删），
    供发布目录保留调试/复验（免实机测试流程，2026-08-12 指令）。
    """
    game_dir = Path(game_dir).resolve()
    if not game_dir.is_dir():
        print(f"[错误] 游戏目录不存在：{game_dir}")
        return 3
    game_name = _safe_name(game_dir.name)
    out_dir = Path(DEFAULT_OUT_BASE) / game_name
    out_text = out_dir / "text"
    out_writeback = out_dir / "writeback"
    out_text.mkdir(parents=True, exist_ok=True)
    out_writeback.mkdir(parents=True, exist_ok=True)

    # 独立项目库：固定 app_dir 按游戏 slug 分库。每次运行强制从零开始——
    # store.upsert 的「pending 不覆盖旧状态」断点续传语义会掩盖识别规则升级
    # （0.25.0 实证：DISPLAY_WORDS 修复后重扫，旧 skipped 条目判定未重跑），
    # 地毯式排查要求每次用最新代码重新判定，翻译记忆也一并清除（防记忆伪影）。
    # 注意：多游戏并行时只清理**本游戏**的 slug 目录——删整个 projects/
    # 会把并行 runner 的工作区一并删除（crash/crusty 并行实证：
    # WinError 32 project.db 被占用，后启动方删除先启动方工作区致其崩溃）。
    if app_dir is None:
        app_dir = Path.home() / ".hanhua_sweep"
    app_dir = Path(app_dir)
    projects_dir = app_dir / "projects"
    my_slug = hashlib.md5(
        str(Path(game_dir).expanduser().absolute()).encode("utf-8")
    ).hexdigest()[:10]
    my_dir = projects_dir / my_slug
    if resume and not my_dir.exists():
        print(f"[错误] --resume 但项目库不存在：{my_dir}")
        return 5
    if my_dir.exists() and not resume:
        shutil.rmtree(my_dir, ignore_errors=False)
    app_dir.mkdir(parents=True, exist_ok=True)
    projects_dir.mkdir(parents=True, exist_ok=True)

    settings = SettingsStore(REAL_USER_DIR / "settings.json")
    settings.load()
    api = settings.api
    # P1-11（审计 Phase D）：GUI/headless 审核策略统一——runner 读取与
    # GUI 相同的设置（ai_review_enabled 开关 + fast/balanced/strict 送审率
    # 上限），CLI --no-review 为最高优先级 override（CLI > settings > 默认）
    if do_review is None:
        do_review = bool(api.ai_review_enabled)
    strategy_rate = {
        "fast": 0.05, "balanced": 0.15, "strict": 0.30,
    }.get(str(api.ai_review_strategy or ""), 0.15)

    print(f"═══ 开始游戏：{game_name} ═══")
    print(f"输出：{out_dir}")
    print(f"项目库：{app_dir}")

    # ── 1 扫描 ──
    if resume:
        print("[1/4] 续跑：跳过扫描（使用现有项目库）")
        project = Project.open_game_dir(game_dir, app_dir)
        # open_game_dir 不建 schema（建表在扫描/翻译流程内）——resume 直接
        # 访问 store 会撞 "no such table"（hickory 实证：profile 表缺失）。
        # init_schema 幂等（IF NOT EXISTS），续跑前显式补齐。
        project.store.init_schema()
        report = None
        profile = project.profile
    else:
        print("[1/4] 扫描识别…")
        project = Project.open_game_dir(game_dir, app_dir)
        report = project.scan_all(
            csv_overwrite_source=csv_overwrite_source)
        profile = project.profile
        print(f"  文本文件 {report.text_files} · v2 文件 {report.v2_files}"
             f" · 识别条目 {report.recognized_entries}")
        for morph, files, entries in report.morphology_stats:
            print(f"  形态 {morph}: {files} 文件 / {entries} 条")
        # 知识库闭环：登记 Unity 结构（版本/runtime/形态）——后续结构相似的
        # 新游戏六库检索直接命中先验（§0.4.4-5 每游戏登记 unity_structure）
        struct_kb = KnowledgeBase(REAL_USER_DIR / "knowledge.db")
        _register_unity_structure(struct_kb, game_name, game_dir, report)
        struct_kb.close()
        for warning in report.warnings:
            print(f"  [警告] {warning}")
        if report.warnings:
            (out_dir / "scan_warnings.txt").write_text(
                "\n".join(report.warnings), encoding="utf-8")
        if not report.unblocked:
            print("[阻断] 扫描未通过，无法继续翻译/写回（见 summary.md）")
            _write_summary(project, report, None, None, game_name, out_dir,
                           blocked=True, language_profile=None)
            return 2
    # 语言分布预检：多语言游戏盲区（faerie 法语/日语/印尼语实证）——
    # 非英语文本占比高 → 分析者需注意保留外语段（续跑时同样执行）
    try:
        lang_profile = _language_profile(project.store.get_entries())
    except Exception:  # noqa: BLE001 - 预检失败不阻断流程
        lang_profile = None
    if lang_profile:
        print("  语言分布：" + "，".join(
            f"{name} {n}" for name, n in lang_profile))

    # F52（8morelives 实证）：游戏自带中文语言包 → 跳过汉化
    # （玩家选中文即完整汉化；翻译写回反而破坏语言选项——
    # 俄语/德语等语言包值被翻成中文写回）。electric-trains 同规则
    # 此前手动跳过，现自动化拦截。
    # 工具移植任务 1（2026-08-16）：游戏级 UnityCN 解密 key 探测——
    # 一次设置全局 key，后续所有加密 bundle 由 UnityPy 解析时自动
    # 解密（key 常驻 global-metadata.dat/游戏二进制，单文件探测
    # 可能 miss）
    try:
        from hanhua.core.unity.unitycn_decrypt import find_and_set_game_key
        if find_and_set_game_key(game_dir):
            print("  [unitycn] 检测到加密 bundle，解密 key 已设置",
                  flush=True)
    except Exception:  # noqa: BLE001 探测失败不阻断
        pass
    if _detect_builtin_chinese_pack(game_dir):
        print("[跳过] 游戏自带中文语言包（TextAsset 对象 ≥70% 中文）"
              "——玩家选中文即完整汉化，无需翻译", flush=True)
        try:
            _write_summary(project, report, None, None, game_name, out_dir,
                           error="游戏自带中文语言包，跳过汉化",
                           language_profile=lang_profile)
        except Exception:  # noqa: BLE001 摘要写入失败不阻断
            pass
        return 0

    # ── 2 翻译（真实本地模型） ──
    stats = None
    review_results: dict[str, ReviewResult] = {}
    review_summary: dict = {}
    entries: list = []
    glossary: GlossaryStore | None = None
    # Phase A：统一审核管线需要 translator（反馈重译/再审）与 lang（记忆
    # 键）；resume 续跑不重新翻译 → 无 translator，反馈重译路径自动跳过。
    translator = None
    lang = ""
    if do_translate and not resume:
        print("[2/4] 翻译（真实本地模型）…")
        manager = LocalModelManager(PROJECT_ROOT, startup_timeout=180)
        # ── 语境识别（任务 #15：runner 与 GUI 同链路）──
        # GUI 的「识别游戏语境」按钮走 _recognize_worker（本地=4B 审核
        # 模型 / 云端=create_client），识别结果经 save_game_context 同步
        # 进 game_profile.context_*——翻译 system prompt 与审校 hint 都
        # 从 profile 注入。headless runner 此前从未调用语境识别：批量
        # 闭环里 profile.context_* 恒空，Game Context 零注入（GUI 手动
        # 点过才有的语境，runner 全量跑反而没有）。此处补齐：本地模式
        # 走 ReviewModelService(4B)（与审核共用端口 8081，跑完审核阶段
        # 直接复用）；云端走 create_client。识别失败不阻断翻译（与 GUI
        # error 降级同语义），仅打印告警。
        try:
            from hanhua.core.game_context import (
                GameContextRecognizer, game_context_summary,
                parse_game_context, sample_entries, save_game_context)
            rows = project.store.get_entries()
            samples = sample_entries(rows)
            if samples:
                ctx_config = api
                if api.mode == "local":
                    from hanhua.core.review_server import ReviewModelService
                    _svc = ReviewModelService(PROJECT_ROOT)
                    _info = _svc.ensure_running()
                    ctx_config = replace(
                        api, base_url=_info["base_url"],
                        api_key=_info["api_key"], model="game-context")
                _recognizer = GameContextRecognizer(ctx_config)
                _raw = _recognizer.recognize(
                    samples,
                    source_lang=str(profile.source_lang or "") or "auto")
                _ctx = parse_game_context(_raw)
                _ctx["_sampled_total"] = len(rows)
                save_game_context(project.store, _ctx)
                _summary = game_context_summary(_ctx)
                if _summary:
                    print(f"  [语境] 游戏语境已建立：{_summary}")
                    # profile 已被 save_game_context 更新——重新读出，
                    # 让下方 build_system_prompt 注入 context_* 字段
                    profile = project.profile
                else:
                    print("  [语境] 识别完成但无可注入内容（全「未知」）"
                          "——按无语境翻译")
            else:
                print("  [语境] 无可识别文本样本，跳过语境识别")
        except Exception as _ctx_exc:  # noqa: BLE001 识别失败不阻断翻译
            print(f"  [语境] 识别失败（{_ctx_exc}）——按无语境翻译，"
                  "不阻断流程")
        # 2026-08-16 用户指令：智能上下文——local_context_auto 时按原文
        # 统计（最长/平均原文 → 预估译文 token）计算安全合理 ctx，不再
        # 用固定值（drova 6144 固定导致大批次降级逐条/显存冗余）。
        # 必须在 ensure_running 之前（ctx 决定 llama 启动的 KV 分配）。
        if getattr(api, "local_context_auto", False):
            from hanhua.core.context_size import smart_context_size
            _bs = batch if batch is not None else max(1, int(api.local_batch_size))
            _origins = [str(r.get("original") or "") for r in project.store.get_entries()]
            _ctx = smart_context_size(_origins, batch_size=_bs,
                                      max_tokens=int(api.max_tokens))
            api = replace(api, local_context_size=_ctx)
            print(f"  [智能上下文] 按文本统计计算 ctx={_ctx}"
                  f"（条目 {len(_origins)} 条 · 批量 {_bs}）", flush=True)
        try:
            runtime = manager.ensure_running(api)
            api = replace(api, base_url=runtime.endpoint,
                          api_key=runtime.api_key, model=runtime.model)
            print(f"  服务就绪：{runtime.backend.upper()} · 端口 {runtime.port}")
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 本地模型启动失败：{exc}")
            _write_summary(project, report, None, None, game_name, out_dir,
                           error=f"本地模型启动失败：{exc}",
                           language_profile=lang_profile)
            return 4

        glossary = GlossaryStore(REAL_USER_DIR / "glossary.db")
        glossary.init_schema()
        # 2026-08-14 用户要求「大大精简提示词」：术语/专名/知识不再
        # 全量拼 system_prompt（296 条术语 ≈ 2800 tokens 是 request
        # exceeds context 根因）——全部改为按条目检索命中注入：
        # glossary_hits / knowledge_hits / 向量召回（见 build_batch_user_prompt
        # 与 batch_translator._build_item）；全量词对仍经 glossary 参数
        # 供条目级命中匹配与确定性直填
        glossary_rows = glossary.list_all()

        # 知识库：跨游戏沉淀的特殊情况规则（全大写动作指令/间隔动作词等
        # 「该翻未翻」模式 + 处置策略），注入翻译，跑完 learn 再积累。
        # 2026-08-14 用户要求：不再全量拼 system_prompt——知识对照由
        # BatchTranslator 按原文 match_text 命中注入（knowledge 实例
        # 全程存活至 run 结束），内置形态规则已由翻译规则 6/11 覆盖。
        # format_reference_pairs 的译例并入 glossary——native 降级重试
        # （Hy-MT2 无 system prompt）靠 references 的 terms 机制带出译例
        knowledge = KnowledgeBase(REAL_USER_DIR / "knowledge.db")
        knowledge_pairs = knowledge.format_reference_pairs()
        # 经验记忆（AgentMemory）：跨游戏持久、证据驱动。本次运行前
        # 重置会话统计；active 记忆译例并入 glossary（混合运用参考档）；
        # 高置信短语由 BatchTranslator 翻译前直接应用（仍过质量门复查，
        # 拒绝则反馈降级直至退休）
        agent_memory = AgentMemory(REAL_USER_DIR / "agent_memory.db")
        agent_memory.init_schema()
        agent_memory.session_reset()
        agent_pairs = agent_memory.reference_pairs()
        # 知识检索统一门面（审计 Phase C，P1-1）：context/vector 证据跨
        # 游戏沉淀——翻译前 index_outbox 让历史证据可被本次命中，翻译后
        # 再次索引本次新沉淀（数据库与向量最终一致）
        from hanhua.core.knowledge_retrieval import (
            create_knowledge_retrieval)
        knowledge_retrieval = create_knowledge_retrieval(
            REAL_USER_DIR, game=game_name)
        indexed0 = knowledge_retrieval.index_outbox()
        print(f"  知识检索：{knowledge_retrieval.capability().summary()}"
              + (f" · 已索引 {indexed0} 条" if indexed0 else ""))

        entries = [_entry_from_row(r) for r in project.store.get_entries()]
        collected_names = collect_known_names(
            [str(e.original or "") for e in entries])
        # 2026-08-14：system_prompt 只含角色+精简规则（术语/专名/知识
        # 全量块已移除，全部按条目检索命中注入）
        system = build_system_prompt(profile, "")
        client = create_client(api)
        lang = f"{profile.source_lang or 'auto'}→{profile.target_lang or 'zh-CN'}"
        batch_size = batch if batch is not None else max(1, int(api.local_batch_size))
        concurrency = runtime.parallel if api.mode == "local" else api.concurrency

        def _restart_translate_service() -> None:
            """F42（8morelives 实证）：翻译服务死亡后重新拉起。

            llama-server 长任务中偶发被静默终止，批量层连续失败 ≥2 批
            时调用本回调——ensure_running 重新探测/启动服务，更新
            client 与 runtime（后续批在新服务上继续，不丢进度）。
            """
            nonlocal runtime, client, api
            try:
                new_rt = manager.ensure_running(api)
                api = replace(api, base_url=new_rt.endpoint,
                              api_key=new_rt.api_key, model=new_rt.model)
                runtime = new_rt
                client = create_client(api)
                translator.client = client  # noqa: F821 构造前赋值（下两行）
                print(f"  [F42] 翻译服务已重启（{new_rt.endpoint}）",
                      flush=True)
            except Exception as exc:  # noqa: BLE001 重启失败不阻断
                print(f"  [F42] 翻译服务重启失败：{exc}", flush=True)

        translator = BatchTranslator(
            client, batch_size=batch_size, concurrency=concurrency,
            memory=project.store, model=api.model, lang=lang,
            system_prompt=system,
            service_restart=_restart_translate_service,
            glossary=[(row["term"], row["translation"])
                      # 与 format_for_prompt 对齐：只取 active 词对做
                      # 强制约束——candidate（审核沉淀未跨游戏复现）仅
                      # 参考不强制（F10 实证：审核沉淀 <b> 标签垃圾词对
                      # b→整句译文 在 candidate 桶，list_all 全量并入
                      # 强制词对 → incremental-rts 'Analytics is ON.'
                      # 误杀 glossary_mismatch）
                      for row in glossary_rows
                      if row.get("status", "active") == "active"]
                     + knowledge_pairs + agent_pairs,
            # 质量门强制词对：术语库 active + 知识库译例；经验记忆词对
            # 只做参考注入不做强制（reference_pairs 设计「参考而非强制」，
            # Morfosi 64 条 ('Locked','锁定') 强制自然句全灭实证）
            glossary_force=[(row["term"], row["translation"])
                            for row in glossary_rows
                            if row.get("status", "active") == "active"]
                           + knowledge_pairs,
            agent_memory=agent_memory, agent_game=game_name,
            # 知识检索接线（审计 Phase C，P1-1）：语境直填 + 向量相似
            # 去重/召回在 headless 生产入口生效
            context_store=knowledge_retrieval.context_store,
            context_game=game_name,
            vector_recall=knowledge_retrieval.vector_recall,
            # 2026-08-14：按原文 match_text 精确命中注入历史特殊文本规则
            # （每条约 884 tokens 的全量对照改为只注入命中的几条，配合
            # 术语 limit=100 消除 ctx 2048 下的 request 超限）
            knowledge=knowledge,
        )
        from hanhua.core.models import is_actionable_translation
        pending_count = sum(is_actionable_translation(e) for e in entries)
        print(f"  条目 {len(entries)} · 待翻译 {pending_count}"
              f" · 批量 {batch_size} · 并发 {concurrency}")
        stats = translator.run(entries, progress_cb=None)
        print(f"  完成：{stats.done} 条（记忆 {stats.from_memory}）"
              f" · 失败 {stats.failed} · 请求 {stats.requests}"
              f" · 耗时 {stats.elapsed:.1f}s")
        # 官方中文搬运（Rendezvous 实证 2026-08-17）：游戏语言设置只有
        # 英文，目标语言列（CSV CHN 列）与 ink 语言版（Chapter1_CHN）
        # 的官方中文玩家读不到——搬运到实际显示位（写回源列/EN 版）。
        # 官方译文优先于模型译文（质量更高且零翻译成本）。
        # 注意：搬运修改内存 entries 后**必须写回 store**（写回阶段用
        # store 数据）——2026-08-17 实证：只改内存导致 Load Game 等
        # 官方译文丢失 + 56 条 ink ^ 前缀未补回。
        try:
            moved = _apply_official_zh(entries)
            ink_moved = _apply_ink_official_zh(game_dir, entries)
            if moved or ink_moved:
                project.store.batch_update_translation_results(entries)
                print(f"  官方中文搬运：CSV {moved} 条 · ink {ink_moved} 条"
                      "（官方译文优先，不重复翻译）", flush=True)
        except Exception:  # noqa: BLE001 搬运失败不阻断（保留模型译文）
            print("  [官方中文搬运] 失败，保留模型译文", flush=True)
        # 命中注入用的 knowledge 实例已用完，关闭（learn 用新实例）
        try:
            knowledge.close()
        except Exception:  # noqa: BLE001 关闭失败不阻断闭环
            pass
        # 翻译后：本次沉淀的共识证据入向量索引（Phase C，供下次/跨游戏复用）
        indexed1 = knowledge_retrieval.index_outbox()
        if indexed1:
            print(f"  知识检索：本次沉淀 {indexed1} 条证据入向量索引")
        # 经验记忆报告：本次会话记忆活动落盘（memory-report.md 与 GUI
        # 记录文档同构）——用户可追踪记忆如何成长、哪些记忆不可信
        mem_report = agent_memory.session_report(game=game_name)
        s = mem_report["session"]
        print(f"  经验记忆：提案 {s['proposed']} · 晋升 {s['confirmed']}"
              f" · 直接应用 {s['direct_applied']}"
              f"（采纳 {s['accepted']} / 拒绝 {s['rejected']}）"
              f" · 退休 {s['retired']}"
              f"{' · ⚠️ 语境冲突 ' + str(s['conflicts']) if s.get('conflicts') else ''}")
        try:
            from hanhua.core.record_writer import _write_memory_report
            _write_memory_report(out_dir, mem_report)
        except Exception as exc:  # noqa: BLE001 报告落盘失败不阻断闭环
            print(f"  [经验记忆] 报告落盘失败：{exc}")
        # 术语库学习：把本游戏确认保留的专名写入全局库，后续游戏自动复用
        learn_glossary = GlossaryStore(REAL_USER_DIR / "glossary.db")
        learn_glossary.init_schema()
        learned = learn_glossary.learn_proper_names(
            entries, collected_names, game_name)
        learn_glossary.close()
        if learned:
            print(f"  术语库学习：新增 {learned} 条专名（累计可跨游戏复用）")
        # 知识库学习：从「该翻未翻」回显条目沉淀新模式（幂等，hits+1）
        learn_knowledge = KnowledgeBase(REAL_USER_DIR / "knowledge.db")
        learned_kb, hits_kb = learn_knowledge.learn(
            entries, game_name, names=set(collected_names))
        learn_knowledge.close()
        if learned_kb or hits_kb:
            print(f"  知识库学习：新增 {learned_kb} 条规则"
                  f" · 累计命中 {hits_kb} 条（特殊情况模式沉淀）")
        # 失败案例自动沉淀：按质量原因组合聚合，同模式每款游戏 1 条
        # （幂等）——「经验大脑」持续积累，修复后再由 knowledge_seed.py
        # 补精确方案；识别层失败（结构规则）由手工案例覆盖
        failed_groups: dict[tuple, list[TextEntry]] = {}
        for e in entries:
            if e.status == "translated" or not e.translation:
                continue
            reasons = tuple(e.meta.get("quality_reasons", ()))
            if not reasons:
                continue
            failed_groups.setdefault(reasons, []).append(e)
        case_kb = KnowledgeBase(REAL_USER_DIR / "knowledge.db")
        case_added = 0
        for reasons, group in failed_groups.items():
            src = str(group[0].original).replace("\n", "\\n")[:48]
            if case_kb.record_case(
                    game=game_name, fail_type="翻译",
                    problem=f"翻译失败模式[{src}…]",
                    root_cause="质量门原因: " + ", ".join(reasons),
                    fix="见本场 fix record（降级链或结构规则）",
                    symptom=f"{len(group)} 条同模式失败",
                    impact="待核", version="", source="auto"):
                case_added += 1
            # 历史案例智能复用：同模式历史案例 → 提示已验证方案（避免重查）
            for past in case_kb.match_case(src, limit=2):
                if "见本场 fix record" in str(past.get("note", "")):
                    continue
                note = past["note"]
                try:
                    parsed = json.loads(note)
                    hint = (f"[知识库] 命中历史案例 {parsed.get('fail_no')} "
                            f"{parsed.get('game')}：{parsed.get('solution', '')[:70]}")
                except (ValueError, TypeError):
                    hint = f"[知识库] 命中历史案例：{note[:90]}"
                print(hint)
            # 质量库联动（死区接入，§0.4.4-5 六库闭环）：质量门拒绝时除
            # fail_case 外还检索 quality 域（scoring_case/common_error/
            # term_consistency 规则知识）——质量规则真实进入失败处理决策，
            # 而非只沉淀不查（用户两次追问知识库不是摆设）
            for past in case_kb.search_keyword(
                    src, domains=("quality",))[:2]:
                print(f"[知识库] 命中质量规则 {past['kind']}："
                      f"{str(past.get('note', ''))[:90]}")
        case_kb.close()
        if case_added:
            print(f"  失败案例沉淀：新增 {case_added} 种失败模式入库")
    elif resume:
        print("[2/4] 续跑：跳过翻译（store 已有译文），加载条目与术语库…")
        glossary = GlossaryStore(REAL_USER_DIR / "glossary.db")
        glossary.init_schema()
        entries = [_entry_from_row(r) for r in project.store.get_entries()]
        done = project.store.count("translated")
        failed = project.store.count("failed")
        skipped = project.store.count("skipped")
        print(f"  条目 {len(entries)} · 已翻译 {done} · 失败 {failed}"
              f" · 跳过 {skipped}")
        # 伪 stats：翻译细节见上轮（summary 按「续跑」来源标注）
        from types import SimpleNamespace
        stats = SimpleNamespace(
            total=len(entries), done=done, failed=failed, from_memory=0,
            requests=0, input_tokens=0, output_tokens=0,
            elapsed=0.0, rate_per_minute=0.0)
    else:
        print("[2/4] 跳过翻译（--no-translate）")

    # ── 翻译后语义审核（翻译质量升级，2026-08-12）──
    # 质量门只查机械问题（回显/格式/长度），Resume→简历 类语义错误
    # 检测不到；审核用强模型五维判定（术语/语境/专名/语义/风格），
    # 不合格条目标记「需要优化」+ 建议，术语词对沉淀全局库。
    # 续跑模式同样执行——runner 中断恢复的主战场正是审核（faerie 实证：
    # 翻译完成 18634 条后卡在旧 timeout=120 审核超时循环，重跑需 4.5h）。
    if do_review and (do_translate or resume):
        try:
            review_results, review_summary = _run_semantic_review(
                project, entries, out_dir, game_name,
                glossary=glossary, skip=False,
                translator=translator, app_dir=PROJECT_ROOT,
                model_name=api.model, lang=lang,
                max_send_rate=strategy_rate)
            if review_summary.get("flagged"):
                print(f"  [审核] 不合格 {review_summary['flagged']} 条"
                      f"（见 review/review-report.md，需人工确认）")
        except Exception as exc:  # noqa: BLE001 - 审核失败不阻断写回
            print(f"  [审核] 失败：{exc}")
            review_results, review_summary = {}, {}

    # Phase B PendingEvidence（审计 §5 P0-3）：审后记忆结算——与 GUI
    # 同源（settle_translation_memory）：APPROVED → promote；判坏 →
    # 撤销；无终态（--no-review / 审核器不可用）→ 机械门即最后裁决，
    # promote。任何分支都执行（审核跳过/失败不阻断结算）。
    settled = settle_translation_memory(
        project.store, entries, str(api.model or ""), lang)
    if settled.get("promoted") or settled.get("revoked"):
        print(f"  [记忆] 审后结算：提交 {settled['promoted']} 条"
              f" · 撤销坏记忆 {settled['revoked']} 条")

    # ── 3 写回（真实）──（先写回再导出，导出才能标注每条实际写回状态）
    writeback_result = None
    writeback_error = None
    if do_writeback:
        print("[3/4] 写回（真实）…")
        try:
            writeback_result = project.write_all(
                font_config=settings.font,
                stage_cb=lambda stage: print(
                    f"  [{stage.phase}] {stage.message}"),
                # 批量闭环（免实机 attest，2026-08-12 指令）：确认候选字体
                # 发布——PENDING_RUNTIME_ATTESTATION / CANDIDATE_ONLY 字体
                # 门降级候选 WARN；对象级闸门（rejected/truncated/逻辑
                # 验证）仍受 allow_partial=False 严格阻断（hickory 实证：
                # 动态 TMP 字体 + 插件已部署 → PENDING，不确认则永远
                # 无法发布闭环）。
                allow_unverified_font_candidate=True,
            )
            print(f"  写回成功：{writeback_result.get('text_files')} 文本文件"
                  f" · {writeback_result['verification'].get('written_translations')}"
                  " 条译文 · "
                  f"总体 {writeback_result['verification'].get('overall')}")
        except Exception as exc:  # noqa: BLE001
            writeback_error = str(exc)
            print(f"[错误] 写回失败：{exc}")
    else:
        print("[3/4] 跳过写回（--no-writeback）")
    _export_writeback_record(project, out_writeback, profile,
                             writeback_result,
                             error_title=("写回失败" if writeback_error else ""),
                             error_detail=writeback_error or "")

    # ── 写回后地毯式审计（2026-08-25 用户指令：写回要像翻译一样审核）──
    # 第 1 层确定性结构审计（字节/行数/结构/占位符/渲染一致）任何文件 FAIL
    # → needs_rewrite=True，阻断本轮闭环（结构与 containment-breach-hd 的
    # 卡死同源，必须重写回）；第 2 层审校模型软复核记 flag 供人工确认。
    # 只读对比源目录 vs 发布目录，不修改任何文件；模型不可用优雅跳过。
    if do_writeback and not writeback_error:
        print("[3/4] 写回审计…")
        from hanhua.core.writeback_audit import (
            audit_writeback, render_audit_report)
        audit_res = audit_writeback(
            project.store, project.game_dir, project.out_dir,
            run_model=True, app_dir=PROJECT_ROOT,
            font_enabled=bool(getattr(settings.font, "enabled", False)),
            on_note=lambda s: print(f"  {s}"))
        try:
            (out_writeback / "audit.txt").write_text(
                render_audit_report(audit_res, game_name),
                encoding="utf-8")
        except Exception:  # noqa: BLE001 - 报告写失败不阻断
            pass
        if audit_res.needs_rewrite:
            failed = ", ".join(
                f.rel_path for f in audit_res.failed_files[:5])
            writeback_error = (
                f"写回审计失败（结构破坏，需重写回）：{failed}"
                + (f" 等 {len(audit_res.failed_files)} 个文件" if
                   len(audit_res.failed_files) > 5 else ""))
            print(f"[错误] {writeback_error}")
            print("  → 详细见 writeback/audit.txt")
        elif audit_res.model_unavailable:
            # 模型复核请求了却不可用 → 第二道防线覆盖不完整 → 阻断发布
            # （不允许带审计缺口的写回发布；用户要求写回审核零错误、
            # 无需人工兜底）。audit.txt 里已有「模型不可用」说明。
            writeback_error = "写回审计不完整：审校模型服务不可用，已阻断发布（需先启动审核模型端口 8081）"
            print(f"[错误] {writeback_error}")
            print("  → 详细见 writeback/audit.txt")
        else:
            print(f"  写回审计通过：{len(audit_res.files)} 文件结构完整"
                  f" · 模型 FLAG {len(audit_res.model_flags)} 条（软复核）")
    elif do_writeback:
        try:
            (out_writeback / "audit.txt").write_text(
                f"游戏：{game_name}\n写回审计时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n写回失败，未执行审计（见 writeback.txt）\n",
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # 知识库闭环：写回结果自动登记 writeback 域（§0.4.4-5）
    wb_kb = KnowledgeBase(REAL_USER_DIR / "knowledge.db")
    _register_writeback(wb_kb, game_name, writeback_result, writeback_error)
    # 组件兼容库联动（死区接入）：写回失败时按错误信息检索 component_compat
    # 域（乱码/方块/黑屏/Dropdown 等组件兼容知识）——组件库真实进入写回
    # 失败处理，而非只种不用
    if writeback_error:
        for past in wb_kb.search_keyword(
                writeback_error, domains=("component_compat",))[:3]:
            print(f"[知识库] 命中组件兼容 {past['kind']}："
                  f"{str(past.get('note', ''))[:90]}")
    wb_kb.close()

    # ── 4 导出三类文本记录（含逐条写回状态）──
    print("[4/4] 导出文本记录…")
    writeback_status: dict[str, str] | None = None
    if writeback_result is not None:
        writeback_status = {
            item["locator"]: item["reason"]
            for item in writeback_result["verification"].get(
                "rejected_entries", [])
        }
    _export_text_records(project, out_text, profile,
                         model_name=str(api.model or ""),
                         writeback_status=writeback_status,
                         review_results=review_results or None)
    translated = project.store.count("translated")
    failed = project.store.count("failed")
    skipped = project.store.count("skipped")
    print(f"  translated {translated} · failed {failed} · skipped {skipped}")

    _write_summary(project, report, stats, writeback_result, game_name,
                   out_dir, error=writeback_error,
                   review_summary=review_summary or None,
                   language_profile=lang_profile)
    print(f"═══ {game_name} 记录完成：{out_dir} ═══")
    # 闭环成功 → 删汉化输出目录与发布备份，只保留原版（做完一个删一个）。
    # 写回失败不删（需排查/回滚，备份是回滚依据）。--no-cleanup 供
    # 发布目录保留调试（免实机测试流程）。
    if writeback_result is not None and not writeback_error and not no_cleanup:
        _cleanup_hanhua_output(game_dir)
    # 闭环成功 → 删库（keep_library=False 默认）。
    # 写回失败不删：库是 --resume 续跑凭据（译文 + 扫描绑定清单全在库里，
    # 删除后连 resume 都无法重试）。faerie 实证 2026-08-12：写回失败后
    # 库被无条件删除，18698 条译文仅靠 git 导出恢复——与「写回失败不删
    # _汉化」同一原则（955 行），失败需排查/重试。
    if not keep_library and not writeback_error:
        _discard_sweep_library(project)
    return 1 if writeback_error else 0


def _rmtree_force(path: Path) -> None:
    """删除目录树，Windows 上先清只读属性再删。

    从游戏目录复制的文件（tool-jobs 的 game.exe/global-metadata.dat）
    常带只读位——shutil.rmtree 遇只读文件 PermissionError，残留累积
    （0.25.0 实证：WinError 5 拒绝访问，每轮残留 tool-jobs 输入副本）。
    """
    def _clear_readonly(func, p, _exc):
        os.chmod(p, 0o777)
        func(p)
    shutil.rmtree(path, onerror=_clear_readonly)


def _cleanup_hanhua_output(game_dir: Path) -> None:
    """闭环后删除汉化输出目录与全部发布备份（只保留原版游戏目录）。

    backup 由写回发布流程生成（`.{name}_汉化.backup-<32hex>`，供失败
    回滚）；闭环成功后无回滚需求，一并删除，避免每轮残留 353MB。
    """
    out = game_dir.parent / (game_dir.name + "_汉化")
    targets = [out] + list(game_dir.parent.glob(
        f".{game_dir.name}_汉化.backup-*"))
    for target in targets:
        try:
            if target.exists():
                _rmtree_force(target)
                print(f"  已清理：{target.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 汉化输出清理失败（{target.name}）：{exc}")


def _discard_sweep_library(project) -> None:
    """清理本游戏的扫描/翻译中间库（仅**本游戏 slug** 目录）。

    Windows 上 sqlite 连接未关闭时 rmtree 会因文件句柄失败（0.25.0 实证：
    库残留导致重扫复用旧状态）。先 close 连接，删除失败则显式告警。

    只删本游戏 slug 目录（store.db 的父目录），不删整个 app_dir——
    双游戏并行时删 projects/ 会把并行 runner 的工作区一并删除
    （crash/crusty 并行实证：WinError 32 project.db 被占用；death-trips
    清理时 deepest-sword 库正被使用同证）。启动清理（§run 前 my_dir）
    与本处结束清理必须保持一致的目标目录。
    """
    try:
        project.store.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        _rmtree_force(Path(project.store.db).parent)
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] sweep 库清理失败（残留可能影响下次判定）：{exc}")


def _write_summary(project, report, stats, writeback_result, game_name,
                   out_dir: Path, *, blocked: bool = False,
                   error: str = "",
                   review_summary: dict | None = None,
                   language_profile: list[tuple[str, int]] | None = None) -> None:
    if report is None:
        # 续跑模式：报告字段从 store 统计构造（扫描详情见上轮 summary.md）
        from types import SimpleNamespace
        counts = {
            st: project.store.count(st)
            for st in ("pending", "translated", "failed", "skipped",
                       "blocked")
        }
        report = SimpleNamespace(
            text_files="（续跑，见上轮 summary.md）",
            v2_files="（续跑，见上轮 summary.md）",
            recognized_entries=sum(counts.values()),
            status_counts=list(counts.items()),
            morphology_stats=[],
            confidence_counts=[],
            tool_statuses=[],
            route=[],
            warnings=[],
            unblocked=True,
        )
    lines = [
        f"# {game_name} 地毯式排查记录",
        "",
        f"- 游戏目录：{project.game_dir}",
        f"- 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1 识别",
        f"- 文本文件：{report.text_files} · 二进制资源：{report.v2_files}",
        f"- 识别条目：{report.recognized_entries}",
        "- 语言分布（抽样预检，多语言游戏盲区）：",
        *[f"  - {name}: {count} 条" for name, count in language_profile or ()],
        "- 形态统计：",
        *[f"  - {m}: {f} 文件 / {e} 条" for m, f, e in report.morphology_stats],
        "- 状态分布：",
        *[f"  - {status}: {count}" for status, count in report.status_counts],
        "- 置信度分布：",
        *[f"  - {confidence}: {count}"
          for confidence, count in report.confidence_counts],
        "- 工具状态：",
        *[f"  - {item.tool_id}: {item.state}" for item in report.tool_statuses],
        "- 阻断步骤：",
        *[f"  - {step.step_id}: {step.status} {step.reason}"
          for step in report.route
          if step.required and step.status != "succeeded"],
    ]
    if report.warnings:
        lines += ["- 警告：", *[f"  - {w}" for w in report.warnings]]
    lines.append("")
    if blocked:
        lines += ["## 状态", "❌ 扫描阻断，未翻译未写回（分析：为什么阻断？）", ""]
    elif error:
        lines += ["## 状态", f"⚠️ 流程异常：{error}", ""]
    elif stats is not None:
        lines += [
            "## 2 翻译",
            f"- 总条目：{stats.total} · 完成：{stats.done}"
            f"（记忆命中 {stats.from_memory}） · 失败：{stats.failed}",
            f"- 请求：{stats.requests} · 输入 {stats.input_tokens} tokens"
            f" · 输出 {stats.output_tokens} tokens",
            f"- 耗时：{stats.elapsed:.1f}s · 吞吐 {stats.rate_per_minute:.0f} 条/分",
            "",
        ]
    else:
        lines += ["## 2 翻译", "- （未翻译）", ""]
    if writeback_result is not None:
        verification = writeback_result.get("verification", {})
        lines += [
            "## 3 写回",
            f"- 文本文件：{writeback_result.get('text_files')}"
            f" · 写入译文：{verification.get('written_translations')}",
            f"- 输入保护：{verification.get('input_protected')}"
            f" · 重开验证：{verification.get('reopen_verified')}"
            f" · 变更文件：{verification.get('changed_files')}",
            f"- 总体闸门：{verification.get('overall')}"
            f" · 字体：{verification.get('font_level')}",
        ]
        gate = verification.get("font_gate")
        if gate:
            lines.append(
                f"- 字体发布门：{gate.get('status')} — {gate.get('detail')}")
        lines.append("")
    else:
        lines += ["## 3 写回", "- （未写回）", ""]
    if stats is not None and review_summary:
        blocked_n = review_summary.get("blocked", 0)
        lines += [
            "## 3.5 语义审核（翻译质量升级）",
            f"- 审核条数：{review_summary.get('reviewed', 0)}"
            f" · 不合格：{review_summary.get('flagged', 0)}"
            f" · 重译未收敛阻断：{blocked_n}",
            f"- 术语沉淀：{review_summary.get('pairs', 0)}",
            "- 不合格清单见 review/review-report.md（需人工确认后优化）；"
            "阻断条目全字段明细见 text/blocked.txt",
            "",
        ]
    lines += [
        "## 4 分析（待办）",
        "- [ ] 成功文本质量抽检（译文是否得当/是否无关文本）",
        "- [ ] 语义审核不合格项确认与优化（review/review-report.md）",
        "- [ ] 失败文本根因系统彻查（同类问题全解）",
        "- [ ] 跳过文本逐条判定（该翻→识别修复；不该翻→记录判定）",
        "- [ ] 写回问题根源修复",
        "- [ ] 修复后用升级版本重跑本游戏全流程（闭环）",
        "- [ ] 闭环后删除汉化输出目录",
        "",
        "记录文件：",
        "- text/translated.txt / text/failed.txt / text/skipped.txt / "
        "text/blocked.txt",
        "- review/review-report.md / review.json（语义审核）",
        "- writeback/writeback.txt",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="地毯式排查单游戏 runner")
    parser.add_argument("game_dir", help="游戏目录")
    parser.add_argument("--batch", type=int, default=None,
                        help="覆盖本地批量大小（默认读 settings）")
    parser.add_argument("--no-translate", action="store_true",
                        help="跳过翻译（只扫描+记录）")
    parser.add_argument("--no-writeback", action="store_true",
                        help="跳过写回")
    parser.add_argument("--no-review", action="store_true",
                        help="跳过翻译后语义审核（默认读 settings 的"
                        " ai_review_enabled 开关，P1-11 与 GUI 一致）")
    parser.add_argument("--keep-library", action="store_true",
                        help="保留扫描中间库（调试）")
    parser.add_argument("--resume", action="store_true",
                        help="项目库已存在时续跑：跳过扫描+翻译，从语义审核/"
                        "写回/导出继续（runner 中断恢复，faerie 实证：翻译完成"
                        "后卡在内嵌审核超时循环）")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="写回成功后保留 `_汉化` 发布目录（默认闭环即删；"
                        "供发布目录保留调试/复验）")
    parser.add_argument("--csv-overwrite-source", action="store_true",
                        help="CSV 覆盖源列模式（游戏语言设置只有英文："
                        "翻译源列写回源列，目标语言列官方中文搬运）")
    args = parser.parse_args()
    return run_game(
        args.game_dir,
        batch=args.batch,
        do_translate=not args.no_translate,
        do_writeback=not args.no_writeback,
        keep_library=args.keep_library,
        # P1-11：None → 读 settings（ai_review_enabled）；--no-review 强制关
        do_review=False if args.no_review else None,
        resume=args.resume,
        no_cleanup=args.no_cleanup,
        csv_overwrite_source=args.csv_overwrite_source,
    )


if __name__ == "__main__":
    sys.exit(main())

