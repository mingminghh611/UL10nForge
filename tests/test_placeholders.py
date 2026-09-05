import pytest

from hanhua.core.placeholders import (extract_placeholders,
                                      is_credit_like,
                                      is_hard_structural,
                                      self_heal_format_tags,
                                      validate_translation, should_skip)


def test_extract_brace():
    assert extract_placeholders("欢迎 {name}！还剩 {0} 秒") == ["{name}", "{0}"]


def test_extract_dotnet_format_brace_placeholder():
    assert extract_placeholders(r"{0}kg\n£{1:0.00}") == [
        "{0}", r"\n", "{1:0.00}",
    ]


def test_extract_percent_and_tags():
    assert extract_placeholders("HP %d%% <b>恢复</b> [b]") == ["%d", "%%", "<b>", "</b>", "[b]"]


def test_validate_preserves_placeholder_multiplicity():
    ok, missing, extra = validate_translation("Lv.{0} -> {0}", "等级{0}")
    assert not ok and missing == ["{0}"] and not extra


def test_extract_and_validate_preserve_cross_type_order():
    assert extract_placeholders("HP %s / {0} <b>text</b>") == [
        "%s", "{0}", "<b>", "</b>",
    ]
    ok, missing, extra = validate_translation(
        "HP %s / {0}", "生命 {0} / %s")
    assert not ok and not missing and not extra


def test_validate_missing():
    ok, missing, extra = validate_translation("Take {item} now", "拿起物品")
    assert not ok and missing == ["{item}"]


def test_validate_extra():
    ok, missing, extra = validate_translation("Take the item", "拿起{item}物品")
    assert not ok and extra == ["{item}"]


def test_validate_cjk_localized_placeholder_and_lone_brace():
    """F12（incremental-rts row207 实证）：模型把 {health} 本地化成中文
    变量名（{伤害}/{速度惩罚}）并在尾部重复原文占位符堆叠——brace
    模式只匹配 ASCII，中文花括号段逃过提取 → Counter 恰好相等。
    中文占位符（变量名本地化破坏运行时替换）与孤立花括号（'健康}'
    的 '}'——{health} 被拆碎）必须判失败。"""
    ok, missing, extra = validate_translation(
        "Thunderstorms spawn {health} HP, {damage} damage",
        "雷暴拥有健康} HP、{伤害}，尾部堆叠{health}{damage}")
    assert not ok
    assert missing == ["<lone-brace>"]
    assert "{伤害}" in extra
    # 正常保留不受影响
    ok2, missing2, extra2 = validate_translation(
        "HP {health}", "生命值 {health}")
    assert ok2 and not missing2 and not extra2
    # 原文无占位符时对话里的花括号（文本内容）不拦截
    ok3, _, _ = validate_translation("他说这是{}", "他说这是{}")
    assert ok3
    # Ren'Py 样式 {w=秒} 等号值标记必须识别为占位符（a-catfiends 真实漏检）
    assert extract_placeholders("HELLO.{w=3}{x}") == ["{w=3}", "{x}"]


def test_validate_missing_renpy_wavetime_tag():
    # 译文丢失 {w=N} 必须被拒绝（现状漏检：译文丢 {w=3} 仍通过）
    ok, missing, extra = validate_translation(
        "SOBER.{w=0.5} NOW.{w=3}{x}", "清醒了。{x}")
    assert not ok and missing == ["{w=0.5}", "{w=3}"]


def test_extract_renpy_close_tag():
    # Ren'Py 结束标签 {/i} {/b} 也属于必须保留的标记
    assert extract_placeholders("Hi {i}you{/i}!") == ["{i}", "{/i}"]


def test_validate_missing_renpy_close_tag():
    ok, missing, extra = validate_translation(
        "Hi {i}you{/i}", "你好{/i}")
    assert not ok and missing == ["{i}"]


def test_self_heal_backfills_missing_renpy_wavetime():
    # a-catfiends 真实样本：译文丢 1 个 {w=0.5} → 按原文顺序插回（{w=3} 前）
    healed = self_heal_format_tags(
        "AND,{w=0.5} IN SOME CASES,{w=0.5}\nEVEN REVERSE THE FLOW OF TIME.{w=3}{x}",
        "在某些情况下，AND的值为{w=0.5}。\n甚至颠倒时间的流动。{w=3}{x}")
    assert healed == ("在某些情况下，AND的值为{w=0.5}。"
                      "\n甚至颠倒时间的流动。{w=0.5}{w=3}{x}")
    ok, missing, extra = validate_translation(
        "AND,{w=0.5} IN SOME CASES,{w=0.5}\nEVEN REVERSE THE FLOW OF TIME.{w=3}{x}",
        healed)
    assert ok


def test_self_heal_backfills_missing_closing_color_tag():
    # interdream 真实样本：译文丢尾部 </color> → append 到末尾
    src = ("<color=#888888FF>(Can be set)</color>\n"
           "<color=#FF0000FF>You will know</color>")
    dst = ("<color=#888888FF>(Can be set)</color>\n"
           "<color=#FF0000FF>You will know")
    assert self_heal_format_tags(src, dst) == src


def test_self_heal_backfills_tail_gap_when_anchor_sparse():
    """F8-B，a-catfiends 真实样本：译文丢尾部 {w=3}{x} 只留 {punch=3,2}
    ——missing(2) >= dst(1) 曾被锚点限制拒绝补全（好译文被弃、失败恒
    现）；缺失全在最后保留占位符之后 → append 位置唯一正确 → 补全。"""
    src = "I am {punch=3,2}NOT who I used to be.{w=3}{x}"
    dst = "我已经不再是曾经的我了。{punch=3,2}"
    healed = self_heal_format_tags(src, dst)
    assert healed == "我已经不再是曾经的我了。{punch=3,2}{w=3}{x}"
    ok, missing, extra = validate_translation(src, healed)
    assert ok


def test_self_heal_still_rejects_mid_gap_when_anchor_sparse():
    """对照：缺失在最后保留占位符之前（中段缺口 + 锚点不足）→ 仍拒绝
    补全（位置不可靠，交 protected/multiline repair 重建）。"""
    src = "{punch=3,2}A {w=3} B{x}"
    dst = "甲 {w=3} 乙"
    assert self_heal_format_tags(src, dst) == dst


def test_self_heal_reorders_reversed_closing_tags():
    # the-keeper 真实样本：</b></color> 逆序 → 重排为原文顺序 </color></b>
    src = "<b><color=#eb5354>Thanks!</color></b>"
    dst = "<b><color=#eb5354>谢谢！</b></color>"
    assert self_heal_format_tags(src, dst) == "<b><color=#eb5354>谢谢！</color></b>"


def test_self_heal_returns_unchanged_when_no_gap():
    assert self_heal_format_tags("<b>Hi</b>", "<b>你好</b>") == "<b>你好</b>"
    assert self_heal_format_tags("Hello world", "你好，世界") == "你好，世界"


def test_self_heal_does_not_remove_extra_placeholders():
    # 模型新增占位符（幻觉）→ 原样返回，不自动删（仍由判定失败暴露）
    dst = "拿起{item}物品"
    assert self_heal_format_tags("Take the item", dst) == dst


def test_self_heal_does_not_repair_reordered_placeholders():
    # 占位符顺序破坏（%s 与 {0} 互换）→ 不是子序列 → 原样返回
    dst = "生命 {0} / %s"
    assert self_heal_format_tags("HP %s / {0}", dst) == dst


def test_self_heal_does_not_reorder_when_opening_tags_differ():
    # 开标签顺序不同（内容结构变化）→ 不重排闭合标签
    src = "<b><color=#fff>Hi</color></b>"
    dst = "<color=#fff><b>你好</b></color>"
    assert self_heal_format_tags(src, dst) == dst


def test_base64_zip_payload_is_skipped():
    # Morfosi level5 str/0 实证：base64 编码 ZIP 包（PK\x03\x04 魔数 UEsDB，
    # 结尾 == 填充符）。此前 _BASE64 字符集不含 '=' → fullmatch 失败漏网，
    # 模型整段回显恒败（untranslated_text）。
    payload = (
        "UEsDBBQAAAgIAACYn+uubW6iYAIAAAUFAAALACQAZ3JhcGgwLmpzb24KACAAAAAAAAEAGAAAg"
        "D7V3rGdAQCAPtXesZ0BAIA+1d6xnQFlVEtzmzAQ/isenRsP4Ne4t9ZxnB7ymLidXLjI0mI0Fh"
        "IjidhOxv+9KyEMdbkg9tvHt9+u+CJC2RqY02ZjBH/SHMj3EfFn8m3Ug49wonuttuITOp93wV"
        "3pnWjweaNOaLSnPszqCpwR7IfaS++coLFRotCmWvM9rLR1Fs0FlRYQMtr5aIWmrxE54etu6U"
        "POeMoW/vSJp2UyuuCRgXJgBq6T6Xjpn3kWQ9LlIoa0EY1iklY1cE+/D0zT+XjWVVlm41lwVth"
        "e9EsCzLSUwkZ2xJ3r0P22LsGAb58Lis0GRp5ACWJfuvhxjX0pCgsuKmHo+V4Y1KxNSn7qVsi"
        "K2kNL74PKxpfJZvNAqk369B9+lwa4MLp67Oqmia/hSsEOb/TMqHUDpYfm+554GlQ6UnmgOwn"
        "vJahnvTG6URxBZxof2ljI7vvPa2urEtiht7dUb4xN3cveDbYbq+/hEro/raSodkH4aWvYSh3"
        "kDtsABkfzCwmHXbExBVo9iz8WftP9cKki8CCMdQjFLpWnt9ON8a5kHTTz3TRupY2CYI6ka1B"
        "UuvMrpnG3I7zBBmVvkAfqr08sHrHuVtyEBHPvnyQ30Ks+XodlS318Alu+NE4KBT1pDzyjTiu"
        "tVLthQ026sG1jCsoGYftG8H9XixSTaVYs5lM2X0xSOqF3nE0zihdlniZZBospCbsnFPZJ5WtL"
        "NMqDU1N9coV71v1VRhtD67Dt3NDjRnxWeiC5UIXeMgOgXtoMV+JgsAheS77mAgXagnNChXmT"
        "r5xIzQ7A82uiPHS6PjlD8z5LTmrxoZ235GQVfiM5uZDLX1BLAwQUAAAICAAAmJ/rpZsc7G0AA"
        "AB4AAAACQAkAG1ldGEuanNvbgoAIAAAAAAAAQAYAACAPtXesZ0BAIA+1d6xnQEAgD7V3rGdA"
        "atWKkstKs7Mz1OyUjDRM9IzNNJRUEovSizIKAaKGII4pZkpIHa0UpqxiVGauZlJspm5sWGic"
        "aJuSrKJUaKRpZGZoYGRUaq5iVIsUH1JZUGqX2JuKkRPQGJJRlpmXkpmXrqee1FmijvIaKXYWg"
        "BQSwECLQAUAAAICAAAmJ/rrm1uomACAAAFBQAACwAkAAAAAAAAAAAAAAAAZ3JhcGgwLmpzb24"
        "KACAAAAAAAAEAGAAAgD7V3rGdAQCAPtXesZ0BAIA+1d6xnQFQSwECLQAUAAAICAAAmJ/rpZsc"
        "7G0AAAB4AAAACQAkAAAAAAAAAAAAAACtAgAAbWV0YS5qc29uCgAgAAAAAAABABgAAIA+1d6x"
        "nQEAgD7V3rGdAQCAPtXesZ0BUEsFBgAAAAACAAIAuAAAAGUDAAAAAA==")
    assert should_skip(payload)
    # 无 = 填充的普通 base64 序列化数据（含数字）仍拦截（原有行为）
    assert should_skip("aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQgZGF0YTEyMzQ1Njc4OTEyMzQ1"
                       "Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3")
    # 纯字母超长串（无数字、无填充符）不是 base64 特征 → 不误伤
    assert not should_skip("A" * 100)


def test_skip_rules():
    assert should_skip("12345")
    assert should_skip("https://example.com/x")
    assert should_skip("user@mail.com")
    assert should_skip("---")
    assert should_skip("a")
    assert should_skip("{0}")
    assert should_skip("1.0.3")
    assert should_skip("Unity.InputSystem")
    assert should_skip("Assets/Plugins/x.dll")
    assert should_skip("Assembly-CSharp")
    assert should_skip("browscap.ini")
    # Unity 实例化对象名 / 点开头扩展名（真实语料漏检样本）
    assert should_skip("frameVertical(Clone)")
    assert should_skip("GreyPipeBendDown(Clone)")
    assert should_skip("Player(Clone)(Clone)")
    assert should_skip(".spriteatlas")
    assert should_skip(".wav")
    # GUID 标识符 / Master Audio 总线行 / 署名年份行 / 富文本纯符号字符画
    assert should_skip("GUID:cef3ca5fc32178c449992c58120ccded")
    assert should_skip("\t2810670744\tSoundFX\t\t\t"
                       "\\Default Work Unit\\Master Audio Bus\\SoundFX\t")
    assert should_skip("Darien Gore (Fleebs) 2019")
    assert should_skip("<color=#2b3534>▓<color=#FE09DA>▓"
                       "<color=#00AEEF>▓<color=white>▓")
    # IL2CPP Burst 编译器符号 / PDB 调试路径（识别层误收的 metadata 字面量）
    assert should_skip("Unity.Burst.Intrinsics.X86, Unity.Burst, Version=0.0.0.0, "
                       "Culture=neutral, PublicKeyToken=null::DoGetCSRTrampoline()"
                       "--89425a97f3f5")
    assert should_skip('PdbAltPath="Faerie Afterlight_Data/Plugins/x86_64"')
    # 版本号横幅（\t**\t\tVERSION 0.4.3\t\t**）：保留原文是行业惯例，跳过翻译
    assert should_skip("\t**\t\tVERSION 0.4.3\t\t**")
    assert should_skip("\t**\t\tVERSION 0.4.0\t\t**")
    # JSON 序列化字符串（引擎把数据序列化成字符串存资源；翻译会破坏语法）
    assert should_skip('{"declarations":{"collection":{"$content":[],'
                       '"$version":"A"},"$version":"A"}}')
    assert should_skip('{"nest":{"source":"Macro","macro":0,"embed":null}}')
    assert should_skip('[1,2,3,"assets"]')
    # 以 {/[ 开头的真实对话/文本：解析失败 → 必须保留
    assert not should_skip("[Catkus Companion]")
    assert not should_skip("[When a 'memory' is saved, the game saves progress.]")
    assert not should_skip("{name} 攻击了 {target}")
    # 开发者模板占位（真实语料漏检样本）：内容未填写的占位字符串
    assert should_skip("beast description here")
    assert should_skip("Quest description here")
    assert should_skip("Option description here!!!")
    assert should_skip("Description here")
    # 含这些词的真实对话/提示：必须保留
    assert not should_skip("I'm new here.")
    assert not should_skip("I'm here to shop!")
    assert not should_skip("Hey, I wonder if there's anything good in here?")
    assert not should_skip("Put your name here, traveler")
    # I2 Localization 复数模板（{0:p:mine|mines} 运行时展开，翻译会破坏语法）
    assert should_skip("{0} - {1} {1:p:mine|mines}")
    assert should_skip("{0} {0:p:charge|charges}")
    assert should_skip("{1:p:mouse|mice} hidden")
    assert should_skip("Reveals {0} random {0:p:column|columns}.")
    assert should_skip("Restores {0} <b>{0:p:heart|hearts}</b>.")
    # 开发者重复占位行（Hello\nHello\nHello\nHello 模型必回显，flabby-pizza 真实样本）
    assert should_skip("Hello\nHello\nHello\nHello")
    assert should_skip("test\ntest\ntest\ntest")
    # 真实重复/句子形态：必须保留
    assert not should_skip("No. No. No. No.")
    assert not should_skip("Hello\nHello")
    assert not should_skip("Hi there\nHi there")
    # 含 :p: 片段但形态不同的真实文本：必须保留
    assert not should_skip("Press P to pause")
    # IL2CPP 生成的模块调试行（\nmodule.renderOrderPriority: 引擎内部字符串）
    assert should_skip("\nmodule.renderOrderPriority: ")
    assert should_skip("\nmodule.sortOrderPriority: ")
    assert not should_skip("Modules are ready")
    # zalgo 乱码文本（组合字符叠加的字体艺术，翻译必然失败）
    assert should_skip("Ĭ̴̔̈̒́̌̔̓"
                       "̱́̃̉́̈́"
                       "'̴̀̏́̒̃̑")
    assert not should_skip("<b>Save</b>")
    assert not should_skip("Hello world")
    assert not should_skip("你好")
    assert not should_skip("OK")
    assert not should_skip("Start Game")
    assert not should_skip("こんにちは。")


@pytest.mark.parametrize("text", [
    "Click/Tap Me To Go To The Settings Screen.",
    "Click/Tap",
    "Load/Save",
    "Audio/Video",
    "On/Off",
    "Continue/",
    "Save/",
    "CREDITS/",
    r"Line one\nLine two",
    "<b>Press E</b> to continue.",
    "<color=#fff>Settings</color>",
])
def test_slashes_inside_display_text_are_not_paths(text):
    assert not should_skip(text)


def test_uri_mentioned_inside_display_sentence_is_not_a_full_value_uri():
    assert not should_skip("https://example.com/help is our support page.")


@pytest.mark.parametrize("text", [
    "https://example.com/settings",
    r"C:\\Games\\BFNS\\settings.json",
    "/usr/local/share/game/settings.json",
    "Assets/Plugins/x.dll",
    "config/ui/settings",
    "config/settings.json",
    r"config\settings.json",
    "../config/settings",
    "<Keyboard>/space",
    "<color=red>https://example.com/a?x=1</color>",
    "<b>http://example.com/help</b>",
])
def test_full_value_paths_uris_and_input_bindings_are_skipped(text):
    assert should_skip(text)


@pytest.mark.parametrize("text", [
    "UI/Navigate",
    "*/{Submit}",
    "Fonts & Materials/",
    "Sprite Assets/",
    "SpriteAssets/",
    "FontMaterials/",
    "DefaultPresets/",
    "Assets/",
    "Materials/",
    "Presets/",
    "*</size></b></color>",
])
def test_unity_input_actions_asset_folders_and_tag_fragments_are_skipped(text):
    assert should_skip(text)


@pytest.mark.parametrize("text", [
    # .NET 程序集全名（Addressables catalog m_AssemblyName 真实值）
    "Unity.ResourceManager, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null",
    "Assembly-CSharp, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null",
    # 三段程序集名（containment 实证：'Namespace.Type, ScpGame, Version=…'
    # 命名空间类型后还有组名段，旧两段 pattern 停在第一个逗号不匹配）
    "DeferredFog, ScpGame, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null",
    "MyGame.Core, MyGame, Version=1.2.3.0, Culture=neutral, PublicKeyToken=null",
    # 协议相对 URL（A* 寻路库版权文件真实值）
    "//arongranberg.com/astar/",
    "//steamworks.github.io",
    # InputAction 绑定路径（swallow-the-sea level0 真实值，含方括号绑定段）
    "SwallowControls/MousePosition[/Mouse/position]",
    "Player/Aim[/Keyboard/mouse/point]",
    # CLI 参数（Burst 生成命令记录真实值）
    "--platform=Windows",
    "-target=Windows",
    "--linker-options=PdbAltPath=\"PanzerShoot_Data/Plugins/x86_64\"",
])
def test_structural_assembly_refs_protocol_urls_and_cli_args_are_skipped(text):
    assert should_skip(text)


@pytest.mark.parametrize("text", [
    # 对照组：带闭合 BB 标签的显示文本必须保持可翻译
    "Save[/b]",
    "Credits [More]",
    "- 下一行对话",
])
def test_bracketed_display_text_is_not_mistaken_for_structural(text):
    assert not should_skip(text)


@pytest.mark.parametrize("text", [
    # credit/署名/版权行：翻译必然破坏人名/品牌/法律文本（真实失败样本）
    "Created by Sam Hogan for the GMTK Game Jam 2020",
    "A* star pathfind project (free version) by Aron Granberg",
    "Horror-Style Impact 1 - from AudioBlocks.com",
    "Trailer Hit - Psyche - from AudioBlocks.com",
    "©FREEZESTUDIOS 2020",
    "Copyright (c) 2020 My Studio",
])
def test_credit_attribution_and_copyright_lines_are_skipped(text):
    assert should_skip(text)


@pytest.mark.parametrize("text", [
    # 对照组：credit 形状的普通句子必须保持可翻译
    "Press - Start",
    "we were found by Gary.",
    "It was made by Gary",
    "Open the door from the inside",
])
def test_attribution_shaped_sentences_are_not_skipped(text):
    assert not should_skip(text)


@pytest.mark.parametrize("text", [
    # 署名/版权反模式（软猜测）：is_credit_like 命中……
    "A game by Kyuppin",
    "Created by Sam Hogan",
    "made in 48h",
    "© 2021 Some Studio",
    "Game by Team Awesome",
])
def test_is_credit_like_soft_guess_hits_credit_lines(text):
    assert is_credit_like(text)
    # 提取层行为不变：is_hard_structural 仍跳过署名行
    assert is_hard_structural(text)


@pytest.mark.parametrize("text", [
    # 含句子虚词/多段句子 → 不是署名行，软猜测不命中
    "A game by Kyuppin, and it was fun",
    "we were found by Gary.",
    "It was made by Gary and it works",
    "This game was made by a team of three",
    "",
])
def test_is_credit_like_soft_guess_misses_sentences(text):
    assert not is_credit_like(text)


@pytest.mark.parametrize("text", [
    # TMP SDF 字体资产名（真实失败样本：ComicsCarToon SDF Zesty）
    "ComicsCarToon SDF Zesty",
    "roquetteplain SDF Bonus",
    "LiberationSans SDF - Outline",
])
def test_tmp_sdf_font_asset_names_are_skipped(text):
    assert should_skip(text)


@pytest.mark.parametrize("text", [
    # 键盘噪音测试占位符（真实失败样本：开发者乱打文本，翻译必然回显）
    "asdasdasd\nasda sdasd",
    "fdji ijsdijn j jnf oij iuhwr i iu iujn iubt tdr rf",
    "aaaaaaaaaaaa",
    # jam 署名带前导空白/换行（roots 真实样本）
    " \nmade in 48h\nfor Ludum Dare 48",
])
def test_keyboard_noise_and_jam_credit_are_skipped(text):
    assert should_skip(text)


@pytest.mark.parametrize("text", [
    # 对照组：真实小写文本/短词必须保持可翻译
    "hello world",
    "press any key",
    "banana bread",
    "welcome to the game",
    "A pretty tasty fruit, nothing special",
    # 2026-08-24（Ice Age Baby Adventure）：开发者自嘲对话含长词+重复
    # 3-gram 命中键盘噪声形态（was/a/and/the、if/can/have/a/in 等功能词
    # 是句子语法骨架）——真实句子不是键盘乱打，必须保持可翻译（此前
    # 被当键盘噪声整条跳过，8 条自嘲对话漏翻）
    "he was a good frog and was good at protecting the crünch",
    "if you want you can have a ride in my spaceship",
    "plus flex sands powerfull powder is so strong",
    "flex sand is perfect for holes sniffing like cocane and making hills",
    "and recreated it with only flex sand",
    "but it creates a superstrong climable and water tight seal",
    # hickory 实证（2026-09-05）：对话拟声词 tck/shh/psst 是纯辅音簇，
    # 命中键盘噪声分支 b（纯辅音词），但拟声词是内容信号——含拟声词的
    # 真实对话必须保持可翻译（此前整句被跳过，对话漏翻）
    "tck – er… everything is broken.",
    "shh – it's fine.",
    "psst – over here!",
    "tsk tsk, shame.",
    "tck tck",
    "brr – cold out here.",
    "shh! listen…",
])
def test_normal_text_is_not_mistaken_for_noise(text):
    assert not should_skip(text)


def test_undertale_bullet_star_is_protected():
    """Undertale 对话行首 "* " 是脚本标记 → 译文必须保留（DELTATRAVELER 样本：
    模型把 "* (Text)" 合成"（Text）"丢了星号）。"""
    original = "* (Thankfully a standard snow\n  poff.)"
    translation = "（幸好有标准的雪）\npoff.)"
    ok, _, _ = validate_translation(original, translation)
    assert ok is False

    kept = "* (幸好有标准的雪)\npoff.)"
    ok, _, _ = validate_translation(original, kept)
    assert ok is True


def test_undertale_timing_code_is_protected():
    """行尾计时码 ")^05" → 译文必须保留（模型常丢 "^NN"）。"""
    original = "* (A snow poff?)^05\n* (In these trying times??!)"
    ok, _, _ = validate_translation(original, "* (雪怪？)^05\n*（在这艰难时刻？？！）")
    assert ok is False

    ok, _, _ = validate_translation(original, "* (雪怪？)^05\n* （在这艰难时刻？？！）")
    assert ok is True


def test_regular_text_bullet_lines_keep_leading_star():
    """普通多行文本行首 "* "（非 markdown 语义）→ 保护后译文也须保留。"""
    ok, _, _ = validate_translation("* Item one\n* Item two", "* 项目一\n* 项目二")
    assert ok is True

    ok, _, _ = validate_translation("* Item one", "项目一")
    assert ok is False


def test_undertale_bullet_literal_escaped_newline_lines_protected():
    """F13（interdream/DELTATRAVELER 实证）：文本资源里换行是 C# 转义
    字面 "\\n"（两个字符）而非真换行——字面 "\\n" 后的行首 "* " 也须
    保护（原模式 (?m)^ 只匹配真行首，字面转义后行首漏保：'* ...^10
    \\n* ...' 第二个 '*' 被模型丢弃未被拦截）。"""
    # 字面 \n（repr 中 \\n）分隔的多行对话：三个 "* " 全部提取
    original = "* ...^10\n* ...^10\n* ..."
    dropped_mid = "...^10\n...^10\n..."
    ok, _, _ = validate_translation(original, dropped_mid)
    assert ok is False

    kept = "* ...^10\n* ...^10\n* ..."
    ok, _, _ = validate_translation(original, kept)
    assert ok is True

    # 真换行场景不回归：第一行丢 "* " 仍拦截
    ok, _, _ = validate_translation("* Item one\n* Item two", "项目一\n* 项目二")
    assert ok is False


def test_undertale_timing_bare_code_is_protected():
    """F13（interdream 实证）：计时码 "^NN" 形式多样（",^05" 逗号后、
    ".^05" 句点后、"^05* " 行首接对话符）→ 均须保护。数学幂 "x^10"
    （前邻字母）不受影响。"""
    # 逗号后计时码：',^05' 模型丢弃 → 拦截
    original = "* They fled the place,^05 taking the chair with them!"
    ok, _, _ = validate_translation(original, "* 他们逃离了那个地方，带着椅子！")
    assert ok is False

    kept = "* 他们逃离了那个地方，^05带着椅子！"
    ok, _, _ = validate_translation(original, kept)
    assert ok is True

    # 行首计时码 + 对话符：'^05* ' 须保留
    original2 = "* Oh shoot,^05 Kris!\n^05* A knife!"
    ok, _, _ = validate_translation(original2, "* 哦，天哪，^05克里斯！\n^05* 一把刀！")
    assert ok is True

    ok, _, _ = validate_translation(original2, "* 哦，天哪，^05克里斯！\n* 一把刀！")
    assert ok is False

    # 数学幂前邻字母 → 不提取 → 模型丢不拦截（无占位符要求）
    ok, _, _ = validate_translation("The value of 2^10 is 1024.",
                                    "2^10 的值是 1024。")
    assert ok is True


# ── baldis 修复：// 注释行 / 混合符号 token ──


def test_slash_slash_comment_line_is_structural():
    """C# 风格注释行（// 后跟空白）不是游戏文本（baldis 实证：TextAsset
    脚本里 '//        word:replacement:notCaseSensitive' 注释行被模型当
    文本翻译成乱语）。"""
    assert is_hard_structural("//        word:replacement:notCaseSensitive")
    assert is_hard_structural("//")
    assert is_hard_structural("// 跳过一个关卡\n")
    # 协议相对 URL / UNC 路径（// 后直接字母数字，无空白）不被注释分支
    # 拦截，但本身仍由 _PROTOCOL_RELATIVE_URL 分支跳过
    assert is_hard_structural("//hostname/path/to/file")
    assert is_hard_structural("//server/share/config.txt")


def test_mixed_symbol_token_is_structural():
    """无空格 + 强代码符号 + 字母的串多为随机会话 token/编码数据
    （baldis 实证：'xChDC-Gs%OmaMl+g' 模型回显恒败）。"""
    assert is_hard_structural("xChDC-Gs%OmaMl+g")
    assert is_hard_structural("a1b2%c3&d4#e5^f6")
    # 不误伤：'100% sure' 有空格、'save+load' 的 + 不是强符号、
    # 'a50%' 长度不足、'a b % c' 有空格（'50%' 本身是格式后缀分支跳过）
    assert not is_hard_structural("100% sure")
    assert not is_hard_structural("save+load")
    # F49（fromivan 实证 2026-09-01）：孤立单字母碎片 'n۶?'（ASCII 字母 +
    # 阿拉伯-印度数字 U+06F6 + '?'）是二进制/专利残留，被 display_evidence
    # 当句子放行误进池；'a50%' 单字母+数字/符号同形态 → 结构跳过。
    # 'ab50%'（2 字母，真实按钮标签形态）不命中。
    assert is_hard_structural("a50%")
    assert is_hard_structural("n۶?")
    assert not is_hard_structural("ab50%")
    assert not is_hard_structural("a b % c")
    # F50（hickory/dcdb50a165/a61ae49375 实证 2026-09-05）：单字母数字碎片
    # 形态与玩家可读 UI 短语重叠——'2F'（楼层，m_text/roomName 双证）、
    # 'x2'/'2x'（倍数）、'2H'（时长）、'1P'（人数）是真实 UI 短语，被 F49
    # 误杀漏提。白名单词放行（is_hard_structural=False）。
    for w in ("2F", "1F", "3F", "x2", "2x", "1x", "0x", "2H", "12H", "1P", "2P"):
        assert not is_hard_structural(w), w
    # F49 原始拦截面守恒：二进制残留/无语义键仍拦（不在白名单的形态）
    assert is_hard_structural("F1")
    assert is_hard_structural("A1")
    assert is_hard_structural("x7")
    assert is_hard_structural("n۶?")


# ── butterflies 修复：§ 键码 / 语言代码 / 键位映射 / 占位名 / credit 名单 ──


def test_section_key_code_is_structural():
    """语言文件键码（§m_quit ###：§ 前缀键 + ### 空值分隔符）→ 结构跳过
    （butterflies 实证 97 条：localization 键值模板的键且值缺失，模型
    回显恒败）。"""
    assert is_hard_structural("§m_quit ###")
    assert is_hard_structural("§nobg ###")
    assert is_hard_structural("§e1_dialogue_jae_m3_win_2_nat ###")
    assert is_hard_structural("§m_language_en ###")
    # 防误伤：§ 在句首的正常文本（带空格 + 语义）不跳过
    assert not is_hard_structural("§ 你好世界")


def test_lang_code_with_slash_is_structural():
    """语言代码目录标记（EN/ / DE/）→ 双语 TextAsset 语种分隔行，
    结构跳过（butterflies 实证：'EN/' 回显被判失败）。"""
    assert is_hard_structural("EN/")
    assert is_hard_structural("de/")


def test_single_char_keymap_lines_is_structural():
    """多行键位映射（k\nm\n/\nh：键盘快捷键组合提示，每行恰好 1 个
    字符）→ 无译义内容，结构跳过（butterflies 实证 4 条）。"""
    assert is_hard_structural("k\nm\n/\nh")
    assert is_hard_structural("A\nB")
    # 防误伤：任一行是多字符的普通文本不跳过
    assert not is_hard_structural("k\nm\n/\nhello world")


def test_xxxx_placeholder_name_is_structural():
    """XXXX 占位名（XXXX t'a：未命名角色/玩家的占位名，XXXX 是标准名字
    占位符）→ 保留原文合理（butterflies 实证：模型回显被判失败）。"""
    assert is_hard_structural("XXXX t'a")
    assert is_hard_structural("XXXX")


def test_credit_aligned_two_column_skip():
    """credit 名单对齐行（kangaroovindaloo    qubodup / pcaeldries
    RICHERlandTV：制作人名单两列对齐）→ 无译义署名，跳过
    （butterflies 实证 8 条）。"""
    assert is_credit_like("kangaroovindaloo    qubodup")
    assert is_credit_like("pcaeldries          RICHERlandTV")
    # 防误伤：含句子虚词的双空格行是正常句子
    assert not is_credit_like("the level  list is here")


def test_ft_music_credit_skip():
    """音乐合作名单（Highraiser ft. inkoutlines, MC Cruel Addict：ft. =
    featuring 合作标签）→ 署名行，跳过（butterflies 实证）。"""
    assert is_credit_like("Highraiser ft. inkoutlines, MC Cruel Addict")
    # 防误伤：含句子虚词的 ft. 行是正常句子
    assert not is_credit_like("the ft. files are in the folder")


def test_json_array_residue_lines_are_structural():
    """JSON 数组残留行（"chara_guard", / null, / true,：kv 语言文件
    逐行提取的 JSON 结构数据，值/键无译义）→ 结构跳过（containment
    ES/sceneStrings.subs 实证 42 条：模型音译 'chara Guardian' / 大写
    'NULL' 恒败）。引号内带空格的对话文本（"Chara, Guard",）不受影响。"""
    assert is_hard_structural('"chara_guard",')
    assert is_hard_structural('"deleted",')
    assert is_hard_structural("null,")
    assert is_hard_structural("true,")
    assert is_hard_structural("none,")
    assert not is_hard_structural('"Chara, Guard",')
    assert not is_hard_structural("null and void")


def test_asterisk_caps_label_is_structural():
    """星号包裹的全大写标注（*SIGH* / *SIGH* Now...：音效/情绪标注，
    SFX 字幕键位）→ 模型稳定回显小写变体（* sigh *），翻译无意义且
    小写残留判失败恒败（containment SCP-035 实证 6 条）。星号强调的
    真实词（*Attention* 驼峰/TitleCase 形态）仍需翻译。"""
    assert is_hard_structural("*SIGH*")
    assert is_hard_structural("*SIGH* Now...")
    assert is_hard_structural("*SIGH*  Now stop this")
    assert not is_hard_structural("*Attention*")
    assert not is_hard_structural("*Sigh*")


def test_slash_name_list_is_credit():
    """斜杠分隔的作者/团队名单（Turtle Sandwich/Catnipbuddy：制作组
    名单，无句子虚词）→ 署名跳过（containment credits 实证：模型回显
    被判 glossary_mismatch）。要求任一侧 ≥2 词：UI 双选项（Click/Tap、
    Load/Save、Audio/Video）是单侧单词，仍可翻译；含虚词的斜杠句子
    不受影响。"""
    assert is_credit_like("Turtle Sandwich/Catnipbuddy")
    assert is_credit_like("Turtle Sandwich / Catnipbuddy")
    assert is_credit_like("Turtle/Catnipbuddy Games")
    assert not is_credit_like("Click/Tap")
    assert not is_credit_like("Load/Save")
    assert not is_credit_like("Get the Key / Find the Door")


def test_lang_credit_line_is_credit():
    """本地化署名行（Russian   -   Nattakara：语言名 + 连字符 + 译者
    名）→ 署名跳过（containment ReadMe 实证：语言名引导模型把译者名
    音译成该语言字母（Nattakara → Наттакара）→ 目标脚本错误恒败）。"""
    assert is_credit_like("Russian   -   Nattakara")
    assert is_credit_like("English - John Doe")
    assert is_credit_like("Chinese - Zhang San")
    assert not is_credit_like("Hello - how are you")


def test_md_bold_lead_lines_are_structural():
    """markdown 加粗段落行（\t**All languages are loaded...：README/
    Changelog 文档说明行，行首 [ \t]** 且行内无闭合星号）→ 结构跳过
    （containment 实证：** 段内词模型稳定保留/半翻 → target_script_
    mismatch 恒败 4 条）。含闭合 **（**Bold** text 对话强调）不匹配。"""
    assert is_hard_structural('\t**All languages are loaded from the "languages.langs" json file on the')
    assert is_hard_structural('\t**\tLocalization system - How it works')
    assert is_hard_structural('\t**language folder to get you started.')
    assert is_hard_structural('**Read the manual first')
    assert not is_hard_structural('**Bold text** here')
    assert not is_hard_structural('**WARNING**')


def test_person_with_nickname_is_credit():
    """人名+引号昵称署名（Sam Lynch ("InnocentSam")：制作人员名单的
    作者名+昵称）→ 署名跳过（containment sharedassets7 TextAsset 实证
    2 条被判 glossary_mismatch 恒败）。昵称内容可含虚词（Tom ('The
    Cat')）——剥掉昵称后主体是纯名字才判。"""
    assert is_credit_like('Sam Lynch ("InnocentSam")')
    assert is_credit_like("Tom ('The Cat')")
    assert is_credit_like('Sally Mae ("Sally Mae")')
    assert not is_credit_like('He said ("What?") wait for me')
    assert not is_credit_like('Press "Start" now')


def test_clone_numbered_is_structural():
    """资源副本实例名（CreditsVolume (1) Profile：Unity 场景对象命名
    惯例「名 (编号) 名」，全词 TitleCase）→ 结构跳过（containment 实证：
    模型输出解释式垃圾 '参考以下翻译：…'）。含小写词的交互提示
    （Press (1) to start）不匹配。"""
    assert is_hard_structural('CreditsVolume (1) Profile')
    assert is_hard_structural('MainMenu (2)')
    assert not is_hard_structural('Press (1) to start')
    assert not is_hard_structural('Wait (1) minute')


def test_yarnspinner_line_hash_and_log_forms_are_structural():
    """YarnSpinner 字符串表键（line:hash）与插件内部串（C# 插值模板/
    调试输出/编辑器节点边标签）→ 结构跳过（count-my-coins 实证：obj=1354
    内 214 个 line:hash 键 + 对话文本同对象——键是内部引用非玩家文本，
    模型回显恒败 untranslated/target_script 双形态）。"""
    assert is_hard_structural('line:1aa64740')
    assert is_hard_structural('line:edd9d0b1')
    assert is_hard_structural('line:FFFFFFFF01')
    assert is_hard_structural(
        "Can't save variables to JSON: {nameof(variableStorage)} is not set")
    assert is_hard_structural('(Debug): 1000')
    assert is_hard_structural('ACTION edge')
    assert is_hard_structural('WAIT edge')
    # 反例：正常对话/长句不误伤
    assert not is_hard_structural('line up and wait for the signal')
    assert not is_hard_structural(
        'Can you save the variables to a json file?')
    assert not is_hard_structural('(Debugging): what happened here?')


def test_hipster_ipsum_is_structural():
    """hipster ipsum 占位文本（'XOXO keytar glossier mumblecore. Tote bag
    listicle normcore kinfolk kogi hoodie...'：containment level3-6 assets
    实证 6 条）→ 结构跳过。模型对占位文本行为随机：回显走豁免路径、
    翻译成中文（'XOXO：Keytar风格…'）→ 行数/内容比对恒败。跳过是唯一
    稳定出口。≥4 特征词才判（真实文本不会堆 4 个 hipster 词）。"""
    assert is_hard_structural(
        'XOXO keytar glossier mumblecore. Tote bag listicle normcore '
        'kinfolk kogi hoodie four dollar toast meh. VHS fixie bespoke '
        'cold-pressed pop-up blue bottle.')
    assert is_hard_structural(
        'Wayfarers taxidermy pinterest mlkshk. Vaporware shoreditch '
        'cardigan umami. Kinfolk hashtag aesthetic kogi hoodie.')
    assert not is_hard_structural(
        'She wore a hoodie and a cardigan to the meeting')
    assert not is_hard_structural(
        'The keytar was on sale for ten dollars')


def test_input_device_regex_is_structural():
    """InControl/Rewired 输入插件设备匹配正则（crash-back-in-time
    sharedassets0.assets 实证 40 条）→ 结构跳过。运行时按正则匹配手柄，
    翻译破坏输入映射；真实显示文本不以 '.*' 或 '^'+元字符开头。"""
    assert is_hard_structural(r'.*x[\-]*box[ ]*360.*')
    assert is_hard_structural(r'.*MadCatz Call of Duty GamePad.*')
    assert is_hard_structural(r'.*xbox[ ]*one.*')
    assert is_hard_structural(r'^([xX]iaoji )?Gamesir-G3[svw]?($| [0-9]+.*)')
    assert is_hard_structural(r'^([xX]iaoji )?Gamesir-G4[svw]?($| [0-9]+.*)')
    # 反例：正常句子（^ 开头 + 括号不是正则）
    assert not is_hard_structural('^Everyone should (press) start')
    assert not is_hard_structural('Press any button to continue')


def test_input_device_names_are_structural():
    """InControl 设备名/设备说明（crash-back-in-time 实证 38 条）→
    结构跳过：品牌词+设备语境、括号型号标识、冒号品牌 ID、纯品牌专名。
    模型对设备专名回显/音译不稳定，翻译破坏按名匹配。"""
    assert is_hard_structural('idroid:con')
    assert is_hard_structural('ipega media gamepad controller')
    assert is_hard_structural('Joy-Con (R)')
    assert is_hard_structural('Joy-Con™ (R)')
    assert is_hard_structural('idroid Snakebyte')
    assert is_hard_structural('idroid:con Snakebyte (M1)')
    assert is_hard_structural('idroid Snakebyte (Mode 1)')
    assert is_hard_structural('ipega Wireless Gamepad Controller')
    assert is_hard_structural('ipega BLUETOOTH Classic GamePad')
    assert is_hard_structural(
        'Nvidia Shield Portable and Nvidia Shield Wireless '
        'Controller (2015 model)')
    assert is_hard_structural(
        'Full-sized ipega gamepad. Must be in Gamepad mode '
        '(hold X + Home).')
    assert is_hard_structural(
        'Micro ipega controller. Must be in Gamepad mode (hold X + Home).')
    # 2026-08-31（ffs-legacy sharedassets0.assets 实证）：Rewired
    # HardwareJoystickMap 设备名——品牌词/设备专词 + 语境词/说明行。
    # 运行时按字符串匹配硬件名，翻译破坏输入匹配。
    assert is_hard_structural('Saitek Pro Flight Yoke')
    assert is_hard_structural('CH Eclipse Yoke')
    assert is_hard_structural('CH Pro Throttle')
    assert is_hard_structural('CH Throttle Quadrant')
    assert is_hard_structural('Mad Catz Micro C.T.R.L.R')
    assert is_hard_structural('BUFFALO BGC-FC801 USB Gamepad')
    assert is_hard_structural('SteelSeries Nimbus+ (OSX)')
    assert is_hard_structural('Stadia Controller rev. A')
    assert is_hard_structural('Insten PS2 adapter')
    assert is_hard_structural('Wireless Controller')
    assert is_hard_structural('Amazon Fire TV Remote')
    assert is_hard_structural('Apple Siri Remote')
    assert is_hard_structural('Atari Jaguar Controller')
    assert is_hard_structural('NES30 Joystick')
    assert is_hard_structural('8Bitdo NES30 Pro')
    assert is_hard_structural('8Bitdo SN30 Pro')
    assert is_hard_structural('Logitech Driving Force GT')
    assert is_hard_structural('Logitech G25')
    assert is_hard_structural('Logitech Extreme 3D Pro')
    assert is_hard_structural('Fanatec Porsche 911 Wheel')
    assert is_hard_structural('Horipad Ultimate')
    assert is_hard_structural('GameCube Controller')
    assert is_hard_structural('GameStick Controller')
    assert is_hard_structural('Nexus Player Gamepad')
    assert is_hard_structural('Nexus Player Remote')
    assert is_hard_structural('Elecom Gamepad')
    assert is_hard_structural('GGE909 Recoil')
    assert is_hard_structural('Unknown Controller')
    # 品牌词 + 设备语境词（ffs-legacy 实证，Rewired 内置设备库）
    assert is_hard_structural('SHIELD Remote')
    assert is_hard_structural('Stadia Controller rev. A')
    assert is_hard_structural('Elecom JC-U3312')
    # 反例：真实游戏文本/普通词不误伤
    assert not is_hard_structural('hihat cymbal')
    assert not is_hard_structural(
        'You collected an invitation to an Uka-Uka Trial. You can access '
        'these levels from the basement, by standing in the middle of '
        'the warp room.')
    assert not is_hard_structural('The controller is connected')
    assert not is_hard_structural('Press the gamepad button to start')
    assert not is_hard_structural('The remote is broken')
    assert not is_hard_structural('You found a wheel')
    assert not is_hard_structural('The car brake is broken')
    assert not is_hard_structural('shield of valor')


def test_hw_element_labels_are_structural():
    """Rewired 映射硬件元素标签（ffs-legacy 实证 2026-08-31）→ 结构跳过。
    轴/按钮/扳机/方向盘标签（'Left Stick X'/'Throttle 1 Up'/'Axis 0'/
    'Gas Pedal'）是运行时按名查找的映射键，翻译破坏输入匹配。孤立
    'Brake'/'Menu'/'Select' 命中（硬件映射对象内）；真实句（'apply the
    brake'）不命中（全串 ^…$ 匹配）。"""
    assert is_hard_structural('Left Stick X')
    assert is_hard_structural('D-Pad Left')
    assert is_hard_structural('Axis 0')
    assert is_hard_structural('Throttle 1 Up')
    assert is_hard_structural('Gas Pedal')
    assert is_hard_structural('Brake')
    assert is_hard_structural('Accelerator')
    assert is_hard_structural('Lever 1 Down')
    assert is_hard_structural('Hat 3 Up')
    assert is_hard_structural('Stick X')
    assert is_hard_structural('Right Stick Button')
    assert is_hard_structural('Action Bottom Row 1')
    assert is_hard_structural('Base Button 7')
    assert is_hard_structural('Blue (X)')
    assert is_hard_structural('Rotate Yoke')
    assert is_hard_structural('Wheel Right')
    assert is_hard_structural('Touchpad Click')
    assert is_hard_structural('Grip Button')
    assert is_hard_structural('Joypad Up')
    assert is_hard_structural('Back Tilt')
    assert is_hard_structural('Analog Stick')
    assert is_hard_structural('Menu Button')
    assert is_hard_structural('Select Button')
    assert is_hard_structural('Start Button')
    assert is_hard_structural('Home Button')
    assert is_hard_structural('Guide Button')
    assert is_hard_structural('Throttle Base Button 1')
    assert is_hard_structural('Grip Hat Down')
    assert is_hard_structural('POV Hat Down-Left')
    assert is_hard_structural('Shifter Down')
    assert is_hard_structural('Hat 1 Up-Right')
    # 反例：真实游戏文本/动词短语不误伤
    assert not is_hard_structural('Press up to jump')
    assert not is_hard_structural('Go left')
    assert not is_hard_structural('apply the brake')
    assert not is_hard_structural('the accelerator')
    assert not is_hard_structural('Turn the wheel left')
    assert not is_hard_structural('Left stick to move')
    assert not is_hard_structural('Brake the door')
    assert not is_hard_structural('A button on the wall')
    assert not is_hard_structural('Touch to begin')
    assert not is_hard_structural('The guide said')
    assert not is_hard_structural('Menu is open')
    # 'Menu'/'Start' 是 DISPLAY_WORDS 白名单显示词（F41：显式显示词证据
    # 优先于结构形态猜测）——裸词返回非结构（该翻）；Rewired 对象内的
    # 裸 Menu/Start 由 class_registry disposition 对象级整体跳过。
    assert not is_hard_structural('Menu')
    assert not is_hard_structural('Start')
    # 全小写裸硬件词（'brake'/'shield'/'wheel'）是普通英语词，不是
    # Rewired 映射标签（标签为 TitleCase）——游戏文本里常见
    assert not is_hard_structural('brake')
    assert not is_hard_structural('shield')
    assert not is_hard_structural('wheel')
    assert not is_hard_structural('Start the game')


def test_input_api_names_are_structural():
    """输入系统 API/组件名孤立词（ffs-full-game-demo 实证 'xinput'：
    设备枚举字符串，无品牌词/语境词不命中设备名分支）→ 结构跳过。
    只收明确 API 形态词（'hid' 是真实英语词 hide 过去式不收）。"""
    assert is_hard_structural('xinput')
    assert is_hard_structural('XInput')
    assert is_hard_structural('dinput8')
    assert is_hard_structural('rawinput')
    assert is_hard_structural('xinput1_4')
    # 反例：真实英语词/游戏文本不误伤
    assert not is_hard_structural('hid')
    assert not is_hard_structural('HID')
    assert not is_hard_structural('Press the input button')


def test_regex_pattern_is_structural():
    """正则表达式串（ffs-full-game-demo 实证 '[dD]+ual[ ]*[sS]+ense'：
    DualSense 手柄匹配模式）→ 结构跳过。字符类+量词是正则形态特征，
    翻译破坏运行时匹配逻辑。不误伤 markdown 链接/按键提示（无量词）。"""
    assert is_hard_structural(r'[dD]+ual[ ]*[sS]+ense')
    assert is_hard_structural(r'[a-z]+[0-9]*')
    assert is_hard_structural(r'[A-Za-z0-9_-]+\.exe')
    assert is_hard_structural(r'\d{4}-\d{2}-\d{2}')
    # 反例：markdown 链接（后跟 '(' 非量词）、按键提示 [A]（无量词）、
    # 正常文本
    assert not is_hard_structural('[text](https://example.com)')
    assert not is_hard_structural('Press [A] to jump')
    assert not is_hard_structural('Select the option')


def test_version_template_and_guid_log_are_structural():
    """版本占位模板（v?.??：crash-back-in-time level0 实证）与 C# 日志
    拼接模板尾部（Rewired 'CustomController device instance GUID:
    sourceId = '）→ 结构跳过。"""
    assert is_hard_structural('v?.??')
    assert is_hard_structural(
        'CustomController device instance GUID: sourceId = ')
    # 反例：完整版本号 v2.5 走 _QUALIFIED 标识符家族跳过（版本号保留
    # 是惯例），带空格的普通短语不误伤
    assert is_hard_structural('v2.5')
    assert not is_hard_structural('version 1.2')


def test_whitespace_padded_fragment_is_structural():
    """首尾空白片段串（' to JSON. '：字符串表拆分的无完整语义碎片，
    crash-back-in-time 实证——译文更长时写回容量截断 → object 闸门
    WARN）→ 结构跳过。含 CJK 的 padding 串（' 继续 '）不误伤。"""
    assert is_hard_structural(' to JSON. ')
    assert is_hard_structural(' Can\'t save variables ')
    assert not is_hard_structural(' 继续 ')
    assert not is_hard_structural('Press Start to begin')


def test_log_template_tail_is_structural():
    """C# 日志拼接模板句（crusty-proto Eflatun.SceneReference.dll
    'The address is not found in the Scene GUID to Address Map. Address: '
    实证：'Address: ' 是代码续行拼接点，日志玩家不可见 → 结构跳过。
    要求 ≥20 字符防 'Press: ' 短 UI 提示误伤；正常玩家句以标点结尾。"""
    assert is_hard_structural(
        'The address is not found in the Scene GUID to Address Map. '
        'Address: ')
    assert is_hard_structural(
        'CustomController device instance GUID: sourceId = ')
    assert not is_hard_structural('Press: ')
    assert not is_hard_structural(
        'The address was not found. Please try again.')
    assert not is_hard_structural('Address: 123 Main Street, New York')


def test_input_binding_path_is_structural():
    """Rewired 输入动作绑定路径（deadbeat 实证：'Game/Jump[/Keyboard/x,
    /Keyboard/upArrow]' 是 MonoBehaviour 内 ActionName/Binding 序列化，
    运行时按字符串解析输入映射，翻译破坏绑定）→ 结构跳过。
    真实显示文本的 [ 后是内容不是设备路径，不命中。"""
    assert is_hard_structural(
        'Win Menu/Up[/Keyboard/upArrow,/Keyboard/leftArrow]')
    assert is_hard_structural(
        'Win Menu/Down[/Keyboard/downArrow,/Keyboard/rightArrow]')
    assert is_hard_structural('Win Menu/Select[/Keyboard/z]')
    assert is_hard_structural(
        'Game/Jump[/Keyboard/x,/Keyboard/upArrow]')
    assert not is_hard_structural('Press [E] to interact')
    assert not is_hard_structural('Click [File/Open] to load')
    assert not is_hard_structural('Type /help [command] to continue')


def test_uppercase_entropy_is_structural():
    """全大写编码/加密串（deadbeat 实证：NIIVMMSEGAROTME… 2567 字符
    无空格全大写，对象内嵌编码数据，翻译请求超模型槽位恒败）→ 结构
    跳过。真实全大写英文句有空格、词/缩写 <32 字符不命中。"""
    assert is_hard_structural(
        'NIIVMMSEGAROTMEJQEVBPPIJFCZLZXOVMFWESPDCVLYEOTPTRXIZJQGGVSQIT'
        'TWKIJTWNRKINZZHRZGPYIMCFYWIMMLVSJCGHRVS')
    assert not is_hard_structural('WELCOME HOME')
    assert not is_hard_structural('FATAL ESCAPE')
    assert not is_hard_structural('XBOX')
    assert not is_hard_structural('DEADBEATSENCORE')


def test_fragment_noise_is_structural():
    """无完整词字母碎片（deadbeat 实证：' e   i t'、'r wr TE' 对象内嵌
    噪声，所有词 ≤2 字符、≥3 段、含首空白或全大写词段）→ 结构跳过。
    'Hi hi hi' 类 TitleCase 语气词无全大写段不命中，走正常翻译。"""
    assert is_hard_structural(' e   i t')
    assert is_hard_structural('r wr TE')
    assert not is_hard_structural('Hi hi hi')
    assert not is_hard_structural('OK go')
    assert not is_hard_structural('a b c')


def test_engine_ctrl_code_is_structural():
    """引擎富文本控制码串（faerie-afterlight 实证：'.^.b'×178、'^tr'、
    '^denvis'——'^' 前缀字母段是引擎样式/命令标记，剥除后无可译英文
    词）→ 结构跳过。要求剥除后无 ≥3 字母连续段，防误伤含 ^ 的真实
    文本（'x^2 + y^2' 剥后 x/y 单字母）。"""
    assert is_hard_structural('.^.b')
    assert is_hard_structural('^tr')
    assert is_hard_structural('^cb')
    assert is_hard_structural('^denvis')
    assert is_hard_structural('^rt')
    assert is_hard_structural('^as')
    # 反例：含 ^ 的真实文本（数学表达式剥后单字母、正常句子无控制码）
    assert not is_hard_structural('x^2 + y^2')
    assert not is_hard_structural('Press {0} to open Map of this area')
    assert not is_hard_structural('Solium dual')
