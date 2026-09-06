from __future__ import annotations

import pytest

from hanhua.core.models import TextEntry
from hanhua.core.quality import (_is_format_template,
                                 _number_tokens,
                                 has_independent_lower_word,
                                 validate_translation_quality)


def _entry(original: str, **meta) -> TextEntry:
    return TextEntry("ui.assets", "Menu/title", original, meta=meta,
                     confidence="high")


def test_quality_accepts_natural_translation_with_preserved_formatting():
    entry = _entry("<b>Hello {name}</b>\nContinue")

    result = validate_translation_quality(entry, "<b>你好，{name}</b>\n继续")

    assert result.passed is True
    assert result.reasons == ()
    assert result.normalized_translation == "<b>你好，{name}</b>\n继续"
    assert result.confidence == "high"


def test_quality_returns_stable_reasons_for_format_and_control_failures():
    entry = _entry("<b>Hello {name}</b>\nContinue")

    result = validate_translation_quality(entry, "译文：<b>你好</b>\x00")

    assert result.passed is False
    assert set(result.reasons) >= {
        "explanatory_prefix", "illegal_control", "placeholder_mismatch",
        "newline_mismatch",
    }


def test_allcaps_natural_sentence_exempts_single_token_pair():
    """全大写自然句（'IT'S LOCKED.'）单 token 词对不强制（Morfosi 64 条
    glossary_mismatch 全灭实证）：全大写文本大写不携带 UI 形态信息
    （每个词都大写），句尾标点 = 喊话式自然句 → (Locked→锁定) 豁免；
    对照：无句尾标点的全大写菜单标签（'NEW GAME'）仍按标签强制。"""
    entry = _entry("IT'S LOCKED.")
    result = validate_translation_quality(
        entry, "它被锁住了。", glossary=[("Locked", "锁定")])
    assert result.passed is True
    assert "glossary_mismatch" not in result.reasons

    # 对照：全大写无句尾标点（菜单按钮形态）→ 词对仍强制
    label = _entry("NEW GAME")
    result_label = validate_translation_quality(
        label, "开始游戏", glossary=[("New", "新")])
    assert result_label.passed is False
    assert "glossary_mismatch" in result_label.reasons


def test_uppercase_action_residue_rejected():
    """知识库规则：全大写动作指令译文残留原动作动词（TOSS 垃圾）判失败。"""
    entry = _entry("TOSS TRASH")
    residue = validate_translation_quality(entry, "TOSS 垃圾")
    clean = validate_translation_quality(entry, "丢垃圾")

    assert "action_word_residue" in residue.reasons
    assert clean.passed is True


def test_uppercase_action_non_verb_retention_allowed():
    """动作动词之外的原词保留不判残留（专名仍可保留）。"""
    entry = _entry("CUT WOOD")
    result = validate_translation_quality(entry, "砍木头")

    assert result.passed is True


def test_quality_rejects_untranslated_english_and_glossary_drift():
    entry = _entry("Use Moon Key to open the basement")

    untranslated = validate_translation_quality(entry, "Use Moon Key to open the basement")
    drift = validate_translation_quality(
        entry, "使用月之钥匙打开地下室", glossary=[("Moon Key", "月光钥匙")])

    assert "untranslated_text" in untranslated.reasons
    assert "glossary_mismatch" in drift.reasons


def test_glossary_exact_pair_wins_over_substring_pair():
    """fix-26 精确词对优先：原文精确命中 (ENTER NAME→输入姓名) 时，
    子串词对 (NAME→名称) 不再生效——正确译文「输入姓名」不得被误判
    glossary_mismatch（force-reboot 实证：AgentMemory 独立词对
    (NAME→名称) 子串命中 ENTER NAME 原文 → 正确译文判失败）。"""
    entry = _entry("ENTER NAME")
    ok = validate_translation_quality(
        entry, "输入姓名",
        glossary=[("ENTER NAME", "输入姓名"), ("NAME", "名称")])
    assert ok.passed is True

    # 精确词对仍是权威：译文不含其 target 照常判失败
    fail = validate_translation_quality(
        entry, "输入昵称",
        glossary=[("ENTER NAME", "输入姓名"), ("NAME", "名称")])
    assert "glossary_mismatch" in fail.reasons


def test_glossary_verb_usage_exempted():
    """F17 动词用法豁免：术语词在原文是动词用法（前邻 to/助动词）→ 与
    术语表的标签含义无关。doubleshake "shouldn't be hard to miss" 的
    miss=错过/遗漏，译文「遗漏」正确——不得被 (miss, 未命中) 误杀；
    'miss: 999' 标签格式（deadbeat 音游 HUD）不受影响照常生效。"""
    entry = _entry("Hm, I think 4 should do. The seeds grow in high "
                   "places around the island, and shouldn't be hard to miss.")
    ok = validate_translation_quality(
        entry, "嗯，我觉得选4就可以了。这些种子生长在岛屿上的高处，"
               "应该不会容易遗漏吧。",
        glossary=[("miss", "未命中")])
    assert ok.passed is True

    # 标签格式不受豁免：deadbeat 实证 miss: 999 模型回显须判失败
    label = _entry("miss: 999")
    fail = validate_translation_quality(
        label, "miss: 999", glossary=[("miss", "未命中")])
    assert "glossary_mismatch" in fail.reasons


def test_short_uppercase_abbreviation_echo_allowed():
    """F19 ≤3 全大写缩写回显豁免：MAX/SFX/UI 是界面标准术语，1.8B
    模型对单 token 缩写稳定回显（count-my-coins 'SFX' 实证；proper_name
    echo 侧已有同规则，本门补一致；driftapocalypse 'MAX' ×3 实证重试
    耗尽仍回显）。4+ 字母 UI 词、多词组合、动作指令不受影响。"""
    for word in ("MAX", "SFX", "UI", "OK"):
        entry = _entry(word)
        ok = validate_translation_quality(entry, word)
        assert ok.passed is True, f"{word} 回显应豁免: {ok.reasons}"

    # 4+ 字母 UI 词典词（QUIT）回显仍判失败
    quit_entry = _entry("QUIT")
    quit_res = validate_translation_quality(quit_entry, "QUIT")
    assert "untranslated_text" in quit_res.reasons
    # 多词组合（MAX SPEED 半翻）仍判失败
    combo = _entry("MAX SPEED")
    combo_res = validate_translation_quality(combo, "MAX SPEED")
    assert "untranslated_text" in combo_res.reasons
    # 动作指令（TOSS TRASH）仍判失败（knowledge 规则）
    action = _entry("TOSS TRASH")
    action_res = validate_translation_quality(action, "TOSS TRASH")
    assert "untranslated_text" in action_res.reasons


def test_glossary_proper_name_echo_casefold_allowed():
    """自动沉淀专名保留映射（KRAPOS→KRAPOS）vs 模型回显 TitleCase 变体
    （Krapos）→ 大小写不敏感放行（count-my-coins 实证：learn 时保留
    检测 casefold，quality 检查却大小写敏感——全大写 target 不在
    TitleCase 译文里 → glossary_mismatch 误判）。"""
    entry = _entry("Krapos")
    kept = validate_translation_quality(
        entry, "Krapos", glossary=[("KRAPOS", "KRAPOS")])
    assert kept.passed is True

    entry2 = _entry("Settings")
    # 人工术语（中文 target）大小写不敏感不影响：模型回显英文仍判失败
    drift = validate_translation_quality(
        entry2, "Settings", glossary=[("Settings", "设置")])
    assert "glossary_mismatch" in drift.reasons


def test_quality_rejects_unchanged_single_english_ui_label():
    result = validate_translation_quality(_entry("Continue", role="ui"), "Continue")

    assert result.reasons == ("untranslated_text",)


def test_quality_allows_decorative_dash_title_not_markdown():
    # "- Quality Settings -" 是装饰性标题（资产真实值），不是 markdown 列表
    result = validate_translation_quality(_entry("- Quality Settings -"), "- 质量设置 -")

    assert result.passed is True


def test_quality_still_rejects_markdown_list_wrapper():
    result = validate_translation_quality(_entry("Choose an option"), "- 选择一项")

    assert "markdown_wrapper" in result.reasons


def test_quality_allows_dash_signature_not_markdown():
    # "-Love, Sean" 签名：译文 "- 爱，肖恩" 的 "- " 是原文破折号延续，不是 markdown 列表
    result = validate_translation_quality(_entry("-Love, Sean"), "- 爱，肖恩")

    assert result.passed is True


def test_bracketed_display_text_is_translatable_not_bbcode():
    result = validate_translation_quality(_entry("[PICK UP]", role="ui"), "[拾取]")

    assert result.passed is True


def test_quality_rejects_renamed_english_and_format_sequence_drift():
    english = validate_translation_quality(_entry("Continue", role="ui"), "Play")
    tags = validate_translation_quality(
        _entry("<b>Hello</b><i>Now</i>"), "<i>你好</i><b>现在</b>")
    newlines = validate_translation_quality(
        _entry("First\nSecond\\nThird"), "第一\\n第二\n第三")

    assert "untranslated_text" in english.reasons
    assert "rich_text_mismatch" in tags.reasons
    assert "newline_mismatch" in newlines.reasons


def test_quality_marks_over_budget_without_failing():
    """超长不判失败：译文质量合格只是物理容量放不下——写回端截断兜底
    （部分翻译 + 省略号），判失败会把好译文整体丢弃、游戏只剩原文
    （taxes 'I did ' 实证）。超出量记入 meta 供报告与人工校对。"""
    entry = _entry("New Game", role="ui", max_chars=4)

    assert validate_translation_quality(entry, "开始游戏").passed
    result = validate_translation_quality(entry, "开启一段全新的游戏旅程")

    assert result.passed is True
    assert result.reasons == ()
    assert entry.meta["length_over_budget"] == 7  # 11 字 - 4 容量


def test_interaction_prompt_requires_the_same_input_token():
    entry = _entry("Press E to open", role="display", reason="interaction_prompt")

    assert validate_translation_quality(entry, "按 E 键打开").passed
    result = validate_translation_quality(entry, "按 F 键打开")

    assert "input_token_mismatch" in result.reasons


@pytest.mark.parametrize(("source", "translation"), [
    # 按键名保留是正确行为：enter 在键列表位置（不是动词）
    ("[press z or enter to continue]", "[按 Z 或 Enter 继续]"),
    ("[press z or enter to restart the game]", "[按 Z 或 Enter 键以重新开始游戏]"),
    ("Press Enter to enter the building", "按 Enter 键进入大楼"),
])
def test_interaction_prompt_keeps_physical_key_names(source, translation):
    entry = _entry(source, role="display", reason="interaction_prompt")

    assert validate_translation_quality(entry, translation).passed


@pytest.mark.parametrize(("source", "translation"), [
    # 真半翻：动作词（非按键）残留仍判失败
    ("Press E to Open", "按 E 键 Open"),
    ("Press Enter to enter the building", "Press Enter 键进入大楼"),
])
def test_interaction_prompt_still_rejects_action_word_leftovers(source, translation):
    entry = _entry(source, role="display", reason="interaction_prompt")

    assert not validate_translation_quality(entry, translation).passed


@pytest.mark.parametrize(("source", "translation"), [
    # 专名并列短语（人名 + 姓）不是英文残留
    ("Polish Localization - Amitte Sukku", "波兰语本地化服务 – Amitte Sukku"),
    # "* (选项文案)" 风格不是 markdown 列表
    ("* (You felt that you shouldn't\n  advance.)",
     "* 你觉得自己不应该\n提前。"),
])
def test_quality_accepts_proper_name_phrases_and_option_style(source, translation):
    entry = _entry(source, role="display")

    assert validate_translation_quality(entry, translation).passed


@pytest.mark.parametrize(("source", "translation"), [
    # 专名/缩写回显（原文无小写词、不在 UI 词典）是合理行为
    ("Crash Bandicoot", "Crash Bandicoot"),
    ("Roquette", "Roquette"),
    ("Profiler", "Profiler"),
    ("IMGUI", "IMGUI"),
])
def test_quality_allows_proper_name_echo(source, translation):
    entry = _entry(source, role="display")

    assert validate_translation_quality(entry, translation).passed


@pytest.mark.parametrize(("source", "translation"), [
    # 回显仍失败：有小写词（真半翻）或 UI 词典词
    ("Hello world", "Hello world"),
    ("Save game", "Save game"),
    ("Continue", "Continue"),
])
def test_quality_rejects_real_echoes(source, translation):
    entry = _entry(source, role="display")

    assert "untranslated_text" in validate_translation_quality(
        entry, translation).reasons


@pytest.mark.parametrize(("source", "translation"), [
    # 低质量原文（梗/自嘲/错拼）回显豁免：原文本就是故意错拼的
    # broken English，模型保留原文是合理行为（come-back 实证：
    # 'supa mario in real loife' 回显被 untranslated_text 拒 →
    # 强制重译再回显 → BLOCKED 留人工，与审核维度 11 低质量豁免对齐）
    ("supa mario in real loife", "Supa Mario in real life"),
    ("ware is sequl. i wan kill ice age baby 4 real now.",
     "ware is sequl. i wan kill ice age baby 4 real now."),
])
def test_quality_allows_low_quality_source_echo(source, translation):
    entry = _entry(source, role="display")

    assert validate_translation_quality(entry, translation).passed


def test_interaction_prompt_input_token_is_subsequence():
    # 译文保留按键序列（顺序一致）且允许出现额外字面量：
    # "Press 1 for Chapter 1" 的章节号 1、"A: " 说话人标记 A 不是按键破坏
    eggs = _entry(
        "Press 1 for Chapter 1 Help, 2 for Chapter 2 Help, or 3 for "
        "Chapter 3 Help\nA/Left Arrow for Previews Page, and D/Right for "
        "Next Page\nEsc to Exit (Pages will unlock as you beat levels)",
        role="display", reason="interaction_prompt",
    )
    assert validate_translation_quality(
        eggs,
        "按下 1 适用于章节 1 求助，第2章需要2个帮助，第3章则需要3个帮助。\n"
        "A/向左键可进入预览页面， D/跳到下一页\n"
        "按 Esc 键退出（完成关卡后，页面将会解锁）").passed
    arrhy = _entry(
        "A: Hey Hal can we swap to the new batch?\n"
        "> H: I'm sorry Dave, I can't do that.\n> A: ...bruh",
        role="display", reason="interaction_prompt",
    )
    assert validate_translation_quality(
        arrhy,
        "A嘿，Hal，我们可以换到新的批次吗？\n"
        "> H: I“对不起，戴夫。” I 做不到那样。\n> A...兄弟").passed


def test_interaction_prompt_merged_lines_exempt():
    """fix-55 交互提示「对象名 + 按键动作行」双行原文的译文合行豁免。

    Flabby Pizza 实证：'Dish\\nG - to throw' 在反馈重译时被模型合并成
    单行「盘子/容器 G 扔掉」——newline_mismatch + line_content_mismatch
    恒定拦截，正确译文被 BLOCKED 留人工（对象名+按键提示共 4 条全部
    阻断）。按键提示双行是 UI 排版，合行无运行时崩溃风险，内容未丢时
    豁免。对象名整行丢失（'G 投掷'）或按键丢失（'盘子/菜肴 - 扔'）
    仍判失败（untranslated_text / input_token_mismatch 承接）。"""
    # 合行但对象名已翻译 + 按键保留 → 豁免通过
    ok = validate_translation_quality(
        _entry("Dish\nG - to throw"), "盘子/容器 G 扔掉")
    assert ok.passed
    assert "newline_mismatch" not in ok.reasons
    assert "line_content_mismatch" not in ok.reasons
    ok2 = validate_translation_quality(
        _entry("Screwdriver\nG - to throw"), "螺丝刀 G 扔掉")
    assert ok2.passed
    # 对象名整行丢失（按键动作行前无内容）→ 仍失败
    dropped = validate_translation_quality(
        _entry("PostCard\nG - to throw"), "G 投掷")
    assert "newline_mismatch" in dropped.reasons
    # 按键丢失（合行但无 G 字面量）→ 仍失败（input_token_mismatch 承接）
    lost_key = validate_translation_quality(
        _entry("Dish\nG - to throw"), "盘子/菜肴 - 扔")
    assert "input_token_mismatch" in lost_key.reasons
    # 保留双行结构 → 照常通过
    preserved = validate_translation_quality(
        _entry("Dish\nG - to throw"), "菜肴/食物\nG 扔掉")
    assert preserved.passed


def test_interaction_prompt_preserves_input_token_count_and_order():
    ordered = _entry(
        "Press E, then hold F to interact",
        role="display", reason="interaction_prompt",
    )
    repeated = _entry(
        "Press E, then hold E to interact",
        role="display", reason="interaction_prompt",
    )

    assert validate_translation_quality(
        ordered, "先按 F，再按 E 交互").reasons == ("input_token_mismatch",)
    assert validate_translation_quality(
        repeated, "按 E 交互").reasons == ("input_token_mismatch",)


def test_interaction_prompt_preserves_quoted_bracketed_and_numeric_glyphs():
    entry = _entry(
        "Press 'E', hold [F], then press 2",
        role="display", reason="interaction_prompt",
    )

    assert validate_translation_quality(entry, "先按 E，再按 F，最后按 2").passed
    assert validate_translation_quality(
        entry, "先按 F，再按 E，最后按 2").reasons == (
            "input_token_mismatch",)


def test_interaction_prompt_preserves_parenthesized_and_angle_wrapped_glyphs():
    cases = (
        ("Press (E) to open", "按 E 键打开", "按 F 键打开"),
        ("Press <E> to open", "按 <E> 键打开", "按 <F> 键打开"),
    )
    for original, preserved, changed in cases:
        entry = _entry(original, role="display", reason="interaction_prompt")

        assert validate_translation_quality(entry, preserved).passed
        assert "input_token_mismatch" in validate_translation_quality(
            entry, changed).reasons


def test_interaction_prompt_preserves_legacy_named_physical_tokens():
    cases = (
        ("Press LB to block", "按 LB 键格挡", "按 RB 键格挡"),
        ("Press R1 to dodge", "按 R1 键闪避", "按 L1 键闪避"),
        ("Press Numpad 1 to select", "按 Numpad 1 键选择", "按 Numpad 2 键选择"),
    )
    for original, preserved, changed in cases:
        entry = _entry(original, role="display", reason="interaction_prompt")

        assert validate_translation_quality(entry, preserved).passed
        assert "input_token_mismatch" in validate_translation_quality(
            entry, changed).reasons


def test_interaction_prompt_preserves_common_named_physical_keys():
    cases = (
        ("Press Esc to exit", "按 Esc 键退出", "按 F 键退出"),
        ("Press Backspace to close", "按 Backspace 键关闭", "按 Delete 键关闭"),
        ("Press D-Pad Up to select", "按 D-Pad Up 选择", "按 D-Pad Down 选择"),
    )
    for original, preserved, changed in cases:
        entry = _entry(original, role="display", reason="interaction_prompt")

        assert validate_translation_quality(entry, preserved).passed
        assert "input_token_mismatch" in validate_translation_quality(
            entry, changed).reasons


def test_interaction_prompt_preserves_complete_physical_chords():
    cases = (
        ("Press Ctrl+Delete to remove", "按 Ctrl+Delete 删除", "按 Ctrl+Backspace 删除"),
        ("Press Page Up+Shift to scroll", "按 Page Up+Shift 滚动", "按 Page Up+Ctrl 滚动"),
        ("Press D-Pad Up+LB to select", "按 D-Pad Up+LB 选择", "按 D-Pad Up+RB 选择"),
    )
    for original, preserved, changed in cases:
        entry = _entry(original, role="display", reason="interaction_prompt")

        assert validate_translation_quality(entry, preserved).passed
        assert "input_token_mismatch" in validate_translation_quality(
            entry, changed).reasons


def test_interaction_prompt_preserves_underscore_and_dash_physical_chords():
    cases = (
        ("Press Ctrl_Delete to remove", "按 Ctrl_Delete 删除", "按 Ctrl_Backspace 删除"),
        ("Press Ctrl-Delete to remove", "按 Ctrl-Delete 删除", "按 Ctrl-Backspace 删除"),
    )
    for original, preserved, changed in cases:
        entry = _entry(original, role="display", reason="interaction_prompt")

        assert validate_translation_quality(entry, preserved).passed
        assert "input_token_mismatch" in validate_translation_quality(
            entry, changed).reasons


def test_interaction_prompt_allows_semantic_inputs_to_be_translated():
    cases = (
        ("Press Any Key", "按任意键"),
        ("right click with Harpoon equipped to reel in", "装备鱼叉后用右键收线"),
        ("Square/X/Y Button: Jump", "方块、叉和三角键：跳跃"),
        ("Press X Button to jump", "按叉键跳跃"),
    )

    for original, translation in cases:
        result = validate_translation_quality(
            _entry(original, role="display", reason="interaction_prompt"),
            translation,
        )
        assert result.passed, (original, result.reasons)


def test_interaction_prompt_rejects_untranslated_action_words_with_chinese_suffix():
    entry = _entry(
        "Press E to open", role="display", reason="interaction_prompt",
    )

    result = validate_translation_quality(entry, "Press E to open（打开）")

    assert "untranslated_text" in result.reasons


def test_multiline_item_label_is_not_treated_as_an_input_action():
    entry = _entry(
        "Key30\nG - to throw\n", role="display", reason="interaction_prompt")

    result = validate_translation_quality(entry, "Key30\nG – 投掷")

    assert result.passed is True
    assert result.reasons == ()


def test_multiline_translation_removes_model_line_end_spaces():
    entry = _entry(
        "Key30\nG - to throw\n", role="display", reason="interaction_prompt")

    result = validate_translation_quality(entry, "Key30  \nG – 投掷  ")

    assert result.passed is True
    assert result.normalized_translation == "Key30\nG – 投掷\n"


def test_multiline_translation_rejects_a_missing_meaningful_line():
    entry = _entry("First\nSecond\nThird", role="display")

    result = validate_translation_quality(entry, "第一\n\n第三")

    assert "line_content_mismatch" in result.reasons


def test_multiline_translation_requires_exact_crlf_delimiters():
    entry = _entry("First\r\nSecond", role="display")

    result = validate_translation_quality(entry, "第一\n第二")

    assert "newline_mismatch" in result.reasons


@pytest.mark.parametrize("delimiter", ["\n", "\r\n", r"\n"])
def test_multiline_translation_preserves_empty_segment_topology(delimiter):
    entry = _entry(delimiter.join(("A", "", "B", "C")), role="display")

    result = validate_translation_quality(
        entry, delimiter.join(("甲", "乙", "", "丙")))

    assert "line_content_mismatch" in result.reasons


def test_camel_tech_abbreviation_echo_is_not_untranslated():
    """VSync 驼峰技术缩写回显（无中文）→ 保留原文合理，不算未翻译。
    （vincent 'VSync: OFF' 真实样本；VSync 在 UI 词典但驼峰豁免放行）"""
    entry = _entry("VSync", role="display")

    result = validate_translation_quality(entry, "VSync")

    assert result.passed is True


def test_camel_echo_of_source_absent_word_still_fails():
    """译文残留原文没有的驼峰缩写 → 仍判未翻译（防模型幻觉新词）。"""
    entry = _entry("Settings", role="display")

    result = validate_translation_quality(entry, "MonoBehaviour")

    assert result.passed is False
    assert "untranslated_text" in result.reasons


def test_format_template_echo_is_not_untranslated():
    """日期/数字格式模板回显（yyyy-MM-dd HH:mm:ss、{0:F2}）→ 格式串是
    string.Format/ToString 参数非显示语义，回显是正确行为（0.26 地毯式
    实证：force-reboot 3 条 PlayFab 日期格式回显被误判 untranslated_text）。"""
    for fmt in ["yyyy-MM-dd HH:mm:ss.FFFF",
                "{0:F2}, {1:F3}",
                "yyyy-MM-ddTHH:mm:ss.FFFZ"]:
        entry = _entry(fmt, role="display")
        result = validate_translation_quality(entry, fmt)
        assert result.passed is True, (fmt, result.reasons)


def test_format_template_not_matching_plain_words():
    """格式模板判定不误伤普通文本（含字母词/句子）。"""
    assert not _is_format_template("help")
    assert not _is_format_template("Time: {0}")
    assert not _is_format_template("Hello there world")
    assert not _is_format_template("value={0}")


def test_service_phrase_brand_echo_is_not_untranslated():
    """'Youtube Music' 品牌短语大小写修正回显 → 合理保留（YouTube 服务名）。"""
    entry = _entry("Youtube Music", role="display")

    result = validate_translation_quality(entry, "YouTube Music")

    assert result.passed is True


def test_dev_placeholder_with_lorem_suffix_is_skipped():
    """开发者填充占位（'description goes here ipsum dolor...'）→ 回显合理。
    （Incremental RTS 真实样本）"""
    text = ("The achievement's description goes here ipsum dolor lorem "
            "sit amet ipsum dolor sit amet ipsum dolor sit amet")
    from hanhua.core.quality import is_lorem_ipsum_placeholder

    assert is_lorem_ipsum_placeholder(text) is True

    entry = _entry(text, role="display")
    result = validate_translation_quality(entry, text)
    assert result.passed is True


def test_explanatory_garbage_output_is_rejected():
    """解释式垃圾输出：模型把翻译不了的词当提问输出解释段落（Mierda →
    '该文本看起来像是随机组合的文字...以下是可能的解释：'）→ 判
    explanatory_prefix 失败（containment 实证）。译文是目标语言内容，
    解释句式不会正常出现。"""
    entry = _entry("Mierda", role="display")
    garbage = ("该文本看起来像是随机组合的文字，没有明确的含义。"
               "以下是可能的解释：\n\n- **Mierda**: 这可能是西班牙语中"
               "的\"shit\"或\"damn\"的缩写，表示\"该死\"或\"糟糕\"的意思。")
    result = validate_translation_quality(entry, garbage)
    assert "explanatory_prefix" in result.reasons

    entry2 = _entry("CreditsVolume (1) Profile", role="display")
    garbage2 = ('参考以下翻译：\n"Volume"可译为"音量"\n"Profile"可译为"简介"')
    result2 = validate_translation_quality(entry2, garbage2)
    assert "explanatory_prefix" in result2.reasons


def test_explanatory_detection_no_false_positive():
    """解释垃圾检测不误伤正常译文：「以下是重要信息」（非「以下是可能的
    解释」精确句式）、短句「没有明确的含义」式描述（<20 字符）→ 不判。"""
    entry = _entry("The following information is important", role="display")
    result = validate_translation_quality(entry, "以下是重要信息，请仔细阅读。")
    assert "explanatory_prefix" not in result.reasons


def test_hipster_ipsum_is_placeholder():
    """hipster ipsum 占位文本（'XOXO keytar glossier mumblecore. Tote bag
    listicle normcore kinfolk kogi hoodie...'：hipster 风格 lorem ipsum
    生成器词汇，containment level3-6 assets 实证 5 条）→ 占位文本，
    模型回显合理。≥4 特征词同句才判（真实文本不会堆 4 个 hipster 词）。"""
    hipster = ("XOXO keytar glossier mumblecore. Tote bag listicle normcore "
               "kinfolk kogi hoodie hashtag edison bulb actually lo-fi "
               "keffiyeh affogato. Health goth flexitarian enamel pin organic.")
    from hanhua.core.quality import is_lorem_ipsum_placeholder
    assert is_lorem_ipsum_placeholder(hipster) is True
    assert is_lorem_ipsum_placeholder("I bought a hoodie from the food truck") is False
    assert is_lorem_ipsum_placeholder("Hello world") is False

    entry = _entry(hipster, role="display")
    result = validate_translation_quality(entry, hipster)
    assert result.passed is True


def test_artistic_case_echo_is_exempt_from_untranslated():
    """艺术化混排字回显（deadbeat 实证：'DeAD' → 模型 'deAD' 大小写
    噪声变体）→ 豁免 untranslated_text（把艺术写法当普通词翻译成
    '死亡' 是错误）。规范形态 dead/DEAD 段长 4 不豁免、TitleCase
    Continue 不豁免——UI 词典词检查仍生效。"""
    result = validate_translation_quality(
        _entry("DeAD", role="display"), "deAD")
    assert result.passed is True
    result = validate_translation_quality(
        _entry("DeAD", role="display"), "DeAD")
    assert result.passed is True
    # 规范形态仍判失败（dead 是 UI 词典词，必须翻译）
    result = validate_translation_quality(
        _entry("dead", role="display"), "dead")
    assert "untranslated_text" in result.reasons
    result = validate_translation_quality(
        _entry("DEAD", role="display"), "DEAD")
    assert "untranslated_text" in result.reasons
    # TitleCase 普通词仍判失败（Continue 该译「继续」）
    result = validate_translation_quality(
        _entry("Continue", role="display"), "continue")
    assert "untranslated_text" in result.reasons


def test_safe_keepers_domain_suffix_after_chinese_is_stripped():
    """SAFE_KEEPERS 域名/版本/扩展名后缀边界（F4，deepest-sword 实证）：
    Python re 的 \b 是 Unicode 词边界，中文（\w）算词字符——译文
    'Speedrun.com上的排行榜' 的 com 后紧跟中文时 \b 不成立 → 域名不剥 →
    com 被当小写普通词残留误判 target_script_mismatch。修复：后缀边界用
    (?![A-Za-z0-9])（只排除 ASCII 词字符继续拼接）。"""
    from hanhua.core.placeholders import SAFE_KEEPERS
    # 域名后缀 + 中文（译文最常见形态）→ 剥
    assert SAFE_KEEPERS.sub(" ", "Speedrun.com上的排行榜") == " 上的排行榜"
    assert SAFE_KEEPERS.sub(" ", "itch.io页面") == " 页面"
    # 版本号 + 中文 → 剥（beta 不被当小写普通词）
    assert SAFE_KEEPERS.sub(" ", "版本0.4.0beta说明") == "版本 说明"
    # 文件扩展名 + 中文 → 剥
    assert SAFE_KEEPERS.sub(" ", "SPOLOUS.exe游戏") == "SPOLOUS 游戏"
    # 用户名/艺名（小写域名分支）+ 中文 → 剥
    assert SAFE_KEEPERS.sub(" ", "yu.una上的") == " 上的"
    # 回归：ASCII 词字符继续拼接不剥（comedy 的 com 不是域名后缀；
    # 大写开头避开小写用户名分支——全小写 welcome.coming 本就是
    # 用户名/艺名形态，旧行为同样剥）
    assert SAFE_KEEPERS.sub(" ", "ABC.comedy") == "ABC.comedy"
    assert SAFE_KEEPERS.sub(" ", "itch.io") == " "


def test_has_independent_lower_word_version_template_letter_exempt():
    """单字母 + 花括号占位符（'v{0}' 版本号模板）不是独立小写普通词
    （F5，deepest-sword 实证）：版本号模板回显是正确行为，v 被当独立
    小写词 → proper_name_echo 豁免失效 → target_script_mismatch 恒败。"""
    assert has_independent_lower_word("v{0}") is False
    assert has_independent_lower_word("{0}v") is False
    # 回归：普通小写词/撇号属格尾巴行为不变
    assert has_independent_lower_word("hello world") is True
    assert has_independent_lower_word("Jump During Playtime's Jumprope") is False
    assert has_independent_lower_word("MEGA CORP") is False


def test_key_name_mistranslated_rejected():
    """fix-27 键名强制保留：原文含物理键名（Shift/RMB/Esc…）译文把键名
    译成中文（RMB→人民币）→ 判失败（force-reboot 实证被记忆沉淀污染）。
    正确保留键名的译文通过（goodmorning 实证）。"""
    # RMB 译成「人民币」→ 失败
    bad = validate_translation_quality(
        _entry("RMB to scope"), "人民币 给 范围")
    assert "key_name_mistranslated" in bad.reasons
    # Shift 译成「移位」→ 失败
    bad2 = validate_translation_quality(
        _entry("Camera Control - Shift + RMB"),
        "相机控制 - 移位 + 人民币")
    assert "key_name_mistranslated" in bad2.reasons
    # 保留键名的正确译文 → 通过
    ok = validate_translation_quality(
        _entry("Camera Control - Shift + RMB"),
        "相机控制 - Shift + RMB")
    assert ok.passed is True
    # 有中文通称的键不强制（回车/空格是标准译法；非交互提示语境，
    # 交互提示的 input_token 保留检查由另一条链负责）
    ok2 = validate_translation_quality(
        _entry("Enter"), "回车")
    assert ok2.passed is True
    # 原文含键名但译文无中文（纯回显）→ 由 untranslated_text 管，本检查不判
    echo = validate_translation_quality(
        _entry("SHIFT"), "SHIFT")
    assert "key_name_mistranslated" not in echo.reasons


def test_glossary_key_pair_exempted():
    """fix-27 键名词对豁免：词对 source 是键名（RMB→人民币、SHIFT→移位）
    是错误沉淀——检查跳过，正确译文保留键名不判 glossary_mismatch
    （goodmorning 实证：'Camera Control - Shift + RMB' 被污染词对误杀）。"""
    entry = _entry("Camera Control - Shift + RMB")
    ok = validate_translation_quality(
        entry, "相机控制 - Shift + RMB",
        glossary=[("RMB", "人民币"), ("SHIFT", "移位")])
    assert ok.passed is True
    # 非键名词对不受影响（Moon Key 照常检查）
    drift = validate_translation_quality(
        _entry("Use Moon Key to open the basement"),
        "使用月之钥匙打开地下室", glossary=[("Moon Key", "月光钥匙")])
    assert "glossary_mismatch" in drift.reasons


def test_single_token_pair_substring_skips_natural_sentence():
    """fix-28 单 token 词对子串命中仅标签语境检查：TIME→时间 词对
    子串命中自然句 'time to take on'（前后都是字母词）→ 词对不适用，
    意译译文「是时候」不判 glossary_mismatch（goodmorning 实证完美
    译文被误杀）。标签语境（'miss: 999' 右邻冒号、'TIME' 单独行）
    照常检查。"""
    # 自然句：TIME 词对不适用 → 意译不再被 glossary_mismatch 误杀
    # （换行结构差异由 batch_translator line_merge 兜底放行）
    ok = validate_translation_quality(
        _entry("you're all ready -\ntime to take on the day!"),
        "你们都准备好了——是时候开始这一天了！",
        glossary=[("TIME", "时间")])
    assert "glossary_mismatch" not in ok.reasons
    # 标签语境：miss 右邻冒号 → 词对适用 → 回显判失败
    # （C5 拒绝表只管审核沉淀端；quality 强制保留——按钮/标签词
    #  漏翻拦截有价值）
    label = _entry("miss: 999")
    fail = validate_translation_quality(
        label, "miss: 999", glossary=[("miss", "未命中")])
    assert "glossary_mismatch" in fail.reasons
    # 句首大写 + 右邻小写词 = 英文句子首词大写规则，不是标签：
    # 'Time for some science!' 意译「是时候」不判失败（inch-by-inch 实证）
    sentence = validate_translation_quality(
        _entry("Time for some science!"),
        "是时候进行一些科学研究了！",
        glossary=[("Time", "时间"), ("TIME", "时间")])
    assert "glossary_mismatch" not in sentence.reasons
    # 句尾标点不是标签标记：'...at this size!' 的 size 右邻感叹号是
    # 句子句尾，意译「这种规模」不判失败（inch-by-inch 实证）
    tail = validate_translation_quality(
        _entry("If I can't finish the antidote at this size!"),
        "如果我没法完成这种规模的解毒剂！",
        glossary=[("size", "大小")])
    assert "glossary_mismatch" not in tail.reasons
    # 句中 TitleCase 仍是 UI 菜单词形态：Settings 词对照常适用，
    # 漏翻（回显 Settings）判失败
    ui_word = validate_translation_quality(
        _entry("Open Settings menu"), "打开 Settings 菜单",
        glossary=[("Settings", "设置")])
    assert "glossary_mismatch" in ui_word.reasons
    # 正确翻译（Settings→设置）不受影响
    ui_ok = validate_translation_quality(
        _entry("Open Settings menu"), "打开设置菜单",
        glossary=[("Settings", "设置")])
    assert "glossary_mismatch" not in ui_ok.reasons
    # 多词短语词对子串命中不受影响（固定表达照常检查）
    drift = validate_translation_quality(
        _entry("ENTER NAME please"), "请填写名字",
        glossary=[("ENTER NAME", "输入姓名")])
    assert "glossary_mismatch" in drift.reasons
    # F10：功能词词对（on/off/in…）不做强制——'Analytics is ON.'
    # 的 ON 是句子强调，译文「已开启」不被 (ON→关于) 误杀
    # （incremental-rts 实证；功能词是句子功能成分，强制必误杀）
    emphasis = validate_translation_quality(
        _entry("<b>Analytics is ON.</b> Turn this off to opt out."),
        '<b>分析功能已开启。</b>若要关闭此功能，请选择“关闭”。',
        glossary=[("ON", "关于"), ("on", "在")])
    assert "glossary_mismatch" not in emphasis.reasons
    # 非功能词词对不受影响：'Click Save.' 漏翻（残留 Save）仍判失败
    # （Save 非功能词，句中大写 + 右邻句号 → 词对照常适用）
    save_leak = validate_translation_quality(
        _entry("Click Save."), "点击 Save。",
        glossary=[("Save", "保存")])
    assert "glossary_mismatch" in save_leak.reasons
    # 正确翻译（Save→保存）不受影响
    save_ok = validate_translation_quality(
        _entry("Click Save."), "点击保存。",
        glossary=[("Save", "保存")])
    assert "glossary_mismatch" not in save_ok.reasons
    # 句中大写 + 右邻冒号仍是 UI 标签形态：词对照常适用
    colon = validate_translation_quality(
        _entry("Set Time: 60"), "设置 Time：60",
        glossary=[("Time", "时间")])
    assert "glossary_mismatch" in colon.reasons
    # 占位符花括号边界豁免（F10）：'{health}' 内词是变量不是可翻译
    # 语义文本——'Increase unit HP by {health}' 译文「生命值」不被
    # (HEALTH→健康) 误杀（incremental-rts 实证）
    hp = validate_translation_quality(
        _entry("Upgraded hull alloy shrugs off fire. Increase unit HP by {health}"),
        "升级后的船体合金能抵御火焰攻击。该单位的生命值将增加 {health}。",
        glossary=[("HEALTH", "健康")])
    assert "glossary_mismatch" not in hp.reasons
    # F10d：支持页链接行（系统名 + URL）模型保留 URL 是正确行为——
    # SAFE_KEEPERS 完整 URL 段剥离后「Windows: 」无小写普通词，
    # 回显不再误判 untranslated_text（incremental-rts 实证三条）
    url_echo = validate_translation_quality(
        _entry("Windows: https://support.microsoft.com/en-us/help/314960"
               "/how-to-install-fonts-in-windows"),
        "Windows：https://support.microsoft.com/en-us/help/314960"
        "/how-to-install-fonts-in-windows",
        glossary=[("on", "在")])
    assert "untranslated_text" not in url_echo.reasons
    assert "glossary_mismatch" not in url_echo.reasons
    # URL 内独立 token（.../font+on+gnu%2Blinux 的 on）不再被介词词对
    # 误杀（URL 段整体剥离）
    url_token = validate_translation_quality(
        _entry("Linux: https://www.google.com/search?q=how+to+install"
               "+a+font+on+gnu%2Blinux"),
        "Linux: https://www.google.com/search?q=how+to+install"
        "+a+font+on+gnu%2Blinux",
        glossary=[("on", "在")])
    assert "glossary_mismatch" not in url_token.reasons


def test_f11_non_translatable_segments_exempt_from_pairs():
    """F11（incremental-rts 实证）：非可翻译文本段继续豁免词对——
    富文本标签属性/标签名、文件名段、方向词单字对、标签透明化的
    句首判定。均与 F10b 花括号占位符同类：标记语言与标识符不是
    可翻译语义文本。"""
    # 富文本标签内部 token：'<size=120%>' 的 size / '</size>' 是标签
    # 属性/标签名，译文「武器：」不被 (size→大小) 误杀
    tag = validate_translation_quality(
        _entry("<size=120%><b>Weapon:</b></size>"),
        "<size=120%><b>武器：</b></size>",
        glossary=[("size", "大小"), ("Weapon", "武器")])
    assert "glossary_mismatch" not in tag.reasons
    # 标签内 token 漏翻不受影响：'<b>HP</b>' 的 HP 在标签内容（非标签
    # 内）→ 词对仍适用（回显判失败）
    leak = validate_translation_quality(
        _entry("<b>Weapon:</b>"), "<b>Weapon：</b>",
        glossary=[("Weapon", "武器")])
    assert "glossary_mismatch" in leak.reasons
    # 标签透明化句首：'<b>Full version on Steam</b>' 的 Full 语义上是
    # 句子首词（左邻标签是装饰层）→ 右邻小写 version → 自然句豁免，
    # 意译「全版本」不被 (full→完整的) 误杀（VICTORY 实证）
    full = validate_translation_quality(
        _entry("<bounce>VICTORY!</bounce>\n<color=#FFD700>You've reached "
               "Level %s!</color>\n\n<b>Full version on Steam</b>"),
        "<bounce>胜利了！</bounce>\n<color=#FFD700>您已达到 %s级！</color>"
        "\n\n<b>Steam全版本版</b>",
        glossary=[("full", "完整的")])
    assert "glossary_mismatch" not in full.reasons
    # 句中标签内词不是句首：'the <b>full version</b>' 的 full 左邻
    # the（词）→ UI 词形态照常 → 漏翻仍判失败
    mid = validate_translation_quality(
        _entry("Buy the <b>full version</b> on Steam"),
        "在 Steam 上购买 <b>full version</b>",
        glossary=[("full", "完整的")])
    assert "glossary_mismatch" in mid.reasons
    # 文件名段：'player-diagnostics.txt' 的 Player 是文件名一部分，
    # 译文保留文件名不被 (Player→玩家) 误杀
    fname = validate_translation_quality(
        _entry("Export player-diagnostics.txt with recent logs, device info, "
               "and current game state."),
        "导出包含最新日志、设备信息以及当前游戏状态的 "
        "player-diagnostics.txt 文件。",
        glossary=[("Player", "玩家")])
    assert "glossary_mismatch" not in fname.reasons
    # 方向词单字对不做全局强制：'(buy factories on the right)' 译文
    # 「右侧」不被 (RIGHT→对) 误杀（方向词在普通文本自由译，方向
    # 语义由输入绑定语境专用检查负责）
    dir_word = validate_translation_quality(
        _entry("Factories produce units automatically. Only one unit type "
               "is available but you will unlock more soon! "
               "(buy factories on the right)"),
        "工厂可以自动生产产品。目前只有一种产品类型可供使用，但未来会"
        "提供更多类型！（请在右侧购买工厂）",
        glossary=[("RIGHT", "对"), ("right", "对"), ("Right", "对")])
    assert "glossary_mismatch" not in dir_word.reasons
    # 方向词词对豁免后漏翻方向词仍被输入绑定检查拦截：
    # 'Hat Right' 是输入绑定语境（设备词 Hat）→ 译文缺「右」判失败
    dir_leak = validate_translation_quality(
        _entry("Hat Right"), "帽子正确",
        glossary=[("Right", "对")])
    assert "direction_mismatch" in dir_leak.reasons


def test_f13_dialogue_script_exempts_single_token_pairs():
    """F13（interdream/DELTATRAVELER 实证）：Undertale 系对话脚本
    （行首 "* " 对话符 / "^NN" 计时码 / 全大写喊话）是叙事自由语义
    文本——单 token 普通词词对强制必然误杀意译：'* They fled the
    place' 译文「那个地方」被 (PLACE→地点) 误杀；'WHAT'S YOUR NAME?'
    译文「你叫什么名字？」被 (NAME→名称) 误杀；'enjoy your time'
    译文「时光」被 (Time→时间) 误杀；'PLAY UNO' 译文「玩 UNO」被
    (Play→播放) 误杀；'Oh shoot' 感叹词被 (SHOOT→射击) 误杀。"""
    # 行首 "* " 对话符 + 计时码：意译不受词对影响
    place = validate_translation_quality(
        _entry("* They fled the place,^05 taking\n  the chair with them!"),
        "* 他们逃离了那个地方，^05还带走了椅子！",
        glossary=[("PLACE", "地点")])
    assert "glossary_mismatch" not in place.reasons
    # 全大写喊话（无 "* " 无计时码）：'WHAT'S YOUR NAME?' 系统提示
    name = validate_translation_quality(
        _entry("WHAT'S YOUR NAME?"), "你叫什么名字？",
        glossary=[("NAME", "名称")])
    assert "glossary_mismatch" not in name.reasons
    # 全大写多词喊话 + 句子标点：'WOULD YOU THREE LIKE TO PLAY UNO?'
    play = validate_translation_quality(
        _entry("WOULD YOU THREE \nLIKE TO PLAY UNO?"),
        "你们三个愿意玩 UNO 吗？",
        glossary=[("Play", "播放")])
    assert "glossary_mismatch" not in play.reasons
    # 字面 "\n"（C# 转义）分隔的全大写喊话同样豁免
    play_literal = validate_translation_quality(
        _entry("WOULD YOU THREE \\nLIKE TO PLAY UNO?"),
        "你们三个愿意玩 UNO 吗？",
        glossary=[("Play", "播放")])
    assert "glossary_mismatch" not in play_literal.reasons
    # "* " 对话符 + 感叹词：'Oh shoot' 是感叹不是射击
    shoot = validate_translation_quality(
        _entry("* Oh shoot,^05 Kris!\n^05* A knife!"),
        "* 哦，天哪，^05克里斯！\n^05* 一把刀！",
        glossary=[("SHOOT", "射击")])
    assert "glossary_mismatch" not in shoot.reasons
    # 对话文本中漏翻普通词仍不被词对豁免掩盖——由其它检查兜底：
    # 这里验证 keep 型与多词短语词对在对话中仍强制
    multi = validate_translation_quality(
        _entry("* You hear using 'F4' can make you have a 'full screen.'"),
        "* 我听说使用 F4 可以全屏。",
        glossary=[("FULL SCREEN", "全屏显示")])
    assert "glossary_mismatch" in multi.reasons
    # 非对话文本不受影响：UI 标签语境词对照常强制（'Enter your NAME'
    # 译文丢「名称」语义仍判失败——注意 NAME 命中处是普通 UI 文本）
    ui = validate_translation_quality(
        _entry("Enter your NAME below."), "在下方输入你的名字。",
        glossary=[("NAME", "名称")])
    assert "glossary_mismatch" in ui.reasons
    # markdown 列表（小写开头）不是对话脚本：不豁免
    md = validate_translation_quality(
        _entry("* item one\n* item two"), "* 项目一\n* 项目二",
        glossary=[("ITEM", "条目")])
    assert "glossary_mismatch" not in md.reasons


# ── Q1 P2：数字一致性（numeric_mismatch） ──────────────────────


def test_numeric_mismatch_detects_changed_values():
    """验收报告三案例：数值改动必须判 numeric_mismatch。"""
    for original, translation in (
        ("Deal 50 damage", "造成 15 点伤害"),
        ("10% boost", "提升 10"),
        ("Score: 1.5", "得分：15"),
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert result.passed is False, (original, translation)
        assert "numeric_mismatch" in result.reasons, (original, translation)


def test_numeric_equivalents_accepted():
    """中文等价转换放行：50→五十/五十点、10%→百分之十/10%、1.5→一点五。"""
    for original, translation in (
        ("Deal 50 damage", "造成五十点伤害"),
        ("10% boost", "提升 10%"),
        ("10% boost", "提升百分之十"),
        ("Score: 1.5", "得分：一点五"),
        ("5 coins", "五枚金币"),
        ("Level 3", "第三关"),
        ("1,500 gold", "一千五百金币"),
        ("10 minutes", "十分钟"),
        ("100", "一百"),
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert result.passed is True, (original, translation, result.reasons)
        assert "numeric_mismatch" not in result.reasons, (original, translation)


def test_numeric_consistency_normal_translations():
    """常规翻译数字原样保留：不得误杀。"""
    for original, translation in (
        ("Press 2 for Level 5", "按 2 进入等级 5"),
        ("50% off sale", "50% 折扣"),
        ("50% off sale", "五折"),
        ("v1.2.3 patch", "版本 1.2.3 补丁"),
        ("12:30 PM", "下午 12:30"),
        ("Take 3 steps back", "向后退三步"),
        ("ID 404 not found", "未找到 ID 404"),
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert result.passed is True, (original, translation, result.reasons)
        assert "numeric_mismatch" not in result.reasons, (original, translation)


def test_numeric_cn_multiplier_and_european_thousands():
    """阿拉伯数字+万/亿乘数换算与欧式千分位点分组（fake-it 实证 C15）：
    '200.000 readers'→'20万读者'、'500.000'→'50万'、'1.5M'→'150万'、
    '1.5 billion'→'15亿' 都是正确译法，不得误杀 numeric_mismatch。
    AI 审核已判通过、机械门却阻断 → 重译恒败 → BLOCKED 留人工。"""
    for original, translation in (
        ("200.000 readers", "20万读者"),
        ("500.000 readers", "50万读者"),
        ("10.000 readers", "1万名读者"),
        ("Reaches 50000 people", "覆盖5万人"),
        ("Costs 1.5 billion", "耗资15亿"),
        ("50,000 damage", "造成五万点伤害"),    # 逗号千分位→中文数字 等价
        ("1.5 million users", "150万用户"),
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert "numeric_mismatch" not in result.reasons, (
            original, translation, result.reasons)
    # 对照：数值真实改动仍判失败——乘数换算不是数字放行口
    for original, translation in (
        ("200.000 readers", "2万读者"),        # 20万→2万 偷改数量级
        ("Reaches 50000 people", "覆盖5千人"),
        ("50万 damage", "造成五千点伤害"),
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert "numeric_mismatch" in result.reasons, (
            original, translation, result.reasons)
    # 中文数字+万 的原文侧（五万=50000）：与「50万」的阿拉伯乘数（=
    # 500000）不同值，必须能区分——乘数换算不是单向放行口
    assert _number_tokens("五万") == [(50000.0, False, False)]


def test_numeric_mismatch_missing_number_rejected():
    """原文数字被译文整体吞掉（无对应）→ 判失败。"""
    result = validate_translation_quality(
        _entry("Gain 3 health"), "恢复生命")
    assert "numeric_mismatch" in result.reasons


def test_numeric_check_exempts_log_and_format_templates():
    """日志/格式模板豁免：%d 等格式说明数字自由改写；占位符 {0} 两侧
    一致不受影响；数字保留的正常波次文本照常通过。"""
    for original, translation in (
        ("Player %d joined", "玩家 %d 加入"),
        ("{0} kg of gold", "{0} 千克黄金"),
        ("Wave 3 of 5 begins", "第 3 波开始"),
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert "numeric_mismatch" not in result.reasons, (original, translation)
    # 对照：波次数字改写（3→5）仍判失败——数字是语义数据
    changed = validate_translation_quality(
        _entry("Wave 3 of 5 begins"), "第 5 波开始")
    assert "numeric_mismatch" in changed.reasons


def test_numeric_leetspeak_for_not_mismatch():
    """leetspeak 形近数字豁免（Ice Age Baby Adventure 实证）：'4 real
    now' 的 4=for 是网络口语对字母的替代，不是语义数字——译文「真的」
    不应判 numeric_mismatch（梗文本的正确翻译被机械门误杀，重译再被
    同一门拒 → BLOCKED 留人工）。"""
    for original, translation in (
        ("ware is sequl. i wan kill ice age baby 4 real now.",
         "ware 是 sequl。我现在真的想杀死冰河世纪宝宝。"),
        ("i wan kill ice age baby 4 real now", "我想立刻杀死冰河世纪宝宝"),
        ("2fast 2furious", "速度太快太激烈"),   # 2=to
        ("gr8 game bro", "很棒的游戏兄弟"),      # 8=ate
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert "numeric_mismatch" not in result.reasons, (original, translation)


def test_numeric_leetspeak_does_not_exempt_semantic_number():
    """leetspeak 豁免只对「原文明显低质量（≥2 个梗/错拼词）」生效——
    普通语义数字（关卡/伤害/数量）被吞仍判失败；普通原文不因含单个
    数字+空格+词 形态就被豁免。"""
    for original, translation in (
        ("Deal 4 damage", "造成伤害"),          # 4 是独立语义数量
        ("Gain 3 health", "恢复生命"),
        ("You have 4 lives", "你有很多条命"),
        ("I need help with 3 gems", "我需要宝石的帮助"),  # 普通原文吞语义数字
    ):
        result = validate_translation_quality(_entry(original), translation)
        assert "numeric_mismatch" in result.reasons, (original, translation)
    # 对照：普通原文的语义数字改动（4→5）仍判失败
    changed = validate_translation_quality(
        _entry("Deal 4 damage"), "造成 5 点伤害")
    assert "numeric_mismatch" in changed.reasons
