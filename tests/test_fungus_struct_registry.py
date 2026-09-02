# -*- coding: utf-8 -*-
"""Fungus 对话系统结构类回归测试。

背景（a-catfiends 实证 2026-09-02）：Fungus 对话组件（BooleanVariable/
FloatVariable/Block/PlayAnimState/MessageReceived/StopBlock/FungusTrigger 等）
的序列化字段值是运行时按名查找键（变量名/块名/动画状态名/消息名），翻译必断
对话流程。此前被 single_visible_string/f38_released 当显示文本放行进队列
（obj145 BooleanVariable 'Menu'、obj145 'milk'、obj3201 'LOCATION' 等 22 条）。

修复：class_registry 登记 Fungus.* 结构类为 config（整体跳过）；Fungus.InfoText/
Fungus.Character 的默认占位（'Information text'/'Character Name'）由 placeholders
_DEFAULT_NAME_PLACEHOLDER 硬结构拦截。**Fungus.Say（对话正文）/Fungus.Menu
（选项文本）必须保留翻译**——精确到类的登记保证只拦结构、不误杀对话。

测试：
1. Fungus 结构类 disposition=config（extractor 整体跳过）。
2. Fungus.Say / Fungus.Menu 等对话/显示类不登记（走既有 display 放行）。
3. 默认名占位（'Information text'/'Character Name'）→ is_hard_structural True。
4. 真实显示文本不受占位误伤（'Character'/'New Game'/'Name your character'）。
"""
from __future__ import annotations

from hanhua.core.placeholders import is_hard_structural
from hanhua.core.unity.class_registry import disposition

FUNGUS_STRUCT = [
    "Fungus.BooleanVariable", "Fungus.FloatVariable", "Fungus.IntegerVariable",
    "Fungus.StringVariable", "Fungus.AudioSourceVariable",
    "Fungus.GameObjectVariable", "Fungus.MaterialVariable",
    "Fungus.SpriteVariable", "Fungus.Block", "Fungus.Flowchart",
    "Fungus.PlayAnimState", "Fungus.MessageReceived", "Fungus.SendMessage",
    "Fungus.StopBlock", "Fungus.StopFlowchart", "FungusTrigger",
]
# 对话/显示类：**不登记**（精确到类，防误杀真对话）
FUNGUS_DISPLAY = ["Fungus.Say", "Fungus.Menu", "Fungus.Character", "Fungus.InfoText"]


def test_fungus_struct_classes_config():
    for cls in FUNGUS_STRUCT:
        assert disposition(cls) == "config", f"{cls} 应登记 config（结构跳过）"


def test_fungus_display_classes_not_registered():
    for cls in FUNGUS_DISPLAY:
        # 不登记 → None → 走既有启发式（Say 对话正文放行 / Character 显示名放行）
        assert disposition(cls) is None, f"{cls} 不应登记 config（它是显示类）"


def test_default_name_placeholders_hard_structural():
    """Fungus 编辑器默认名占位（未填写字段）→ 硬结构跳过，不进队列。"""
    for text in ("Information text", "Character Name", "New Sprite",
                 "New Game Object", "New Text", "Game name", "Enter name"):
        assert is_hard_structural(text), f"{text!r} 应判硬结构（默认名占位）"


def test_default_placeholder_does_not_hit_real_display():
    """真实显示文本不得被默认名占位规则误伤。"""
    for text in ("Character", "Information", "New Game", "Options", "Save",
                 "Name your character", "Character Select", "New Game Plus"):
        assert not is_hard_structural(text), f"{text!r} 是真显示文本，不得误伤"


def test_fungus_say_dialogue_content_still_translatable():
    """Fungus.Say 的对话正文是显示文本（storyText），不得被任何 Fungus
    结构规则拦——回归保护：整类 config 会让对话也跳过。"""
    from hanhua.core.models import TextEntry, is_actionable_translation
    e = TextEntry("f", "k", "Maybe this kid has some kind of power.",
                  meta={"script_class": "Fungus.Say", "role": "display",
                        "disposition": "translate", "confidence": "high"})
    assert is_actionable_translation(e)
    # Say 内含真实对话, 终检闸门不误伤
    assert not e.meta.get("obj_is_key_list")
