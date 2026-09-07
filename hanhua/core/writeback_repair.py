# -*- coding: utf-8 -*-
"""审计→修复有界闭环（0.42.1 P2）：把写回审计 FAIL 从「阻断+人工」
升级为「先自动修复，修不好才人工」。

背景（fromivan 审计 2026-09-07）：审计层（writeback_audit）已经能
确定性抓住占位符丢失/译文未落盘/渲染不一致，但抓到之后只有一条路
——needs_rewrite 阻断整轮闭环等人工。实证里三类失败各有确定性修法：
- 占位符丢失 → self_heal_format_tags 机械补全（无模型调用，翻译管线
  已在用，batch_translator 0.38 同款）后重写文件即可；
- 渲染不一致/译文未落盘 → 磁盘与 store 渲染出现分歧（外部改动/上轮
  写回被中断），从 store 重渲染单文件即恢复；
- 译文质量（模型软复核 flag）→ 有翻译服务在场时按审核反馈强制重译。

铁律（宁漏勿坏）：
- 修复动作只有「改 store 里该条译文」与「从 store 重渲染该文件」两类，
  绝不直接改磁盘字节——磁盘永远由渲染管线产出，编码/EOL/结构守恒
  由 _encode/_render_from_store 同源保证；
- 结构性守恒失败（编码/行数/JSON 结构/CSV 分隔符）不自动修——
  重渲染只会复现同一份坏 store 数据，盲目重写浪费轮次，直接
  needs_manual 交人工（结构破坏的根因在译文本身，须人判）；
- 每轮修完重跑 audit_deterministic 复核，最多 max_rounds 轮；
  失败集合不再收敛（修复无效）即停——有界闭环，不空转。

repair_service：可 选翻译服务（BatchTranslator 同接口——
retranslate_with_feedback(entry, feedback) -> (passed, translation)）。
不给（None）→ 质量类失败跳过修复只记录（模型层本就是软复核，
没有模型绝不盲改译文）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .writeback_audit import (
    FileAudit,
    _encode_from_store,
    _render_from_store,
    audit_deterministic,
)

# 软复核 verdict 白名单：这些判定指向译文质量/语义，重译是合理修法。
# MODEL_UNAVAILABLE 是覆盖缺口（不是译文问题）→ 不重译只记录。
_RETRANSLATE_VERDICTS = {"SEMANTIC_ERROR", "QUALITY", "STRUCTURE_BROKEN"}


def _entries_by_file(store) -> dict[str, list[dict]]:
    grouping: dict[str, list[dict]] = {}
    for e in store.get_entries():
        grouping.setdefault(e["file_id"], []).append(e)
    return grouping


def _placeholder_flag_key(message: str) -> str:
    """占位符失败消息「key_path: orig → trans 丢失 […]」取 key_path。

    key_path 本身不含「: 」（分隔符是「: 」双字符），首段切分安全。
    """
    return message.split(": ", 1)[0].strip()


def _missing_flag_key(message: str) -> str | None:
    """译文缺失消息取 key_path（两种消息格式）。

    - translation_missing_in_output: <key_path>（orig → trans）
    - csv_target_residue: row N: cell（条目 <key_path> 已有译文未落盘）
    """
    if message.startswith("translation_missing_in_output: "):
        rest = message[len("translation_missing_in_output: "):]
        return rest.rsplit("（", 1)[0].strip() or None
    if message.startswith("csv_target_residue: "):
        if "（条目 " not in message:
            return None
        seg = message.split("（条目 ", 1)[1]
        return seg.split(" ", 1)[0].strip() or None
    return None


def _parse_entry_meta(e: dict) -> dict:
    meta = e.get("meta") or "{}"
    if isinstance(meta, dict):
        return meta
    try:
        return json.loads(meta)
    except (json.JSONDecodeError, TypeError):
        return {}


def _structure_failed(a: FileAudit) -> bool:
    """结构性守恒失败（重渲染无法治愈，须人工）。"""
    return not (a.encoding_conserved and a.eol_conserved
                and a.line_conserved and a.structure_ok
                and a.quote_paired and a.comma_conserved
                and a.strict_parse_ok and a.delimiter_conserved)


def _rewrite_file(store, game_dir: Path, out_dir: Path, f: dict,
                  entries: list[dict], font_enabled: bool) -> None:
    """从 store 重渲染单文件并落盘（与 writer 产盘同源：_render_from_store
    + _encode_from_store 完全复用审计侧渲染，编码/EOL/结构守恒口径一致）。"""
    from .paths import resolve_relative_under

    src = resolve_relative_under(game_dir, f["rel_path"])
    out = resolve_relative_under(out_dir, f["rel_path"])
    if not src.is_file():
        return
    body = _render_from_store(store, src, f, entries,
                              normalize_fallback_punctuation=font_enabled)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_encode_from_store(body, src, f))


def repair_writeback(store, game_dir: Path, out_dir: Path, audit_results,
                     *, repair_service=None, max_rounds: int = 2,
                     font_enabled: bool = False,
                     on_note=None) -> dict:
    """审计 FAIL 有界自动修复闭环。

    分诊（按失败原因）：
    1. placeholder_lost → self_heal_format_tags 机械补全译文 →
       update_translation 落库 → 重渲染该文件；
    2. render_consistent / translation_missing（含 csv_target_residue）
       → 重渲染该文件（修磁盘分歧；行号 meta 过期等 store 侧根因
       重渲染后仍会 FAIL → 走满轮数后 needs_manual，绝不伪造通过）；
    3. 模型软复核 flag（质量类 verdict）→ repair_service 在场时按
       审核反馈强制重译落库 + 重渲染；不在场 → 跳过并记录；
    4. 结构性守恒失败（编码/EOL/行数/结构/分隔符）→ 不自动修，
       直接 needs_manual（重渲染只复现坏数据，宁漏勿坏）。

    每轮修完对全库重跑 audit_deterministic 复核；失败集合不收缩
    （修复无效）即提前停。返回：
    {"rounds", "repaired_files", "healed_entries", "retranslated",
     "skipped_quality", "needs_manual", "final_failed", "converged"}
    """
    game_dir = Path(game_dir)
    out_dir = Path(out_dir)
    files_by_rel = {f["rel_path"]: f for f in store.get_files()
                    if not f["format"].startswith("v2_")}
    report: dict = {
        "rounds": 0,
        "repaired_files": [],      # 至少成功重渲染过一次的文件
        "healed_entries": 0,       # 占位符自愈落库条数
        "retranslated": 0,         # 按反馈重译通过落库条数
        "skipped_quality": [],     # 质量类失败跳过（无 repair_service）
        "needs_manual": [],        # 最终仍 FAIL（含结构性直接人工）
        "final_failed": [],        # 最终 FAIL 的 rel_path（同上，机器用）
        "converged": True,         # 失败集合收敛为空
    }

    def _note(msg: str) -> None:
        if on_note:
            on_note(msg)

    # 首轮分诊基于调用方传入的审计结果（audit_writeback 全量结果）；
    # 之后每轮用新跑的 audit_deterministic。
    current: list[FileAudit] = list(getattr(audit_results, "files", []))
    model_flags = list(getattr(audit_results, "model_flags", []))
    prev_failed: set[str] | None = None
    # 结构性失败的 rel：终态必须算未收敛（这类失败重渲染只会复现，
    # 但不能因为它「本轮没再报」就当已修复——宁漏勿坏，人工确认前
    # 不给通过口径）
    structure_manual_rels: set[str] = set()

    for round_no in range(1, max_rounds + 1):
        failed = [a for a in current if not a.passed]
        # 分诊 3 的质量 flag 独立于 failed（软复核通道）：首轮处理完
        # 即清空（重译落库后 flag 已消费，后续轮不重复处理同一 flag）
        pending_flags = model_flags if round_no == 1 else []
        if not failed and not pending_flags:
            break
        failed_rels = {a.rel_path for a in failed}
        if (prev_failed is not None and failed_rels
                and failed_rels >= prev_failed):
            # 失败集合不再收缩：修复无效（如行号 meta 过期的 store 侧
            # 根因，重渲染复现同一失败）→ 停，剩余交人工
            _note(f"[写回修复] 第 {round_no - 1} 轮后失败集合未收缩，停止自动修复")
            report["rounds"] = round_no - 1
            break
        prev_failed = set(failed_rels)
        report["rounds"] = round_no

        entries_by_file = _entries_by_file(store)
        rewrite_rels: set[str] = set()

        for a in failed:
            f = files_by_rel.get(a.rel_path)
            if f is None:
                report["needs_manual"].append(
                    f"{a.rel_path}（文件记录缺失，无法重渲染）")
                continue
            entries = entries_by_file.get(f["id"], [])

            # ── 分诊 1：占位符丢失 → 机械自愈 ─────────────────────
            if a.placeholder_lost:
                from .placeholders import self_heal_format_tags

                for msg in a.placeholder_lost:
                    key_path = _placeholder_flag_key(msg)
                    e = next((x for x in entries
                              if str(x.get("key_path")) == key_path), None)
                    if e is None:
                        continue
                    original = str(e.get("original") or "")
                    translation = str(e.get("translation") or "")
                    healed = self_heal_format_tags(original, translation)
                    if healed != translation:
                        store.update_translation(f["id"], key_path, healed)
                        report["healed_entries"] += 1
                        rewrite_rels.add(a.rel_path)
                        _note(f"[写回修复] 占位符自愈 {a.rel_path} {key_path}")

            # ── 分诊 2：磁盘分歧（渲染不一致/译文未落盘）→ 重渲染 ──
            if (not a.render_consistent) or a.translation_missing:
                rewrite_rels.add(a.rel_path)

            # ── 分诊 4：结构性守恒失败 → 人工（重渲染复现坏数据）──
            if _structure_failed(a):
                report["needs_manual"].append(
                    f"{a.rel_path}（结构性守恒失败，须人工排查译文数据）")
                structure_manual_rels.add(a.rel_path)

        # ── 分诊 3（全量）：模型软复核质量 flag → 反馈重译 ──────────
        # 不依赖 failed 循环：软复核独立于硬闸门，flag 对应文件在
        # 确定性层可能全 PASS（files 为空/不含该 rel）——但质量 flag
        # 仍须修（审核闭环语义）。flag 详情带「src[:50] → dst[:50]」
        # 行对，按原文前缀定位条目（定位不到只记录，不盲改）。
        flag_rels = {rel for (rel, verdict, _i) in model_flags
                     if verdict in _RETRANSLATE_VERDICTS}
        for rel in sorted(flag_rels):
            f = files_by_rel.get(rel)
            if f is None:
                continue
            entries = entries_by_file.get(f["id"], [])
            for (frel, verdict, issue) in model_flags:
                if frel != rel or verdict not in _RETRANSLATE_VERDICTS:
                    continue
                if repair_service is None:
                    report["skipped_quality"].append(
                        f"{rel}: [{verdict}] {issue[:80]}")
                    continue
                for e in entries:
                    original = str(e.get("original") or "")
                    if not original or original[:50] not in issue:
                        continue
                    entry_obj = _to_text_entry(e)
                    passed, translation = repair_service.retranslate_with_feedback(
                        entry_obj, issue)
                    if passed and translation and translation != original:
                        store.update_translation(f["id"],
                                                 str(e.get("key_path")),
                                                 translation)
                        report["retranslated"] += 1
                        rewrite_rels.add(rel)
                        _note(f"[写回修复] 反馈重译 {rel} {e.get('key_path')}")

        # 重渲染本轮命中文件（渲染/编码与写回管线完全同源）。
        # 注意必须重取 entries：entries_by_file 是修复前快照，
        # update_translation 只改库不改快照——用旧快照渲染会把
        # 自愈/重译前的旧译文写回磁盘（修复白做，实测踩坑）。
        entries_by_file = _entries_by_file(store)
        for rel in sorted(rewrite_rels):
            f = files_by_rel.get(rel)
            if f is None:
                continue
            try:
                _rewrite_file(store, game_dir, out_dir, f,
                              entries_by_file.get(f["id"], []), font_enabled)
                if rel not in report["repaired_files"]:
                    report["repaired_files"].append(rel)
            except Exception as exc:  # noqa: BLE001 渲染/编码失败=数据问题
                report["needs_manual"].append(
                    f"{rel}（重渲染失败：{str(exc)[:80]}）")

        # 每轮复核：全量确定性重审（模型层是软复核，修复判定只看硬闸门）
        current = audit_deterministic(store, game_dir, out_dir,
                                      _entries_by_file(store))

    # 终态：仍 FAIL 的文件交人工（含结构性失败）。结构性失败的 rel
    # 并入 final_failed——重审（真实审计跑在好文件上）会把它漏掉，
    # 但该文件的人工态不该因复检通过被洗白。
    final_failed_rels = {a.rel_path for a in current if not a.passed}
    final_failed_rels |= structure_manual_rels
    for rel in sorted(final_failed_rels):
        if not any(rel in m for m in report["needs_manual"]):
            report["needs_manual"].append(
                f"{rel}（{max_rounds} 轮修复后仍 FAIL）")
    report["final_failed"] = sorted(final_failed_rels)
    report["converged"] = not final_failed_rels
    if final_failed_rels:
        _note(f"[写回修复] 仍有 {len(final_failed_rels)} 个文件 FAIL，转人工")
    else:
        _note("[写回修复] 全部文件修复通过")
    return report


def _to_text_entry(e: dict):
    """store 行 dict → TextEntry（retranslate_with_feedback 入参形态）。"""
    from .models import TextEntry

    return TextEntry(
        file_id=e["file_id"], key_path=e["key_path"],
        original=e["original"], translation=e.get("translation") or "",
        status=e.get("status", "pending"),
        locked=bool(e.get("locked")), meta=_parse_entry_meta(e))
