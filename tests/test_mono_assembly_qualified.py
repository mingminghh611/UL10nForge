# -*- coding: utf-8 -*-
"""mono_dll #US 程序集限定类型引用泄漏回归（B9b，8 More Lives 实证 2026-09-02）。

背景：#US 用户字符串堆里含反射按名加载的类型引用串（'Inventory,
Assembly-CSharp'——Type.GetType 字符串字面量）。`_is_sentence_display_text`
的放宽规则把这类『短句形态』当显示文本放行（8 More Lives 4 条译成
「库存，Assembly-CSharp」/「装甲，Assembly-CSharp」，反射加载断链）。

修复：句子判定前置拦截——含 `, <程序集段>` 后缀（Assembly-CSharp/UnityEngine/
mscorlib/System/Unity.*…，与 models._ASSEMBLY_SEGMENT 同词表）即类型引用，
非对话句。真对话 'Hello, world'（第二段普通词）不误杀。

测试：
1. 程序集限定类型引用值 → 非句子显示文本（不 pending 翻译）。
2. 带逗号的真实 UI/对话句 → 仍放行。
"""
from __future__ import annotations

from hanhua.core.unity.mono_dll import _is_sentence_display_text


def test_assembly_qualified_type_refs_not_sentence():
    for text in ("Inventory, Assembly-CSharp", "Armor, Assembly-CSharp",
                 "Background, Assembly-CSharp", "Race, Assembly-CSharp",
                 "System.Boolean, mscorlib",
                 "MyGame.GameState, Assembly-CSharp",
                 "SaveSystem.PlayerData, Assembly-CSharp-firstpass"):
        assert not _is_sentence_display_text(text), \
            f"程序集限定类型引用被当句子放行: {text!r}"


def test_real_sentences_with_comma_still_display():
    for text in ("Hello, world", "Hello, my friend",
                 "Come on, let us go!", "Press J to interact.",
                 "Your health is low, be careful.", "Nice, you made it!",
                 "Inventory is full, sell some items.",
                 "Save, Load and Quit options are here.",
                 "Well, well, well."):
        assert _is_sentence_display_text(text), \
            f"真对话/UI 句被程序集段规则误杀: {text!r}"
