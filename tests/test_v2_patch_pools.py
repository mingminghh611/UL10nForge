# -*- coding: utf-8 -*-
"""F1 尾部 NUL 修复单元测试（写回升级计划阶段一 1.4）。

覆盖：
- patch_metadata_strings 纯函数：explicit 记录 <length> 字段更新、
  v39 implicit 链式重建（非末条紧凑头部/末条右对齐/空洞清零/全 dataIndex 更新）、
  超长 payload / 未知 data_index / 非 metadata / 未知版本防御；
- _patch_metadata 集成：写回 + 重开验证严格长度比对（绝不 rstrip 掩盖
  尾部 NUL）、F2 占位符破坏拒绝、非 metadata 文件跳过、无条目零改动；
- _patch_dll 集成：#US 三处联动（压缩前缀 + 数据 + flag 移到新数据末尾后）、
  前缀 2 字节 → 1 字节收缩、重开验证流式遍历、F2 占位符破坏拒绝。
"""
import json
import struct

import pytest

from hanhua.core.unity import il2cpp
from hanhua.core.unity.writer import (
    WriteResult, _encode_compressed_uint, _patch_dll, _patch_metadata,
)

METADATA_MAGIC = 0xFAB11BAF


# --------------------------------------------------------------------------
# 合成 fixture 构造器
# --------------------------------------------------------------------------

def _build(version: int, literals: list[str | bytes], *, data_off: int = 0x200,
           lit_off: int = 0x100) -> bytes:
    """构造指定版本的合成 metadata（与 test_v2_metadata_versions._build 同构）。

    v39：4 字节 <dataIndex> 差分记录 + header 0x10 记录数强校验；
    其他版本：8 字节 <length, dataIndex> 显式记录。数据区无间隙连续排列。
    元素可为 bytes（原始字节直放，模拟 parse 会过滤掉的非 UTF-8/空记录）。
    """
    chunks = [s.encode("utf-8") if isinstance(s, str) else bytes(s)
              for s in literals]
    data = b"".join(chunks)
    offsets = []
    pos = 0
    for c in chunks:
        offsets.append((pos, len(c)))
        pos += len(c)
    header = bytearray(0x30)
    struct.pack_into("<II", header, 0, METADATA_MAGIC, version)
    if version == 39:
        table_size = len(literals) * 4
        struct.pack_into("<I", header, 0x10, len(literals))
        struct.pack_into("<II", header, 0x14, data_off, len(data))
        lit_arr = b"".join(struct.pack("<I", off) for off, _ in offsets)
    else:
        table_size = len(literals) * 8
        struct.pack_into("<II", header, 0x10, data_off, len(data))
        lit_arr = b"".join(struct.pack("<II", ln, off) for off, ln in offsets)
    struct.pack_into("<II", header, 0x08, lit_off, table_size)
    buf = bytes(header) + b"\x00" * (lit_off - 0x30) + lit_arr
    buf += b"\x00" * (data_off - len(buf)) + data
    return buf


def _us_heap(records: list[tuple[bytes, int]]) -> bytes:
    """构造自洽 #US 堆：offset 0 占位字节，records = [(UTF-16 数据字节, flag)]。

    每条 = ECMA-335 压缩前缀（值 = 数据字节数 + 1）+ 数据 + flag，
    从 offset 1 起连续 —— 与 _walk_us_heap_records 流式遍历完全一致。
    """
    out = bytearray(b"\x00")
    for data, flag in records:
        out += _encode_compressed_uint(len(data) + 1)
        out += data
        out += bytes((flag,))
    return bytes(out)


def _records(raw: bytes) -> dict[int, tuple[int, int]]:
    """重新解析池 → {data_index: (length, data_pos)}（与提取侧同一解析器）。"""
    return {di: (ln, pos) for di, ln, pos in il2cpp.parse_string_literals(raw)}


def _meta_entry(offset: int, length: int, original: str, translation: str,
                *, kind: str = "il2cpp", **extra) -> dict:
    """构造写回 entry（disposition=translate 通过写回门禁）。"""
    return {
        "file_id": "meta", "key_path": f"meta#{offset}",
        "original": original, "translation": translation,
        "meta": json.dumps({
            "kind": kind, "file_offset": offset, "length": length,
            "disposition": "translate", **extra,
        }),
    }


def _us_entry(offset: int, utf16_len: int, original: str, translation: str,
              **extra) -> dict:
    """构造 #US 条目。offset = 记录起始（压缩前缀）位置 = record_offset，
    与 mono_dll 提取端新语义一致。"""
    return {
        "file_id": "dll", "key_path": f"us#{offset}",
        "original": original, "translation": translation,
        "meta": json.dumps({
            "kind": "us", "record_offset": offset, "utf16_len": utf16_len,
            "disposition": "translate", **extra,
        }),
    }


# --------------------------------------------------------------------------
# patch_metadata_strings 纯函数：explicit 布局（v24/27/29/31）
# --------------------------------------------------------------------------

def test_explicit_shortened_translation_no_trailing_nul():
    """F1 核心：缩短译文后记录 <length> 字段 = 实际字节数，运行时按记录
    长度读取 = 译文，绝不带尾部 NUL 填充。"""
    raw = _build(31, ["Hello player"])            # 12 字节
    patched = il2cpp.patch_metadata_strings(raw, {0: "你好".encode("utf-8")})
    assert _records(patched) == {0: (6, 0x200)}
    # 游戏侧按记录长度读取 = 译文，无 NUL
    assert patched[0x200:0x200 + 6].decode("utf-8") == "你好"
    # 文件总长与 header 布局完全不变
    assert len(patched) == len(raw)
    assert struct.unpack_from("<II", patched, 0) == (METADATA_MAGIC, 31)


def test_explicit_equal_length_translation_keeps_length():
    raw = _build(24, ["Hello player"])
    payload = "你好世界".encode("utf-8")           # 恰好 12 字节 == 容量
    patched = il2cpp.patch_metadata_strings(raw, {0: payload})
    assert _records(patched) == {0: (12, 0x200)}
    assert patched[0x200:0x200 + 12].decode("utf-8") == "你好世界"


def test_explicit_multiple_changes_and_untouched_record():
    raw = _build(27, ["Hello player", "Press start", "Goodbye"])
    patched = il2cpp.patch_metadata_strings(raw, {
        0: "你好".encode("utf-8"),                  # 12 → 6
        12: "开始".encode("utf-8"),                 # 11 → 6
    })
    records = _records(patched)
    assert records[0] == (6, 0x200)                # 原位覆盖
    assert records[12] == (6, 0x200 + 12)          # 原位覆盖
    assert records[23] == (7, 0x200 + 23)          # 未写条目原样不动
    assert patched[0x200:0x200 + 6].decode("utf-8") == "你好"
    assert patched[0x200 + 12:0x200 + 18].decode("utf-8") == "开始"
    assert patched[0x200 + 23:0x200 + 30].decode("utf-8") == "Goodbye"


def test_explicit_empty_changes_returns_original_object():
    raw = _build(31, ["Hello player"])
    assert il2cpp.patch_metadata_strings(raw, {}) is raw


# --------------------------------------------------------------------------
# patch_metadata_strings 纯函数：v39 implicit 链式重建
# --------------------------------------------------------------------------

def test_v39_shorten_first_record_chains_all_indexes():
    """F1 核心（v39）：无长度字段，收缩后全部记录连续紧凑排列，dataSize
    同步改小（末条差分以 data_size 为锚）——运行时差分读取长度 = 实际
    字节数，空洞落在数据区声明之外，绝不进字符串。"""
    raw = _build(39, ["Alpha", "Beta", "Gamma"])   # 数据区 14 字节
    patched = il2cpp.patch_metadata_strings(raw, {0: b"A"})
    records = _records(patched)
    assert records[0] == (1, 0x200)                # "A"（5 → 1）
    assert records[1] == (4, 0x201)                # "Beta" 前移，差分 = 实际长度
    assert records[5] == (5, 0x205)                # "Gamma" 前移到紧凑尾部
    assert patched[0x200:0x201].decode("utf-8") == "A"
    assert patched[0x201:0x205].decode("utf-8") == "Beta"
    assert patched[0x205:0x20A].decode("utf-8") == "Gamma"
    # dataSize 同步改小为总长 10；空洞（原数据区尾部）清零且在声明之外
    assert struct.unpack_from("<I", patched, 0x18)[0] == 10
    assert patched[0x20A:0x20E] == b"\x00" * 4
    assert struct.unpack_from("<II", patched, 0) == (METADATA_MAGIC, 39)


def test_v39_shorten_last_record_compacts_tail():
    raw = _build(39, ["Alpha", "Beta", "Gamma"])
    patched = il2cpp.patch_metadata_strings(raw, {9: b"G"})
    records = _records(patched)
    assert records[0] == (5, 0x200)                # 未写条目原位
    assert records[5] == (4, 0x205)                # 未写条目原位
    assert records[9] == (1, 0x209)                # 末条紧凑，差分 = 10-9 = 1
    assert patched[0x209:0x20A].decode("utf-8") == "G"
    assert struct.unpack_from("<I", patched, 0x18)[0] == 10
    assert patched[0x20A:0x20E] == b"\x00" * 4     # 空洞清零且在声明之外


def test_v39_all_records_changed_compact_head():
    raw = _build(39, ["Alpha", "Beta", "Gamma"])
    patched = il2cpp.patch_metadata_strings(raw, {
        0: b"A", 5: b"B", 9: b"G",
    })
    records = _records(patched)
    assert records[0] == (1, 0x200)
    assert records[1] == (1, 0x201)
    assert records[2] == (1, 0x202)                # 末条紧凑，差分 = 3-2 = 1
    assert patched[0x200:0x201] == b"A"
    assert patched[0x201:0x202] == b"B"
    assert patched[0x202:0x203] == b"G"
    assert struct.unpack_from("<I", patched, 0x18)[0] == 3


def test_v39_empty_changes_returns_original_object():
    raw = _build(39, ["Alpha", "Beta"])
    assert il2cpp.patch_metadata_strings(raw, {}) is raw


# --------------------------------------------------------------------------
# patch_metadata_strings 纯函数：防御
# --------------------------------------------------------------------------

def test_patch_metadata_rejects_oversized_payload():
    raw = _build(31, ["Hello player"])             # 容量 12 字节
    with pytest.raises(ValueError, match="超过容量"):
        il2cpp.patch_metadata_strings(
            raw, {0: "你好世界玩家啊".encode("utf-8")})   # 18 字节


def test_patch_metadata_rejects_unknown_data_index():
    raw = _build(31, ["Hello player"])
    with pytest.raises(ValueError, match="不在字面量记录表中"):
        il2cpp.patch_metadata_strings(raw, {99: b"hi"})


def test_patch_metadata_rejects_non_metadata_file():
    with pytest.raises(ValueError, match="magic"):
        il2cpp.patch_metadata_strings(b"\x00" * 64, {0: b"hi"})


def test_patch_metadata_rejects_unsupported_version():
    raw = bytearray(_build(31, ["Hello"]))
    struct.pack_into("<I", raw, 4, 32)             # 版本号改为未知
    with pytest.raises(ValueError, match="不支持的 metadata 版本"):
        il2cpp.patch_metadata_strings(bytes(raw), {0: b"hi"})


# --------------------------------------------------------------------------
# _patch_metadata 集成：写回 + 重开验证
# --------------------------------------------------------------------------

def test_patch_metadata_integration_explicit_verify_no_rstrip(tmp_path):
    """集成：真实文件写回 + 重开验证（内部严格 length == len(payload)，
    这里再按重开后的记录长度读取，证明运行时无尾部 NUL）。"""
    raw = _build(31, ["Hello player", "Press start"])
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    result = WriteResult()
    _patch_metadata(path, [
        _meta_entry(0x200, 12, "Hello player", "你好"),
        _meta_entry(0x200 + 12, 11, "Press start", "开始"),
    ], result)
    assert result.written == 2
    assert result.rejected == []
    reopened = path.read_bytes()
    records = _records(reopened)
    assert records[0] == (6, 0x200)
    assert records[12] == (6, 0x20C)
    assert reopened[0x200:0x200 + 6].decode("utf-8") == "你好"
    assert reopened[0x20C:0x20C + 6].decode("utf-8") == "开始"


def test_patch_metadata_integration_v39_chain(tmp_path):
    raw = _build(39, ["Alpha", "Beta", "Gamma"])
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    result = WriteResult()
    _patch_metadata(path, [
        _meta_entry(0x200, 5, "Alpha", "A"),
        _meta_entry(0x205, 4, "Beta", "B"),
        _meta_entry(0x209, 5, "Gamma", "G"),
    ], result)
    assert result.written == 3
    reopened = path.read_bytes()
    assert reopened[0x200:0x201] == b"A"
    assert reopened[0x201:0x202] == b"B"
    assert reopened[0x202:0x203] == b"G"


def test_patch_metadata_integration_truncation_records_and_verifies(tmp_path):
    """截断路径：译文超容量按字符截断 + 省略号，重开验证仍严格通过
    （长度字段/链式索引同步更新，绝不填 NUL 掩盖）。"""
    raw = _build(39, ["Hello player"])             # 12 字节，v39 单条=末条
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    result = WriteResult()
    _patch_metadata(path, [
        _meta_entry(0x200, 12, "Hello player", "你好世界很长"),
    ], result)
    assert result.written == 1
    assert result.truncated == 1
    reopened = path.read_bytes()
    length, data_pos = _records(reopened)[0]
    # 运行时按记录读取 = 截断后译文（"你好世…" 12 字节），无尾部 NUL
    assert length == 12
    assert reopened[data_pos:data_pos + length].decode("utf-8") == "你好世…"


def test_patch_metadata_restores_placeholder_within_capacity(tmp_path):
    """F2：译文缺 {0} → 机械恢复补末尾；恢复后恰好等于容量 → 不截断，
    占位符完整保留，写回成功（好译文不被 reject 丢弃）。"""
    raw = _build(31, ["Press {0} to continue"])    # 21 字节
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    result = WriteResult()
    _patch_metadata(path, [
        _meta_entry(0x200, 21, "Press {0} to continue", "点击开始游戏"),
    ], result)
    assert result.written == 1
    assert result.rejected == []
    reopened = path.read_bytes()
    patched = il2cpp.parse_string_literals(reopened)
    assert patched[0][1] == 21                     # 「点击开始游戏{0}」21 字节
    data_off = patched[0][2]
    assert reopened[data_off:data_off + 21].decode("utf-8") == "点击开始游戏{0}"


def test_patch_metadata_placeholder_order_free_passes(tmp_path):
    """F2 边界：译文含全部占位符（顺序无关）→ 不拒绝。string.Format
    按索引取参，顺序变化不崩溃。"""
    raw = _build(31, ["Hello {0} {1}"])             # 13 字节
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    result = WriteResult()
    _patch_metadata(path, [
        _meta_entry(0x200, 13, "Hello {0} {1}", "{1}你好{0}"),
    ], result)
    assert result.written == 1
    assert result.rejected == []


def test_patch_metadata_skips_non_metadata_file(tmp_path):
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(b"\x00" * 64)
    result = WriteResult()
    _patch_metadata(path, [_meta_entry(4, 12, "Hello", "你好")], result)
    assert path.read_bytes() == b"\x00" * 64
    assert result.written == 0
    assert result.attempted == 0


def test_patch_metadata_no_write_entries_leaves_file_untouched(tmp_path):
    """所有条目 disposition=structural → 无变更 → 文件原样（含 mtime 语义）。"""
    raw = _build(39, ["Alpha", "Beta"])
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    result = WriteResult()
    entry = _meta_entry(0x200, 5, "Alpha", "A")
    entry["meta"] = json.dumps({
        "kind": "il2cpp", "file_offset": 0x200, "length": 5,
        "disposition": "structural", "role": "structural",
    })
    _patch_metadata(path, [entry], result)
    assert path.read_bytes() == raw
    assert result.written == 0


def test_patch_metadata_reopen_verifies_unpatched_records(tmp_path, monkeypatch):
    """C3：重开验证全池比对——紧凑重建若静默损坏未补丁记录（内容被改但
    仍合法 UTF-8/结构），此前只查被改记录会静默通过，现在拒绝。"""
    raw = _build(39, ["Alpha", "Beta", "Gamma"])
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    real_patch = il2cpp.patch_metadata_strings

    def corrupting_patch(raw_bytes, changes):
        out = bytearray(real_patch(raw_bytes, changes))
        # 破坏未补丁记录 Beta（重建后前移到 0x201..0x205）——内容仍合法
        # ASCII/UTF-8，结构完整，旧验证（只查被改记录）无法发现
        assert out[0x201:0x205] == b"Beta"
        out[0x201:0x205] = b"Zzzz"
        return bytes(out)

    monkeypatch.setattr(il2cpp, "patch_metadata_strings", corrupting_patch)
    with pytest.raises(ValueError, match="未补丁记录"):
        _patch_metadata(path, [_meta_entry(0x200, 5, "Alpha", "A")],
                        WriteResult())


def test_patch_metadata_reopen_rejects_record_count_change(tmp_path, monkeypatch):
    """C3：重建增删记录（末条之后游标偏差/空洞清零越界）→ 记录数变化
    必须拒绝——未补丁记录逐条比对的前提是数量一致。"""
    raw = _build(39, ["Alpha", "Beta", "Gamma"])
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    real_patch = il2cpp.patch_metadata_strings

    def dropping_patch(raw_bytes, changes):
        out = bytearray(real_patch(raw_bytes, changes))
        # 删除末条记录（Gamma 0x205..0x20A）并收缩 header 记录数/数据声明：
        # 模拟重建游标偏差——文件仍合法但记录数减少
        out[0x205:0x20A] = b""
        struct.pack_into("<I", out, 0x10, 2)
        struct.pack_into("<I", out, 0x18, len(out) - 0x200)
        return bytes(out)

    monkeypatch.setattr(il2cpp, "patch_metadata_strings", dropping_patch)
    with pytest.raises(ValueError, match="记录数"):
        _patch_metadata(path, [_meta_entry(0x200, 5, "Alpha", "A")],
                        WriteResult())


# --------------------------------------------------------------------------
# _patch_dll 集成：#US 三处联动 + 重开验证
# --------------------------------------------------------------------------

def test_patch_dll_short_translation_shrinks_prefix(tmp_path):
    """F1 核心（#US 侧）：长原文 + 短译文 → 压缩前缀 2 字节 → 1 字节，
    prefix / data / flag 三处联动，flag 移到新数据末尾之后。"""
    original = "Hello World!!"                     # 13 字符 = 26 字节
    heap = _us_heap([(original.encode("utf-16-le"), 1)])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    result = WriteResult()
    _patch_dll(path, [
        _us_entry(1, 26, original, "你好"),
    ], result)
    assert result.written == 1
    blob = path.read_bytes()
    # 新前缀 1 字节（ln = 4 + 1 = 5），原 2 字节前缀残字节被新数据覆盖
    assert blob[1] == 0x05
    assert blob[2:6] == "你好".encode("utf-16-le")
    # flag 重算为 1（含中文字符），位于新数据末尾之后
    assert blob[6] == 1
    # 重开验证已内部执行（流式遍历 + 严格比对）；这里按运行时读取语义复核
    records = {tok: (off, raw)
               for tok, off, raw in _walk_us_heap(path.read_bytes())}
    data_off, raw_bytes = records[1]
    assert raw_bytes == "你好".encode("utf-16-le") + b"\x01"


def _walk_us_heap(data: bytes) -> list[tuple[int, int, bytes]]:
    """测试本地副本：流式遍历 #US 堆（与 mono_dll._walk_us_heap_records 同）。"""
    from hanhua.core.unity.mono_dll import _walk_us_heap_records
    return _walk_us_heap_records(data)


def test_patch_dll_equal_length_keeps_prefix_width(tmp_path):
    original = "Settings"
    heap = _us_heap([(original.encode("utf-16-le"), 0)])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    result = WriteResult()
    _patch_dll(path, [
        _us_entry(1, 16, original, "设置"),
    ], result)
    assert result.written == 1
    blob = path.read_bytes()
    assert blob[1] == 0x05                        # ln = 4 + 1，仍 1 字节
    assert blob[2:6] == "设置".encode("utf-16-le")
    assert blob[6] == 1                           # 中文 → flag 1


def test_patch_dll_preserves_placeholder_when_truncating(tmp_path):
    """#20（F2 #US 侧）：译文超容量截断时占位符优先保留（容量感知修剪，
    不再整条拒绝）——Minato 24 条「缺失 {n} 占位符」写回拒绝的根因。"""
    original = "Press {0} to continue"
    heap = _us_heap([(original.encode("utf-16-le"), 0)])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    result = WriteResult()
    # 译文超容量（60 字节 > 42 = 20 字符），占位符 {0} 位于 25 字符后——
    # 旧行为机械追加补丁被截断掉 → 拒绝；新行为从 body 尾部修剪
    # 到容量内，{0} 保留在末尾（格式不被破坏），截断写入
    _patch_dll(path, [
        _us_entry(1, 42, original, "X" * 25 + " {0}"),
    ], result)
    assert result.written == 1
    assert result.truncated == 1
    assert len(result.rejected) == 0
    blob = path.read_bytes()
    # 记录布局：offset 1 = ln(1B) + 文本(UTF-16) + flag(1B)；容量 42 字节
    # = 21 字符：占位符 tail "{0}"（3 字符，不含相邻空格）优先保留，
    # body 修剪到 21-3=18 字符 → "X"*18 + "{0}"
    text = blob[2:-1].decode("utf-16-le")
    assert text == "X" * 18 + "{0}"
    assert text.endswith("{0}")                 # 占位符保留在末尾
    assert blob[-1] == 0                         # ASCII flag


def test_patch_dll_multiple_entries_verify_all(tmp_path):
    # 记录1（offset 1）= prefix(1)+"A"(2)+flag(1) → 占 1..4
    # 记录2（offset 5）= prefix(1)+"Settings"(16)+flag(1) → 占 5..22
    heap = _us_heap([
        ("A".encode("utf-16-le"), 0),
        ("Settings".encode("utf-16-le"), 0),
    ])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    result = WriteResult()
    _patch_dll(path, [
        _us_entry(5, 16, "Settings", "设置"),
        _us_entry(1, 2, "A", "B"),                 # 填充记录也可写
    ], result)
    assert result.written == 2
    blob = path.read_bytes()
    # 记录 @1："B"（2 字节）+ flag（ASCII → 0），前缀 0x03，占 1..4
    assert blob[1] == 0x03
    assert blob[2:4] == "B".encode("utf-16-le")
    assert blob[4] == 0
    # 记录 @5："设置"（4 字节）+ flag（中文 → 1），前缀 0x05，占 5..10
    assert blob[5] == 0x05
    assert blob[6:10] == "设置".encode("utf-16-le")
    assert blob[10] == 1


# --------------------------------------------------------------------------
# A11 写回镜像门：场景名语料命中的 #US 条目 → 语义回退（不写不拒）
# --------------------------------------------------------------------------
#
# midnight-maid-night 黑屏实证：tag/name 比较键（"Ruth"/"Hallway Trigger"）
# 进池翻译写回 → tag == "Ruth" 恒 false → NRE 级联黑屏。提取侧语料门拦
# 新扫描，此门兜存量库 translated 条目。回归断言：
# - note_logic_reverted 记账（reverted_locators + logic_reverted_sources，
#   W3 运行时排除表），绝不 note_rejected（不阻断发布）；
# - 磁盘字节保真（语料命中的记录原样保留）；
# - 非语料条目照常写入（门不误伤）。

def test_patch_dll_scene_name_corpus_gate_reverts(tmp_path):
    """语料命中 → 语义回退 + 字节保真；同文件非语料条目照常写。

    记录布局：@1 = prefix(1)+"Ruth"(8)+flag(1) → 占 1..10；
    @11 = prefix(1)+"Settings"(16)+flag(1) → 占 11..28。
    """
    heap = _us_heap([
        ("Ruth".encode("utf-16-le"), 0),
        ("Settings".encode("utf-16-le"), 0),
    ])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    before = path.read_bytes()
    result = WriteResult()
    _patch_dll(path, [
        _us_entry(1, 8, "Ruth", "露丝"),
        _us_entry(11, 16, "Settings", "设置"),
    ], result, scene_names=frozenset({"Ruth", "Hallway Trigger"}))
    # 'Settings' 正常写入；'Ruth' 语义回退（不 written 不 rejected）
    assert result.written == 1
    assert len(result.rejected) == 0
    blob = path.read_bytes()
    # 记录 @11 照常写
    assert blob[11] == 0x05
    assert blob[12:16] == "设置".encode("utf-16-le")
    # 记录 @1（Ruth）字节保真：与写回前完全一致
    assert blob[1:11] == before[1:11]
    assert blob[1:2] == b"\x09"
    assert blob[2:10] == "Ruth".encode("utf-16-le")
    # 语义回退记账：logic_reverted（发布侧 W3 排除表）+ 审计段
    assert result.reverted_locators == {"dll:us#1"}
    assert "Ruth" in result.logic_reverted_sources
    audit_reasons = [rec.get("reason") for rec in result.logic_audit
                     if rec.get("stage") == "semantic_revert"]
    assert audit_reasons == ["scene_name_corpus_gate"]


def test_patch_dll_scene_name_corpus_requires_exact_match(tmp_path):
    """全等匹配：语料含 'Ruth' 但条目是含它的句子 → 不命中，照常写。"""
    original = "Ruth is waiting"
    heap = _us_heap([(original.encode("utf-16-le"), 0)])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    result = WriteResult()
    _patch_dll(path, [
        _us_entry(1, 30, original, "露丝在等你"),
    ], result, scene_names=frozenset({"Ruth"}))
    assert result.written == 1
    assert len(result.rejected) == 0
    assert result.reverted_locators == set()
    blob = path.read_bytes()
    assert blob[2:2 + 10] == "露丝在等你".encode("utf-16-le")


def test_patch_dll_scene_name_corpus_empty_is_noop(tmp_path):
    """空语料（存量库无 KV）→ 门不生效，行为与 0.46.0 完全一致。"""
    original = "Ruth"
    heap = _us_heap([(original.encode("utf-16-le"), 0)])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    result = WriteResult()
    _patch_dll(path, [
        _us_entry(1, 8, original, "露丝"),
    ], result, scene_names=frozenset())
    assert result.written == 1
    assert result.reverted_locators == set()


# --------------------------------------------------------------------------
# 被 parse 过滤的记录（空/非 UTF-8）不得破坏记录区写入
# --------------------------------------------------------------------------
#
# cosl/minato 真实实证：metadata 池含 parse 会过滤掉的记录（length==0
# 或非 UTF-8 字节）。旧实现按 valid 记录序号写记录区：
# - explicit：序号偏移 → length 字段写错条目 → 重开区间重叠（cosl）
# - implicit：过滤条目残留旧 dataIndex，dataSize 缩小后越界 → 重开解析
#   整体拒绝（verify=[]，minato 末 2 条残留 528463/528466 > 新 dataSize）
# 回归断言：写回后重开 parse 数量与内容不变，被过滤记录数据原样保留。

def test_explicit_mid_filtered_record_no_misalignment():
    """中间夹一条非 UTF-8 记录 → length 字段仍写到正确条目。"""
    raw = _build(31, ["Hello", b"\xff\xfe\xff\xfe", "World"])
    patched = il2cpp.patch_metadata_strings(raw, {0: "好".encode("utf-8")})
    assert _records(patched) == {0: (3, 0x200), 9: (5, 0x209)}
    # 记录区逐条：第 0 条 length=3（未偏移），被过滤记录 length 原样，
    # 末条 length=5
    assert struct.unpack_from("<II", patched, 0x100) == (3, 0)
    assert struct.unpack_from("<II", patched, 0x108) == (4, 5)
    assert struct.unpack_from("<II", patched, 0x110) == (5, 9)
    # 非 UTF-8 记录数据原位未动（explicit 不搬移，只覆盖被改记录）
    assert patched[0x205:0x209] == b"\xff\xfe\xff\xfe"


def test_explicit_duplicate_data_index_all_records_updated():
    """同一 data_index 多条记录（空字符串共享数据）：只更新 length>0 的
    实际条目，空记录保持 0（运行时读空，不新增区间重叠）。"""
    # "" 与 "Hello" 共享 data_index 0（空字符串长 0，紧跟前者偏移）
    raw = _build(31, ["", "Hello", "World"])
    patched = il2cpp.patch_metadata_strings(raw, {0: "好".encode("utf-8")})
    assert struct.unpack_from("<II", patched, 0x100) == (0, 0)   # 空记录保持 0
    assert struct.unpack_from("<II", patched, 0x108) == (3, 0)   # 实际条目更新
    assert struct.unpack_from("<II", patched, 0x110) == (5, 5)
    assert _records(patched) == {0: (3, 0x200), 5: (5, 0x205)}


def test_v39_filtered_records_chain_all_entries():
    """空记录 + 非 UTF-8 记录（夹中间）→ 记录区全部条目链式更新。"""
    raw = _build(39, ["Hello", "", b"\xff\xff", "World"])
    patched = il2cpp.patch_metadata_strings(raw, {0: "好".encode("utf-8")})
    # 全部 4 条 dataIndex 链式更新且不越界
    indexes = struct.unpack_from("<4I", patched, 0x100)
    data_size = struct.unpack_from("<I", patched, 0x18)[0]
    assert data_size == 10
    assert all(di <= data_size for di in indexes)
    assert tuple(indexes) == (0, 3, 3, 5)
    # 重开 parse：valid 记录内容正确（World 链式更新到 5），非 UTF-8
    # 数据搬运到紧凑链中（0x203:0x205）
    assert _records(patched) == {0: (3, 0x200), 5: (5, 0x205)}
    assert patched[0x203:0x205] == b"\xff\xff"


def test_v39_filtered_tail_record_no_stale_index():
    """末尾夹非 UTF-8 记录（minato 实证场景）：记录区末条残留旧
    dataIndex 会在 dataSize 缩小后越界 → 全部条目必须链式更新。"""
    raw = _build(39, ["Hello", "World", b"\x00\xff\x00\xfe"])
    patched = il2cpp.patch_metadata_strings(raw, {0: "好".encode("utf-8")})
    indexes = struct.unpack_from("<3I", patched, 0x100)
    data_size = struct.unpack_from("<I", patched, 0x18)[0]
    assert data_size == 12
    assert all(di < data_size for di in indexes)   # 无越界残留
    assert tuple(indexes) == (0, 3, 8)
    # World 链式更新到 3，被过滤记录数据搬到紧凑链尾部（0x208:0x20C）
    assert _records(patched) == {0: (3, 0x200), 3: (5, 0x203)}
    assert patched[0x208:0x20C] == b"\x00\xff\x00\xfe"


# --------------------------------------------------------------------------
# 同源盲区消除：独立读取器交叉验证
# --------------------------------------------------------------------------

def test_cross_validate_pool_matches_synthetic():
    """合成文件（含空/非 UTF-8 记录）：parse 与独立读取器交叉一致。"""
    assert il2cpp._cross_validate_pool(
        _build(39, ["Alpha", "", b"\xff\xff", "Gamma"]))
    assert il2cpp._cross_validate_pool(
        _build(31, ["", "Hello", b"\xfe", "World"]))


def test_cross_validate_pool_rejects_overlap():
    """记录区间重叠（parse 防御拒绝 → []，独立读取器无防御 → 分叉）。"""
    data = b"ABC"
    lit = struct.pack("<II", 2, 0) + struct.pack("<II", 2, 1)  # [0,2) 与 [1,3) 重叠
    header = bytearray(0x30)
    struct.pack_into("<II", header, 0, METADATA_MAGIC, 31)
    struct.pack_into("<II", header, 0x10, 0x200, 3)
    struct.pack_into("<II", header, 0x08, 0x100, 16)
    raw = (bytes(header) + b"\x00" * (0x100 - 0x30) + lit
           + b"\x00" * (0x200 - 0x100 - 16) + data)
    assert il2cpp.parse_string_literals(raw) == []    # parse 防御拒绝
    assert not il2cpp._cross_validate_pool(raw)       # 交叉验证同样失败


def test_cross_validate_pool_rejects_non_pool():
    """非 metadata / 空文件 → 交叉验证失败。"""
    assert not il2cpp._cross_validate_pool(b"")
    assert not il2cpp._cross_validate_pool(b"\x00" * 0x40)


# --------------------------------------------------------------------------
# 写回差异白名单（字节范围锁定：数据区/记录区之外零字节被碰）
# --------------------------------------------------------------------------

def test_diff_whitelist_explicit_passes_normal_patch():
    """explicit 正常补丁：差异全部落在白名单内（不抛），未写数据原样。"""
    raw = _build(31, ["Hello player", "Press start"])
    patched = il2cpp.patch_metadata_strings(raw, {0: "好".encode("utf-8")})
    # 数据区剩余容量保持原字节（未被任何记录引用）
    assert patched[0x203:0x20C] == raw[0x203:0x20C]


def test_diff_whitelist_explicit_rejects_outside_change():
    """白名单外字节变动（header lit_size 字段被意外改动）→ ValueError。"""
    raw = _build(31, ["Hello player"])
    blob = bytearray(raw)
    blob[0x0C] ^= 0x01                                # lit_size 字段
    with pytest.raises(ValueError, match="白名单"):
        il2cpp._assert_diff_whitelist(
            raw, blob, record_mode="explicit", lit_off=0x100,
            lit_table_size=8, data_off=0x200, data_size=12,
            data_size_pos=0x14, entry_size=8,
            changes={0: "好".encode("utf-8")}, by_index={0: (12, 0x200)})


def test_diff_whitelist_implicit_rejects_header_change():
    """implicit：header 版本字段被意外改动 → ValueError。"""
    raw = _build(39, ["Alpha", "Beta", "Gamma"])
    blob = bytearray(raw)
    blob[0x04] ^= 0x01                                # 版本字段
    with pytest.raises(ValueError, match="白名单"):
        il2cpp._assert_diff_whitelist(
            raw, blob, record_mode="implicit", lit_off=0x100,
            lit_table_size=12, data_off=0x200, data_size=14,
            data_size_pos=0x18, entry_size=4,
            changes={0: b"A"}, by_index={0: (5, 0x200)}, cursor=10)


# --------------------------------------------------------------------------
# 占位符全量校验：未截断但缺 {n} 也拒绝（不依赖截断路径）
# --------------------------------------------------------------------------

def test_patch_dll_restores_missing_placeholder_without_truncation(tmp_path):
    """译文未截断但删掉 {0} → 机械恢复补到末尾并写入成功（string.Format
    按索引取参位置无关；模型漏写 {n} 是稳定行为，补回比 reject 丢弃
    好译文更优）。"""
    original = "Press {0} to continue"
    heap = _us_heap([(original.encode("utf-16-le"), 0)])
    path = tmp_path / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    result = WriteResult()
    _patch_dll(path, [_us_entry(1, 42, original, "继续")], result)
    assert result.written == 1
    assert result.rejected == []
    blob = path.read_bytes()
    # 恢复后译文 = 「继续{0}」10 字节（5 码元）+ flag → 前缀 0x0B
    assert blob[1] == 0x0B
    assert blob[2:12].decode("utf-16-le") == "继续{0}"


def test_patch_metadata_restores_missing_placeholder_without_truncation(tmp_path):
    raw = _build(31, ["Press {0} to continue"])
    path = tmp_path / "global-metadata.dat"
    path.write_bytes(raw)
    result = WriteResult()
    _patch_metadata(
        path, [_meta_entry(0x200, 21, "Press {0} to continue", "继续")], result)
    assert result.written == 1
    assert result.rejected == []
    reopened = path.read_bytes()
    patched = il2cpp.parse_string_literals(reopened)
    assert patched[0][1] == 9          # 「继续{0}」9 字节
    assert patched[0][2] is not None
    data_off = patched[0][2]
    assert reopened[data_off:data_off + 9].decode("utf-8") == "继续{0}"
