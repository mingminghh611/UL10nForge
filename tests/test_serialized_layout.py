# -*- coding: utf-8 -*-
"""serialized_layout 回归（0.46.0 A10 / 0.49.0 A13）。

UnityPy 对 SerializedFile 保存时重算 data_offset，丢弃原文件的页对齐布局
（如 4096）。restore_data_offset 用零填充重建原布局；条件不满足时必须
原样返回。本文件锁定各分支行为（gen 9-21 / gen>=22 / gen<9），防复现。
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


def _build_sf22(meta=1158, data_offset=4096, body=b"DATA", unknown=0):
    """构造一个 gen>=22（LargeFilesSupport，48 字节头）形态的文件。"""
    pad = data_offset - (48 + meta)
    assert pad >= 0
    total = data_offset + len(body)
    return (b"\x00" * 8                        # 0x00 零
            + struct.pack(">I", 22)            # version @0x08
            + b"\x00" * 4                      # 0x0c 零
            + b"\x01\x00\x00\x00"              # endian @0x10 + reserved
            + struct.pack(">Iqqq", meta, total, data_offset, unknown)
            # 0x14 meta / 0x18 file_size / 0x20 data_offset / 0x28 unknown
            + b"\x00" * meta                   # metadata
            + b"\x00" * pad                    # 页对齐零填充
            + body)


def _build_legacy(meta=80, version=6, body=b"DATA"):
    """构造一个 gen<9 形态的文件：16 字节头 + 数据 + endian 字节 + 尾部 metadata。

    metadata_size 含 endian 字节（UnityPy/AssetRipper 读侧都从
    file_size - metadata_size 处读 endian）。data_offset=16（数据紧邻头）。
    """
    tail = b"\x00" * meta             # metadata（首字节即 endian 探针位置——见下）
    meta_total = 1 + len(tail)        # endian 字节 + metadata
    total = 16 + len(body) + meta_total
    head = struct.pack(">IIII", meta_total, total, version, 16)
    blob = head + body + b"\x00" + tail   # endian=0（小端）
    return blob


# ---------------- original_header：gen 9-21 ----------------

def test_original_header_gen9_to_21():
    raw = _build_sf(version=21, meta=100, data_offset=4096)
    meta, size, ver, off = original_header(raw)
    assert (meta, ver, off) == (100, 21, 4096)
    assert size == len(raw)


def test_original_header_rejects_other_gens_and_bad_input():
    assert original_header(b"") is None
    assert original_header(b"\x00" * 10) is None
    # gen 8/22 由各自分支处理（不再是 None）
    assert original_header(_build_legacy()) is not None
    assert original_header(_build_sf22()) is not None
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


# ---------------- original_header：gen >= 22 ----------------

def test_original_header_gen22():
    raw = _build_sf22(meta=1158, data_offset=4096, body=b"X" * 64)
    meta, size, ver, off = original_header(raw)
    assert (meta, ver, off) == (1158, 22, 4096)
    assert size == len(raw)


def test_original_header_gen22_rejects_bad_shapes():
    body = b"X" * 64
    # file_size 不等于真实长度
    raw = bytearray(_build_sf22(meta=100, data_offset=4096, body=body))
    struct.pack_into(">q", raw, 0x18, len(raw) + 1)
    assert original_header(bytes(raw)) is None
    # 前导零字段被占用（非 gen22 布局误判）
    raw = bytearray(_build_sf22(meta=100, data_offset=4096, body=body))
    raw[0] = 1
    assert original_header(bytes(raw)) is None
    # data_offset 越界（< 头长）
    raw = bytearray(_build_sf22(meta=100, data_offset=4096, body=body))
    struct.pack_into(">q", raw, 0x20, 8)
    assert original_header(bytes(raw)) is None


def test_original_header_gen22_streaming_head():
    """流式 48 字节头（restore_from_path 形态）：只读头必须能解析。"""
    raw = _build_sf22(meta=1158, data_offset=4096, body=b"X" * 64)
    head = raw[:48]
    meta, size, ver, off = original_header(head, file_size=len(raw))
    assert (meta, ver, off) == (1158, 22, 4096)
    # 不传 file_size：缓冲区只有 48 字节，48+1158 > 48 → None
    assert original_header(head) is None


# ---------------- original_header：gen < 9 ----------------

def test_original_header_legacy_with_endian_probe():
    raw = _build_legacy()
    meta, size, ver, off = original_header(raw)
    # metadata_size 含 endian 字节
    assert (meta, ver, off) == (81, 6, 16)
    assert size == len(raw)


def test_original_header_legacy_needs_valid_endian_probe():
    """gen<9 必须能探到合法 endian 字节（0/1）——这是「metadata 在尾」
    的唯一形态学证据。探针非法 / 不可得 → None（宁漏勿坏）。"""
    raw = _build_legacy()
    # 尾部探针位置放非法值（0x7F）
    bad = bytearray(raw)
    pos = len(bad) - 81  # file_size - metadata_size
    bad[pos] = 0x7F
    assert original_header(bytes(bad)) is None
    # 流式形态：不传 endian_byte 且缓冲区不覆盖尾部 → None
    head = raw[:16]
    assert original_header(head, file_size=len(raw)) is None
    # 传合法探针 → 正常解析
    meta, size, ver, off = original_header(
        head, file_size=len(raw), endian_byte=b"\x00")
    assert (meta, ver, off) == (81, 6, 16)


# ---------------- restore：gen 9-21 ----------------

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
    # 原头为 None
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


# ---------------- restore：gen >= 22 ----------------

def test_restore_gen22_rebuilds_page_alignment():
    """gen>=22：保存产物 data_offset=48+meta 对齐 → 重建恢复 4096。

    形态实证（catfiend globalgamemanagers，gen 22）：原 data_offset=4096，
    UnityPy 保存后 1216。
    """
    meta = 1158
    orig = _build_sf22(meta=meta, data_offset=4096, body=b"X" * 64)
    header = original_header(orig)
    assert header is not None
    # 模拟 UnityPy 保存产物：data_offset = align16(48+1158) = 1216
    body = b"Y" * 64
    natural = 48 + meta
    s_offset = natural + (16 - natural % 16) % 16  # 1216
    saved = (b"\x00" * 8 + struct.pack(">I", 22) + b"\x00" * 4
             + b"\x01\x00\x00\x00"
             + struct.pack(">Iqqq", meta, s_offset + len(body), s_offset, 0)
             + b"\x00" * meta + b"\x00" * (s_offset - natural) + body)
    rebuilt = restore_data_offset(saved, header)
    s_meta, = struct.unpack(">I", rebuilt[0x14:0x18])
    s_fsize, s_doff = struct.unpack(">qq", rebuilt[0x18:0x28])
    assert (s_meta, s_doff) == (meta, 4096)
    assert s_fsize == 4096 + len(body) == len(rebuilt)
    # 对象数据按原偏移就位，中段是零填充
    assert rebuilt[4096:] == body
    assert rebuilt[s_offset:4096] == b"\x00" * (4096 - s_offset)
    # version/endian/unknown 头字段原样保留
    assert rebuilt[8:12] == struct.pack(">I", 22)
    assert rebuilt[0x28:0x30] == b"\x00" * 8


def test_restore_gen22_passthrough_cases():
    meta = 100
    body = b"DATA"
    orig = _build_sf22(meta=meta, data_offset=4096, body=body)
    header = original_header(orig)
    assert header is not None
    natural = 48 + meta  # 148 → 对齐后 160
    # 布局已一致（s_offset == o_offset）
    saved = (b"\x00" * 8 + struct.pack(">I", 22) + b"\x00" * 4
             + b"\x01\x00\x00\x00"
             + struct.pack(">Iqqq", meta, 4096 + len(body), 4096, 0)
             + b"\x00" * meta + b"\x00" * (4096 - natural) + body)
    assert restore_data_offset(saved, header) is saved
    # 原布局紧凑（o_offset < 自然终点）→ 不动
    compact = _build_sf22(meta=meta, data_offset=160, body=body)
    h2 = original_header(compact)
    saved2 = (b"\x00" * 8 + struct.pack(">I", 22) + b"\x00" * 4
              + b"\x01\x00\x00\x00"
              + struct.pack(">Iqqq", meta, 160 + len(body), 160, 0)
              + b"\x00" * meta + body)
    assert restore_data_offset(saved2, h2) is saved2
    # 元数据长大撑破原布局（s_offset > o_offset）→ 不动
    grown = (b"\x00" * 8 + struct.pack(">I", 22) + b"\x00" * 4
             + b"\x01\x00\x00\x00"
             + struct.pack(">Iqqq", 500, 560 + len(body), 560, 0)
             + b"\x00" * 500 + body)
    assert restore_data_offset(grown, h2) is grown
    # meta 不一致 → 不动
    mismatch = (b"\x00" * 8 + struct.pack(">I", 22) + b"\x00" * 4
                + b"\x01\x00\x00\x00"
                + struct.pack(">Iqqq", 101, 160 + len(body), 160, 0)
                + b"\x00" * 101 + body)
    assert restore_data_offset(mismatch, h2) is mismatch


def test_restore_gen22_preserves_growing_body():
    """gen>=22 对象数据变长（字体替换场景）时 file_size 正确重算。"""
    meta = 100
    orig = _build_sf22(meta=meta, data_offset=4096, body=b"X" * 16)
    header = original_header(orig)
    natural = 48 + meta
    s_offset = natural + (16 - natural % 16) % 16
    saved = (b"\x00" * 8 + struct.pack(">I", 22) + b"\x00" * 4
             + b"\x01\x00\x00\x00"
             + struct.pack(">Iqqq", meta, s_offset + 2048, s_offset, 0)
             + b"\x00" * meta + b"\x00" * (s_offset - natural) + b"Y" * 2048)
    rebuilt = restore_data_offset(saved, header)
    s_fsize, s_doff = struct.unpack(">qq", rebuilt[0x18:0x28])
    assert (s_fsize, s_doff) == (4096 + 2048, 4096)
    assert rebuilt[4096:] == b"Y" * 2048


# ---------------- restore：gen < 9 ----------------

def test_restore_legacy_fixes_hardcoded_data_offset():
    """gen<9：UnityPy 保存把 data_offset 字段硬编码 32 → 修回原值 16。

    写序同构（头/数据/endian/metadata 尾），仅头部字段漂移。
    """
    orig = _build_legacy()
    header = original_header(orig)
    assert header is not None
    # 模拟 UnityPy 保存产物：同一布局，data_offset 字段=32（硬编码）
    body = b"DATA"
    meta_total = 81
    saved = (struct.pack(">IIII", meta_total, 16 + len(body) + meta_total,
                         6, 32)
             + body + b"\x00" + b"\x00" * 80)
    rebuilt = restore_data_offset(saved, header)
    m, s, v, o = struct.unpack(">IIII", rebuilt[:16])
    assert (m, s, v, o) == (meta_total, len(saved), 6, 16)
    # 除头部字段外逐字节恒等
    assert rebuilt[16:] == saved[16:]


def test_restore_legacy_passthrough_cases():
    orig = _build_legacy()
    header = original_header(orig)
    # 字段已是 16（无漂移）→ 原样返回
    assert restore_data_offset(orig, header) is orig
    # 原头为 None → 原样返回
    assert restore_data_offset(orig, None) is orig


def test_restore_legacy_nonstandard_offset_passthrough():
    """原 doff 非 16（头后带填充的罕见形态）→ 不动（宁漏勿坏：
    数据物理位置无法从保存产物对应）。"""
    raw = _build_legacy()
    # 构造头部 data_offset=32 声称头后有填充的文件（头后真有 16 字节填充）
    body = b"DATA"
    meta_total = 81
    total = 32 + len(body) + meta_total
    raw32 = (struct.pack(">IIII", meta_total, total, 6, 32)
             + b"\x00" * 16 + body + b"\x00" + b"\x00" * 80)
    header = original_header(raw32)
    assert header is not None
    # 保存产物：UnityPy 把数据写在 16（不管原 doff）+ 字段硬编码 32
    saved = (struct.pack(">IIII", meta_total, 16 + len(body) + meta_total,
                         6, 32)
             + body + b"\x00" + b"\x00" * 80)
    assert restore_data_offset(saved, header) is saved
