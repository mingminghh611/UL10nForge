"""键名保护回归测试：Localization 表键/字典键/标识符绝不翻译，写回也绝不覆盖。

背景：Unity Localization（UITable 报错 no translation found for xx in UITable）
与 I2/字典结构的表键被误译后，游戏按键名查找必然失败。三层防线：
1) 提取期：键字段/键风格标识符 → skipped
2) 对象级：SharedTableData 键列表对象 → 全部标识符降级
3) 写回期：历史误译的键条目也不写回
"""
from __future__ import annotations

import json

from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.placeholders import (is_key_style_identifier, looks_like_key_field,
                                      should_skip)


# ── is_key_style_identifier ──
def test_key_style_detection():
    assert is_key_style_identifier("ui_newGame")
    assert is_key_style_identifier("MENU_PLAY")
    assert is_key_style_identifier("phone_call_01")
    assert is_key_style_identifier("UITable_en")
    assert is_key_style_identifier("lockedEntrance")
    assert is_key_style_identifier("en")
    assert is_key_style_identifier("ru")


def test_word_style_is_not_key():
    # 单词式写法 = 任意语言的 UI 标签（显示值）
    assert not is_key_style_identifier("CREDITOS")
    assert not is_key_style_identifier("SENSIBILIDAD")
    assert not is_key_style_identifier("Settings")
    assert not is_key_style_identifier("V-SYNC")
    assert not is_key_style_identifier("NewGame")
    assert not is_key_style_identifier("Hello")
    assert not is_key_style_identifier("Yes")
    # 显示单词白名单（小写）
    assert not is_key_style_identifier("start")
    assert not is_key_style_identifier("menu")
    assert not is_key_style_identifier("save")


def test_lowercase_word_not_key_style():
    """纯小写纯字母单词 → 显示文本（待办 A 治理，222am 实证：shower/
    city/bedroom/eggs/ladder/mug 等常见场景词不在 165 词白名单 → 曾判
    键跳过）。键名几乎总带分隔符或混合大小写，无分隔符纯小写是自然
    语言单词形态。"""
    assert not is_key_style_identifier("shower")
    assert not is_key_style_identifier("city")
    assert not is_key_style_identifier("bedroom")
    assert not is_key_style_identifier("eggs")
    assert not is_key_style_identifier("ladder")
    assert not is_key_style_identifier("mug")
    # 对照：带分隔符/混合大小写/数字的键不受影响
    assert is_key_style_identifier("phone_call_01")
    assert is_key_style_identifier("m_address")
    assert is_key_style_identifier("lockedEntrance")
    assert is_key_style_identifier("v1_0")


def test_key_style_not_plain_text():
    assert not is_key_style_identifier("Hello player")      # 有空格
    assert not is_key_style_identifier("BOSS: Took you long")  # 标点
    assert not is_key_style_identifier("你好")                # 非 ASCII
    assert not is_key_style_identifier("[LMB] Next")


def test_sentence_word_with_period_not_key_style():
    """B26（dead-catch 实证）：单词 + 句尾点号是自然语言短句（对话行
    'Listen.'/'Alright.'/'Good.'），不是键名——_IDENTIFIER 把句尾点当
    标识符字符导致误杀，进入 pending 后被 _should_downgrade_pending
    二次降级 skipped。键名不会以句号结尾。"""
    assert not is_key_style_identifier("Listen.")
    assert not is_key_style_identifier("Alright.")
    assert not is_key_style_identifier("Good.")
    assert not is_key_style_identifier("Okay.")
    assert not is_key_style_identifier("Sure.")
    assert not is_key_style_identifier("listen.")   # 小写同理
    assert not should_skip("Listen.")
    assert not should_skip("Alright.")
    # 对照 1：多段限定名（中间带点）不匹配句子形态，仍是键
    assert is_key_style_identifier("Assets.Scripts.Foo")
    assert is_key_style_identifier("MainMenu.SubTitle")
    # 对照 2：两字母缩写点（'e.g.'/'i.e.'）不是句子词形态（中间也带点），
    # 仍按键处理
    assert is_key_style_identifier("e.g.")
    assert is_key_style_identifier("i.e.")


def test_should_skip_uses_key_style():
    assert should_skip("ui_newGame")
    assert should_skip("MENU_PLAY")
    assert should_skip("en")
    assert not should_skip("CREDITOS")
    assert not should_skip("NEW GAME")
    assert not should_skip("Hello player")


# ── JSON 键字段 ──
def test_looks_like_key_field():
    assert looks_like_key_field("Key")
    assert looks_like_key_field("id")
    assert looks_like_key_field("m_Key")
    assert looks_like_key_field("key_id")
    assert looks_like_key_field("locale")
    assert looks_like_key_field("language")
    assert not looks_like_key_field("title")
    assert not looks_like_key_field("text")
    assert not looks_like_key_field("name")
    assert not looks_like_key_field("m_LocalizedString")


def test_json_extract_skips_key_fields():
    from hanhua.core.formats import json_format
    text = ('{"m_TableData": [{"Key": "ui_newGame", "m_LocalizedString": "New Game"},'
            ' {"Key": "Settings", "m_LocalizedString": "Settings"}]}')
    entries = json_format.extract_json_text(text, "t")
    by_path = {e.key_path: e for e in entries}
    assert by_path["m_TableData/0/Key"].status == STATUS_SKIPPED
    assert by_path["m_TableData/1/Key"].status == STATUS_SKIPPED   # 白名单单词在键字段也是键
    assert by_path["m_TableData/0/m_LocalizedString"].status == "pending"
    assert by_path["m_TableData/1/m_LocalizedString"].status == "pending"


def test_json_write_back_never_touches_keys():
    from hanhua.core.formats import json_format
    text = '{"Key": "ui_newGame", "m_LocalizedString": "New Game"}'
    legacy_key = [TextEntry(file_id="t", key_path="Key", original="ui_newGame",
                            translation="新游戏", status="translated")]
    out = json_format.apply_json(legacy_key, text)
    assert "新游戏" not in out and '"ui_newGame"' in out
    value = [TextEntry(file_id="t", key_path="m_LocalizedString", original="New Game",
                       translation="开始游戏", status="translated")]
    out2 = json_format.apply_json(value, text)
    assert "开始游戏" in out2


# ── v2 写回保护（历史误译键条目） ──
def test_v2_write_guard_blocks_legacy_keys():
    from hanhua.core.unity.writer import _should_write_entry
    key = {"original": "ui_newGame", "translation": "新游戏", "meta": "{}"}
    assert not _should_write_entry(key)
    word_key_in_list_obj = {"original": "Settings", "translation": "设置",
                            "meta": '{"obj_is_key_list": true}'}
    assert not _should_write_entry(word_key_in_list_obj)
    # 无值特征对象里的单词式（Input 绑定名/枚举名）→ 历史误译也不写回
    code_word = {"original": "WASD", "translation": "移动", "meta": '{"obj_has_values": false}'}
    assert not _should_write_entry(code_word)
    code_word2 = {"original": "Bold", "translation": "粗体", "meta": "{}"}
    assert not _should_write_entry(code_word2)
    # DLL/IL2CPP 代码池：无空格标识符一律是键
    dll = {"original": "Reset", "translation": "重置", "meta": '{"kind": "us"}'}
    assert not _should_write_entry(dll)
    # 值特征对象里的单词式值 → 正常写回
    value = {"original": "CREDITOS", "translation": "制作名单",
             "meta": '{"obj_has_values": true}'}
    assert _should_write_entry(value)
    dialogue = {"original": "BOSS: Took you long enough.", "translation": "老板：让你久等了。",
                "meta": "{}"}
    assert _should_write_entry(dialogue)


# ── v1 写回保护（_render 丢弃键译文但保留行） ──
def test_v1_render_downgrades_keys_keeps_lines():
    from hanhua.core import writer as text_writer
    import json as _json
    from hanhua.core.memory import ProjectStore
    from hanhua.core.models import TextEntry
    from pathlib import Path
    import tempfile

    d = Path(tempfile.mkdtemp())
    (d / "strings.txt").write_text("a=hello\nkey=ui_newGame\n", encoding="utf-8")
    from hanhua.core.extractor import parse_file
    pf = parse_file(d / "strings.txt")
    store = ProjectStore(Path(tempfile.mkdtemp()) / "p.db")
    store.init_schema()
    store.add_file(pf.file_id, "strings.txt", pf.format, pf.encoding, pf.eol, pf.meta)
    # 历史误译：ui_newGame 被翻译（键），hello 正常翻译（值）
    store.upsert_entries([
        {"file_id": e.file_id, "key_path": e.key_path, "original": e.original,
         "status": e.status, "meta": e.meta} for e in pf.entries])
    store.update_translation("strings.txt", "kv/a/0", "你好")
    store.update_translation("strings.txt", "kv/key/1", "新游戏")
    out_dir = d.parent / (d.name + "_汉化")
    text_writer.write_back(store, d, out_dir)
    out = (out_dir / "strings.txt").read_text(encoding="utf-8")
    assert "a=你好" in out              # 正常值写回
    assert "key=ui_newGame" in out      # 键条目保留原文
    assert "新游戏" not in out


def test_rescan_refreshes_translated_entry_metadata_without_losing_translation():
    """规则升级后重扫必须刷新定位元数据，否则 UI 值会被旧规则错误拦截。"""
    from hanhua.core.memory import ProjectStore
    from pathlib import Path
    import tempfile

    store = ProjectStore(Path(tempfile.mkdtemp()) / "project.db")
    store.init_schema()
    store.upsert_entries([{
        "file_id": "bundle", "key_path": "asset#1/str/1", "original": "SETTINGS",
        "status": "pending", "meta": {"kind": "rawstr", "obj": 1, "offset": 8},
    }])
    store.update_translation("bundle", "asset#1/str/1", "设置")

    store.upsert_entries([{
        "file_id": "bundle", "key_path": "asset#1/str/1", "original": "SETTINGS",
        "status": "pending",
        "meta": {"kind": "rawstr", "obj": 1, "offset": 8, "obj_has_values": True},
    }])

    entry = store.get_entries()[0]
    assert entry["translation"] == "设置"
    assert entry["status"] == "translated"
    meta = json.loads(entry["meta"])
    assert {key: meta[key] for key in (
        "kind", "obj", "offset", "obj_has_values",
    )} == {
        "kind": "rawstr", "obj": 1, "offset": 8, "obj_has_values": True,
    }
    assert meta["quality_passed"] is True
    assert meta["quality_reasons"] == []
    assert meta["quality_source"] == "manual_api"
    assert meta["confidence_promoted"] is True
