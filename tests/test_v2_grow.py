"""Unity 序列化字符串变长写入测试。"""
import json
import os
import shutil
import struct
from pathlib import Path

import pytest
from hanhua.core.unity.writer import (
    WriteResult,
    _apply_localization_translations,
    _assert_asset_diff_whitelist,
    _dispose_environment,
    _patch_asset,
    _patch_serialized_string,
)


def _serialized(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def test_patch_serialized_string_grows_without_overwriting_next_field():
    """变长写入必须后移后续字段，而不能吞掉它。"""
    next_field = struct.pack("<I", 0) + b"NEXT"
    raw = bytearray(_serialized("NEW GAME") + next_field)

    _patch_serialized_string(raw, 4, "开始游戏")

    assert int.from_bytes(raw[:4], "little") == 12
    assert raw[4:16] == "开始游戏".encode("utf-8")
    assert raw[16:24] == next_field


def test_patch_serialized_string_shrinks_and_keeps_alignment_padding():
    """长度头是实际长度，填充只服务于下一个字段的 4 字节对齐。"""
    next_field = struct.pack("<I", 0) + b"NEXT"
    raw = bytearray(_serialized("NEW GAME") + next_field)

    _patch_serialized_string(raw, 4, "设置")

    assert int.from_bytes(raw[:4], "little") == 6
    assert raw[4:10] == "设置".encode("utf-8")
    assert raw[10:12] == b"\x00\x00"
    assert raw[12:20] == next_field


def test_patch_serialized_string_preserves_following_serialized_string():
    """同一对象内从后向前修改时，前面字符串扩容不影响已经修改的后项。"""
    first = _serialized("NEW GAME")
    second = _serialized("SETTINGS")
    raw = bytearray(first + second + struct.pack("<I", 0))

    _patch_serialized_string(raw, len(first) + 4, "选项")
    _patch_serialized_string(raw, 4, "开始游戏")

    first_len = int.from_bytes(raw[:4], "little")
    assert raw[4:4 + first_len].decode("utf-8") == "开始游戏"
    second_length_offset = 4 + first_len
    second_len = int.from_bytes(raw[second_length_offset:second_length_offset + 4], "little")
    assert raw[second_length_offset + 4:second_length_offset + 4 + second_len].decode("utf-8") == "选项"


# ── C2 差异白名单：补丁只允许改目标串区，其余字节零变动 ──
def test_diff_whitelist_in_place_diff_must_stay_in_span():
    """原位补丁（new_end == old_end）：diff 必须严格落在目标 span 内——
    改动任何 span 外字节（相邻字段/长度头之外）都拒绝。"""
    raw = _serialized("NEW GAME") + _serialized("SETTINGS")
    patched = bytearray(raw)
    _old_end, new_end = _patch_serialized_string(patched, 4, "设置")
    assert new_end == 12        # 6 字节 + 对齐，原位（old_end == 12）
    _assert_asset_diff_whitelist(
        raw, bytes(patched), [(0, 12, new_end)], "test")   # 不抛
    tampered = bytearray(patched)
    tampered[12] ^= 0xFF                                   # 篡改相邻字段
    with pytest.raises(ValueError, match="越出目标串区"):
        _assert_asset_diff_whitelist(
            raw, bytes(tampered), [(0, 12, new_end)], "test")


def test_diff_whitelist_growth_preserves_following_segments():
    """变长补丁：后续区段被推挤（内容不变仅位置移动）允许；后续区段
    内容被意外改动（子串缺失）拒绝。"""
    raw = _serialized("NEW GAME") + _serialized("SETTINGS")
    patched = bytearray(raw)
    old_end, new_end = _patch_serialized_string(patched, 4, "开始游戏")
    assert new_end > old_end    # 变长：SETTINGS 整体右移 4 字节
    spans = [(0, old_end, new_end)]
    _assert_asset_diff_whitelist(raw, bytes(patched), spans, "test")
    tampered = bytearray(patched)
    tampered[-1] ^= 0xFF                                   # 破坏 SETTINGS 内容
    with pytest.raises(ValueError, match="内容丢失|被改动"):
        _assert_asset_diff_whitelist(
            raw, bytes(tampered), spans, "test")


def test_apply_localization_translation_uses_entry_id():
    tree = {
        "m_TableData": [
            {"m_Id": 1, "m_Localized": "VOLUME"},
            {"m_Id": 2, "m_Localized": "FULLSCREEN"},
        ]
    }

    changed = _apply_localization_translations(tree, [(2, "全屏")])

    assert changed is True
    assert [row["m_Localized"] for row in tree["m_TableData"]] == ["VOLUME", "全屏"]


@pytest.mark.skipif(not os.getenv("HANHUA_SEWER_CALL_DIR"), reason="需要本机 SEWER CALL 样本")
def test_sewer_call_plain_asset_rebuild_has_exact_string_length(tmp_path):
    """普通 SerializedFile 写回后长度头必须是译文真实字节数，不能含尾随 NUL。"""
    from UnityPy import Environment

    source = Path(os.environ["HANHUA_SEWER_CALL_DIR"]) / "SEWER CALL_Data/sharedassets1.assets"
    target = tmp_path / source.name
    shutil.copy2(source, target)
    entry = {
        "original": "Pick up flashlight",
        "translation": "拾起手电筒",
        "meta": json.dumps({"kind": "rawstr", "obj": 51, "offset": 76,
                            "obj_has_values": True}),
    }

    original_env = Environment()
    original_env.load([str(source)])
    original_50 = next(obj for obj in original_env.objects if obj.path_id == 50).get_raw_data()
    _dispose_environment(original_env)

    _patch_asset(target, [entry], WriteResult())

    env = Environment()
    try:
        env.load([str(target)])
        prompt = next(obj for obj in env.objects if obj.path_id == 51).get_raw_data()
        unchanged_50 = next(obj for obj in env.objects if obj.path_id == 50).get_raw_data()
        payload = "拾起手电筒".encode("utf-8")
        assert int.from_bytes(prompt[72:76], "little") == len(payload)
        assert prompt[76:76 + len(payload)] == payload
        assert b"\x00" not in prompt[76:76 + len(payload)]
        assert unchanged_50 == original_50
    finally:
        _dispose_environment(env)


@pytest.mark.skipif(not os.getenv("HANHUA_SEWER_CALL_DIR"), reason="需要本机 SEWER CALL 样本")
def test_sewer_call_uitable_bundle_roundtrip(tmp_path):
    """真实 Addressables UITable 写回后必须能重开且对象表大小随译文增长。"""
    from UnityPy import Environment
    from hanhua.core.unity.writer import WriteResult, _patch_asset

    source = (
        Path(os.environ["HANHUA_SEWER_CALL_DIR"])
        / "SEWER CALL_Data/StreamingAssets/aa/StandaloneWindows64"
        / "localization-string-tables-english(en)_assets_all.bundle"
    )
    target = tmp_path / source.name
    shutil.copy2(source, target)
    table_id = 4111038547412706082
    entry = {
        "original": "NEW GAME",
        "translation": "开始游戏",
        "meta": json.dumps({"kind": "rawstr", "obj": table_id, "offset": 84,
                            "obj_has_values": True}),
    }

    _patch_asset(target, [entry], WriteResult())

    env = Environment()
    try:
        env.load([str(target)])
        table = next(obj for obj in env.objects if obj.path_id == table_id)
        raw = table.get_raw_data()
        assert table.byte_size == 524
        assert int.from_bytes(raw[80:84], "little") == 12
        assert raw[84:96] == "开始游戏".encode("utf-8")
        assert raw[96:100] == b"\x00" * 4
    finally:
        from hanhua.core.unity.writer import _dispose_environment
        _dispose_environment(env)


@pytest.mark.skipif(not os.getenv("HANHUA_SEWER_CALL_DIR"), reason="需要本机 SEWER CALL 样本")
def test_sewer_call_localization_entry_writes_by_stable_id(tmp_path):
    """结构化 StringTable 值不受通用引擎词/标识符过滤，并按 m_Id 写回。"""
    from UnityPy import Environment

    source = (
        Path(os.environ["HANHUA_SEWER_CALL_DIR"])
        / "SEWER CALL_Data/StreamingAssets/aa/StandaloneWindows64"
        / "localization-string-tables-english(en)_assets_all.bundle"
    )
    target = tmp_path / source.name
    shutil.copy2(source, target)
    table_id = 4111038547412706082
    volume_id = 8273326364184576
    entry = {
        "original": "VOLUME",
        "translation": "音量",
        "meta": json.dumps({
            "kind": "localization", "obj": table_id,
            "entry_id": volume_id, "locale": "en", "table": "UITable_en",
        }),
    }

    _patch_asset(target, [entry], WriteResult())

    env = Environment()
    try:
        env.load([str(target)])
        table = next(obj for obj in env.objects if obj.path_id == table_id).read_typetree()
        values = {row["m_Id"]: row["m_Localized"] for row in table["m_TableData"]}
        assert values[volume_id] == "音量"
    finally:
        _dispose_environment(env)


def test_patch_serialized_string_original_mismatch_raises():
    """定位器漂移：长度头边界合法但内容与原文不符 → 必须拒绝，不静默写坏。

    pyromaniac 实证（0.51.0 线）：旧库 obj+offset 定位器落到兄弟对象数据
    上，raw[480:484] 读出 'ked\x00' 当长度头——0x00646b65=6579563 越界
    崩溃是「越界形态」；同样场景下若兄弟数据恰是「不越界的小值」，仅边界
    检查拦不住，字节会被写进错误位置。expected_original 内容自证兜底。
    """
    raw = bytearray(_serialized("Clicked") + struct.pack("<I", 0))
    with pytest.raises(ValueError, match="字符串内容与原文不符"):
        _patch_serialized_string(raw, 4, "点击", expected_original="Start")
    # 原文一致时正常写入（回归防误伤）
    old_end, new_end = _patch_serialized_string(
        raw, 4, "点击", expected_original="Clicked")
    assert raw[4:10] == "点击".encode("utf-8")
    assert new_end >= old_end


def test_patch_serialized_string_non_utf8_original_raises():
    """定位器指向二进制数据（解码即失败）→ 拒绝而不是 UnicodeDecodeError
    裸抛（调用方统一按 ValueError 捕获按条拒绝）。"""
    raw = bytearray(struct.pack("<I", 4) + b"\xff\xfe\xfd\xfc"
                    + struct.pack("<I", 0))
    with pytest.raises(ValueError, match="非 UTF-8"):
        _patch_serialized_string(raw, 4, "文本", expected_original="Start")
