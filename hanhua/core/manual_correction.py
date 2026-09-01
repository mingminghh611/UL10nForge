"""Phase B-2：人工修正统一回流（审计 §6 P1-6 收口）。

审校页人工编辑译文是最高权重证据（人改即终局），但历史 set_manual
只写 translation/status：审核终态残留（发布门误判）、坏记忆留存
（被改正的译文继续命中）、经验记忆/术语库/矢量索引缺失。本模块把
人工修正收拢为单一回流点，原子完成五步：

  1. apply_manual_correction —— 清旧审核状态、写 MANUAL/APPROVED 终态；
  2. add_memory —— 工作记忆提交（pending=0 立即可命中，INSERT OR
     REPLACE 按 src_hash 覆盖旧行——被改正的坏译文不再命中）；
  3. upsert_manual —— 经验记忆最高权重人工证据（evidence=3 active，
     终结既有冲突/退休记录）；
  4. enqueue_vector —— 矢量索引 outbox 出队（Phase C 消费）；
  5. record_audit —— 审计日志（谁改了什么、从什么改成什么）。

空输入（清空译文）等同重置待译：不写记忆、撤销该原文全部旧记忆行、
出队「删除」指令，仍写审计。
"""

from __future__ import annotations

from .review_outcome import APPROVED


def manual_correction(store, file_id: str, key_path: str, translation: str,
                      *, model: str = "", lang: str = "",
                      agent_memory=None, game_name: str = "",
                      error_patterns=None) -> dict:
    """人工修正统一回流（见模块 docstring）。返回本次写入的结果 dict：
    {applied, original, before_translation, translation, status, memory,
    agent}——applied=False 表示条目不存在（不落任何副作用）。

    #43 阶段 B：error_patterns（ErrorPatternStore 实例）非空时，人工
    改正 ≠ 原 AI 译文 → 沉淀错误模式（original, wrong=被改正的译文,
    correct=人工译文, source=human_corrected → verified/0.95）——
    后续同原文出现即提高风险识别（重构指令 §16 反馈系统）。
    """
    result = store.apply_manual_correction(file_id, key_path, translation)
    if not result.get("applied"):
        return result
    normalized = result["translation"]
    if normalized:
        # 工作记忆：提交（pending=0 立即可命中；覆盖旧译文行）。
        # BUILTIN 冲突门禁在 add_memory 内部：人工把 Disabled 改成
        # 「残疾人士」属误改，不得覆盖权威（权威由内置表恒胜出）。
        store.add_memory(result["original"], normalized, model, lang)
        # 经验记忆：最高权重人工证据（未接 agent_memory 时跳过——
        # GUI/脚本可按需传入；核心回流不因缺 agent 而失败）
        if agent_memory is not None:
            agent_memory.upsert_manual(result["original"], normalized,
                                       game_name)
        # 错误模式（阶段 B）：AI 坏译文被人工纠正 = 最高权重错误证据
        before = result.get("before_translation") or ""
        if (error_patterns is not None and before
                and before != normalized):
            status = error_patterns.record(
                result["original"], normalized, wrong=before,
                context="", game=game_name, source="human_corrected")
            result["error_pattern"] = status
    else:
        # 清空译文 = 重置待译：撤销该原文全部旧记忆（不限 model/lang）
        store.remove_memory_all(result["original"])
    store.enqueue_vector(kind="manual", file_id=file_id, key_path=key_path,
                         original=result["original"],
                         translation=normalized)
    store.record_audit(kind="manual", file_id=file_id, key_path=key_path,
                       original=result["original"],
                       before=result["before_translation"],
                       after=normalized, model=model, lang=lang,
                       note="人工修正" if normalized else "人工清空（重置待译）")
    result["memory"] = bool(normalized)
    result["agent"] = bool(normalized and agent_memory is not None)
    result["outcome"] = APPROVED if normalized else ""
    return result
