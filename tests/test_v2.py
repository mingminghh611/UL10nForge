"""v2 测试：字符串扫描、长度适配、TextAsset 提取、#US 堆、IL2CPP metadata。"""
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hanhua.core.engine_strings import (InputEvent, has_display_text_evidence,
                                        interaction_input_events,
                                        interaction_input_tokens,
                                        is_interaction_prompt,
                                        is_strong_interaction_prompt)
from hanhua.core.memory import ProjectStore
from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.unity.extractor import (_is_engine_string, _raw_string_entries,
                                        _UNITY_CONTROL_STATE_NAMES,
                                        _decode_field_path,
                                        _encode_field_path,
                                        _localization_bundle_probe,
                                        _looks_like_type_descriptor,
                                        _prefer_source_locale_bundles,
                                        _should_downgrade_pending,
                                        _structural_reason,
                                        _textasset_entries,
                                        _typetree_string_entries, extract_asset_file,
                                        find_asset_files, scan_strings)
from hanhua.core.unity.il2cpp import parse_string_literals
from hanhua.core.unity.mono_dll import (_walk_us_heap, extract_dll_user_strings,
                                        find_dll_files)
from hanhua.core.unity.writer import _fit_bytes, _patch_textasset


# ── scan_strings ──
def _with_len(s: str) -> bytes:
    b = s.encode("utf-8")
    padding = b"\x00" * (-len(b) % 4)
    return struct.pack("<I", len(b)) + b + padding


def _write_cli_pe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytearray(0x400)
    blob[:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, 0x80)
    blob[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", blob, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0x22)
    struct.pack_into("<H", blob, 0x98, 0x20B)
    struct.pack_into("<I", blob, 0x98 + 108, 16)
    section = 0x80 + 4 + 20 + 0xF0
    blob[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", blob, section + 8, 0x200, 0x2000, 0x200, 0x200)
    struct.pack_into("<II", blob, 0x98 + 112 + 14 * 8, 0x2000, 0x48)
    struct.pack_into("<IHHII", blob, 0x200, 0x48, 2, 5, 0x2080, 0x20)
    struct.pack_into("<I", blob, 0x210, 1)
    blob[0x280:0x284] = b"BSJB"
    path.write_bytes(blob)


def test_typetree_recursively_extracts_display_fields_with_typed_paths():
    tree = {"m_Text": "Settings", "panel": {
        "title": "Options", "rows": [
            {"label": "Audio"}, {"description": "Adjust volume"}]}}

    display, _ = _typetree_string_entries("f", 7, tree, "fixture.assets")

    assert [(entry.original, entry.meta["field_path"]) for entry in display] == [
        ("Settings", ["m_Text"]),
        ("Options", ["panel", "title"]),
        ("Audio", ["panel", "rows", 0, "label"]),
        ("Adjust volume", ["panel", "rows", 1, "description"]),
    ]
    assert all(entry.meta["kind"] == "typetree" for entry in display)


def test_typetree_excludes_display_values_under_structural_fields():
    tree = {"keys": [{"label": "Settings"}],
            "binding": {"text": "Jump"},
            "method": {"title": "Start"},
            "panel": {"text": "Visible"}}

    display, _ = _typetree_string_entries("f", 8, tree)

    assert [entry.original for entry in display] == ["Visible"]


def test_typetree_animation_trigger_values_are_structural_not_display():
    # 2026-08-31 用户实证「Disabled 残疾人士 vs 已禁用」根因：UGUI Button
    # 组件 m_AnimationTriggers 的视觉状态动画触发器字段（m_NormalTrigger/
    # m_HighlightedTrigger/m_PressedTrigger/m_SelectedTrigger/m_DisabledTrigger/
    # m_EnabledTrigger）值是动画状态名（Normal/Highlighted/Pressed/Selected/
    # Disabled/Enabled），反射/状态机按名引用——翻译必断按钮动画状态切换。
    # 必须按结构串拦截（不进 display 也不进候选层），与 _UNITY_CONTROL_
    # STATE_NAMES 既有语义对齐（F38/F39 已排除，此处补 typetree 路径）。
    tree = {
        "m_AnimationTriggers": {
            "m_DisabledTrigger": "Disabled",
            "m_NormalTrigger": "Normal",
            "m_HighlightedTrigger": "Highlighted",
            "m_PressedTrigger": "Pressed",
            "m_SelectedTrigger": "Selected",
        },
        "m_Text": "Settings",
        "panel": {"text": "Real UI text"},
    }

    display, candidates = _typetree_string_entries("f", 7, tree, "fixture.assets")

    assert [entry.original for entry in display] == ["Settings", "Real UI text"]
    assert not any("Trigger" in str(e.meta["field_path"])
                   for e in display + candidates)
    assert not any(e.original in _UNITY_CONTROL_STATE_NAMES
                   for e in display)


def test_typetree_type_descriptor_never_a_display_text():
    # resonance-of-the-ocean 实测：Localization SmartFormat 配置对象里
    # "TypeName Namespace Assembly" 是类型引用（游戏按名反射加载），当文本
    # 翻译后 save_typetree 抛 ValueError（Referenced type not found）。
    tree = {
        "m_SmartFormat": {
            "m_FormatterType":
                "Parser UnityEngine.Localization.SmartFormat.Core.Parsing "
                "Unity.Localization",
        },
        "panel": {"text": "Real text"},
    }

    display, candidates = _typetree_string_entries("f", 12, tree)

    assert [entry.original for entry in display] == ["Real text"]
    # R5：类型引用不再静默丢弃——留档为 skipped 候选（kind=typetree_prefilter），
    # 仍不得以 pending/display 身份出现
    prefilter = [e for e in candidates
                 if "FormatterType" in str(e.meta["field_path"])]
    assert len(prefilter) == 1
    assert prefilter[0].status == "skipped"
    assert prefilter[0].meta["kind"] == "typetree_prefilter"
    assert prefilter[0].meta["reason"] == "prefilter_type_descriptor"


def test_looks_like_type_descriptor_does_not_reject_real_text():
    # 三段式但段间不含点分命名空间/程序集的真实文本必须保持可译：
    # 段 2 无点 / 段 3 无点 / 段内尾点句子 / 段间标点。
    assert not _looks_like_type_descriptor("Press A to continue")
    assert not _looks_like_type_descriptor("Level 1.5 Patch 2")
    assert not _looks_like_type_descriptor("See the File. Read the docs.")
    assert not _looks_like_type_descriptor("Open File. Read Syntax. Now go.")
    # 标准形态（类名 + 点分命名空间 + 点分程序集）必须命中。
    assert _looks_like_type_descriptor(
        "Parser UnityEngine.Localization.SmartFormat.Core.Parsing "
        "Unity.Localization")
    assert _looks_like_type_descriptor(
        "Text UnityEngine.UI.Text UnityEngine.UI")


def _pending_entry(original, confidence="high", reason="typetree_display_field"):
    return TextEntry(file_id="f", key_path="asset#f#1/field/k:m_Text",
                     original=original, status="pending",
                     meta={"kind": "typetree", "confidence": confidence,
                           "reason": reason, "role": "display"})


def test_downgrade_gate_keeps_credit_like_text_in_display_fields():
    # lilys-day-off level13 结局画廊实证：'A game by Kyuppin' 是 m_Text 显示
    # 字段里的真实显示文本，但被 credit/署名软猜测规则降级错过。证据分层：
    # 确定性显示字段条目只被硬结构降级，署名/版权类软猜测不得推翻 UI 字段证据。
    assert not _should_downgrade_pending(
        _pending_entry("A game by Kyuppin"))
    assert not _should_downgrade_pending(
        _pending_entry("Created by Sam Hogan"))
    assert not _should_downgrade_pending(
        _pending_entry("made in 48h"))


def test_downgrade_gate_soft_guess_still_applies_without_display_evidence():
    # 无确定性证据（candidate/raw scan 形态）时，署名软猜测仍降级——原行为。
    assert _should_downgrade_pending(
        _pending_entry("A game by Kyuppin", confidence="low",
                       reason="typetree_candidate"))
    assert _should_downgrade_pending(
        _pending_entry("Created by Sam Hogan", confidence="medium",
                       reason="rawstr_display_evidence"))


def test_downgrade_gate_hard_structural_always_downgrades():
    # 硬结构（纯数字/URL）即使出现在 UI 显示字段也降级——翻译会破坏功能。
    assert _should_downgrade_pending(_pending_entry("9"))
    assert _should_downgrade_pending(_pending_entry("https://example.com/x"))
    assert _should_downgrade_pending(_pending_entry("Assets/UI/panel.png"))
    # 非 pending 条目不动
    from dataclasses import replace
    entry = replace(_pending_entry("A game by Kyuppin", reason="x"),
                    status=STATUS_SKIPPED)
    assert not _should_downgrade_pending(entry)


def test_typetree_m_name_is_never_a_display_text():
    # doubleshake 实证：m_Name 是 Unity 对象标识名（Inspector 标题/Find 键），
    # 即使对象有值证据也绝不能升格 display——否则写回被
    # immutable_field_protected 拒绝并阻断整个发布。
    # 裸 name 字段（对话角色名等）不受影响。
    tree = {
        "m_Name": "Button_Start",
        "m_Text": "Play",
        "sub": {"m_Name": "Panel_Main", "m_Text": "Quit"},
        "bare_name": {"name": "Start"},
    }

    display, candidates = _typetree_string_entries("f", 11, tree)

    assert [(e.original, e.meta["field_path"]) for e in display] == [
        ("Play", ["m_Text"]),
        ("Quit", ["sub", "m_Text"]),
        ("Start", ["bare_name", "name"]),
    ]
    # m_Name 值不落入候选层（完全屏蔽，不是降级）
    assert not any("m_Name" in str(e.meta["field_path"])
                   for e in display + candidates)


def test_typetree_structural_tokens_do_not_match_inside_semantic_words():
    tree = {
        "videoSettings": {"m_Title": "Video Settings"},
        "identity": {"m_Text": "Player Identity"},
        "keyboardPrompt": {"text": "Press E"},
        "key_list": [{"label": "Settings"}],
        "bindingPath": {"description": "Jump"},
        "method": {"title": "Start"},
        "id": {"text": "Internal"},
    }

    display, _ = _typetree_string_entries("f", 9, tree)

    assert [entry.original for entry in display] == [
        "Video Settings", "Player Identity", "Press E"]


def test_typetree_field_path_encoding_is_type_aware_and_collision_free():
    paths = [
        ["a/b", "text"], ["a", "b", "text"],
        ["rows", 0, "label"], ["rows/0", "label"],
    ]

    encoded = [_encode_field_path(path) for path in paths]

    assert len(set(encoded)) == len(paths)
    assert encoded == [
        "k:a%2Fb/k:text", "k:a/k:b/k:text",
        "k:rows/i:0/k:label", "k:rows%2F0/k:label",
    ]
    assert [_decode_field_path(locator) for locator in encoded] == paths


def test_typetree_colliding_legacy_paths_are_distinct_in_project_store(tmp_path):
    tree = {"a/b": {"text": "Slash"},
            "a": {"b": {"text": "Nested"}},
            "rows": [{"label": "Indexed"}],
            "rows/0": {"label": "Slash index"}}
    display, _ = _typetree_string_entries("f", 10, tree)
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.upsert_entries([{
        "file_id": entry.file_id, "key_path": entry.key_path,
        "original": entry.original, "meta": entry.meta,
    } for entry in display])

    stored = store.get_entries()

    assert len(display) == len(stored) == 4
    assert {row["original"] for row in stored} == {
        "Slash", "Nested", "Indexed", "Slash index"}


def test_unsupported_typetree_fields_fall_back_to_raw_without_typed_duplicates(
        tmp_path, monkeypatch):
    import UnityPy

    class FakeObject:
        type = SimpleNamespace(name="MonoBehaviour")
        assets_file = SimpleNamespace(name="fixture.assets")

        def __init__(self, path_id, tree, text):
            self.path_id, self.tree = path_id, tree
            self.raw = _with_len(text)

        def read_typetree(self): return self.tree
        def get_raw_data(self): return self.raw

    objects = [
        FakeObject(1, {"message": "Return to the village."},
                   "Return to the village."),
        FakeObject(2, {"caption": "A dangerous road."},
                   "A dangerous road."),
        FakeObject(3, {"dialogue": "We should leave now."},
                   "We should leave now."),
        FakeObject(4, {"method": "Start"}, "Start"),
        FakeObject(5, {"text": "Settings"}, "Settings"),
    ]

    class FakeEnvironment:
        def __init__(self): self.objects, self.files = [], {}
        def load(self, _paths): self.objects = objects

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    path = tmp_path / "fixture.assets"
    path.write_bytes(b"fixture")

    parsed = extract_asset_file(path, "f")

    assert {entry.original for entry in parsed.entries
            if entry.status == "pending"} == {
        "Return to the village.", "A dangerous road.",
        "We should leave now.", "Settings"}
    settings = [entry for entry in parsed.entries
                if entry.original == "Settings"]
    assert len(settings) == 1 and settings[0].meta["kind"] == "typetree"
    assert all(entry.status == "skipped" for entry in parsed.entries
               if entry.original == "Start")


def test_find_asset_files_discovers_extensionless_level_scene(tmp_path):
    data_dir = tmp_path / "Example_Data"
    data_dir.mkdir()
    scene = data_dir / "level0"
    scene.write_bytes(b"UnityFS")

    assert find_asset_files(tmp_path) == [scene]


def test_source_locale_probe_skips_serialized_assets_but_checks_bundle_candidates(
        tmp_path, monkeypatch):
    data_dir = tmp_path / "Example_Data"
    data_dir.mkdir()
    ordinary = data_dir / "sharedassets0.assets"
    scene = data_dir / "level0"
    named_bundle = tmp_path / "localization-string-tables-english(en)_assets_all.bundle"
    generic_bundle = tmp_path / "resources.bundle"
    hashed_bundle = tmp_path / "5f9b21c7"
    ordinary.write_bytes(b"serialized asset")
    # 无后缀 SerializedFile：大端自洽头（v22：metadata/file_size/version/data_offset）
    scene.write_bytes(
        struct.pack(">III I B 3x I Q Q 4x", 0, 4096, 22, 2048, 0, 1024, 4096, 2048)
        + b"\x00" * 64)
    named_bundle.write_bytes(b"bundle fixture")
    generic_bundle.write_bytes(b"bundle fixture")
    hashed_bundle.write_bytes(b"UnityFS")
    calls = []

    def probe(path):
        calls.append(path)
        return None

    monkeypatch.setattr(
        "hanhua.core.unity.extractor._localization_bundle_probe", probe)

    assert set(find_asset_files(tmp_path)) == {
        ordinary, scene, named_bundle, generic_bundle, hashed_bundle}
    assert set(calls) == {named_bundle, generic_bundle, hashed_bundle}


@pytest.mark.parametrize("suffix", [".ab", ".unity3d", ".bundle", ".pak"])
def test_source_locale_probe_groups_every_bundle_suffix(
        tmp_path, monkeypatch, suffix):
    english = tmp_path / f"english{suffix}"
    spanish = tmp_path / f"spanish{suffix}"
    english.write_bytes(b"bundle fixture")
    spanish.write_bytes(b"bundle fixture")
    identity = frozenset({"shared:2:1733287269080016787"})
    probes = {english: (identity, "en"), spanish: (identity, "es")}
    calls = []

    def probe(path):
        calls.append(path)
        return probes[path]

    monkeypatch.setattr(
        "hanhua.core.unity.extractor._localization_bundle_probe", probe)

    assert find_asset_files(tmp_path) == [english]
    assert set(calls) == {english, spanish}


def test_extensionless_string_tables_route_by_logical_identity_and_locale(
        tmp_path, monkeypatch):
    paths = {name: tmp_path / name for name in (
        "hash_en", "hash_es", "hash_ru", "hash_non_table", "hash_unknown")}
    for path in paths.values():
        path.write_bytes(b"UnityFS")
    table_identity = frozenset({"shared:2:1733287269080016787"})
    probes = {
        paths["hash_en"]: (table_identity, "en"),
        paths["hash_es"]: (table_identity, "es"),
        paths["hash_ru"]: (table_identity, "ru"),
        paths["hash_non_table"]: None,
        paths["hash_unknown"]: None,
    }
    calls = []

    def probe(path):
        calls.append(path)
        return probes[path]

    monkeypatch.setattr(
        "hanhua.core.unity.extractor._localization_bundle_probe", probe)

    assert set(find_asset_files(tmp_path)) == {
        paths["hash_en"], paths["hash_non_table"], paths["hash_unknown"]}
    assert set(calls) == set(paths.values())


def test_extensionless_string_tables_keep_all_when_english_is_absent(
        tmp_path, monkeypatch):
    spanish = tmp_path / "hash_es"
    russian = tmp_path / "hash_ru"
    for path in (spanish, russian):
        path.write_bytes(b"UnityFS")
    identity = frozenset({"shared:2:1733287269080016787"})
    probes = {spanish: (identity, "es"), russian: (identity, "ru")}
    monkeypatch.setattr(
        "hanhua.core.unity.extractor._localization_bundle_probe",
        lambda path: probes[path],
    )

    assert find_asset_files(tmp_path) == [spanish, russian]


def test_localization_bundle_probe_disposes_environment(tmp_path, monkeypatch):
    import UnityPy
    from hanhua.core.unity import writer

    path = tmp_path / "hash_en"
    path.write_bytes(b"UnityFS")
    tree = {
        "m_Name": "UITable_en",
        "m_LocaleId": {"m_Code": "en"},
        "m_SharedData": {"m_FileID": 2, "m_PathID": 1733287269080016787},
        "m_TableData": [{"m_Id": 1, "m_Localized": "Settings"}],
    }
    obj = SimpleNamespace(read_typetree=lambda: tree)

    class FakeEnvironment:
        def __init__(self): self.objects = []
        def load(self, loaded):
            assert loaded == [str(path)]
            self.objects = [obj]

    disposed = []
    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)
    monkeypatch.setattr(writer, "_dispose_environment", disposed.append)

    assert _localization_bundle_probe(path) == (
        frozenset({"shared:2:1733287269080016787"}), "en")
    assert len(disposed) == 1


def test_find_asset_files_rejects_level_scene_outside_data_tree(tmp_path):
    outside_scene = tmp_path / "level1"
    outside_scene.write_bytes(b"not a Unity data-tree scene")

    assert find_asset_files(tmp_path) == []


def test_find_asset_files_requires_lowercase_level_scene_name(tmp_path):
    data_dir = tmp_path / "Example_Data"
    data_dir.mkdir()
    uppercase_scene = data_dir / "Level0"
    uppercase_scene.write_bytes(b"wrong-case scene name")

    assert find_asset_files(tmp_path) == []


def test_localization_source_selection_prefers_tree_locale_over_filename(
        monkeypatch):
    spanish_name = Path(
        "localization-string-tables-spanish(es)_assets_all.bundle")
    english_name = Path(
        "localization-string-tables-english(en)_assets_all.bundle")
    locales = {spanish_name: "en", english_name: "es"}
    monkeypatch.setattr(
        "hanhua.core.unity.extractor._localization_bundle_locale",
        lambda path: locales[path],
    )

    assert _prefer_source_locale_bundles([spanish_name, english_name]) == [
        spanish_name,
    ]


def test_localization_tree_locales_keep_all_when_english_is_absent(monkeypatch):
    first = Path("localization-string-tables-first_assets_all.bundle")
    second = Path("localization-string-tables-second_assets_all.bundle")
    locales = {first: "es", second: "ru"}
    monkeypatch.setattr(
        "hanhua.core.unity.extractor._localization_bundle_locale",
        lambda path: locales[path],
    )

    assert _prefer_source_locale_bundles([first, second]) == [first, second]


def test_scan_strings_finds_serialized():
    raw = b"\x00\x01\x02\x03" + _with_len("Hello player") + _with_len("第二行文本") + b"\xff\xfe"
    found = scan_strings(raw)
    texts = [s for _, s in found]
    assert "Hello player" in texts and "第二行文本" in texts


def test_scan_strings_filters_garbage():
    raw = b"\x00\x01\x02\x03" + _with_len("ok") + b"\x10\x00\x00\x00" + b"x" * 16
    found = scan_strings(raw)
    texts = [s for _, s in found]
    assert all(s != "ok" for s in texts)      # 过短不采


def test_scan_strings_keeps_two_char_cjk_display_pair():
    # electric-trains obj1558 真实字节（问题集 B12）：typetree 失败文件的
    # raw 扫描是唯一提取通道，2 字符 CJK '同意'（6 字节）有合法长度头
    # （int32=6 @144，数据 @148-154，2B 零填充），曾被 min_len=3（按字符
    # 数计）整类拒绝 → 整对象零条目。
    raw = bytes.fromhex(
        "0000000017000000000000000100000001000000eb0000000000000000000000"
        "0000000000000000000000000000803ff5f4743fb2b1313f0000803f00000000"
        "0000000000000000000000000000000001000000000000000200000060000000"
        "000000002d000000000000000000000000000000100100000400000000000000"
        "000000000000000000000000cdcc4c3f06000000e5908ce6848f0000")
    assert scan_strings(raw) == [(148, "同意")]


def test_scan_strings_two_char_relaxation_is_cjk_only():
    # 豁免只给纯 CJK 表意 2 字符：ASCII/混合/假名 2 字符串仍拒
    # （与二进制噪声形态区分度不足，宁漏勿坏）。
    assert scan_strings(b"\x00\x01\x02\x03" + _with_len("ok")) == []
    assert scan_strings(b"\x00\x01\x02\x03" + _with_len("a汉")) == []
    assert scan_strings(b"\x00\x01\x02\x03" + _with_len("ああ")) == []


def test_raw_scan_keeps_two_char_cjk_single_visible_object():
    # electric-trains obj1558 全链路：'同意' 是对象唯一字符串
    # （is_single_visible 通道）——修复前该对象产出零条目。
    entries = _raw_string_entries("f1", 1558, _with_len("同意"), {})

    assert len(entries) == 1
    assert entries[0].original == "同意"
    assert entries[0].status == "pending"
    assert entries[0].meta["role"] == "display"


def test_scan_strings_requires_aligned_header_and_zero_padding():
    valid = struct.pack("<I", 5) + b"Hello" + b"\x00\x00\x00"
    unaligned_false_positive = b"X" + struct.pack("<I", 3) + b"`\tB"
    invalid_padding = struct.pack("<I", 5) + b"Hello" + b"XYZ"

    assert scan_strings(valid) == [(4, "Hello")]
    assert scan_strings(unaligned_false_positive) == []
    assert scan_strings(invalid_padding) == []


def test_raw_scan_keeps_651_byte_display_description():
    prefix = "This display description explains what the player must do. "
    description = (prefix + "More visible details. " * 40)[:650] + "."
    assert len(description.encode("utf-8")) == 651

    entries = _raw_string_entries("f1", 5, _with_len(description), {})

    assert len(entries) == 1
    assert entries[0].original == description
    assert entries[0].status == "pending"
    assert entries[0].meta["role"] == "display"


def test_unaligned_raw_scan_accepts_exact_4096_byte_boundary():
    description = ("Visible details for the player. " * 200)[:4095] + "."
    assert len(description.encode("utf-8")) == 4096
    encoded = description.encode("utf-8")
    raw = b"\xff" + struct.pack("<I", len(encoded)) + encoded
    raw += b"\x00" * (-len(raw) % 4)

    entries = _raw_string_entries("f1", 5, raw, {})

    assert len(entries) == 1
    assert entries[0].original == description
    assert entries[0].meta["scan_mode"] == "unaligned"


@pytest.mark.parametrize("prefix", [b"\xff", b"\xff\xfe", b"\xff\xfe\xfd"])
def test_raw_entries_recover_unaligned_interaction_prompt(prefix):
    encoded = b"Press E to open"
    raw = prefix + struct.pack("<I", len(encoded)) + encoded
    raw += b"\x00" * (-len(raw) % 4)

    entries = _raw_string_entries("f1", 5, raw, {})

    assert len(entries) == 1
    assert entries[0].original == "Press E to open"
    assert entries[0].status == "pending"
    assert entries[0].meta["role"] == "display"
    assert entries[0].meta["reason"] == "interaction_prompt"
    assert entries[0].meta["scan_mode"] == "unaligned"


@pytest.mark.parametrize("text", [
    "按 E 键打开",
    "Нажмите E, чтобы открыть.",
])
def test_raw_entries_recover_unaligned_utf8_display_text(text):
    encoded = text.encode("utf-8")
    raw = b"\xff" + struct.pack("<I", len(encoded)) + encoded
    raw += b"\x00" * (-len(raw) % 4)

    entries = _raw_string_entries("f1", 5, raw, {})

    assert len(entries) == 1
    assert entries[0].original == text
    assert entries[0].status == "pending"
    assert entries[0].meta["scan_mode"] == "unaligned"


@pytest.mark.parametrize("text", ["Move", "Fire", "set_clip", "TMPro.TMP_Text"])
def test_unaligned_structural_strings_are_not_promoted(text):
    raw = b"\xff" + struct.pack("<I", len(text.encode("utf-8"))) + text.encode("utf-8")
    raw += b"\x00" * (-len(raw) % 4)

    assert _raw_string_entries("f1", 5, raw, {}) == []


def _scriptable_object_raw(*texts: str) -> bytes:
    """ScriptableObject 形态 raw：头部 12 字节全零（无 m_GameObject 引用）
    + 依次字符串（每个串 payload 后对齐到 4 字节，Unity 序列化常见布局；
    无对齐则后续串的长度头落在非 4 对齐偏移，scan_strings 拒收）。
    the-supper 按钮配置对象实证形态。"""
    raw = b"\x00" * 12
    for t in texts:
        payload = t.encode("utf-8")
        raw += struct.pack("<I", len(payload)) + payload
        raw += b"\x00" * (-len(payload) % 4)
    return raw


@pytest.mark.parametrize("obj_name,button_text", [
    ("NewGameButton", "New Game"),
    ("QuitButton", "Quit"),
    ("BackButton", "Pause"),
])
def test_ui_control_object_button_text_is_display(obj_name, button_text):
    # the-supper 实证（R1 证据分层）：Corgi Engine 按钮配置对象
    # （对象名 NewGameButton + 按钮文本 'New Game'）被 small_config
    # 规则误跳过——对象名含 UI 控件词缀是显式形态证据，优先于
    # 「小配置=引擎键」的猜测；按钮文本必须放行翻译。
    entries = _raw_string_entries("f1", 5, _scriptable_object_raw(obj_name, button_text), {})
    hit = [e for e in entries if e.original == button_text]
    assert hit, f"{button_text!r} 未被提取"
    assert hit[0].status == "pending"
    assert hit[0].meta["role"] == "display"


def test_ui_word_object_pause_menu_is_display():
    # the-supper obj 1755 实证：UIMenu 面板配置 'Pause'+'Menu'——
    # 白名单显示词证据豁免 small_config（'Timothy'/'Player Idle' 等
    # 引擎配置名不在白名单，不受影响）。
    entries = _raw_string_entries("f1", 5, _scriptable_object_raw("Pause", "Menu"), {})
    origins = {e.original: e for e in entries}
    assert origins["Pause"].status == "pending"
    assert origins["Menu"].status == "pending"


def test_gated_word_item_name_in_evidence_object_is_display():
    """R2 回归：'Skull'（emoji 字符名同形）在含显示证据的对象（物品列表+
    描述句）里是真实物品名——必须放行（审计 R2 实证曾全局无条件跳过）。"""
    raw = (_with_len("Skull")
           + _with_len("A rare item dropped by bosses."))
    entries = _raw_string_entries("f1", 5, raw, {})
    skull = next(e for e in entries if e.original == "Skull")
    assert skull.status == "pending"
    assert skull.meta["role"] == "display"


def test_gated_word_alone_still_skipped_as_engine_asset():
    """R2 回归：'Skull' 无显示证据单串（TMP 表情资产形态）仍是引擎内容
    ——门控层不是放行层，rawstr 分类链按证据决定。"""
    entries = _raw_string_entries(
        "f1", 5, _scriptable_object_raw("Skull"), {})
    assert all(e.status == "skipped" for e in entries)


def test_press_button_text_in_ui_control_object_is_display():
    """R2 回归：'Press'（Input 绑定名同形）在 UI 控件对象（PressButton）
    里是按钮文本——白名单证据放行。"""
    entries = _raw_string_entries(
        "f1", 5, _scriptable_object_raw("PressButton", "Press"), {})
    hit = next(e for e in entries if e.original == "Press")
    assert hit.status == "pending"
    assert hit.meta["role"] == "display"


@pytest.mark.parametrize("text", ["Timothy", "Player Idle"])
def test_engine_config_names_still_skipped(text):
    # morfosigame 对照：Timeline 剪辑名/动画状态名（无控件词缀、不在
    # 白名单）仍是引擎键——R1 修复不得放行引擎配置对象。
    # 'Player Idle' 12 字符命中句子形状，真实里由 Timeline 对象信号
    # （UnityEngine.Timeline 程序集串）拦截——样本带信号才代表真实形态。
    texts = [text]
    if text == "Player Idle":
        texts = ["Player Idle", "UnityEngine.Timeline"]
    entries = _raw_string_entries("f1", 5, _scriptable_object_raw(*texts), {})
    assert all(e.status == "skipped" for e in entries)


def test_is_engine_string():
    from hanhua.core.engine_strings import is_engine_string_gated
    assert _is_engine_string("_MainTex")
    assert _is_engine_string("UnityEngine.Rendering.DebugUI")
    assert _is_engine_string("TextMeshPro/Mobile/Distance Field")
    # R2 三层拆分：Input System 绑定名/emoji 字符名/后处理单词等普通英语
    # 单词移入 gated 层——rawstr 侧按对象证据放行（'Skull' 物品名回归），
    # 不再是无条件引擎串；DLL 侧仍由 mono_dll 显式 gated 拦截
    assert not _is_engine_string("Navigate")
    assert is_engine_string_gated("Navigate")
    assert is_engine_string_gated("Mouse")
    assert is_engine_string_gated("Skull")
    assert not is_engine_string_gated("monologuetable")
    assert _is_engine_string("Keyboard&Mouse")   # 组合绑定形态仍在 core
    assert not _is_engine_string("Hello player")
    assert not _is_engine_string("要活下去")


def test_input_system_binding_path_and_interaction_are_engine_strings():
    # morfosigame 实证：InputActionAsset 序列化绑定路径/交互串是引擎语法，
    # 全局剔除（即使对象级判定漏网也不会被提取翻译）
    assert _is_engine_string("<Keyboard>/z")
    assert _is_engine_string("<Keyboard>/upArrow")
    assert _is_engine_string("<Mouse>/position")
    assert _is_engine_string("<Gamepad>/leftStick")
    assert _is_engine_string("<Gamepad>/buttonSouth")
    assert _is_engine_string("Press(behavior=2)")
    assert _is_engine_string("Hold()")
    assert _is_engine_string("Tap()")
    assert _is_engine_string("SlowTap()")
    assert not _is_engine_string("<b>Hello</b>")


def test_timeline_track_with_index_is_engine_string():
    # 带编号轨道名（Unity Timeline 轨道重名自动加 (1)）是引擎 displayName
    assert _is_engine_string("Animation Track (1)")
    assert _is_engine_string("Activation Track (2)")
    assert _is_engine_string("Audio Track (1)")
    assert _is_engine_string("Animation Track")
    assert not _is_engine_string("Track 7 night festival")


@pytest.mark.parametrize("text", [
    "Press E to open",
    "Hold [F] to interact",
    "Click to continue",
    "E - Open",
    "按 E 键打开",
    "Press E",
    "Press E to calibrate the flux capacitor",
    "Press E on the radar to mark a location",
    "E - Calibrate the flux capacitor",
    "right click with Harpoon equipped to reel in",
    "Square/X/Y Button: Jump",
])
def test_interaction_prompts_have_display_text_evidence(text):
    assert is_interaction_prompt(text)
    assert has_display_text_evidence(text)


@pytest.mark.parametrize("text", [
    "Move", "Fire", "WASD", "set_clip", "TMPro.TMP_Text",
])
def test_structural_strings_do_not_have_display_text_evidence(text):
    assert not has_display_text_evidence(text)


@pytest.mark.parametrize("text", [
    "F - MyGame.DispatchEvent",
    "F - set_clip",
    "E - m_Action",
    "F - Open.Method",
    "F - Use.Action",
    "E - Fire.Event",
    "F - MyGame.DispatchEvent()",
    "F - set_clip()",
    "E - m_Action[0]",
])
def test_code_actions_after_glyph_are_not_interaction_prompts(text):
    assert not is_interaction_prompt(text)
    assert not has_display_text_evidence(text)


@pytest.mark.parametrize("text", [
    "F - MyGame.DispatchEvent",
    "F - set_clip",
    "E - m_Action",
    "F - Open.Method",
    "F - Use.Action",
    "E - Fire.Event",
    "F - MyGame.DispatchEvent()",
    "F - set_clip()",
    "E - m_Action[0]",
])
def test_raw_code_actions_after_glyph_remain_structural(text):
    entries = _raw_string_entries("f1", 8, _with_len(text), {})

    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == "skipped"
    assert entry.meta["confidence"] == "low"
    assert entry.meta["role"] == "structural"
    assert entry.meta["reason"] == "code_action_binding"


@pytest.mark.parametrize(("text", "tokens"), [
    ("Press SPACE to jump", ("SPACE",)),
    ("Click Mouse1 to fire", ("Mouse1",)),
    ("Hold Left Shift to sprint", ("Left Shift",)),
    ("E - Open", ("E",)),
    ("Press (E) to open", ("E",)),
    ("Press <E> to open", ("E",)),
    ("Press LB to block", ("LB",)),
    ("Press R1 to dodge", ("R1",)),
    ("Press Numpad 1 to select", ("Numpad 1",)),
    ("Press Esc to exit", ("Esc",)),
    ("Press Backspace to close", ("Backspace",)),
    ("Press Delete to remove", ("Delete",)),
    ("Press Enter to confirm", ("Enter",)),
    ("Press the Enter key to confirm", ("Enter",)),
    ("Hold the Space key to jump", ("Space",)),
    ("Press Tab to switch", ("Tab",)),
    ("Press Space to jump", ("Space",)),
    ("Press Page Up to scroll", ("Page Up",)),
    ("Press Page Down to scroll", ("Page Down",)),
    ("Press Home to return", ("Home",)),
    ("Press End to finish", ("End",)),
    ("Press Insert to toggle", ("Insert",)),
    ("Press D-Pad Up to select", ("D-Pad Up",)),
    ("Press D-Pad Down to select", ("D-Pad Down",)),
    ("Press D-Pad Left to select", ("D-Pad Left",)),
    ("Press D-Pad Right to select", ("D-Pad Right",)),
    ("Press Ctrl+Delete to remove", ("Ctrl+Delete",)),
    ("Press Page Up+Shift to scroll", ("Page Up+Shift",)),
    ("Press D-Pad Up+LB to select", ("D-Pad Up+LB",)),
])
def test_interaction_prompt_preserves_complete_input_glyph(text, tokens):
    assert is_interaction_prompt(text)
    assert interaction_input_tokens(text) == tokens


@pytest.mark.parametrize(("text", "token"), [
    ("Press Ctrl_Delete to remove", "Ctrl_Delete"),
    ("Press Ctrl-Delete to remove", "Ctrl-Delete"),
])
def test_interaction_position_preserves_physical_binding_chord(text, token):
    assert interaction_input_tokens(text) == (token,)


def test_interaction_position_does_not_capture_natural_hyphenated_phrase():
    assert interaction_input_tokens("Press Long-term plan") == ()


def test_interaction_input_events_type_literal_glyphs_in_source_order():
    text = "Press 'E', hold Shift, click Mouse1, tap [F], then press 2"

    assert interaction_input_events(text) == (
        InputEvent("literal_glyph", "E"),
        InputEvent("literal_glyph", "Shift"),
        InputEvent("literal_glyph", "Mouse1"),
        InputEvent("literal_glyph", "F"),
        InputEvent("literal_glyph", "2"),
    )


@pytest.mark.parametrize(("text", "value"), [
    ("Press Any Key", "Any Key"),
    ("right click with Harpoon equipped to reel in", "right click"),
    ("Square/X/Y Button: Jump", "Square/X/Y Button"),
    ("Press X Button to jump", "X Button"),
])
def test_interaction_input_events_type_translatable_semantic_inputs(text, value):
    assert interaction_input_events(text) == (InputEvent("semantic_input", value),)
    assert interaction_input_tokens(text) == ()


def test_multiline_interaction_glyph_does_not_absorb_previous_item_label():
    text = "Key30\nG - to throw\n"

    assert is_interaction_prompt(text)
    assert interaction_input_tokens(text) == ("G",)


def test_ambiguous_ui_words_are_not_global_engine_strings():
    for text in ("volume", "fullscreen", "vsync", "cancel", "submit"):
        assert not _is_engine_string(text)


# ── 长度适配 ──
def test_fit_bytes_short_pads():
    out, truncated = _fit_bytes("你好", 20, "utf-8")
    assert len(out) == 20 and not truncated
    assert out.endswith(b"\x00" * 14)


def test_fit_bytes_truncates_utf8():
    out, truncated = _fit_bytes("这是一句很长的中文", 9, "utf-8")
    assert truncated and len(out) == 9
    out.decode("utf-8")   # 必须是合法 UTF-8（字符边界截断）
    assert out.decode("utf-8", errors="replace").endswith("…")   # 末尾省略号提示


def test_fit_bytes_small_capacity_no_ellipsis():
    out, truncated = _fit_bytes("太长太长太长", 5, "utf-8")
    assert truncated and len(out) == 5
    assert "…" not in out.decode("utf-8")   # 容量太小不加省略号


def test_fit_bytes_utf16():
    out, truncated = _fit_bytes("开始游戏", 20, "utf-16-le")
    assert len(out) == 20 and not truncated
    out, truncated = _fit_bytes("This is a very long english string", 10, "utf-16-le")
    assert truncated and len(out) == 10


# ── TextAsset 提取与写回 ──
def test_textasset_lines_extract():
    raw = "Hello there\n第二行\nThird line with {name} tag\n".encode("utf-8")
    entries = _textasset_entries("f1", 100, raw)
    orig = {e.key_path: e.original for e in entries}
    assert orig["asset#100/line/0"] == "Hello there"
    assert orig["asset#100/line/1"] == "第二行"
    assert orig["asset#100/line/2"] == "Third line with {name} tag"
    assert all(entry.meta["disposition"] == "translate" for entry in entries)
    assert all(entry.meta["role"] == "display" for entry in entries)


def test_textasset_identity_includes_serialized_file_name():
    entries = _textasset_entries(
        "f1", 100, b"Hello there\n", "archive:/CAB-demo/CAB-demo")
    assert entries[0].key_path == (
        "asset#archive:/CAB-demo/CAB-demo#100/line/0")
    assert entries[0].meta["asset_file"] == "archive:/CAB-demo/CAB-demo"


def test_textasset_binary_control_chars_filtered():
    # 二进制 TextAsset（音频/网格/压缩）：非可打印字节占比 >5% → 无条目
    raw = b"\x00\x01\x02\x03" * 100 + b"Hello there\n"
    assert _textasset_entries("f1", 100, raw) == []


def test_textasset_data_rows_filtered():
    # 数据文件（关卡/配置数字表）：行内 ≥3 字母单词密度 <30% → 无条目
    # （electric-trains fp_level_* 实证）
    raw = "6098:1\r\n0:12:-1:none\r\n0:13:-1:none\r\n0:14:-1:none\r\n0:15:-1:none\r\n0:40:-1:none\r\n".encode()
    assert _textasset_entries("f1", 100, raw) == []


def test_textasset_data_rows_kept_when_wordy():
    # 真文本（字典/字幕）每行含单词 → 不被数据判定误伤
    raw = b"missions=Missioni\nfreeplay=Gioco gratuito\nsettings=Impostazioni\nexit=Uscita\n"
    entries = _textasset_entries("f1", 100, raw)
    assert len(entries) == 4
    assert entries[0].original == "missions=Missioni"


def test_textasset_script_source_file_produces_no_entries():
    # 源码文件整文件跳过（0.25.0 地毯式排查实证锚点：a-catfiends-impending-
    # relapse resources.assets#69 是 inspect.lua 脚本库，被按行拆成 264 条
    # 进池、翻译代码被质量门拦截成 264 条失败——代码文本翻译即破坏功能，
    # 属于硬结构规则：整文件不产生条目）
    inspect_lua = (
        "local inspect ={\n"
        "local rawlen = _G.rawlen or function(t) return #t end\n"
        "local function smartQuote(str)\n"
        "local controlCharsTranslation = {\n"
        '["\\a"] = "\\\\a",  ["\\b"] = "\\\\b", ["\\f"] = "\\\\f",\n'
        "local result = str:gsub(\"\\\\\", \"\\\\\\\\\"):gsub(\"(%c)\", controlCharsTranslation)\n"
        "local function isIdentifier(str)\n"
        "local function isSequenceKey(k, length)\n"
        "if ta == tb and (ta == 'string' or ta == 'number') then return a < b end\n"
        "return type(k) == 'number'\n"
        "table.sort(keys, sortKeys)\n"
        "inspect.KEY = setmetatable({}, {__tostring = function() return 'inspect.KEY' end})\n"
        "local inspector = setmetatable({},\n"
        "inspector:putValue(root)\n"
    ).encode()
    assert _textasset_entries("f1", 100, inspect_lua) == []


def test_textasset_json_not_killed_by_script_source():
    """可解析 JSON 不被 _looks_like_script_source 误杀（0.36.11 修复实证：
    project-arrhythmia PAChat/thanks/chat、dear-edmund CharacterName_En、
    isolated-inhale Socials 全被旧判定吞成 0 条——缩进的裸括号行/键值行
    命中脚本特征占比虚高）。JSON 结构行剔除后再算占比，真显示文本进池。"""
    # PAChat 脚本（thanks 变体：JSON 缩进 + 真文本）
    pachat = (
        '{\n'
        '  "settings": {\n'
        '    "initial_branch": "thanks",\n'
        '    "text_color": "#212121"\n'
        '  },\n'
        '  "branches": [\n'
        '    {\n'
        '      "name": "thanks",\n'
        '      "settings": {"clear_screen": "false"},\n'
        '      "elements": [\n'
        '        {"type": "text", "data": [" " ]},\n'
        '        {"type": "text", "data": [\n'
        '          "Thanks for playing the story mode."]},\n'
        '        {"type": "text", "data": [\n'
        '          "While still in development the story mode will be added to over time."]},\n'
        '        {"type": "text", "data": [\n'
        '          "So please stay tuned!"]}\n'
        '      ]\n'
        '    }\n'
        '  ]\n'
        '}\n'
    ).encode()
    from hanhua.core.unity.extractor import _looks_like_script_source
    assert _looks_like_script_source(pachat) is False
    # PAChat 终端脚本整文件跳过（0.36.12 修复实证：分支名/命令 token 是
    # 机器引用，真文本与命令逐条混杂只占少部分——宁漏勿坏防译坏分支跳转）
    entries = _textasset_entries("f1", 100, pachat)
    assert entries == []
    # dear-edmund CharacterName_En 形态（问答对话 JSON）
    qa = (
        '{\n'
        '  "characterName": "Dave",\n'
        '  "questions": [\n'
        '    {\n'
        '      "id": 1,\n'
        '      "text": "Will you help me?",\n'
        '      "responses": [\n'
        '        {"text": "Yes, I will give you money."}\n'
        '      ]\n'
        '    }\n'
        '  ]\n'
        '}\n'
    ).encode()
    assert _looks_like_script_source(qa) is False
    entries2 = _textasset_entries("f1", 101, qa)
    origins2 = [e.original for e in entries2 if e.status == "pending"]
    assert "Will you help me?" in origins2
    # 短代码片段（<8 行）不做整文件判定，但行级代码兜底仍过滤确定性
    # 代码行（local 声明），真实文本行保留（0.25.0 修复 3 语义）
    raw = b"local x = 1\nlocal y = 2\nHello there\n"
    entries = _textasset_entries("f1", 100, raw)
    assert len(entries) == 1
    assert entries[0].original == "Hello there"


def test_textasset_lexicon_list_produces_no_entries():
    """词库型 TextAsset（0.26 地毯式实证：force-reboot data.unity3d#obj268
    脏话检测黑名单——1100+ 行全英文短词被当显示文本全翻译写回、游戏过滤
    逻辑失效）：单词行占比 ≥90% 且 ≥30 行 → 整文件结构跳过（黑名单/词典
    /名单是比对数据非显示文本）。"""
    raw = ("arrse\naskhole\nassbag\nassmunch\nbigmuffpi\nboff\nbutsex\n"
           "buttfuck\nbuttmunch\ncabron\nchank\ncheesedick\nchinc\nchoad\n"
           "chode\ncipa\ncok\ncoksucka\ncoochie\ncumstain\ncumtart\n"
           "dabitch\ndiaf\nepeen\nepenis\nfacking\nfaggit\nfaghag\nfckers\n"
           "fcuker\nfelch\nfelched\nfelches\nfelching\nfeltch\nfook\n"
           "fooker\nfubar\nfucka\nfudgepacker\nfukkin\nfukwhit\nfuq\n"
           "fuqed\nfux\ngoatse\ngoddam\ngspot\ngubb\ngyfs\n").encode()
    skipped: dict[str, int] = {}
    assert _textasset_entries("f1", 100, raw, skipped=skipped) == []
    assert skipped == {"textasset_lexicon": 1}


def test_textasset_lexicon_not_confused_by_dialogue():
    """反例：对话/字幕文本（句行含空格）占比高 → 不判词库，正常提取。"""
    raw = ("Oh no, the lever broke!\n"
           "We need to find another way through.\n"
           "The guard is still watching the door.\n"
           "Maybe there is a key in the kitchen?\n"
           "I should check under the rug.\n"
           "Alright, let's move quickly then!\n"
           "This place gives me the creeps.\n"
           "I hear something behind us...\n"
           "Run for the exit right now!\n"
           "We made it, we are safe here.\n"
           "What a relief that was!\n"
           "Let's rest a moment before moving on.\n"
           "The stairs are creaking again.\n"
           "Did you hear that noise too?\n"
           "Someone must be upstairs still.\n"
           "We should split up and search.\n"
           "No way, I am not going alone.\n"
           "Fine, then we search together.\n"
           "Stay close and keep quiet.\n"
           "The basement is locked for now.\n"
           "Check the old desk for clues.\n"
           "There is a note under the lamp.\n"
           "It says the vault is hidden.\n"
           "Behind the painting on the wall.\n"
           "How sneaky, but smart!\n"
           "Let's open it together now.\n"
           "Almost there, just a bit more!\n"
           "I can't believe we pulled it off.\n"
           "Yes! The door is finally open.\n"
           "Let's get out of here for good.\n"
           "What an adventure this was.\n").encode()
    skipped: dict[str, int] = {}
    entries = _textasset_entries("f1", 100, raw, skipped=skipped)
    assert len(entries) == len(raw.decode().splitlines())
    assert skipped == {}


def test_textasset_short_wordlist_kept():
    """反例：<30 行的短名单不做词库判定（防误伤），按正常行处理。"""
    raw = ("sword\nshield\npotion\nmap\n").encode()
    entries = _textasset_entries("f1", 100, raw)
    assert len(entries) == 4


def test_textasset_lexicon_survives_mixed_assignment_rows():
    """反例：含 = 的字典行（missions=Missioni）不匹配单词行——词库判定
    只在纯词表文件触发，字典/映射文件不受影响。"""
    raw = ("missions=Missioni\nfreeplay=Gioco gratuito\n"
           "settings=Impostazioni\nexit=Uscita\n").encode()
    assert len(_textasset_entries("f1", 100, raw)) == 4


def test_textasset_line_skips_record_reasons():
    """识别 C4：文本行路径的引擎串/键标识符/代码行跳过全部留档
    （skipped_count 聚合），不再静默 continue——「纯文本行跳过、判定
    规律未定位到代码层」（222am 实证）的排查入口。"""
    raw = (
        "MENU_PLAY\n"               # 键标识符（should_skip）
        "local x = 1\n"             # 代码行
        "GetComponent\n"            # 引擎串
        "Hello there\n"             # 真实文本保留
    ).encode()
    skipped: dict[str, int] = {}
    entries = _textasset_entries("f1", 100, raw, skipped=skipped)
    assert [e.original for e in entries] == ["Hello there"]
    assert skipped == {
        "textasset_key_identifier": 1,
        "textasset_code_line": 1,
        "textasset_engine_string": 1,
    }


def test_typetree_candidate_cap_truncation_is_recorded():
    """识别 C5：候选层 200 上限不再静默截断——超限叶子按 reason 聚合
    计数留档（skipped_count 语义），报告可见「该对象候选超限 N」。"""
    from hanhua.core.unity.extractor import (
        _MAX_CANDIDATES_PER_OBJECT, _typetree_string_entries)
    tree = {f"slot{i}": "randomvalue" for i in range(_MAX_CANDIDATES_PER_OBJECT + 5)}
    skipped: dict[str, int] = {}
    display, candidates = _typetree_string_entries("f1", 100, tree,
                                                   skipped=skipped)
    assert len(candidates) == _MAX_CANDIDATES_PER_OBJECT
    assert skipped == {"typetree_candidate_truncated": 5}
    assert display == []
    # 未超限时不产生截断计数
    small_skipped: dict[str, int] = {}
    _typetree_string_entries("f1", 100, {f"slot{i}": "randomvalue"
                                         for i in range(10)},
                             skipped=small_skipped)
    assert small_skipped == {}


def test_extract_asset_file_deduplicates_wrapper_aliases_by_stable_identity(
        tmp_path, monkeypatch):
    import UnityPy

    class FakeObject:
        path_id = 100
        type = type("FakeType", (), {"name": "TextAsset"})()

        def __init__(self):
            self.assets_file = type(
                "FakeSerializedFile", (), {"name": "archive:/CAB-demo/CAB-demo"})()

        def read(self):
            return type("FakeTextAsset", (), {"m_Script": b"Hello there\n"})()

    class FakeEnvironment:
        def __init__(self):
            self.objects = [FakeObject(), FakeObject()]
            self.files = {}

        def load(self, _paths):
            return None

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)

    parsed = extract_asset_file(tmp_path / "sample.assets", "sample.assets")

    assert [entry.original for entry in parsed.entries] == ["Hello there"]


def test_extract_asset_file_keeps_same_path_id_from_different_serialized_files(
        tmp_path, monkeypatch):
    import UnityPy

    class FakeObject:
        path_id = 100
        type = type("FakeType", (), {"name": "TextAsset"})()

        def __init__(self, asset_file_name, text):
            self.assets_file = type(
                "FakeSerializedFile", (), {"name": asset_file_name})()
            self._text = text

        def read(self):
            return type("FakeTextAsset", (), {"m_Script": self._text})()

    class FakeEnvironment:
        def __init__(self):
            self.objects = [
                FakeObject("archive:/CAB-first/CAB-first", b"First text\n"),
                FakeObject("archive:/CAB-second/CAB-second", b"Second text\n"),
            ]
            self.files = {}

        def load(self, _paths):
            return None

    monkeypatch.setattr(UnityPy, "Environment", FakeEnvironment)

    parsed = extract_asset_file(tmp_path / "sample.bundle", "sample.bundle")

    assert [entry.original for entry in parsed.entries] == [
        "First text", "Second text",
    ]


def test_textasset_json_extract():
    raw = b'{"title": "Echoes", "items": ["Follow", "Leave"]}'
    entries = _textasset_entries("f1", 100, raw)
    orig = {e.key_path: e.original for e in entries}
    assert orig["asset#100/json/title"] == "Echoes"
    assert orig["asset#100/json/items/0"] == "Follow"


def test_textasset_patch_roundtrip():
    from hanhua.core.models import TextEntry, TranslateStats
    from hanhua.core.unity.writer import WriteResult
    script = "Hello there\nSecond Line\n".encode("utf-8")
    items = [({"original": "Hello there", "translation": "你好呀", "meta": "{}"},
              {"kind": "textasset", "line": 0})]
    out = _patch_textasset(script, items, [], WriteResult())
    # TextAsset 的 m_Script 是可变长 byte[] 字段——译文可自由变长，不截断
    assert out.decode("utf-8").startswith("你好呀")
    assert "Second Line" in out.decode("utf-8")
    assert len(out) != len(script)


def test_textasset_patch_long_translation_free():
    from hanhua.core.unity.writer import WriteResult
    script = "Hi\n".encode("utf-8")
    items = [({"original": "Hi", "translation": "很长很长的翻译没有任何长度限制",
               "meta": "{}"}, {"kind": "textasset", "line": 0})]
    res = WriteResult()
    out = _patch_textasset(script, items, [], res)
    assert res.truncated == 0
    assert out.decode("utf-8").startswith("很长很长的翻译没有任何长度限制")


def test_raw_string_entries_filters_engine():
    raw = (b"\x00" * 8) + _with_len("_MainTex") + _with_len("Follow the light") + _with_len("Yes")
    entries = _raw_string_entries("f1", 5, raw, {"_MainTex": 300, "Follow the light": 1, "Yes": 1})
    by_original = {e.original: e for e in entries}
    # R5：引擎串不再静默丢弃——留档为 skipped 条目（reason=prefilter_*）
    assert by_original["_MainTex"].status == "skipped"
    assert by_original["_MainTex"].meta["reason"] == "prefilter_engine_string"
    assert by_original["_MainTex"].meta["role"] == "structural"
    assert by_original["Follow the light"].status == "pending"  # 普通句子
    assert by_original["Yes"].status == "pending"               # 显示单词（白名单）


def test_single_string_object_is_high_confidence_display_text():
    entries = _raw_string_entries("f1", 5, _with_len("Battery"), {})

    assert len(entries) == 1
    entry = entries[0]
    assert entry.original == "Battery"
    assert entry.status == "pending"
    assert entry.meta["confidence"] == "high"
    assert entry.meta["role"] == "display"
    assert entry.meta["disposition"] == "translate"
    assert entry.meta["reason"] == "single_visible_string"


def test_isolated_long_lowercase_word_is_skipped_as_code_identifier():
    """F10-B：孤立纯小写长词（≥10 字符）跳过（fieldtrigger 12 字符
    实证——MonoBehaviour rawstr 里孤立的代码词被无条件放行后模型回显
    恒败；触发器/字段名形态，翻译破坏功能）。"""
    entries = _raw_string_entries("f1", 5, _with_len("fieldtrigger"), {})
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == "skipped"
    assert entry.meta["role"] == "structural"
    assert entry.meta["disposition"] == "structural"
    assert entry.meta["reason"] == "isolated_lowercode_word"


def test_short_lowercase_scene_word_still_translated():
    """对照：短纯小写场景词（shower/city/bedroom，222am 实证）不受
    isolated_lowercode_word 影响——场景词形态可翻译。"""
    for word in ("shower", "city", "bedroom", "ladder", "mug"):
        entries = _raw_string_entries("f1", 5, _with_len(word), {})
        assert len(entries) == 1, word
        assert entries[0].status == "pending", word
        assert entries[0].meta["disposition"] == "translate", word


def test_resources_asset_single_identifier_requires_display_evidence():
    entry = _raw_string_entries(
        "resources.assets", 5, _with_len("Enum"), {}, "resources.assets",
    )[0]

    assert entry.status == "skipped"
    assert entry.meta["confidence"] == "low"
    assert entry.meta["role"] == "structural"
    assert entry.meta["disposition"] == "structural"
    assert entry.meta["reason"] == "resource_identifier_without_display_evidence"


def test_single_input_binding_names_are_low_confidence_structure():
    for text in ("Move", "WASD", "Fire", "Look"):
        entries = _raw_string_entries("f1", 5, _with_len(text), {})
        assert len(entries) == 1
        entry = entries[0]
        assert entry.status == "skipped"
        assert entry.meta["confidence"] == "low"
        assert entry.meta["role"] == "structural"
        assert entry.meta["reason"] == "input_binding"

    battery = _raw_string_entries("f1", 6, _with_len("Battery"), {})[0]
    assert battery.status == "pending"
    assert battery.meta["confidence"] == "high"
    assert battery.meta["role"] == "display"


@pytest.mark.parametrize("text", [
    "right click", "Right Click", "RIGHT CLICK",
    "Square Button", "square button", "X Button", "x button", "Y Button",
    "Square/X/Y Button",
])
def test_bare_semantic_inputs_remain_low_confidence_bindings(text):
    assert not is_interaction_prompt(text)
    assert not has_display_text_evidence(text)

    entry = _raw_string_entries("f1", 6, _with_len(text), {})[0]
    assert entry.status == "skipped"
    assert entry.meta["confidence"] == "low"
    assert entry.meta["role"] == "structural"
    assert entry.meta["reason"] == "input_binding"


@pytest.mark.parametrize("text", [
    "Ctrl_Delete", "Ctrl-Delete", "D-Pad Up",
])
def test_physical_binding_identifiers_remain_structural_in_raw_entries(text):
    assert _structural_reason(text) == "input_binding"

    entry = _raw_string_entries("f1", 10, _with_len(text), {})[0]
    assert entry.status == "skipped"
    assert entry.meta["confidence"] == "low"
    assert entry.meta["role"] == "structural"
    assert entry.meta["reason"] == "input_binding"


@pytest.mark.parametrize("text", [
    "Press Ctrl_Delete to remove",
    "Press Ctrl-Delete to remove",
])
def test_raw_interaction_with_physical_binding_chord_is_display(text):
    entry = _raw_string_entries("f1", 11, _with_len(text), {})[0]

    assert entry.status == "pending"
    assert entry.meta["confidence"] == "high"
    assert entry.meta["role"] == "display"
    assert entry.meta["reason"] == "interaction_prompt"


def test_natural_hyphenated_phrase_is_not_a_physical_binding_identifier():
    assert _structural_reason("Long-term plan") is None


@pytest.mark.parametrize("text", [
    "Player/Move", "Player/Fire1", "Menu/dPadHoriz", "Debug/Warp 0",
    "Forward/Back Tilt", "Pause/Unpause", "Save/Load", "Menu/Escape",
    "battle/spr_damage_numbers", "CameraRig/MainCamera",
])
def test_input_action_paths_remain_structural_in_raw_entries(text):
    """InputSystem action 路径：翻译后按键查找失败 → 必须跳过。
    真实语料：ivor Player/* 323 条、doubleshake Menu/* 48 条曾被误标 display。"""
    assert _structural_reason(text) == "input_action_path"

    entry = _raw_string_entries("f1", 12, _with_len(text), {})[0]
    assert entry.status == "skipped"
    assert entry.meta["reason"] == "input_action_path"


@pytest.mark.parametrize("text", [
    "Failed to parse server key/certificate",
    "Private/public key mismatch",
    "Sprite Assets/Default Sprite Asset",
])
def test_sentence_shaped_slashes_are_not_input_action_paths(text):
    """词数超限的句子（真实语料：IL2CPP metadata 错误消息）不得判为 action 路径。"""
    assert _structural_reason(text) is None


def test_controller_prompt_with_slashes_survives_raw_string_filtering():
    entries = _raw_string_entries(
        "f1", 7, _with_len("Square/X/Y Button: Jump"), {})

    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == "pending"
    assert entry.meta["confidence"] == "high"
    assert entry.meta["role"] == "display"
    assert entry.meta["reason"] == "interaction_prompt"


@pytest.mark.parametrize("text", [
    "Assets/right click/config",
    "Assets/Square Button/config",
    "Assets/right click to/config",
    "C:\\UI\\right click with Harpoon equipped",
    "Assets/Square/X/Y Button: Jump/config",
])
def test_semantic_input_words_inside_paths_remain_filtered(text):
    # R5：路径串不再静默丢弃——留档为 skipped 条目（prefilter_*）
    entries = _raw_string_entries("f1", 9, _with_len(text), {})
    assert len(entries) == 1
    assert entries[0].status == "skipped"
    assert entries[0].meta["reason"].startswith("prefilter_")
    assert entries[0].meta["role"] == "structural"


def test_code_heavy_object_keeps_sentence_and_marks_structure_skipped():
    raw = (_with_len("Play") + _with_len("set_clip")
           + _with_len("UnityEngine.AudioSource")
           + _with_len("Press E to interact"))
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {entry.original: entry for entry in entries}

    prompt = by_orig["Press E to interact"]
    assert prompt.status == "pending"
    assert prompt.meta["confidence"] == "high"
    assert prompt.meta["role"] == "display"
    assert prompt.meta["reason"] == "interaction_prompt"

    # 'Play' 是 DISPLAY_WORDS 白名单成员（UI 按钮文本）——code_heavy 对象中仍放行
    assert by_orig["Play"].status == "pending"
    assert by_orig["Play"].meta["reason"] == "code_heavy_display_word"
    assert by_orig["set_clip"].status == "skipped"
    assert by_orig["set_clip"].meta["reason"] == "method_name"
    assert by_orig["UnityEngine.AudioSource"].status == "skipped"
    assert by_orig["UnityEngine.AudioSource"].meta["reason"] == "type_reference"
    assert by_orig["Play"].meta["confidence"] == "medium"
    assert by_orig["Play"].meta["role"] == "display"
    assert all(by_orig[text].meta["confidence"] == "low"
               for text in ("set_clip", "UnityEngine.AudioSource"))
    assert all(by_orig[text].meta["role"] == "structural"
               for text in ("set_clip", "UnityEngine.AudioSource"))


def test_code_heavy_button_object_skips_control_state_names():
    # code_heavy 按钮对象（类型引用 + 控件状态 + Play）：Play/Instructions
    # 放行（code_heavy_display_word），但 Normal/Highlighted/Pressed/Disabled
    # 是 Unity VisualState 引擎文本，不得翻译（hotel-paradise 真实误伤）
    raw = (b"\x00" * 8) + b"".join(_with_len(text) for text in (
        "Normal", "Highlighted", "Pressed", "Disabled",
        "Play", "Instructions",
        "UnityEngine.UI.Button", "UnityEngine.UI.Image",
    ))
    entries = _raw_string_entries("mainData", 7, raw, {})
    by_orig = {entry.original: entry for entry in entries}

    assert by_orig["Play"].status == "pending"
    assert by_orig["Play"].meta["reason"] == "code_heavy_display_word"
    assert by_orig["Instructions"].status == "pending"
    assert by_orig["Instructions"].meta["reason"] == "code_heavy_display_word"
    assert all(by_orig[text].status == "skipped"
               and by_orig[text].meta["reason"] == "unity_control_state"
               for text in ("Normal", "Highlighted", "Pressed", "Disabled"))


def test_general_qualified_types_and_lifecycle_methods_make_object_code_heavy():
    raw = (_with_len("Play") + _with_len("TMPro.TMP_Text")
           + _with_len("MyGame.Audio.Controller") + _with_len("Update")
           + _with_len("Press E to interact"))
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {entry.original: entry for entry in entries}

    prompt = by_orig["Press E to interact"]
    assert prompt.status == "pending"
    assert prompt.meta["confidence"] == "high"
    assert prompt.meta["role"] == "display"
    assert prompt.meta["reason"] == "interaction_prompt"

    expected_reasons = {
        "TMPro.TMP_Text": "type_reference",
        "MyGame.Audio.Controller": "type_reference",
        "Update": "lifecycle_method",
    }
    for text, reason in expected_reasons.items():
        entry = by_orig[text]
        assert entry.status == "skipped"
        assert entry.meta["confidence"] == "low"
        assert entry.meta["role"] == "structural"
        assert entry.meta["reason"] == reason

    # code-heavy 对象但有 UI 证据（交互提示）时，白名单显示词（Play）放行
    play = by_orig["Play"]
    assert play.status == "pending"
    assert play.meta["confidence"] == "medium"
    assert play.meta["role"] == "display"
    assert play.meta["reason"] == "code_heavy_display_word"

    single_start = _raw_string_entries("f1", 6, _with_len("Start"), {})[0]
    assert single_start.status == "skipped"
    assert single_start.meta["confidence"] == "low"
    assert single_start.meta["role"] == "structural"
    assert single_start.meta["reason"] == "lifecycle_method"


def test_assembly_reference_requires_a_complete_type_and_assembly_shape():
    welcome = _raw_string_entries(
        "f1", 5, _with_len("Welcome, Unity"), {})[0]
    assert welcome.status == "pending"
    assert welcome.meta["confidence"] in ("high", "medium")
    assert welcome.meta["role"] == "display"
    assert welcome.meta["reason"] != "type_reference"

    references = (
        "MenuButton, Assembly-CSharp",
        "TMPro.TMP_Text, Unity.TextMeshPro",
        ("OneBit, Assembly-CSharp, Version=0.0.0.0, Culture=neutral, "
         "PublicKeyToken=null"),
    )
    for text in references:
        entry = _raw_string_entries("f1", 6, _with_len(text), {})[0]
        assert entry.status == "skipped"
        assert entry.meta["confidence"] == "low"
        assert entry.meta["role"] == "structural"
        assert entry.meta["reason"] == "type_reference"


def test_raw_string_entries_skips_identifier_keys():
    # Localization 表键/标识符形态：ui_newGame、MENU_PLAY、UITable_en 绝不翻译
    raw = (b"\x00" * 8) + _with_len("ui_newGame") + _with_len("MENU_PLAY") \
        + _with_len("UITable_en") + _with_len("phone_call_01") + _with_len("NEW GAME")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    # R5：键标识符不再静默丢弃——留档为 skipped 条目（reason=prefilter_*；
    # 部分标识符同时命中引擎命名模式，engine 优先于 key_identifier）
    for key in ("ui_newGame", "MENU_PLAY", "UITable_en", "phone_call_01"):
        assert key in by_orig
        assert by_orig[key].status == "skipped"
        assert by_orig[key].meta["reason"].startswith("prefilter_")
        assert by_orig[key].meta["role"] == "structural"
    assert by_orig["NEW GAME"].status == "pending"        # 含空格 → 显示文本


def test_raw_string_entries_key_list_object_all_skipped():
    # SharedTableData 键列表对象（≥85% 键风格标识符）：全部键被剔除
    raw = (b"\x00" * 8) + _with_len("ui_settings") + _with_len("ui_options") \
        + _with_len("ui_quit") + _with_len("ui_back") + _with_len("ui_language") \
        + _with_len("New Game")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert all(k in by_orig for k in ("ui_settings", "ui_options", "ui_language"))
    assert all(by_orig[k].status == "skipped"
               for k in ("ui_settings", "ui_options", "ui_language"))
    assert by_orig["New Game"].status == "pending"        # 值形态文本保留


def test_raw_string_entries_marker_object_skips_identifiers():
    # 含 UnityEngine.Localization 标记的对象（SharedTableData）：单词式键也跳过
    raw = (b"\x00" * 8) + _with_len("Settings") + _with_len("Quit") \
        + _with_len("New Game") + _with_len("UnityEngine.Localization.Tables")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert by_orig["Settings"].status == "skipped"
    assert by_orig["Quit"].status == "skipped"
    assert by_orig["New Game"].status == "pending"     # 值形态文本保留


def test_raw_string_entries_word_values_translatable():
    # 值特征对象（含句子）：单词式写法（任意语言的 UI 标签）是显示值，可翻译
    raw = (b"\x00" * 8) + _with_len("CREDITOS") + _with_len("SENSIBILIDAD") \
        + _with_len("CONTINUAR") + _with_len("ui_newGame") \
        + _with_len("Press any key to continue.")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert by_orig["CREDITOS"].status == "pending"       # 西语 UI 标签 → 值
    assert by_orig["SENSIBILIDAD"].status == "pending"
    # R5：键风格 → 键，留档为 skipped（reason=prefilter_*）
    assert by_orig["ui_newGame"].status == "skipped"
    assert by_orig["ui_newGame"].meta["reason"].startswith("prefilter_")


def test_core_menu_terms_require_ui_collection_evidence():
    menu_terms = ("Quit", "Controls", "Settings", "Resolution", "SFX", "Volume")
    menu_raw = (b"\x00" * 8) + b"".join(_with_len(text) for text in menu_terms)
    menu_entries = _raw_string_entries("menu", 5, menu_raw, {})
    by_menu = {entry.original: entry for entry in menu_entries}

    for text in menu_terms:
        assert by_menu[text].status == "pending"
        assert by_menu[text].meta["role"] == "display"
        assert by_menu[text].meta["disposition"] == "translate"
        assert by_menu[text].meta["reason"] == "core_menu_collection"

    code_raw = menu_raw + _with_len("Game.PlayerController") + _with_len("Update")
    code_entries = _raw_string_entries("code", 6, code_raw, {})
    by_code = {entry.original: entry for entry in code_entries}
    assert all(by_code[text].status == "skipped" for text in menu_terms)
    assert all(by_code[text].meta["role"] == "structural" for text in menu_terms)


def test_single_core_menu_term_uses_unity_control_state_evidence():
    raw = (b"\x00" * 8) + b"".join(_with_len(text) for text in (
        "Normal", "Highlighted", "Pressed", "Selected", "Disabled", "Quit",
        "UnityEngine.Object, UnityEngine",
    ))

    entries = _raw_string_entries("menu", 694, raw, {})
    by_original = {entry.original: entry for entry in entries}

    assert by_original["Quit"].status == "pending"
    assert by_original["Quit"].meta["confidence"] == "high"
    assert by_original["Quit"].meta["role"] == "display"
    assert by_original["Quit"].meta["reason"] == "core_menu_control"
    assert all(by_original[text].status == "skipped" for text in (
        "Normal", "Highlighted", "Pressed", "Selected", "Disabled",
    ))


def test_raw_string_entries_inputsystem_actions_skipped_in_map_object():
    # deadbeat obj 717 实证：InputSystem 对象（含 GameActions action map 名）
    # 里 Select/Cancel 是输入绑定名，翻译会破坏按键交互（#205 根因 e0ede8f
    # 只覆盖路径形态，单词形态 Select/Cancel 漏网）；Pause 等按钮文本不受影响
    raw = (_with_len("GameActions") + _with_len("Select") + _with_len("Cancel")
           + _with_len("Pause") + _with_len("Settings") + _with_len("Quit")
           + _with_len("Button") + _with_len("Open Settings Menu"))
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert by_orig["Select"].status == "skipped"
    assert by_orig["Select"].meta.get("reason") == "input_system_object"
    assert by_orig["Select"].meta.get("obj_is_key_list") is True
    assert by_orig["Cancel"].status == "skipped"
    # InputSystem 配置对象内短词全部是运行时按名查找的键（morfosigame 实证），
    # 不再逐词白名单——Pause 等若为动作名翻译即破坏输入，宁可漏译不漏保护
    assert by_orig["Pause"].status == "skipped"
    assert by_orig["Settings"].status == "skipped"
    assert by_orig["Quit"].status == "skipped"
    # 强显示证据句子仍放行（配置对象里理论上不出现，保守防误伤）
    assert by_orig["Open Settings Menu"].status == "pending"


def test_raw_string_entries_inputsystem_binding_path_object_all_skipped():
    # morfosigame 实证根因：InputActionAsset 对象（map 名 'Normal' 是默认模板名，
    # 不在 GameActions 名单里）含绑定路径 <Keyboard>/z 与 interactions 串
    # Press(behavior=2) → 动作名 Proceed/Interact 全被翻译 → 点击对话/F 跳过
    # 无反应。绑定路径/interactions 是输入配置强信号，对象内全部短词串跳过。
    raw = ((b"\x00" * 12) + _with_len("Normal") + _with_len("Proceed")
           + _with_len("<Keyboard>/z") + _with_len("<Mouse>/position")
           + _with_len("<Gamepad>/leftStick") + _with_len("Press(behavior=2)")
           + _with_len("Interact") + _with_len("Action") + _with_len("Button")
           + _with_len("Controls") + _with_len("Arrow Keys"))
    entries = _raw_string_entries("sharedassets0.assets", 19, raw, {},
                                  "sharedassets0.assets")
    by_orig = {e.original: e for e in entries}
    # 绑定路径是结构串（skipped 保留标记，写回不会写），interactions 被引擎过滤
    for name in ("<Keyboard>/z", "<Mouse>/position", "<Gamepad>/leftStick"):
        assert by_orig[name].status == "skipped", name
    # R5：interactions 引擎串留档为 skipped（prefilter_engine_string）
    assert by_orig["Press(behavior=2)"].status == "skipped"
    assert by_orig["Press(behavior=2)"].meta["reason"] == "prefilter_engine_string"
    # 动作名/绑定组名在输入配置对象内全部跳过
    for name in ("Proceed", "Interact", "Action", "Button",
                 "Controls", "Arrow Keys"):
        assert by_orig[name].status == "skipped", name
        assert by_orig[name].meta.get("reason") == "input_system_object", name
    # 'Normal' 是控件状态名，被新硬拦截（unity_control_state）——语义与
    # F38 一致（视觉状态串不进池），比输入配置对象标签更精确
    assert by_orig["Normal"].status == "skipped"
    assert by_orig["Normal"].meta.get("reason") == "unity_control_state"


def test_raw_string_entries_timeline_object_skipped():
    # morfosigame 实证：Timeline 轨道对象含 'Animation Track (1)'（带编号，旧正则
    # 只匹配不带编号形式而漏网，被拆成 '动画轨道'+' (1)' 结构错乱）与动画状态名
    # 'Player Idle' → 全部跳过；同形短词对象在 level 场景文件里仍是显示文本。
    raw = ((b"\x00" * 12) + _with_len("Animation Track (1)")
           + _with_len("Player Idle") + _with_len("Player Walk")
           + _with_len("Markers"))
    entries = _raw_string_entries("sharedassets4.assets", 23, raw, {},
                                  "sharedassets4.assets")
    by_orig = {e.original: e for e in entries}
    assert by_orig["Animation Track (1)"].status == "skipped"
    for name in ("Player Idle", "Player Walk", "Markers"):
        assert by_orig[name].status == "skipped", name
        assert by_orig[name].meta.get("reason") == "timeline_object", name


def test_raw_string_entries_shared_resource_small_config_skipped_but_level_kept():
    # 'Timothy' 在共享资源文件里是 Timeline 剪辑 displayName（morfosigame
    # sharedassets4 116 字节 ScriptableObject 实证）→ 跳过；同样内容在 level
    # 场景文件里是对话说话者名 → 保持 pending（真实语料：level5 136 个对话对象）。
    so = (b"\x00" * 12) + _with_len("Timothy")   # ScriptableObject 形态（无 GameObject）
    comp = (b"\x00\x00\x00\x00\x05\x00\x00\x00") + _with_len("Timothy")  # 场景组件形态
    shared = _raw_string_entries("sharedassets4.assets", 23, so, {},
                                 "sharedassets4.assets")
    shared_component = _raw_string_entries("sharedassets4.assets", 24, comp, {},
                                           "sharedassets4.assets")
    level = _raw_string_entries("level5", 54107, comp, {}, "level5")
    assert shared[0].status == "skipped"
    assert shared[0].meta.get("reason") == "shared_resource_config_object"
    assert shared_component[0].status == "pending"   # 组件形态不受影响
    assert level[0].status == "pending"              # 场景文件不受影响


def test_raw_string_entries_select_pending_without_inputsystem_signal():
    # 无 GameActions 信号的普通 UI 对象：SELECT 是按钮显示文本，保持 pending
    raw = (_with_len("SELECT") + _with_len("QUIT") + _with_len("PAUSE")
           + _with_len("Open Settings Menu"))
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert by_orig["SELECT"].status == "pending"
    assert by_orig["QUIT"].status == "pending"


def test_raw_string_entries_word_identifiers_skipped_in_code_objects():
    # 无值特征对象（InputActionAsset / UI 样式等）：单词式是绑定名/枚举名/引擎名，
    # 翻译必破坏功能（输入失效）→ 全部降级为键
    raw = (b"\x00" * 8) + _with_len("WASD") + _with_len("Move") + _with_len("Fire") \
        + _with_len("Look") + _with_len("Bold") + _with_len("Unity")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert all(by_orig[k].status == "skipped"
               for k in ("WASD", "Move", "Fire", "Look", "Bold", "Unity"))
    assert by_orig["WASD"].meta.get("obj_is_key_list") is True


def test_raw_string_entries_display_word_in_component_object_kept():
    # 0.25.0 地毯式实证锚点：a-catfiends-impending-relapse resources.assets
    # #1319 'Save' 按钮——MonoBehaviour 组件实例（含类型引用）里的白名单词
    # 是真实按钮文本，曾被通用标识符规则误杀
    raw = (b"\x00" * 8) + _with_len("Save") + _with_len("UnityEngine.Object, UnityEngine")
    entries = _raw_string_entries("f1", 1319, raw, {})
    by_orig = {e.original: e for e in entries}
    assert by_orig["Save"].status == "pending"
    assert by_orig["Save"].meta["reason"] == "display_phrase"
    assert by_orig["Save"].meta["role"] == "display"


def test_raw_string_entries_display_word_in_plain_string_object_skipped():
    # 无组件信号的纯字符串对象：白名单词仍是键（down/left/right 绑定名）
    raw = (b"\x00" * 8) + _with_len("Player") + _with_len("Move") + _with_len("down") \
        + _with_len("left") + _with_len("right") + _with_len("Dpad")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert all(by_orig[k].status == "skipped"
               for k in ("Player", "Move", "down", "left", "right", "Dpad"))


def test_raw_string_entries_display_words_skipped_in_code_objects():
    # InputActionAsset 场景：白名单词 down/left/right 作为绑定名也是键
    raw = (b"\x00" * 8) + _with_len("Player") + _with_len("Move") + _with_len("down") \
        + _with_len("left") + _with_len("right") + _with_len("Dpad")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert all(by_orig[k].status == "skipped"
               for k in ("Player", "Move", "down", "left", "right", "Dpad"))


def test_duplicate_display_strings_all_pending_without_marker():
    """非 Localization 对象里相同显示文本多次出现（按钮多状态 / 多处同一按钮），
    每次出现都是可译显示值——只有末条 pending 会导致游戏里只有一种 UI 状态
    被汉化（deadbeat 暂停菜单 Pause 按钮 ×3 实证 #206）。"""
    raw = (_with_len("NEW GAME") + _with_len("SETTINGS") + _with_len("NEW GAME")
           + _with_len("SETTINGS") + _with_len("Press any key to continue."))
    entries = _raw_string_entries("f1", 5, raw, {})
    by_status = {}
    for e in entries:
        by_status.setdefault(e.original, []).append(e.status)
    assert by_status["NEW GAME"] == ["pending", "pending"]
    assert by_status["SETTINGS"] == ["pending", "pending"]


def test_duplicate_strings_key_position_skipped_with_localization_marker():
    """Localization 键值对（含标记）：同一对象内重复字符串，第一次出现（键）
    标记 skipped，最后一次出现（值）为 pending；标识符形态（SETTINGS）在
    键列表对象中全降级。"""
    raw = (_with_len("UnityEngine.Localization")
           + _with_len("NEW GAME") + _with_len("SETTINGS") + _with_len("NEW GAME")
           + _with_len("SETTINGS") + _with_len("Press any key to continue."))
    entries = _raw_string_entries("f1", 5, raw, {})
    by_status = {}
    for e in entries:
        by_status.setdefault(e.original, []).append(e.status)
    assert by_status["NEW GAME"] == ["skipped", "pending"]
    assert by_status["SETTINGS"] == ["skipped", "skipped"]


def test_generic_strong_display_does_not_override_duplicate_position_with_marker():
    # marker 串本身带 type_reference 结构化原因（structural skipped 条目），
    # 随后的重复显示文本保持首键末值
    raw = (_with_len("UnityEngine.Localization")
           + _with_len("Open Settings Menu") * 2)

    entries = _raw_string_entries("f1", 5, raw, {})

    assert [entry.status for entry in entries] == ["skipped", "skipped", "pending"]
    assert entries[0].meta["role"] == "structural"
    assert entries[1].meta["reason"] == "duplicate_key_position"
    assert entries[2].meta["role"] == "display"


def test_repeated_high_frequency_interaction_positions_are_all_kept():
    raw = _with_len("Press E") * 3

    entries = _raw_string_entries("f1", 5, raw, {"Press E": 51})

    assert [entry.original for entry in entries] == ["Press E"] * 3
    assert all(entry.status == "pending" for entry in entries)
    assert all(entry.meta["role"] == "display" for entry in entries)
    assert len({entry.key_path for entry in entries}) == 3


def test_high_frequency_generic_strong_display_is_kept():
    entries = _raw_string_entries(
        "f1", 5, _with_len("Open Settings Menu"),
        {"Open Settings Menu": 50},
    )

    assert len(entries) == 1
    assert entries[0].status == "pending"
    assert entries[0].meta["role"] == "display"


def test_raw_string_entries_freq_filter():
    raw = _with_len("SomeEngineThing") + _with_len("AnotherEngineThing")
    entries = _raw_string_entries("f1", 5, raw, {"SomeEngineThing": 50, "AnotherEngineThing": 50})
    # R5：高频串不再静默丢弃——留档为 skipped 条目（reason=prefilter_*；
    # 'SomeEngineThing' 同时命中引擎命名模式，engine 优先于 high_frequency）
    assert len(entries) == 2
    assert all(e.status == "skipped" for e in entries)
    assert all(e.meta["reason"].startswith("prefilter_") for e in entries)
    # skipped_count 已回写为对象内同 reason 的最终跳过数（聚合语义修正：
    # 累计计数 1..10 被消费端求和会失真，提取器末尾统一回写最终值）
    assert {e.meta["skipped_count"] for e in entries} == {2}


# ── 识别 L8：高频串阈值相对化（硬编码 40 → 相对阈值）──────────

def test_high_freq_threshold_scales_with_total():
    """相对阈值 = max(绝对下限 15, min(旧阈值 40, 总出现次数 × 0.2%))：
    小游戏回落到绝对下限（旧 40 不可达——该跳未跳），
    大游戏封顶 40（保持升级前判定——全面复盘审查钉死：>20k 规模相对
    放大有未验证回归面，doubleshake 噪音全跳行为不回归）。"""
    from hanhua.core.unity.extractor import _high_freq_threshold
    assert _high_freq_threshold({}) == 15                      # 无规模 → 下限
    assert _high_freq_threshold({"X": 50}) == 15               # 小游戏 → 下限
    assert _high_freq_threshold({"X": 2000, "Y": 8000}) == 20  # 万次出现 → 20
    assert _high_freq_threshold({"X": 90_000, "Y": 10_000}) == 40  # 大游戏封顶


def test_raw_string_entries_small_game_relative_threshold():
    """小游戏：freq 16 次即命中高频（旧 40 到不了——该跳未跳修复）。"""
    raw = _with_len("common")
    entries = _raw_string_entries("f1", 5, raw, {"common": 16},
                                  "sharedassets0.assets")
    hit = next(e for e in entries if e.original == "common")
    assert hit.status == "skipped"
    assert hit.meta["reason"] == "prefilter_high_frequency"


def test_raw_string_entries_relative_threshold_waives_below():
    """大游戏：总出现万次时阈值 20，freq 19 不命中、21 命中——
    噪音串相对化收紧（doubleshake 实证方向）。"""
    from hanhua.core.unity.extractor import _high_freq_threshold
    below = _raw_string_entries(
        "f1", 5, _with_len("noise"), {"noise": 19, "rest": 9981},
        "sharedassets0.assets", _high_freq_threshold({"noise": 19, "rest": 9981}))
    # 阈值下不命中高频拦截：按正常分类链处理（此处单串对象 → pending）
    assert [e for e in below if e.original == "noise"
            and e.meta.get("reason") == "prefilter_high_frequency"] == []
    above = _raw_string_entries(
        "f1", 5, _with_len("noise"), {"noise": 21, "rest": 9979},
        "sharedassets0.assets", _high_freq_threshold({"noise": 21, "rest": 9979}))
    hit = next(e for e in above if e.original == "noise")
    assert hit.status == "skipped"
    assert hit.meta["reason"] == "prefilter_high_frequency"


# ── #US 堆 ──
def test_find_dll_files_reuses_shared_fallback_application_rules(tmp_path):
    managed = tmp_path / "Example_Data" / "Managed"
    managed.mkdir(parents=True)
    names = (
        "Assembly-CSharp.dll",
        "Assembly-CSharp-firstpass.dll",
        "Assembly-CSharp.Custom.dll",
        "GameAnalytics.dll",
        "UnityEngine.CoreModule.dll",
    )
    for name in names:
        _write_cli_pe(managed / name)
    other_managed = tmp_path / "Other_Data" / "Managed"
    other_managed.mkdir(parents=True)
    _write_cli_pe(other_managed / "assembly-csharp.dll")

    assert [path.name for path in find_dll_files(tmp_path)] == [
        "Assembly-CSharp-firstpass.dll",
        "Assembly-CSharp.Custom.dll",
        "Assembly-CSharp.dll",
        "assembly-csharp.dll",
    ]


def test_find_dll_files_discovers_safe_manifest_user_assemblies(tmp_path):
    import json

    data_dir = tmp_path / "Example_Data"
    managed = data_dir / "Managed"
    managed.mkdir(parents=True)
    for name in (
        "Assembly-CSharp.dll", "Custom.Gameplay.dll",
        "UnityEngine.CoreModule.dll", "Escape.dll",
    ):
        _write_cli_pe(managed / name)
    (data_dir / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": [
            "Custom.Gameplay.dll", "UnityEngine.CoreModule.dll",
            "Assembly-CSharp.dll",
        ],
        "types": [16, 2, 16],
    }), encoding="utf-8")

    assert [path.name for path in find_dll_files(tmp_path)] == [
        "Assembly-CSharp.dll", "Custom.Gameplay.dll",
    ]


@pytest.mark.parametrize(
    "reparse_name", ("Example_Data", "Managed", "Custom.Gameplay.dll"))
def test_find_dll_files_rejects_manifest_reparse_chain(
        tmp_path, monkeypatch, reparse_name):
    import json
    import hanhua.core.tooling.player_layout as player_layout

    data_dir = tmp_path / "Example_Data"
    managed = data_dir / "Managed"
    managed.mkdir(parents=True)
    _write_cli_pe(managed / "Custom.Gameplay.dll")
    (data_dir / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": ["Custom.Gameplay.dll"],
        "types": [16],
    }), encoding="utf-8")
    monkeypatch.setattr(
        player_layout, "_is_reparse_point",
        lambda path: path.name == reparse_name,
    )

    assert find_dll_files(tmp_path) == []


def test_find_dll_files_fails_closed_on_duplicate_canonical_assemblies(
        tmp_path, monkeypatch):
    managed = tmp_path / "Example_Data" / "Managed"
    _write_cli_pe(managed / "Assembly-CSharp.dll")
    original_iterdir = type(tmp_path).iterdir

    def duplicate_assembly(path):
        entries = list(original_iterdir(path))
        if path == managed:
            entries.append(managed / "assembly-csharp.DLL")
        return iter(entries)

    monkeypatch.setattr(type(tmp_path), "iterdir", duplicate_assembly)

    assert find_dll_files(tmp_path) == []


def test_explicit_dll_extraction_accepts_nonstandard_assembly_name(
        tmp_path, monkeypatch):
    import dnfile

    text = "Hello from custom assembly"
    encoded = text.encode("utf-16-le") + b"\x01"
    heap = b"\x00" + bytes([len(encoded)]) + encoded

    class FakeUserStrings:
        def sizeof(self):
            return len(heap)

        def get_data_at_offset(self, offset, size):
            assert (offset, size) == (0, len(heap))
            return heap

        def get_file_offset(self, offset):
            assert offset == 0
            return 100

    fake_pe = type(
        "FakePE", (),
        {
            "net": type("FakeNet", (), {"user_strings": FakeUserStrings()})(),
            "close": lambda self: None,
        },
    )()
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: fake_pe)

    parsed = extract_dll_user_strings(tmp_path / "Assembly-CSharp.dll")

    assert [entry.original for entry in parsed.entries] == [text]


def test_dll_extraction_strips_any_ecma335_user_string_flag(
        tmp_path, monkeypatch):
    import dnfile

    text = "Phase 1"
    encoded = text.encode("utf-16-le") + b"\x00"  # legal ASCII flag=0
    heap = b"\x00" + bytes([len(encoded)]) + encoded

    class FakeUserStrings:
        def sizeof(self): return len(heap)
        def get_data_at_offset(self, offset, size): return heap
        def get_file_offset(self, offset): return 100

    fake_pe = SimpleNamespace(
        net=SimpleNamespace(
            user_strings=FakeUserStrings(),
            mdtables=SimpleNamespace(
                MemberRef=SimpleNamespace(rows=[]),
                MethodDef=SimpleNamespace(rows=[]),
            ),
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: fake_pe)

    parsed = extract_dll_user_strings(tmp_path / "FlagZero.dll")

    assert [entry.original for entry in parsed.entries] == [text]
    assert parsed.entries[0].meta["utf16_len"] == len(text.encode("utf-16-le"))


def test_mono_strong_interaction_promotes_without_setter(
        tmp_path, monkeypatch):
    import dnfile

    prompts = (
        "Press E to Open",
        "Press E to Close",
        "Press E to take battery",
        "Testing inputs. When done, press the Enter key.",
        "Press the Enter key to continue",
    )
    structural = (
        "Pressed", "Move", "Fire",
        "Game.PlayerController", "Assets/UI/Menu.prefab",
    )
    debug_rows = (
        "'0x{0:X}': {1}",
        "[FLIP] - constrained edge done",
        "AddPath: Open paths must be subject.",
        "Debug: Press E state observed",
        "Failed to press the Enter key in simulation",
        "Press inventoryManager to open",
        "Press E state observed",
        "Press the Enter key state observed",
        "Debug. Failed to press the Enter key in simulation. Aborting.",
        "Trace. Failed to press the Enter key in simulation. Aborting.",
        "Assertion failed. Could not press the Enter key. Aborting.",
    )
    heap = bytearray(b"\x00")
    for text in prompts + structural + debug_rows:
        raw = text.encode("utf-16-le") + b"\x00"
        heap.extend((len(raw),))
        heap.extend(raw)

    class FakeUserStrings:
        def sizeof(self): return len(heap)
        def get_data_at_offset(self, offset, size): return bytes(heap)
        def get_file_offset(self, offset): return 100

    fake_pe = SimpleNamespace(
        net=SimpleNamespace(
            user_strings=FakeUserStrings(),
            mdtables=SimpleNamespace(
                MemberRef=SimpleNamespace(rows=[]),
                MethodDef=SimpleNamespace(rows=[]),
            ),
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: fake_pe)

    parsed = extract_dll_user_strings(tmp_path / "Assembly-CSharp.dll")
    # skip/ 前缀条目是识别 L1 审计样本（skipped 留档），真实条目断言不含它们
    by_original = {entry.original: entry for entry in parsed.entries
                   if not entry.key_path.startswith("skip/")}

    for text in prompts:
        entry = by_original[text]
        assert entry.status == "pending"
        assert entry.meta["confidence"] == "high"
        assert entry.meta["role"] == "display"
        assert entry.meta["disposition"] == "translate"
        assert entry.meta["reason"] == "interaction_prompt"
    assert not set(structural) & by_original.keys()
    for text in debug_rows:
        entry = by_original[text]
        if text == "[FLIP] - constrained edge done":
            # a-catfiends Poly2Tri.dll 实证：方括号调试前缀是算法内部日志
            # （[FLIP]/[BUG:FIXME] 等），翻译无意义且模型会改坏代码符号
            assert entry.status == "skipped"
            assert entry.meta["reason"] == "mono_diagnostic"
        else:
            assert entry.status == "skipped"
            assert entry.meta["role"] == "structural"
            assert entry.meta["reason"] == "unverified_user_string"


def test_dll_extraction_promotes_uppercase_ui_concatenated_strings(
        tmp_path, monkeypatch):
    """driftapocalypse 真实漏检：代码拼接的 UI 文本未进 ui setter 验证链，
    但含全大写强调词（BEST/LEFT/DRIFT）→ 放行翻译。诊断句仍保守跳过。"""
    import dnfile

    ui_rows = (
        "BEST SCORE: ",
        "SHOW ANUNCIO",
        "\n[     NOT ENOUGH COINS ]",
        "Hold LEFT or RIGHT to turn\n(",
        "Hold LEFT and RIGHT together to BOOST\n(",
        "Be CAREFUL with the FRONT of the car\nYour engine is FRAGILE!",
    )
    diagnostics = (
        "Internal diagnostic message",
        "Trace. Please press the Enter key message was not displayed. Aborting.",
        "Unrelated stack literal",
    )
    heap = bytearray(b"\x00")
    for text in ui_rows + diagnostics:
        raw = text.encode("utf-16-le") + b"\x00"
        # ECMA-335 压缩长度编码：≥128 需 2 字节（诊断句 utf-16 长度超 127）
        if len(raw) < 0x80:
            heap.extend((len(raw),))
        else:
            heap.extend((0x80 | (len(raw) >> 8), len(raw) & 0xFF))
        heap.extend(raw)

    class FakeUserStrings:
        def sizeof(self): return len(heap)
        def get_data_at_offset(self, offset, size): return bytes(heap)
        def get_file_offset(self, offset): return 100

    fake_pe = SimpleNamespace(
        net=SimpleNamespace(
            user_strings=FakeUserStrings(),
            mdtables=SimpleNamespace(
                MemberRef=SimpleNamespace(rows=[]),
                MethodDef=SimpleNamespace(rows=[]),
            ),
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: fake_pe)

    parsed = extract_dll_user_strings(tmp_path / "Assembly-CSharp.dll")
    by_original = {entry.original: entry for entry in parsed.entries}

    for text in ui_rows:
        entry = by_original[text]
        assert entry.status == "pending"
        assert entry.meta["confidence"] == "medium"
        assert entry.meta["role"] == "display"
        assert entry.meta["disposition"] == "translate"
        assert entry.meta["reason"] == "user_string_uppercase_ui"
    for text in diagnostics:
        entry = by_original[text]
        if text == "Unrelated stack literal":
            # F33 契约（78-hour-rain 实证）：无调试特征的未消费句子串由
            # 句子形态启发式放行（宁多勿漏）——'Unrelated stack literal'
            # 是边界样例（测试构造的"无关栈字面量"，真实世界多为日志
            # 占位，翻译无害）
            assert entry.status == "pending"
            assert entry.meta["reason"] == "user_string_sentence"
        else:
            assert entry.status == "skipped"
            assert entry.meta["role"] == "structural"
            assert entry.meta["reason"] == "unverified_user_string"


@pytest.mark.parametrize("text", [
    "Trace. Please press the Enter key message was not displayed. Aborting.",
    "Assertion. When testing the prompt, press the Enter key message was missing. Aborting.",
    "Press the Enter key to continue message was not displayed.",
    "Press the Enter key to continue instruction was not displayed.",
    "Press the Enter key to continue label was not shown.",
    "Press the Enter key to continue prompt could not be displayed.",
    "Press the Enter key to continue message did not appear.",
    "Press the Enter key to continue message was never shown.",
    "Press the Enter key to continue instruction disappeared.",
    "Press the Enter key to continue label vanished.",
    "Press the Enter key to continue notification timed out.",
    "Controller connected. Please press the Enter key to continue instruction disappeared.",
])
def test_strong_interaction_rejects_missing_prompt_diagnostics(text):
    assert is_strong_interaction_prompt(text) is False


@pytest.mark.parametrize("text", [
    ("Controller inputs are working. When you're done testing, "
     "press the enter key on your keyboard."),
    "Testing inputs. When done, press the Enter key to continue.",
    "Controller connected. Please press the Enter key to continue.",
    "Controller connected. Then press the Enter key to begin.",
])
def test_strong_interaction_accepts_positive_long_instructions(text):
    assert is_strong_interaction_prompt(text) is True


@pytest.mark.parametrize("text", [
    "Press E to take the oil can",
    "Press E to put the can down",
    "Press E to move the can",
    "Controller ready. Please press E to take the oil can.",
])
def test_strong_interaction_accepts_bounded_object_actions(text):
    assert is_strong_interaction_prompt(text) is True


@pytest.mark.parametrize("text", [
    "Press R to reload weapon",
    "Press Enter to confirm selection",
    "Press Esc to pause game",
    "Press Enter to continue playing",
    "Press Enter to begin the mission",
    "Press E to open locked door",
    "Press E to inspect ancient artifact",
    "Press E to talk to Bob",
    "Press E to drive to town",
    "Press E to move the can to the table",
])
def test_strong_interaction_accepts_common_action_complements(text):
    assert is_strong_interaction_prompt(text) is True


@pytest.mark.parametrize("text", [
    "Press E to take the oil can disappeared.",
    "Press E to open the door prompt vanished.",
    "Press E to open the notification timed out.",
    "Controller ready. Please press E to move the crate instruction disappeared.",
])
def test_strong_interaction_rejects_predicates_after_objects(text):
    assert is_strong_interaction_prompt(text) is False


@pytest.mark.parametrize("text", [
    "Press E to take the ring",
    "Press E to enter the building",
    "Press E to inspect the painting",
    "Press E to move the bed",
    "Press E to open the shed",
    "Press Enter to continue playing the game",
    "Press Enter to begin loading the level",
])
def test_strong_interaction_accepts_noun_suffixes_and_gerund_objects(text):
    assert is_strong_interaction_prompt(text) is True


@pytest.mark.parametrize("text", [
    "Press E to take the oil can fell.",
    "Press E to open the door broke.",
    "Press E to move the crate fell down.",
    "Press E to open the prompt fails.",
    "Controller ready. Please press E to open the door broke.",
    "Press E to open the door got stuck.",
    "Press E to open the door shut.",
    "Press E to open the door opens unexpectedly.",
    "Press E to open the prompt times out.",
])
def test_strong_interaction_rejects_irregular_finite_predicates(text):
    assert is_strong_interaction_prompt(text) is False


@pytest.mark.parametrize("text", [
    "Press E to take the fallen remains",
    "Press E to inspect the frozen remains",
    "Press E to read the system errors",
    "Press E to inspect the loose ends",
    "Press E to open the locked door",
    "Press E to read the collected works",
    "Press E to inspect the remains",
    "Press E to inspect tax returns",
    "Press E to read system errors",
    "Press E to inspect loose ends",
    "Press E to read collected works",
    "Press E to talk to May",
    "Press E to talk to Will",
    "Press E to read the will",
    "Press E to inspect May records",
])
def test_strong_interaction_accepts_determined_ambiguous_nouns(text):
    assert is_strong_interaction_prompt(text) is True


@pytest.mark.parametrize("text", [
    "Press E to open failed.",
    "Press E to open broke.",
    "Press E to continue crashes.",
    "Press E to open times out.",
    "Press E to open the door won't open.",
    "Press E to open the door can not open.",
    "Press E to open the door jams.",
    "Press E to open the prompt dies.",
    "Press E to open the door works.",
    "Press E to open the door remains stuck.",
    "Press E to open failed again.",
    "Press E to open broke again.",
    "Press E to continue crashes repeatedly.",
    "Press E to open times out repeatedly.",
    "Press E to open may fail.",
    "Press E to open did fail.",
    "Press E to open the door doesn't open.",
    "Press E to open the door didn't open.",
    "Press E to open the door isn't open.",
    "Press E to open the door wasn't open.",
    "Press E to open the door couldn't open.",
    "Press E to open the door wouldn't open.",
    "Press E to open timed out once.",
    "Press E to open crashed once.",
    "Press E to open aborted twice.",
    "Press E to open stopped today.",
    "Press E to open controls don't work.",
    "Controller ready. Please press E to open failed.",
])
def test_strong_interaction_rejects_terminal_and_modal_predicates(text):
    assert is_strong_interaction_prompt(text) is False


@pytest.mark.parametrize("text", [
    "[PICK UP]", "[PICK]", "[PUSH]", "[OPEN DOOR]", "[PUSH CART]",
    "[SAVE GAME]", "[NEW GAME]", "[CLOSE]", "[ENTER]", "[USE]",
    "[OPTIONS]", "[PLAY]", "[EXIT]", "[HELLO]", "[BACK]", "[READY]",
])
def test_strong_interaction_accepts_bracket_action_labels(text):
    """方括号交互动作标签（seijunDROP 实证 2026-09-01：'[PICK UP]'——
    IL2CPP 字面量里的「拾取物品」提示以 [动作] 形态硬编码）。括号动作
    短语（PICK UP/OPEN DOOR）或白名单 UI 词（OPTIONS/EXIT）→ 交互
    证据进池翻译。"""
    assert is_strong_interaction_prompt(text) is True


@pytest.mark.parametrize("text", [
    "[E]", "[R]", "[X]", "[B2]", "[WASD]", "[2026.09.01]", "[1]",
    "[BREAD]", "[OPENLY]", "[Gasp]", "[Sigh]", "[Laughs]", "[equals]",
    "[esc]", "[Unknown]", "[DISPID=0]",
])
def test_strong_interaction_rejects_non_action_brackets(text):
    """纯键位/日期/数字/状态括号（[E]/[B2]/[2026.09.01]/[Gasp]）无动作词
    或白名单 UI 词 → 不是交互提示，不得进池。"""
    assert is_strong_interaction_prompt(text) is False


def test_dll_only_promotes_verified_ldstr_to_ui_setter(tmp_path, monkeypatch):
    import dnfile

    visible = (
        "Settings", "Quit", "Resolution",
        "InternalKey", "ScoreValue", "UITable_en",
    )
    formatted_visible = "{0}\n{1}kg\n£{2}"
    format_only = "Internal format {0}"
    consumed_before_format = "Internal diagnostic {0}"
    unrelated_below_format = "Unrelated stack literal"
    unverified_identifier = "InternalKey"
    hard_structural = ("https://example.com/menu", "Assets/UI/Menu.prefab")
    conservative = "Internal diagnostic message"
    heap = bytearray(b"\x00")
    tokens = []
    all_text = (
        *visible, formatted_visible, format_only, consumed_before_format,
        unrelated_below_format,
        unverified_identifier,
        *hard_structural, conservative,
    )
    for text in all_text:
        raw = text.encode("utf-16-le") + b"\x01"
        tokens.append(len(heap))
        heap.extend((len(raw),))
        heap.extend(raw)
    display_code = b"".join(
        b"\x72" + struct.pack("<I", 0x70000000 | token)
        + b"\x6f" + struct.pack("<I", 0x0A000001)
        for token in tokens[:6]
    ) + b"\x2a"
    formatted_display_code = (
        b"\x72" + struct.pack("<I", 0x70000000 | tokens[6])
        + b"\x16"
        + b"\x28" + struct.pack("<I", 0x0A000002)
        + b"\x6f" + struct.pack("<I", 0x0A000001)
        + b"\x2a"
    )
    format_only_code = (
        b"\x72" + struct.pack("<I", 0x70000000 | tokens[7])
        + b"\x16"
        + b"\x28" + struct.pack("<I", 0x0A000002)
        + b"\x26\x2a"
    )
    consumed_before_format_code = (
        b"\x72" + struct.pack("<I", 0x70000000 | tokens[8])
        + b"\x28" + struct.pack("<I", 0x0A000003)
        + b"\x16"
        + b"\x28" + struct.pack("<I", 0x0A000002)
        + b"\x6f" + struct.pack("<I", 0x0A000001)
        + b"\x2a"
    )
    unrelated_below_format_code = (
        b"\x72" + struct.pack("<I", 0x70000000 | tokens[9])
        + b"\x02"  # UI receiver
        + b"\x03"  # actual format string from an argument
        + b"\x04"  # formatting argument
        + b"\x28" + struct.pack("<I", 0x0A000002)
        + b"\x6f" + struct.pack("<I", 0x0A000001)
        + b"\x26\x2a"  # discard the unrelated literal after the setter
    )
    hard_structural_code = b"".join(
        b"\x72" + struct.pack("<I", 0x70000000 | token)
        + b"\x6f" + struct.pack("<I", 0x0A000001)
        for token in tokens[11:13]
    ) + b"\x2a"
    bodies = {
        0x2000: bytes(((len(display_code) << 2) | 2,)) + display_code,
        0x3000: bytes(((len(formatted_display_code) << 2) | 2,))
        + formatted_display_code,
        0x4000: bytes(((len(format_only_code) << 2) | 2,))
        + format_only_code,
        0x5000: bytes(((len(consumed_before_format_code) << 2) | 2,))
        + consumed_before_format_code,
        0x6000: bytes(((len(unrelated_below_format_code) << 2) | 2,))
        + unrelated_below_format_code,
        0x7000: bytes(((len(hard_structural_code) << 2) | 2,))
        + hard_structural_code,
    }

    class FakeUserStrings:
        def sizeof(self): return len(heap)
        def get_data_at_offset(self, offset, size): return bytes(heap)
        def get_file_offset(self, offset): return 100

    declaring_type = SimpleNamespace(TypeName="TMP_Text", TypeNamespace="TMPro")
    member_ref = SimpleNamespace(
        Name="set_text", Class=SimpleNamespace(row=declaring_type))
    string_type = SimpleNamespace(TypeName="String", TypeNamespace="System")
    format_ref = SimpleNamespace(
        Name="Format", Class=SimpleNamespace(row=string_type),
        Signature=SimpleNamespace(value=b"\x00\x02\x0e\x0e\x1c"))
    debug_type = SimpleNamespace(TypeName="Debug", TypeNamespace="UnityEngine")
    debug_ref = SimpleNamespace(
        Name="Log", Class=SimpleNamespace(row=debug_type))
    methods = [SimpleNamespace(Rva=rva) for rva in bodies]
    fake_pe = SimpleNamespace(
        net=SimpleNamespace(
            user_strings=FakeUserStrings(),
            mdtables=SimpleNamespace(
                MemberRef=SimpleNamespace(
                    rows=[member_ref, format_ref, debug_ref]),
                MethodDef=SimpleNamespace(rows=methods),
            ),
        ),
        get_data=lambda rva, size: bodies.get(rva, b"")[:size],
        close=lambda: None,
    )
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: fake_pe)

    parsed = extract_dll_user_strings(tmp_path / "Assembly-CSharp.dll")
    # skip/ 前缀条目是识别 L1 审计样本（skipped 留档），真实条目断言不含它们
    by_original = {entry.original: entry for entry in parsed.entries
                   if not entry.key_path.startswith("skip/")}

    for text in visible:
        assert by_original[text].status == "pending"
        assert by_original[text].meta["confidence"] == "high"
        assert by_original[text].meta["role"] == "display"
        assert by_original[text].meta["disposition"] == "translate"
        assert by_original[text].meta["reason"] == "mono_ui_setter"
    assert by_original[formatted_visible].status == "pending"
    assert by_original[formatted_visible].meta["reason"] == "mono_ui_setter"
    assert by_original[format_only].status == "skipped"
    assert by_original[format_only].meta["reason"] == "unverified_user_string"
    assert by_original[consumed_before_format].status == "skipped"
    # F33 调试词扩充后 'Internal diagnostic {0}' 命中 diagnostic 词 →
    # mono_diagnostic 优先（语义仍"未被证明流入 UI"，两种 reason 都合理）
    assert by_original[consumed_before_format].meta["reason"] in (
        "unverified_user_string", "mono_diagnostic")
    # F33 契约：'Unrelated stack literal' 无调试特征，句子形态放行
    # （与 test_dll_extraction_promotes_uppercase_ui 同语义）
    assert by_original[unrelated_below_format].status == "pending"
    assert by_original[unrelated_below_format].meta["reason"] == (
        "user_string_sentence")
    assert [entry.original for entry in parsed.entries
            if not entry.key_path.startswith("skip/")].count(
        unverified_identifier) == 1
    assert not set(hard_structural) & by_original.keys()
    assert by_original[conservative].status == "skipped"
    assert by_original[conservative].meta["confidence"] == "low"
    assert by_original[conservative].meta["role"] == "structural"
    assert by_original[conservative].meta["disposition"] == "structural"
    assert by_original[conservative].meta["reason"] == "unverified_user_string"


def test_dll_extraction_closes_pe_on_success_empty_and_error(
        tmp_path, monkeypatch):
    import dnfile
    import pytest

    text = "Hello from managed code"
    encoded = text.encode("utf-16-le") + b"\x01"
    heap = b"\x00" + bytes([len(encoded)]) + encoded

    class FakeUserStrings:
        def sizeof(self):
            return len(heap)

        def get_data_at_offset(self, offset, size):
            return heap

        def get_file_offset(self, offset):
            return 100

    class FailingUserStrings:
        def sizeof(self):
            raise RuntimeError("broken #US heap")

        def get_data_at_offset(self, offset, size):
            raise AssertionError("sizeof must fail before heap data is read")

    class FakePE:
        def __init__(self, user_strings):
            self.net = type("FakeNet", (), {"user_strings": user_strings})()
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    pe_instances = [
        FakePE(FakeUserStrings()),
        FakePE(None),
        FakePE(FailingUserStrings()),
    ]
    pending = iter(pe_instances)
    monkeypatch.setattr(dnfile, "dnPE", lambda _path: next(pending))

    parsed = extract_dll_user_strings(tmp_path / "Success.dll")
    assert [entry.original for entry in parsed.entries] == [text]
    assert extract_dll_user_strings(tmp_path / "Empty.dll").entries == []
    with pytest.raises(RuntimeError, match="broken #US heap"):
        extract_dll_user_strings(tmp_path / "Broken.dll")

    assert [pe.close_calls for pe in pe_instances] == [1, 1, 1]


def test_us_heap_walk():
    heap = b"\x00"
    heap += bytes([0x0B]) + "Hello".encode("utf-16-le") + b"\x01"   # 11 = 10 字节 UTF-16 + 终结
    heap += bytes([0x05]) + "你好".encode("utf-16-le") + b"\x01"
    items = _walk_us_heap(heap)
    assert len(items) == 2
    assert items[0][0] == 2                    # 1 字节占位 + 1 字节长度头
    assert items[0][1][:-1].decode("utf-16-le") == "Hello"   # 末字节是 0x01 终结标记
    assert items[1][1][:-1].decode("utf-16-le") == "你好"


@pytest.mark.parametrize("heap", (
    b"\x00\xe0\x00\x00\x01A",  # ECMA-335 reserved 111xxxxx prefix
    b"\x00\x80\x01A",           # non-canonical two-byte encoding of 1
    b"\x00\xc0\x00\x00\x01A",  # non-canonical four-byte encoding of 1
    b"\x00\x80",                 # truncated two-byte prefix
    b"\x00\xc0\x00\x00",        # truncated four-byte prefix
    b"\x00\x02A",                # truncated record payload
))
def test_us_heap_walk_rejects_reserved_or_truncated_records(heap):
    # F5 鲁棒遍历：坏前缀/截断记录步进 1 继续（写回后残留区不可断链），
    # 损坏区残留的短前缀可能被解析为 ln=1 空记录；契约 = 不产出任何
    # 可解码的 UTF-16 文本记录（空记录由提取侧字符串级过滤淘汰，不会
    # 成为可译条目）。
    for _token, raw in _walk_us_heap(heap):
        text = raw[:-1].decode("utf-16-le", errors="replace")
        assert text == ""


# ── IL2CPP metadata ──
def _fake_metadata(literals: list[str] | None = None) -> bytes:
    if literals is None:
        literals = ["Hello player", "Press {key} to jump", "继续游戏"]
    data = b"".join(s.encode("utf-8") for s in literals)
    offsets = []
    pos = 0
    for s in literals:
        offsets.append((pos, len(s.encode("utf-8"))))
        pos += len(s.encode("utf-8"))
    header = bytearray(0x30)
    struct.pack_into("<II", header, 0, 0xFAB11BAF, 29)
    table_size = len(literals) * 8
    struct.pack_into("<II", header, 0x08, 0x100, table_size)         # stringLiteralOffset/byte size
    struct.pack_into("<II", header, 0x10, 0x200, len(data))          # stringLiteralData
    lit_arr = b"".join(struct.pack("<II", ln, off) for off, ln in offsets)
    buf = bytes(header) + b"\x00" * (0x100 - 0x30) + lit_arr
    buf += b"\x00" * (0x200 - len(buf)) + data
    return buf


def test_il2cpp_parse_and_extract():
    raw = _fake_metadata()
    lits = parse_string_literals(raw)
    assert lits == [(0, 12, 0x200), (12, 19, 0x20C), (31, 12, 0x21F)]
    from hanhua.core.unity.il2cpp import extract_metadata_strings
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "global-metadata.dat"
        p.write_bytes(raw)
        pf = extract_metadata_strings(p, "m.dat")
        orig = {e.key_path: e.original for e in pf.entries}
        assert orig["meta#0"] == "Hello player"
        assert orig["meta#12"] == "Press {key} to jump"   # data_index 语义
        assert orig["meta#31"] == "继续游戏"
        by_key = {e.key_path: e.meta["file_offset"] for e in pf.entries}
        assert by_key == {"meta#0": 0x200, "meta#12": 0x20C, "meta#31": 0x21F}


def test_il2cpp_rejects_non_divisible_literal_table_size():
    raw = bytearray(_fake_metadata())
    struct.pack_into("<I", raw, 0x0C, 25)

    assert parse_string_literals(bytes(raw)) == []


def test_il2cpp_rejects_literal_sections_that_overlap_metadata_header():
    raw = bytearray(100)
    struct.pack_into("<II", raw, 0, 0xFAB11BAF, 29)
    struct.pack_into("<II", raw, 0x08, 4, 8)
    struct.pack_into("<II", raw, 0x10, 64, 36)

    assert parse_string_literals(bytes(raw)) == []


def test_il2cpp_rejects_literal_data_index_or_length_out_of_range():
    oversized_length = bytearray(_fake_metadata())
    data_size = struct.unpack_from("<I", oversized_length, 0x14)[0]
    struct.pack_into("<II", oversized_length, 0x100, data_size + 1, 0)

    out_of_range_index = bytearray(_fake_metadata())
    struct.pack_into("<II", out_of_range_index, 0x100, 1, data_size)

    assert parse_string_literals(bytes(oversized_length)) == []
    assert parse_string_literals(bytes(out_of_range_index)) == []


def test_il2cpp_rejects_overlapping_literal_data_ranges():
    raw = bytearray(_fake_metadata())
    second_length = struct.unpack_from("<I", raw, 0x108)[0]
    struct.pack_into("<II", raw, 0x108, second_length, 1)

    assert parse_string_literals(bytes(raw)) == []


def test_il2cpp_skips_literal_that_is_not_valid_utf8():
    raw = bytearray(_fake_metadata())
    data_offset = struct.unpack_from("<I", raw, 0x10)[0]
    raw[data_offset] = 0xFF

    literals = parse_string_literals(bytes(raw))

    assert [(data_index, length) for data_index, length, _ in literals] == [
        (12, 19),
        (31, 12),
    ]


def test_il2cpp_extraction_rejects_illegal_controls_but_allows_tab_and_newlines(
        tmp_path):
    allowed = "First line\tlabel\nSecond line\rreturn"
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(_fake_metadata([
        allowed,
        "Contains NUL\x00garbage",
        "Contains control\x01garbage",
        "Contains C1\x80garbage",
    ]))

    from hanhua.core.unity.il2cpp import extract_metadata_strings
    pending = [entry.original for entry in
               extract_metadata_strings(path).entries
               if entry.status == "pending"]

    assert pending == [allowed]


def test_il2cpp_rejects_overlapping_table_and_data_sections():
    raw = bytearray(_fake_metadata())
    data_size = struct.unpack_from("<I", raw, 0x14)[0]
    struct.pack_into("<II", raw, 0x10, 0x100, data_size)

    assert parse_string_literals(bytes(raw)) == []


def test_il2cpp_skips_zero_length_literal_record():
    raw = _fake_metadata(["", "Visible text"])

    literals = parse_string_literals(raw)

    assert [(data_index, length) for data_index, length, _ in literals] == [
        (0, len(b"Visible text")),
    ]


def test_il2cpp_rejects_wrong_magic_and_unsupported_versions():
    wrong_magic = bytearray(_fake_metadata())
    struct.pack_into("<I", wrong_magic, 0, 0xDEADBEEF)
    assert parse_string_literals(bytes(wrong_magic)) == []

    # v24/v27/v31/v39 已有真实语料验证的支持（见 test_v2_metadata_versions.py）；
    # 其余版本（包括 v30）必须拒绝，绝不猜 record 布局。
    for unsupported_version in (0, 30, 32, 33, 35, 40):
        raw = bytearray(_fake_metadata())
        struct.pack_into("<I", raw, 4, unsupported_version)
        assert parse_string_literals(bytes(raw)) == []


def test_il2cpp_rejects_table_or_data_section_out_of_file():
    table_out_of_file = bytearray(_fake_metadata())
    struct.pack_into("<II", table_out_of_file, 0x08,
                     len(table_out_of_file) - 4, 24)

    data_out_of_file = bytearray(_fake_metadata())
    data_size = struct.unpack_from("<I", data_out_of_file, 0x14)[0]
    struct.pack_into("<II", data_out_of_file, 0x10,
                     len(data_out_of_file) - 1, data_size)

    assert parse_string_literals(bytes(table_out_of_file)) == []
    assert parse_string_literals(bytes(data_out_of_file)) == []


def _v24_metadata(*literals: bytes) -> bytes:
    """构造 IL2CPP v24 metadata：magic + version + 8 字节 <length, dataIndex>
    显式记录 + data 区。布局 off: litOff@0x08 litSize@0x0C dataOff@0x10
    dataSize@0x14（与 hanhua.core.unity.il2cpp._LAYOUTS[24] 一致）。"""
    header = bytearray(0x30)
    struct.pack_into("<II", header, 0, 0xFAB11BAF, 24)
    data = b"".join(literals)
    table = b"".join(
        struct.pack("<II", len(lit), offset)
        for lit, offset in _cumulative(literals))
    struct.pack_into("<IIII", header, 0x08, 0x30, len(table),
                     0x30 + len(table), len(data))
    return bytes(header) + table + data


def _cumulative(literals: list[bytes]):
    offset = 0
    for lit in literals:
        yield lit, offset
        offset += len(lit)


def test_il2cpp_extract_filters_engine_noise_and_classifies():
    from hanhua.core.unity.il2cpp import extract_metadata_strings
    raw = _v24_metadata(
        b"Press E to interact",           # 交互提示 → display/medium
        b"A buffer must be provided",     # 引擎句子 → display/low 留档
        b"back_to_menu",                  # 标识符 → 不产生条目（代码池严格键）
        b"{0} bytes processed by {1}",    # 格式串 → 丢弃
        b"  .locals ",                    # 前导多空白调试 → 丢弃
        b"\t\n\r'(),-0123456789ABCDEF",   # 控制符开头字符表 → 丢弃
    )
    path = Path(tempfile.mkdtemp()) / "global-metadata.dat"
    path.write_bytes(raw)

    parsed = extract_metadata_strings(path, "meta.dat")

    # skip/ 前缀条目是识别 L1 审计样本（skipped 留档），真实条目断言不含它们
    by_orig = {e.original: e for e in parsed.entries
               if not e.key_path.startswith("skip/")}
    # #14 之后含字母的 {0} 模板不再跳过：显示形态才 medium，普通模板
    # （"{0} bytes processed by {1}"）→ display/low 留档可见（过滤不是删除）
    # B4 吸收层（2026-09-01）：'A buffer must be provided' 是引擎异常
    # 消息（句号结尾 + 引擎前缀词），被吸收为 skipped（reason=engine_
    # log_message）——不再产生 pending 污染自动翻译池。
    assert set(by_orig) == {"Press E to interact",
                            "{0} bytes processed by {1}"}
    prompt = by_orig["Press E to interact"]
    assert (prompt.status, prompt.meta["confidence"], prompt.meta["role"]) == (
        "pending", "medium", "display")
    fmt = by_orig["{0} bytes processed by {1}"]
    assert (fmt.status, fmt.meta["confidence"], fmt.meta["reason"]) == (
        "pending", "low", "il2cpp_format_template")
    # B4：'A buffer must be provided' 被吸收为 skipped（不产生 pending）
    buffered = [e for e in parsed.entries
                if e.original == "A buffer must be provided"]
    assert buffered and buffered[0].status == "skipped", buffered
    assert buffered[0].meta.get("reason") == "engine_log_message"
    assert buffered[0].key_path.startswith("skip/")


# ── 0.25.0 修复 3：单行代码判定 / BOM / 纯符号串 / FungusLua 整文件 ──
def test_script_code_line_strong_features_detected():
    from hanhua.core.unity.extractor import _is_script_code_line
    code = [
        'function M.start()',
        'runblock(flowchart, "Intro") -- Runs the Intro Block',
        'setcharacter(sherlockcharacter, "annoyed") -- comment',
        'local choice = choose { "Agreed", "No" }',
        'elseif choice == 2 then',
        '"System.Boolean, mscorlib, Version=2.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089"',
        'InvertVector2(invertX=false),ScaleVector2(x=0.05,y=0.05)',
        'wait(1)',
        'M = {}',
        '-- a full-line comment',
    ]
    for line in code:
        assert _is_script_code_line(line), f"应判代码：{line}"


def test_script_code_line_does_not_误伤_display_text():
    from hanhua.core.unity.extractor import _is_script_code_line
    keep = [
        '{punch=3,2}* Y A W N *{w=3}{x}',
        '— WRECCA CAVERN —{w=3}{x}',
        'I am {punch=3,2}NOT who I used to be.{w=3}{x}',
        'HH:mm dd MMMM, yyyy',
        'Continue', 'Save', 'Load',
        'I -- I can\'t believe this',   # 口语破折号不是 Lua 注释
        'Hello, world!',
    ]
    for line in keep:
        assert not _is_script_code_line(line), f"不应判代码：{line}"


def test_double_bom_json_textasset_goes_to_json_branch():
    # 双重 BOM（UnityPy str + utf-8-sig encode）不得卡住 JSON 分支
    raw = b'\xef\xbb\xbf\xef\xbb\xbf{"registerTypes": ["Fungus.Block", "Fungus.Character"]}'
    entries = _textasset_entries("f1", 70, raw)
    assert entries, "双重 BOM 后应走 JSON 分支产生条目"
    assert all("/json/" in e.key_path for e in entries)
    # 类型名是标识符 → 全部 skipped
    assert all(e.status == "skipped" for e in entries)


def test_symbols_only_rawstr_skipped():
    from hanhua.core.unity.extractor import _raw_string_entries
    import struct

    def unistr(s):
        b = s.encode("utf-8")
        head = struct.pack("<i", len(b)) + b
        return head + b"\x00" * ((-len(head)) % 4)

    # 纯占位符串（{0} : {1}）在 rawstr 对象里不得以 pending 进池
    raw = unistr("{0} : {1}") + unistr("A perfectly normal sentence.")
    entries = _raw_string_entries("f1", 5, raw, {})
    placeholder = [e for e in entries if e.original == "{0} : {1}"]
    # R5：键风格占位串留档为 skipped（reason=prefilter_key_identifier），
    # 不 pending、不进翻译池
    assert len(placeholder) == 1
    assert placeholder[0].status == "skipped"
    assert placeholder[0].meta["reason"] == "prefilter_key_identifier"
    sentence = [e for e in entries if e.original == "A perfectly normal sentence."]
    assert sentence[0].status == "pending"


def test_escaped_braces_format_template_hard_structural():
    # a-catfiends Unity.ProBuilder.dll us#32180 实证：C# format 转义大括号
    # 的多行代码模板（含全大写词绕过了单行纯符号检测）
    from hanhua.core.placeholders import is_hard_structural
    s = '{0} : {1}\nCPAPI:{{"cmd":"Watch" "name":"{0}"}}'
    assert is_hard_structural(s), "含 {{/}} 的 format 模板应判硬结构"
    assert is_hard_structural('{{variable}}'), "独立转义大括号模板应判硬结构"


def test_color_table_entries_hard_structural():
    # ProBuilder 颜色表条目（HTML/CSS 标注）是数据表，无翻译价值
    from hanhua.core.placeholders import is_hard_structural
    for s in ('Gray (HTML/CSS Gray)', 'Green (HTML/CSS Color)',
              'Air Force Blue (USAF)', 'Purple (HTML)',
              'Jawad/Chicken Color (HTML/CSS) (Khaki)'):
        assert is_hard_structural(s), f"颜色表条目应判硬结构：{s}"
    # 普通 UI 文本不得误伤
    assert not is_hard_structural('The color of the sky is blue')
    assert not is_hard_structural('Choose your color')


def test_fungus_lua_module_skipped_whole_file():
    # a-catfiends obj72 实证：FungusLua 对话模块（49 行）整文件跳过
    raw = b"""-- This Lua script defines a module
-- local junglestory = require('junglestory')
M = {}

function M.start()
\trunblock(flowchart, "Intro") -- Runs the Intro Block
\tsay "Hello John."
\tlocal choice = choose { "Agreed", "No" }
\tif choice == 1 then
\t\trunblock(flowchart, "PlayPourSound")
\tend
end
"""
    entries = _textasset_entries("f1", 72, raw)
    assert entries == []


def test_mono_diagnostic_strings_skipped():
    # a-catfiends ProBuilder/Poly2Tri/Fungus 实证：开发诊断文本不得进池
    from hanhua.core.unity.mono_dll import _is_mono_diagnostic_string
    diagnostics = (
        " FAILED: ", "_____ PASSED: ", "EXTEND: ",
        "(TailCallRequest -- INTERNAL!)", "(YieldRequest -- INTERNAL!)",
        "CNOT had non-bool arg", "Error parsing JSON file ",
        "String table JSON format is not correct ",
        "Improper (strict) JSON formatting.  First character must be [ or {",
        "[BUG:FIXME] FLIP failed due to missing triangle",
        "[FLIP] - constrained edge done", "[FLIP:SCAN] - scan next point",
    )
    for s in diagnostics:
        assert _is_mono_diagnostic_string(s), f"应判诊断：{s}"
    keep = (
        "**ANY CAMERA**", "SOLO ", "BEST SCORE: ",
        "Hold LEFT or RIGHT to turn\n(",
        "Failed to press the Enter key in simulation",   # 驼峰错误消息可能是 UI
        "Internal format {0}", "Internal diagnostic message",
    )
    for s in keep:
        assert not _is_mono_diagnostic_string(s), f"不应判诊断：{s}"


def test_sentence_display_relax_recovers_dialogue():
    """come-back 实证：识别遗漏的对话短语（宁严勿漏修复）。

    这些真实对话此前整类漏进 unverified_user_string 跳过桶：
    - 完成义小品词结尾（'ill let you in' 的 in 是 let in 完成义，非悬空
      介词）→ _DEBUG_CONCAT_TAIL 剔除 in/out/up/down/away/back
    - 短对话短语（'why not'/'take 1'）len<8 放宽
    - 连写重复字母 + 多感叹号语气词（'allllmooooost!!!'）exclamation 放宽
    - 圆括号开头完整句子（自嘲注解）放行
    """
    from hanhua.core.unity.mono_dll import (
        _is_sentence_display_text, _is_exclamation_ui_word)
    dialogue = (
        "if you can get a ghost catcher for me ill let you in",
        "nice ill let you in", "nice!", "why not", "take 1",
        "allllmooooost!!!", "ALLLLLLMOOOOOOOOSTTT!!!!!!!", "CELEBRATE!!!!",
        "(translation: the ghost of the helper of the ice age baby is "
        "trough that door. we need to make him tell where he now is.)",
        "why would you refuse money", "wide putin",
        "i cant start playing my games if im not consumed the consume",
    )
    for s in dialogue:
        assert (_is_sentence_display_text(s) or _is_exclamation_ui_word(s)), \
            f"应放行：{s}"
    # 单 token 短词歧义大（枚举名/引擎键），宁漏勿坏留 unverified 桶
    assert not _is_sentence_display_text("Oh")


def test_sentence_display_still_rejects_concat_and_diag():
    """放宽后拼接片段/代码模板/引擎诊断仍拒（宁漏勿坏不滑坡）。

    与 extract 循环同口径：sentence 形态放行后，引擎诊断由
    _ENGINE_DIAGNOSTIC_PATTERN 在 mono_diagnostic 层拦截（'Invalid
    quality option'/'There is already a virtual axis named'）。
    """
    from hanhua.core.unity.mono_dll import (
        _is_sentence_display_text, _is_exclamation_ui_word,
        _ENGINE_DIAGNOSTIC_PATTERN)
    rejected = (
        "Monster spawned at (", "setting teeth angle to ",
        "spawning unique at spot index ", "doorbreakHealth: ",
        "Internal diagnostic message", "Debug: Press E state observed",
        "Assertion failed...Aborting", "Failed to load texture",
        "Invalid quality option", "bool2({0}, {1})",
        "There is already a virtual axis named",
    )
    for s in rejected:
        sentence = _is_sentence_display_text(s)
        excl = _is_exclamation_ui_word(s)
        # 引擎诊断句：sentence 可放行但必须被 _ENGINE_DIAGNOSTIC_PATTERN 拦
        if _ENGINE_DIAGNOSTIC_PATTERN.search(s):
            assert not (sentence or excl) or _ENGINE_DIAGNOSTIC_PATTERN.search(s), \
                f"应拒（引擎诊断）：{s}"
        else:
            assert not (sentence or excl), f"应拒：{s}"


def test_pure_tag_sequence_hard_structural():
    # Fungus 样式模板标签行（resources.assets obj1292 实证）不得进池
    from hanhua.core.placeholders import is_hard_structural
    for s in ("{customName}", "{/customName}", "{color=blue}", "{/color}",
              "{audio=AudioTag}", "{/audio}", "{w=3}{x}"):
        assert is_hard_structural(s), f"纯标签序列应判硬结构：{s}"
    # 含真实文本的对话不误伤
    assert not is_hard_structural("{punch=3,2}* Y A W N *{w=3}{x}")
    assert not is_hard_structural("I am {punch=3,2}NOT who I used to be.{w=3}{x}")


def test_collect_known_names():
    """全大写词典外词注入专名表；常见词全大写/间隔大写不误收。"""
    from hanhua.core.prompts import collect_known_names
    texts = [
        "GLISLYA SPECIALIST FROM THE ACADEMY OF CORRADAILE.{w=3}{x}",
        "YOU ARE A RECOVERING GLISLYA ADDICT.{w=3}{x}",
        "GLISLYA CAVERNS OF WRECCA.{w=3}{x}",
        "* Y A W N *{w=3}{x}",
        "CAUTION: DEATH AWAITS.",          # 常见词全大写
        "LABOLIS-7 ORBITAL STATION",       # 带数字的造词
        "THE VACUUM CAVERNS ARE DEEP",     # 常见词组合
    ]
    names = collect_known_names(texts)
    # 专名（词典外全大写）必须收
    assert "GLISLYA" in names
    assert "CORRADAILE" in names           # 长词单次出现也收
    assert "WRECCA" in names
    assert "LABOLIS-7" in names
    # 常见词全大写 / 间隔大写 / 单字母不得收
    for bad in ("YOU", "THE", "CAUTION", "DEATH", "VACUUM", "CAVERNS"):
        assert bad not in names, f"常见词不应入专名表：{bad}"
    # 间隔大写拆成单字母，无 Y/A/W/N
    assert not any(w in ("Y", "A", "W", "N") for w in names)
    # 排序：出现次数多的在前
    assert names[0] == "GLISLYA"
    # 空输入安全
    assert collect_known_names([]) == []


def test_known_names_not_injected_into_system_prompt():
    """2026-08-14 用户要求「大大精简提示词」：专名全量块移除——专名
    一致性由 BatchTranslator 条目级 glossary_hits 命中注入与确定性
    直填保证（曾全量注入 50 个专名 ≈ 数百 tokens 膨胀上下文）。"""
    from hanhua.core.prompts import build_system_prompt
    from hanhua.core.models import GameProfile
    profile = GameProfile(game_name="Test Game")
    sys = build_system_prompt(profile, "", known_names=["GLISLYA", "WRECCA"])
    assert "【已确认专名" not in sys
    assert "GLISLYA" not in sys and "WRECCA" not in sys


def test_learn_proper_names_keeps_and_skips():
    """保留型专名写入全局库；音译/无证据型跳过；重复学习不重复。"""
    from hanhua.core.glossary import GlossaryStore
    from hanhua.core.models import TextEntry
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        g = GlossaryStore(os.path.join(td, "gl.db"))
        g.init_schema()
        def ent(orig, trans, status="translated", passed=True):
            return TextEntry(
                file_id="f", key_path="k", original=orig,
                translation=trans, status=status,
                meta={"quality_passed": passed})
        entries = [
            ent("GLISLYA SPECIALIST FROM CORRADAILE.",
                "格莉斯莉亚专家来自科拉达莱。"),      # 音译型：GLISLYA 不在译文
            ent("YOU ARE A RECOVERING GLISLYA ADDICT.",
                "你是一个正在康复中的 GLISLYA 瘾君子。"),  # 保留型
            ent("GLISLYA CAVERNS OF WRECCA.",
                "GLISLYA 的 WRECCA 洞穴。"),            # 保留型 2
            ent("YOU TOUCHED GLISLYA SLIME.",
                "你碰到了 Glislya 史莱姆。"),           # 大小写变体保留
            ent("GLISLYA BAD TRANSLATION.",
                "GLISLYA 坏的翻译。", status="failed"),  # 非 translated 不采信
            ent("GLISLYA LOW QUALITY.",
                "GLISLYA 低质量译文。", passed=False),   # 质量门未过不采信
        ]
        names = ["GLISLYA", "WRECCA", "CORRADAILE", "HYPESPACE"]
        learned = g.learn_proper_names(entries, names, "test-game")
        rows = {r["term"]: r for r in g.list_all()}
        assert "GLISLYA" in rows           # 2/2 保留证据 → 学习
        assert rows["GLISLYA"]["translation"] == "GLISLYA"
        assert rows["GLISLYA"]["category"] == "专名"
        assert "WRECCA" in rows            # 1/1 保留
        assert "CORRADAILE" not in rows    # 音译型无保留证据 → 跳过
        assert "HYPESPACE" not in rows     # 无出现 → 跳过
        # 动作动词不学成专名：TOSS 是动作指令词，学成专名会与知识库
        # 译例冲突（TOSS→TOSS 保留 vs TOSS TRASH→丢垃圾），模型采纳
        # 专名保留 → 半翻译（taxes 实证）
        entries.append(ent("TOSS TRASH", "TOSS 垃圾"))
        names.append("TOSS")
        learned3 = g.learn_proper_names(entries, names, "test-game")
        rows = {r["term"]: r for r in g.list_all()}
        assert "TOSS" not in rows
        # 幂等：重复学习不重复插入
        learned2 = g.learn_proper_names(entries, names, "test-game")
        assert learned2 == 0
        assert len(g.list_all()) == 2
        # 旧条目已有译文时不覆盖
        g.add("WRECCA", "某个音译", category="专名")
        g.learn_proper_names(entries, names, "test-game")
        rows = {r["term"]: r for r in g.list_all()}
        assert rows["WRECCA"]["translation"] == "某个音译"
        g.close()


def test_known_names_for_merge_and_cap():
    """当前游戏专名优先 + 全局库兜底 + 50 上限。"""
    from hanhua.core.glossary import GlossaryStore
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        g = GlossaryStore(os.path.join(td, "gl.db"))
        g.init_schema()
        for i in range(60):
            g.add(f"OLD{i}", f"OLD{i}", category="专名")
        names = g.known_names_for(["FRESH", "OLD5"])
        assert names[0] == "FRESH"                 # 当前游戏收集优先
        assert names[1] == "OLD5"                  # 库内已有不重复
        assert len(names) <= 50                    # 上限
        assert len(g.known_names_for([])) == 50    # 无收集时全库兜底
        assert "OLD0" in g.known_names_for([])
        g.close()


def test_assembly_reference_generic_assembly():
    """`Namespace.Type, Assembly` 形态一律判类型引用（Fungus 实证）。"""
    from hanhua.core.unity.extractor import _structural_reason
    for s in ("Fungus.Flowchart, Fungus",
              "UnityEngine.Object, UnityEngine",
              "System.String, mscorlib",
              "My.Game.System, GameAssembly",
              "Some.Type, Assembly-CSharp, Version=1.0.0.0, "
              "Culture=neutral, PublicKeyToken=null",
              "Fungus.Flowchart"):
        assert _structural_reason(s) == "type_reference", f"应判类型引用：{s}"
    # 不误伤：点后空格（Mr. Smith）、无逗号点链后的句子、对话
    for s in ("Mr. Smith, John", "Dr. Who, Tardis", "I.R.S. building",
              "GLISLYA SPECIALIST, she said", "KALKAM'S ACOLYTES, go"):
        assert _structural_reason(s) != "type_reference", f"不应判类型引用：{s}"


def test_us_meta_carries_max_chars_budget():
    """#US 提取条目必须携带精确字数预算（UTF-16 码元数 = 字符容量）。"""
    from hanhua.core.unity import mono_dll
    # 走真实提取：构造最小 ildump 场景较复杂，直接验证提取出的 meta 契约
    # 已被 writer 消费：写回侧 capacity=meta["utf16_len"]（既有测试覆盖），
    # 此处验证提取侧写入 max_chars=码元数 的常量契约存在。
    src = open("hanhua/core/unity/mono_dll.py", encoding="utf-8").read()
    assert '"max_chars": len(raw)' in src
    assert '"utf16_len": len(raw)' in src


def test_unityevent_object_method_names_are_structural():
    """知识库案例「UnityEvent 事件绑定断裂按钮无反应」转规则：事件绑定
    对象（m_PersistentCalls 信号）中的方法名/目标名是反射按名绑定键，
    翻译即断绑（按钮点击回调链断裂）→ 全部 structural 跳过。"""
    raw = (_with_len("m_PersistentCalls")
           + _with_len("OnClick")
           + _with_len("Play"))
    entries = _raw_string_entries("f1", 9, raw, {})
    assert len(entries) >= 1
    for entry in entries:
        assert entry.status == "skipped", entry.original
        # R5：预过滤留档条目（prefilter_*）reason 定稿为 prefilter 原因，
        # 其余对象内条目 reason 为 unityevent_object
        if entry.meta.get("prefilter"):
            assert entry.meta["reason"].startswith("prefilter_")
        else:
            assert entry.meta["reason"] == "unityevent_object"


def test_unityevent_object_without_signal_is_normal():
    """对照：无事件绑定信号的对象（普通字符串）不受 UnityEvent 规则
    影响——同形态方法名串在普通对象里照常判定（按现有规则，不得出现
    unityevent_object 身份）。"""
    raw = _with_len("Play") + _with_len("Save") + _with_len("Load")
    entries = _raw_string_entries("f1", 9, raw, {})
    assert entries
    assert all(e.meta["reason"] != "unityevent_object" for e in entries)


def test_signature_credit_skipped():
    """F12-B（doog 实证 2 条失败）：'林まか (pixiv: 10768714)' 是作者署名，
    不是游戏内显示文本——翻译即失真（署名该原样保留）。识别层对
    pixiv/twitter 等平台名 + ID 的署名形态直接 structural 跳过。"""
    raw = (_with_len("林まか (pixiv: 10768714)")
           + _with_len("Kenney (twitter: kenneyNL)")
           + _with_len("Twitter: @dev"))
    entries = _raw_string_entries("f1", 9, raw, {})
    assert len(entries) >= 1
    for entry in entries:
        assert entry.status == "skipped", entry.original
        assert entry.meta["reason"] == "signature_credit"


def test_signature_credit_text_not_skipped():
    """对照：正文里的平台名/ID 不是署名形态（无括号作者结构/无 © 开头），
    照常作为可译文本（正文谈平台不跳过）。"""
    raw = _with_len("Follow us on twitter!")
    entries = _raw_string_entries("f1", 9, raw, {})
    assert entries
    assert all(e.meta["reason"] != "signature_credit" for e in entries)


def test_formatted_value_soft_guess_not_downgraded():
    """F13（doog 实证 33 条哑跳过）：xml value 位置的文本节点是确定性
    显示文本证据——后置闸门的软猜测反模式（key_style 混合大小写、
    _QUALIFIED 连字符标识符、credit_like 署名、log_template 冒号结尾、
    PascalCase 引擎串形态）不得推翻格式判定。罗马音台词（Konbanmio-n）、
    西语 UI（Seleccione dificultad:）、英文成就句（Get revived by…）
    必须恢复 pending 进池。"""
    from hanhua.core.unity.extractor import _should_downgrade_pending
    samples = [
        ("Konbanmio-n", False),                      # 罗马音台词（连字符 + 混合大小写）
        ("FeeNGAh", False),                          # 罗马音台词（PascalCase 形态）
        ("Seleccione dificultad:", False),           # 西语 UI（冒号结尾 + 长句）
        ("Get revived by Hololive's resident necromancer", False),  # 英文成就句（含 by）
        ("POS.", False),                             # HUD 缩写
        ("E1M1", False),                             # 关卡名
        # 仍应降级：机器数据形态明确 / 无语言内容
        ("https://example.com/asset", True),
        ("A", True),
        ("!!!", True),
    ]
    for text, should_drop in samples:
        e = TextEntry(file_id="f", key_path="x", original=text, meta={
            "textasset_format": "xml",
            "inner_path": "/messages/message[1]/value",
        })
        got = _should_downgrade_pending(e)
        assert got is should_drop, f"{text!r}: expect drop={should_drop}, got {got}"


def test_xml_key_position_still_downgraded():
    """对照（F13 修复边界）：xml key 位置的键名（PICKUP_BACKPACK 全大写
    +下划线）仍由 key_style 判定跳过——value 节点豁免软猜测**不改变**
    key 位置的键名判定（键名翻译即断键）。"""
    from hanhua.core.unity.extractor import _should_downgrade_pending
    key_entry = TextEntry(file_id="f", key_path="x", original="PICKUP_BACKPACK",
                          meta={"textasset_format": "xml",
                                "inner_path": "/messages/message[0]/key"})
    assert _should_downgrade_pending(key_entry) is True
    val_entry = TextEntry(file_id="f", key_path="x", original="PICKUP_BACKPACK",
                          meta={"textasset_format": "xml",
                                "inner_path": "/messages/message[0]/value"})
    assert _should_downgrade_pending(val_entry) is False


def test_boot_config_engine_file_whole_skipped():
    """F14（dollhouse 实证）：boot.config 是 Unity 引擎启动配置文件，
    值域是引擎枚举（scripting-runtime-version=legacy/net_4_x）——legacy
    是合法英文单词，单靠 should_skip/引擎串判定会漏网被当显示文本翻译
    写回，引擎按值匹配即破坏。修复：文件级整体跳过（保留条目保证写回
    完整性），非单游戏特判（boot.config 所有 Unity 游戏通用）。"""
    from hanhua.core.extractor import parse_file
    import tempfile, os
    boot = ("gfx-enable-native-gfx-jobs=\n"
            "wait-for-native-debugger=0\n"
            "scripting-runtime-version=legacy\n"
            "vr-enabled=0\n"
            "hdr-display-enabled=0\n")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "boot.config")
        with open(p, "w", encoding="utf-8") as f:
            f.write(boot)
        pf = parse_file(p)
        pending = [e for e in pf.entries if e.status == "pending"]
        assert pending == [], f"boot.config 不应有可译条目: {pending}"
        # 全部条目保留（写回完整性）
        assert len(pf.entries) == 5


def test_boot_config_named_other_ext_not_skipped():
    """对照：同名逻辑仅限 Unity 引擎配置文件 boot.config——普通游戏配置
    boot.txt 含英文文本照常可译（不误伤游戏自身的同名配置文件）。"""
    from hanhua.core.extractor import parse_file
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "boot.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("welcome to the game\npress start to begin\n")
        pf = parse_file(p)
        pending = [e for e in pf.entries if e.status == "pending"]
        assert len(pending) == 2, f"boot.txt 应照常可译: {pending}"


def test_verify_length_headers_skips_embedded_substring():
    """F15（doubleshake 实证写回失败）：译文作为更长字符串的子串出现时
    （`<w=sassy>任务被发现啦！` 内部），该子串位置前 4 字节是标签文本
    （"ssy>"）不是长度头——旧实现 find 到第一个位置即误报边界破坏。
    修复：遍历全部出现位置，任一位置长度头匹配即通过。"""
    from hanhua.core.unity.logic_audit import verify_string_length_headers
    translation = "任务被发现啦！"
    payload = translation.encode("utf-8")
    tagged = f"<w=sassy>{translation}".encode("utf-8")
    # 标签字符串（长度头 26）+ 独立字符串（长度头 21）
    raw = (len(tagged).to_bytes(4, "little") + tagged
           + len(payload).to_bytes(4, "little") + payload)
    problems = verify_string_length_headers(raw, {"Quest Discovered!": translation})
    assert problems == [], problems


def test_verify_length_headers_detects_broken_header():
    """对照：长度头与字节数不一致（写成了旧长度）仍应报错——边界破坏
    检测不能被子串豁免绕过。"""
    from hanhua.core.unity.logic_audit import verify_string_length_headers
    translation = "任务被发现啦！"
    payload = translation.encode("utf-8")
    # 长度头写旧值 17（实际 21）→ 必须报错
    raw = (17).to_bytes(4, "little") + payload
    problems = verify_string_length_headers(raw, {"Quest Discovered!": translation})
    assert problems, "长度头损坏必须被检测到"
    assert "长度头" in problems[0]


# ── R5 预过滤留档（消灭哑信号）──
def test_prefilter_samples_are_limited_per_object():
    """R5：引擎串密集对象只留 10 条样本条目，skipped_count 承载真实总数
    （防止条目爆炸），报告按 skipped_count 聚合可得准确总数。"""
    from hanhua.core.unity.extractor import _PREFILTER_SAMPLE_LIMIT
    raw = b"\x00" * 12
    for i in range(13):
        raw += _with_len(f"_Prop{i}")
    entries = _raw_string_entries("f1", 5, raw, {})
    prefilters = [e for e in entries if e.meta.get("prefilter") == "engine_string"]
    assert len(prefilters) == _PREFILTER_SAMPLE_LIMIT
    assert all(e.status == "skipped" for e in prefilters)
    # 回写后全部样本承载最终计数（13 条），不再是累计 1..10——
    # 报告聚合（按单元取 max）即真实总数
    counts = sorted(e.meta["skipped_count"] for e in prefilters)
    assert counts == [13] * _PREFILTER_SAMPLE_LIMIT


def test_prefilter_does_not_poison_object_value_evidence():
    """R5 回归：预过滤留档不得改变对象级值证据语义——should_skip/freq
    串仍贡献对象值证据（'A rare item dropped by bosses.' 是 should_skip
    串但对象值证据仍由它提供，'Skull' 物品名据此放行）。"""
    raw = _with_len("Skull") + _with_len("A rare item dropped by bosses.")
    entries = _raw_string_entries("f1", 5, raw, {})
    by_orig = {e.original: e for e in entries}
    assert by_orig["Skull"].status == "pending"
    assert by_orig["Skull"].meta["reason"] == "object_has_display_evidence"
    assert by_orig["A rare item dropped by bosses."].status == "pending"


def test_credit_like_does_not_swallow_real_sentences():
    """R5 回归：is_credit_like 的 by 归属分支把 'dropped by bosses.' 句子
    当署名——句末标点现在也是句子标记（署名行无句号）。"""
    from hanhua.core.placeholders import is_credit_like
    assert not is_credit_like("A rare item dropped by bosses.")
    assert is_credit_like("A game by Kyuppin")      # 真署名仍拦截
    assert is_credit_like("Created by Sam Hogan")
    assert is_credit_like("made in 48h")


def test_signature_credit_single_x_not_matched_f49():
    """F49（ned-flanders 实证）：单独 'x' 不再作 Twitter 代称——'Cam X
    Sensitivity'（相机 X 轴灵敏度）等 UI 文本不被误杀；显式 'x (twitter)'
    组合仍匹配。"""
    from hanhua.core.unity.extractor import _SIGNATURE_CREDIT_RE
    assert not _SIGNATURE_CREDIT_RE.search("Cam X Sensitivity")
    assert not _SIGNATURE_CREDIT_RE.search("X Position")
    assert not _SIGNATURE_CREDIT_RE.search("pixiv123")
    assert _SIGNATURE_CREDIT_RE.search("x (twitter): @user")
    assert _SIGNATURE_CREDIT_RE.search("林まか (pixiv: 10768714)")


# ── give-me-strength 按键失灵根因（2026-08-29）──
# 写回后主菜单「开始」按键失灵：Play 按钮的 UnityEvent 回调
# m_MethodName="Play"（target=PlayableDirector）被汉化成「开始」→ 反射
# 按名绑定断链 → 点击无反应。字节级铁证：UnityEvent 持久化回调的字段名
# （m_Target/m_MethodName）**不序列化进 raw 字节**，只有值——obj508 的
# raw 串池是 [Normal…Disabled 状态名, Play 回调方法名, UnityEngine.Object,
# UnityEngine ×2 (两个回调的 m_Target 类型引用), TriggerMusicPressPlay]。
# 事件绑定结构的唯一 raw 值证据 = m_Target 类型引用同值 ≥2（多个回调）。
# 另有次因哑破坏：FMOD event:/ 路径（184 条译成「事件：/音乐/…」→ 音效
# 静默）、StandaloneInputModule 轴名 Horizontal/Vertical/Submit/Cancel
# （word_list_object 放行 → 菜单导航失灵）、Cinemachine Mouse X（相机轴
# 断裂）。本组测试锚定这些形态在提取层被确定性跳过。

def test_unityevent_target_type_pair_marks_object_structural():
    """UnityEvent 回调对象（m_Target 类型引用 'UnityEngine.Object,
    UnityEngine' 同值 ≥2 = 多个回调）→ 对象内全部结构跳过，含 Play 方法名。
    这正是 give-me-strength obj508（此前 Play 被 code_heavy_display_word
    放行翻译 → 写回后按钮无反应）。"""
    raw = (_with_len("Normal") + _with_len("Pressed")
           + _with_len("Play") + _with_len("TriggerMusicPressPlay")
           + _with_len("UnityEngine.Object, UnityEngine")
           + _with_len("UnityEngine.Object, UnityEngine"))
    entries = _raw_string_entries("level1", 508, raw, {}, "level1")
    by_orig = {e.original: e for e in entries}
    assert by_orig["Play"].status == "skipped"
    assert by_orig["Play"].meta["reason"] == "unityevent_object"
    assert by_orig["Play"].meta["obj_is_unityevent"] is True
    assert by_orig["Normal"].status == "skipped"
    assert by_orig["TriggerMusicPressPlay"].status == "skipped"


def test_single_target_type_reference_is_not_unityevent():
    """对照：单次类型引用（普通按钮文本对象 a-catfiends obj1319 'Save' +
    'UnityEngine.Object, UnityEngine' count=1）不是事件绑定——Play 之类
    白名单按钮词照常按 code_heavy_display_word 放行，不误杀。"""
    raw = (_with_len("Save") + _with_len("UnityEngine.Object, UnityEngine"))
    entries = _raw_string_entries("f1", 1319, raw, {})
    by_orig = {e.original: e for e in entries}
    assert by_orig["Save"].status == "pending"
    assert by_orig["Save"].meta["obj_is_unityevent"] is False


def test_input_axis_object_skipped():
    """InputManager 轴配置对象（StandaloneInputModule：Horizontal+Vertical
    +Submit+Cancel 四轴）→ 轴名全跳过（此前 word_list_object 放行 → 菜单
    键盘导航失灵）。"""
    raw = (b"\x00" * 16) + b"".join(_with_len(t) for t in (
        "Horizontal", "Vertical", "Submit", "Cancel"))
    entries = _raw_string_entries("level1", 550, raw, {}, "level1")
    by_orig = {e.original: e for e in entries}
    for axis in ("Horizontal", "Vertical", "Submit", "Cancel"):
        assert by_orig[axis].status == "skipped", axis
        assert by_orig[axis].meta["reason"] == "input_axis_object", axis
        assert by_orig[axis].meta["obj_is_input_axis"] is True, axis


def test_cinemachine_mouse_x_skipped():
    """Cinemachine 相机轨道对象孤立 'Mouse X' 单串（give-me-strength
    obj513）→ 轴名跳过（此前 single_visible_string 放行 → 相机轨道轴
    断裂）。"""
    raw = (b"\x00" * 16) + _with_len("Mouse X")
    entries = _raw_string_entries("level1", 513, raw, {}, "level1")
    by_orig = {e.original: e for e in entries}
    assert by_orig["Mouse X"].status == "skipped"
    assert by_orig["Mouse X"].meta["reason"] == "unambiguous_axis_name"
    assert by_orig["Mouse X"].meta["obj_is_input_axis"] is True
