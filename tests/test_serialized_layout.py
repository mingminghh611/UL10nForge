# -*- coding: utf-8 -*-
"""serialized_layout 回归（0.46.0，问题集 A10 根因 B）。

UnityPy 对 gen 9-21 SerializedFile 保存时重算 data_offset，丢弃原文件的
页对齐布局（如 4096）。restore_data_offset 用零填充重建原布局；条件不
满足时必须原样返回。本文件锁定各分支行为，防复现。
"""
import struct

from hanhua.core.unity.serialized_layout import (
    original_header, restore_data_offset)


def _build_sf(version=21, meta=100, data_offset=4096, body=b"DATA"):
    """构造一个 gen 9-21 形态的 SerializedFile 头 + 元数据 + 数据。"""
    pad = data_offset - (20 + meta)
    assert pad >= 0
    total = data_offset + len(body)
    return (struct.pack(">IIII", meta, total, version, data_offset)
            + b"\x01\x00\x00\x00"          # endian + reserved
            + b"\x00" * meta               # metadata
            + b"\x00" * pad                # 页对齐零填充
            + body)


def test_original_header_gen9_to_21():
    raw = _build_sf(version=21, meta=100, data_offset=4096)
    meta, size, ver, off = original_header(raw)
    assert (meta, ver, off) == (100, 21, 4096)
    assert size == len(raw)


def test_original_header_rejects_other_gens_and_bad_input():
    assert original_header(b"") is None
    assert original_header(b"\x00" * 10) is None
    # gen 22 布局不同
    assert original_header(_build_sf(version=22)) is None
    assert original_header(_build_sf(version=8)) is None
    # 头部不自洽：file_size 超出实际长度
    raw = bytearray(_build_sf(version=21))
    struct.pack_into(">I", raw, 4, len(raw) + 9999)
    assert original_header(bytes(raw)) is None


def test_original_header_streaming_head_needs_file_size():
    """A10b：流式 24 字节头 + file_size 参数（restore_from_path 形态）。

    24 字节头永远装不下 metadata——不传 file_size 时全部误判 None →
    data_offset 恢复静默失效（fake-it 全部 gen21 文件 4096→3904/1328
    实证）。传真实文件长度必须正常解析；自洽校验同样生效。
    """
    raw = _build_sf(version=21, meta=3871, data_offset=4096)
    head = raw[:24]
    # 旧形态（无 file_size）：24 < 20+3871 → None（流式调用这是 bug 形态）
    assert original_header(head) is None
    # 修复形态：传真实长度 → 正常解析
    meta, size, ver, off = original_header(head, file_size=len(raw))
    assert (meta, ver, off) == (3871, 21, 4096)
    assert size == len(raw)
    # 自洽校验仍生效：声称的 file_size 比真实长度还大 → None
    assert original_header(head, file_size=100) is None


def test_restore_roundtrip_rebuilds_page_alignment():
    """保存后 data_offset 被重算为自然终点 → 重建恢复 4096 布局。"""
    orig = _build_sf(version=21, meta=100, data_offset=4096, body=b"HELLO")
    header = original_header(orig)
    assert header is not None
    # 模拟 UnityPy 保存产物：同 meta/version，data_offset=120（自然终点）
    body = b"HELLO-NEW"
    saved = (struct.pack(">IIII", 100, 20 + 100 + len(body), 21, 120)
             + b"\x01\x00\x00\x00" + b"\x00" * 100 + body)
    rebuilt = restore_data_offset(saved, header)
    meta, size, ver, off = struct.unpack(">IIII", rebuilt[:16])
    assert (meta, ver, off) == (100, 21, 4096)
    assert size == 4096 + len(body)
    # 对象数据按原偏移就位，前导是零填充
    assert rebuilt[4096:] == body
    assert rebuilt[120:4096] == b"\x00" * (4096 - 120)
    # 头部 endian/reserved 块保留
    assert rebuilt[16:20] == b"\x01\x00\x00\x00"


def test_restore_passthrough_when_layout_already_matches():
    """保存产物 data_offset 与原值一致 → 原样返回。"""
    orig = _build_sf(version=21, meta=100, data_offset=4096, body=b"DATA")
    header = original_header(orig)
    assert restore_data_offset(orig, header) is orig


def test_restore_passthrough_compact_original_layout():
    """原 data_offset 小于自然终点（紧凑布局）→ 不动。"""
    # 原文件紧凑：data_offset = 120（自然终点，未页对齐）
    orig = _build_sf(version=21, meta=100, data_offset=120, body=b"DATA")
    header = original_header(orig)
    assert header is not None
    # 保存产物 16 对齐后 data_offset=128
    saved = (struct.pack(">IIII", 100, 128 + 4, 21, 128)
             + b"\x01\x00\x00\x00" + b"\x00" * 100 + b"DATA")
    assert restore_data_offset(saved, header) is saved


def test_restore_passthrough_misaligned_offset():
    orig = _build_sf(version=21, meta=100, data_offset=4100, body=b"DATA")
    header = original_header(orig)
    assert header is not None
    saved = (struct.pack(">IIII", 100, 124 + 4, 21, 124)
             + b"\x01\x00\x00\x00" + b"\x00" * 100 + b"DATA")
    assert restore_data_offset(saved, header) is saved


def test_restore_passthrough_metadata_grew_past_original():
    """元数据长大撑破原布局（s_offset > o_offset）→ 原样返回交上层把关。"""
    orig = _build_sf(version=21, meta=100, data_offset=128, body=b"DATA")
    header = original_header(orig)
    # 保存产物 meta 涨到 200，data_offset = 220 > 原 128
    saved = (struct.pack(">IIII", 200, 220 + 4, 21, 220)
             + b"\x01\x00\x00\x00" + b"\x00" * 200 + b"DATA")
    assert restore_data_offset(saved, header) is saved


def test_restore_passthrough_version_or_meta_mismatch():
    orig = _build_sf(version=21, meta=100, data_offset=4096)
    header = original_header(orig)
    # 版本不一致
    saved = (struct.pack(">IIII", 100, 120, 21, 120)
             + b"\x01\x00\x00\x00")
    assert restore_data_offset(saved, (100, 0, 19, 4096)) is saved
    # metadata_size 不一致
    assert restore_data_offset(saved, (101, 0, 21, 4096)) is saved
    # 原头为 None（gen>=22 等）
    assert restore_data_offset(saved, None) is saved


def test_restore_preserves_growing_body():
    """对象数据变长（字体替换场景）时 file_size 正确重算。"""
    orig = _build_sf(version=21, meta=100, data_offset=4096, body=b"X" * 16)
    header = original_header(orig)
    saved = (struct.pack(">IIII", 100, 120 + 2048, 21, 120)
             + b"\x01\x00\x00\x00" + b"\x00" * 100 + b"Y" * 2048)
    rebuilt = restore_data_offset(saved, header)
    meta, size, ver, off = struct.unpack(">IIII", rebuilt[:16])
    assert (meta, ver, off) == (100, 21, 4096)
    assert size == 4096 + 2048 == len(rebuilt)
    assert rebuilt[4096:] == b"Y" * 2048
