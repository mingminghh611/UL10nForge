"""写回预演（Dry Run）——只分析不落盘（0.39.0 M4，设计文档 §46/§62）。

在正式写回前生成 WritebackPlan：对项目库全部待写条目跑与真实写回
**完全同一套分类链**（write_back_v2 的 disposition 闸门 + 写回前逻辑
形态审计 + 可选 AI 分诊），输出四类计数：

- 预计写回：会被真正写入的条目
- 需要人工：AI 分诊 review（真实写回时保守回退保留原文，用户可先处理）
- 拒绝：disposition 闸门不放行（disposition_*/role_*/legacy_key_guard）
       / AI 分诊 reject
- 高风险：warn 级逻辑形态（只警告不阻断，真实写回照写——按钮文本
         back/retry 大量命中短词形态，宁漏勿坏不误拦）+ revert 级
         （键环境确定性回退，写回时自动保留原文）

安全契约（§62「分析写回但不修改游戏」）：
- 不复制游戏目录、不建 staging、不触碰 out_dir、不写任何文件；
- 不改 store（AI 分诊以 store=None 运行——判定缓存不落库，正式写回
  独立判定；预演绝不留下副作用）；
- 分类链与 write_back_v2 单一来源复用（_should_write_entry /
  _write_rejection_reason / audit_entries_before_writeback /
  run_writeback_triage 同一 pool 口径），预演计数 = 正式写回的行为
  预测，不另起一套规则（两套口径必然漂移，B 节历史教训）。

不在预演范围（真实写回才发生，报告如实说明）：
- 输出副本缺失（output_file_missing）——取决于 staging 复制现场；
- Addressables catalog 校验（_validate_addressables_catalog_sources）；
- 字节层重开验证 / 字体管线 / 四态发布闸门。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hanhua.core.paths import resolve_relative_under
from hanhua.core.quality import is_write_ready

# 每类示例上限：报告可读性（全量明细在真实写回的发布报告里）
_EXAMPLE_LIMIT = 5


@dataclass
class WritebackPlan:
    """写回预演结果（§63 Writeback Report 的预演形态）。"""

    # 文本文件侧（write_back_text 路径）
    text_planned: int = 0
    text_files: int = 0
    text_dropped: int = 0  # 键名/键字段保护：译文被丢弃保留原文
    text_dropped_items: list[str] = field(default_factory=list)

    # v2 二进制侧（write_back_v2 路径）
    v2_planned: int = 0
    needs_review: int = 0          # AI 分诊 review（真实写回保守回退）
    rejected: int = 0              # disposition 闸门 / AI 分诊 reject
    high_risk: int = 0             # warn 级形态（真实写回照写）
    auto_revert: int = 0           # revert 级（真实写回确定性回退）
    rejected_items: list[str] = field(default_factory=list)
    high_risk_items: list[str] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    revert_items: list[str] = field(default_factory=list)

    # 分诊层状态
    triage_degraded: bool = False  # 模型缺席/失败 → 预演未覆盖分诊面
    triage_note: str = ""

    # v2 条目分布（按文件格式）
    v2_files_touched: int = 0

    @property
    def planned_total(self) -> int:
        return self.text_planned + self.v2_planned

    def summary(self) -> str:
        """§62 四类计数主报告（可整体进 GUI 日志/运行记录）。"""
        lines = [
            "── 写回预演（不修改游戏）──",
            f"预计写回：{self.planned_total}"
            f"（文本 {self.text_planned} / 二进制 {self.v2_planned}）",
            f"需要人工：{self.needs_review}",
            f"拒绝：{self.rejected}",
            f"高风险：{self.high_risk + self.auto_revert}"
            f"（warn 照写 {self.high_risk} / 自动回退 {self.auto_revert}）",
        ]
        if self.text_dropped:
            lines.append(f"文本键保护丢弃译文：{self.text_dropped}"
                         f"（键名/键字段保留原文）")
        if self.triage_note:
            lines.append(self.triage_note)
        for label, items in (("高风险示例", self.high_risk_items),
                             ("需要人工示例", self.review_items),
                             ("自动回退示例", self.revert_items),
                             ("拒绝示例", self.rejected_items),
                             ("文本丢弃示例", self.text_dropped_items)):
            if items:
                lines.append(f"{label}：{'；'.join(items)}")
        lines.append(
            "预演未覆盖：副本缺失/catalog 校验/字节层重开验证/字体管线/"
            "四态发布闸门（正式写回时执行）")
        lines.append("确认无误后点击「写回游戏」开始正式写回")
        return "\n".join(lines)


def _meta_of(entry: dict) -> dict:
    import json
    raw = entry.get("meta")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return {}


def _locator(entry: dict) -> str:
    return str(entry.get("key_path") or entry.get("locator") or "")


def _example(items: list[str], text: str) -> None:
    if len(items) < _EXAMPLE_LIMIT:
        items.append(text[:80])


def build_writeback_plan(store, game_dir=None, *,
                         triage_service=None, triage_app_dir=None
                         ) -> WritebackPlan:
    """生成写回计划：与 write_back_v2 / write_back_text 同一分类链，
    零磁盘零 store 副作用。

    triage_service / triage_app_dir：与 write_back_v2 同语义（None →
    分诊层不参与，与 Lite/无模型通道正式写回行为一致）。预演中分诊以
    store=None 运行：判定缓存不落库（预演不留副作用），正式写回独立
    判定（宁漏勿坏：预演判定不预先约束正式写回）。
    """
    from hanhua.core.placeholders import (
        is_key_style_identifier, looks_like_key_field,
    )
    from hanhua.core.unity.writer import _entries_by_file

    plan = WritebackPlan()
    files = store.get_files()
    v2_files = [f for f in files if f["format"].startswith("v2_")]
    text_files = [f for f in files if not f["format"].startswith("v2_")]
    if game_dir is not None:
        # 与 write_back_v2 同源路径安全校验（只读：resolve 抛异常即预演
        # 失败——正式写回必失败，提前暴露比预演假绿更安全）
        from pathlib import Path
        game = Path(game_dir)
        for f in v2_files:
            resolve_relative_under(game, f["rel_path"])
    entries_by_file = _entries_by_file(store, {f["id"] for f in v2_files})

    # ── 文本侧：write_back_text 对每个文件整体重渲染（write_ready 条目
    # 换译文，其余回退原文）；键名/键字段译文被丢弃保留原文（writer
    # ._render 的键保护同条件）。一次全量按 file_id 分组——循环内逐文件
    # 全表扫是 O(N×M)（与 writer.write_back 2026-08-20 性能修复同模式）──
    all_entries_by_file: dict[str, list[dict]] = {}
    for e in store.get_entries():
        all_entries_by_file.setdefault(e["file_id"], []).append(e)
    for f in text_files:
        rows = all_entries_by_file.get(f["id"], [])
        touched = False
        for e in rows:
            if not is_write_ready(e.get("status", ""),
                                  e.get("translation", ""),
                                  e.get("meta", "{}")):
                continue
            if e["translation"] == e["original"]:
                continue
            if (is_key_style_identifier(e["original"])
                    or (f["format"] == "json"
                        and looks_like_key_field(
                            e["key_path"].rsplit("/", 1)[-1]))):
                plan.text_dropped += 1
                _example(plan.text_dropped_items,
                         f"{_locator(e)}: {e['original'][:40]}")
                continue
            plan.text_planned += 1
            touched = True
        if touched:
            plan.text_files += 1
    return _finish_plan(plan, v2_files, entries_by_file,
                        triage_service, triage_app_dir)


def _finish_plan(plan: WritebackPlan, v2_files, entries_by_file,
                 triage_service, triage_app_dir) -> WritebackPlan:
    from hanhua.core.unity.writer import (
        _should_write_entry, _write_rejection_reason,
    )
    from hanhua.core.unity.logic_audit import audit_entries_before_writeback

    # ── v2 侧：与 write_back_v2 完全同序分类 ──
    pool: list[dict] = []  # 分诊池口径 = write_back_v2（translate + 有译文）
    audit_records: list[dict] = []
    for f in v2_files:
        entries = entries_by_file.get(f["id"], ())
        touched = False
        for e in entries:
            if e.get("translation") == e.get("original"):
                continue
            if not _should_write_entry(e):
                plan.rejected += 1
                _example(plan.rejected_items,
                         f"{f['rel_path']} :: {_locator(e)}: "
                         f"{_write_rejection_reason(e)}")
                continue
            pool.append(e)
            touched = True
        if touched:
            plan.v2_files_touched += 1
        # 写回前逻辑审计（与 write_back_v2 同一函数同一过滤条件）
        audit_records.extend(audit_entries_before_writeback(
            e for e in entries
            if _should_write_entry(e) and e["translation"] != e["original"]))
    severity_by_locator: dict[str, str] = {}
    for rec in audit_records:
        severity_by_locator[str(rec.get("locator") or "")] = \
            str(rec.get("severity") or "note")

    # ── AI 分诊（可选，与正式写回同 pool 口径；store=None 不落缓存）──
    triage_skip: dict[tuple[str, str], str] = {}
    if triage_service is not None or triage_app_dir is not None:
        from hanhua.core.unity.writeback_ai_triage import run_writeback_triage
        try:
            triage_skip, triage_report = run_writeback_triage(
                pool, None, service=triage_service,
                app_dir=triage_app_dir)
            if triage_report.degraded:
                plan.triage_degraded = True
                plan.triage_note = (
                    f"AI 分诊降级：{triage_report.error[:100]}"
                    "（正式写回分诊层 fail-closed，受影响条目保守回退）")
        except Exception as exc:  # noqa: BLE001 分诊异常 = 未覆盖（照写）
            plan.triage_degraded = True
            plan.triage_note = (
                f"AI 分诊预演异常（已跳过，正式写回全部照写）："
                f"{str(exc)[:100]}")

    # ── 终分类：planned / high_risk / auto_revert / review / reject ──
    # 顺序与真实写回一致：分诊跳过（写前移出 patch 流）先于对象循环里的
    # 语义回退（typetree_logic_key_evidence）；rawstr 的 logic_key_evidence
    # 依赖对象字符串池（须打开 bundle）不在预演范围——报告已如实标注。
    from hanhua.core.unity.logic_audit import typetree_logic_key_evidence
    for e in pool:
        key = (str(e.get("file_id") or ""), str(e.get("key_path") or ""))
        triage_reason = triage_skip.get(key, "")
        if triage_reason:
            if triage_reason.startswith("ai_triage_reject"):
                plan.rejected += 1
                _example(plan.rejected_items,
                         f"{_locator(e)}: {e['original'][:40]}"
                         f" → AI 分诊拒绝")
            else:
                plan.needs_review += 1
                _example(plan.review_items,
                         f"{_locator(e)}: {e['original'][:40]}")
            continue
        meta = _meta_of(e)
        if meta.get("kind") == "typetree":
            verdict = typetree_logic_key_evidence(
                meta, str(e.get("original") or ""))
            if verdict and verdict[0] == "revert":
                plan.auto_revert += 1
                _example(plan.revert_items,
                         f"{_locator(e)}: {e['original'][:40]}"
                         f"（{verdict[1]}，写回时保留原文）")
                continue
        severity = severity_by_locator.get(_locator(e), "note")
        if severity == "revert":
            plan.auto_revert += 1
            _example(plan.revert_items,
                     f"{_locator(e)}: {e['original'][:40]}（写回时保留原文）")
        elif severity == "warn":
            plan.high_risk += 1
            _example(plan.high_risk_items,
                     f"{_locator(e)}: {e['original'][:40]} → "
                     f"{str(e.get('translation') or '')[:20]}")
        else:
            plan.v2_planned += 1
    return plan
