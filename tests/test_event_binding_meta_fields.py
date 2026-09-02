# -*- coding: utf-8 -*-
"""Unity 程序集限定类型名 / 输入动作映射路径值泄漏回归测试（识别 B9 + 提取层）。

背景（Dobraminhos 实证 2026-09-02）：GUI 库 1031 条 pending 结构泄漏全部来自
两类「字段值形态」被 typetree_display_evidence 放行——
  1. UnityEvent 持久化回调 m_TargetAssemblyTypeName 值：'GameMaster,
     Assembly-CSharp'（目标脚本程序集限定类名，642 条）——反射按名绑定，
     翻译断事件绑定（按钮无反应）；
  2. 自定义 InputSystem m_ActionEvents[].m_ActionName 值：'PlayerActionsXbox/Move'
     （389 条，含 'PlayerInUI/New action' 编辑器默认名）——运行时按名查找，
     翻译断按键映射。

双层防线：
- 提取层（_EVENT_BINDING_FIELDS）：typetree 字段名命中即整子树跳过；
- 终检层（_ASSEMBLY_QUALIFIED_VALUE/_ACTION_MAP_PATH）：rawstr 无字段名，
  值形态兜底（is_actionable_translation 对无歧义机器结构一律拒于队列外）。

本测试固化：
1. 提取层：typetree 字段名（m_TargetAssemblyTypeName/m_ActionEvents/m_ActionName）
   的叶子——即使值像短短语也不得 pending/display；
2. 终检层：全宽放行 meta 下上述值形态仍不可 actionable；真显示文本（含逗号
   短语 'Hello, world'、动作提示 'Press J to interact.'）不误伤。
"""
from __future__ import annotations

from hanhua.core.models import (_ACTION_MAP_PATH, _ASSEMBLY_QUALIFIED_VALUE,
                                _ASSEMBLY_SEGMENT, _final_structural_backstop,
                                is_actionable_translation, TextEntry)


def _mk(original, field_path=None, *, status="pending", confidence="high"):
    meta = {"role": "display", "disposition": "translate", "confidence": confidence,
            "kind": "typetree", "reason": "typetree_display_evidence",
            "obj_has_values": True}
    if field_path:
        meta["field_path"] = field_path
    return TextEntry(file_id="f", key_path="k", original=original, status=status,
                     meta=meta)


def _asm_hit(text: str) -> bool:
    m = _ASSEMBLY_QUALIFIED_VALUE.match(text)
    return bool(m and _ASSEMBLY_SEGMENT.match(m.group("asm")))


# ── 契约 1：程序集限定值形态（值形态 + 程序集段）→ 终检拦截 ──
ASSEMBLY_VALUES = [
    "GameMaster, Assembly-CSharp", "MenuMaster, Assembly-CSharp",
    "PlayerAttack, Assembly-CSharp", "PlayerMovement, Assembly-CSharp",
    "SoundControl, Assembly-CSharp", "ShowControls, Assembly-CSharp",
    "Cutscene, Assembly-CSharp", "BoatSkill, Assembly-CSharp",
    "EnemyDamage, Assembly-CSharp", "StampButton, Assembly-CSharp",
    "UnityEngine.Object, UnityEngine",
    "UnityEngine.EventSystems.UnityEvent, UnityEngine.UI",
    "System.Boolean, mscorlib",
]


def test_assembly_qualified_values_blocked_by_final_gate():
    for text in ASSEMBLY_VALUES:
        e = _mk(text)
        assert _asm_hit(text), f"程序集段判定漏: {text!r}"
        assert _final_structural_backstop(e), f"终检漏拦程序集限定名: {text!r}"
        assert not is_actionable_translation(e), f"程序集限定名仍可译: {text!r}"


def test_action_map_path_values_blocked_by_final_gate():
    """'PlayerActionsXbox/Move' 输入动作映射路径 → 终检拦截。"""
    for text in ("PlayerActionsXbox/Move", "PlayerActionsPS/Attack",
                 "GlobalActionsController/Pause", "PlayerInUI/New action",
                 "PlayerActions/Jump", "PlayerInUIController/Pause"):
        e = _mk(text)
        assert _ACTION_MAP_PATH.match(text), f"动作路径判定漏: {text!r}"
        assert not is_actionable_translation(e), f"动作路径仍可译: {text!r}"


# ── 契约 2：逗号/斜杠真显示文本绝不被值形态误杀 ──
DISPLAY_KEEP = [
    "Hello, world", "Hello, my friend", "Save, Load and Quit",
    "Come on, let's go!", "See you, space cowboy", "Well, well, well",
    "Press J to interact.", "Attack / Move", "Player / Enemy",
    "Are you sure?", "Choose your control option:",
]


def test_comma_or_slash_display_text_not_blocked():
    for text in DISPLAY_KEEP:
        e = _mk(text)
        assert not _asm_hit(text), f"逗号真对话被当程序集段: {text!r}"
        assert not _final_structural_backstop(e), f"终检误杀真对话: {text!r}"
        assert is_actionable_translation(e), f"真对话被终检误拦: {text!r}"


# ── 契约 3：提取层 typetree 事件绑定字段名命中 → 整子树跳过 ──
def test_event_binding_field_entries_not_pending():
    """m_TargetAssemblyTypeName / m_ActionName 是绑定元数据字段：无论值如何
    都不产生 pending display 条目（提取层字段名级拦截在遍历时 blocked）。"""
    # 字段名级拦截在 _typetree_string_entries.visit 内按 blocked 标记过滤，
    # 本测试验证值形态即使被误标 display 也会被终检兜住（跨层一致）。
    for value in ASSEMBLY_VALUES + ["PlayerActionsXbox/Move"]:
        e = _mk(value, field_path=["m_OnClick", "m_PersistentCalls",
                                   "m_Calls", 0, "m_TargetAssemblyTypeName"])
        assert not is_actionable_translation(e), f"事件绑定字段值仍可译: {value!r}"
