"""SerializedFile 布局保真：gen 9-21 / gen>=22 data_offset 恢复 + gen<9 字段
修正（0.46.0 / 0.49.0）。

问题（场景写坏根因 B，问题集 A10/A13）：
    UnityPy ``SerializedFile.save()`` 会重算 ``data_offset``，**丢弃原文件
    的布局**：
    - gen 9-21：``data_offset = 20 + metadata_size``（16 对齐）——原文件
      常页对齐（如 4096），保存后变 3904/1328/720 任意值；
    - gen >= 22（LargeFilesSupport，Unity 2020+，48 字节头）：``data_offset
      = 48 + metadata_size``（16 对齐）——同样丢弃原页对齐布局（实证：
      catfiend globalgamemanagers 4096 → 1216）。
    这是对「最小变更」原则的破坏：写回只改文本字段，文件头布局却被重排。

    gen < 9（legacy，metadata 在文件尾）：UnityPy 保存把头部 data_offset
    字段**硬编码 32**，而原文件按 AssetRipper 写序语义 data_offset=16
    （16 字节头后即 16 对齐，数据紧邻头部）。字段漂移使 UnityPy 自身
    重开（byte_start += data_offset）整体偏移 16 字节。仅当原 doff==16
    且数据物理位置一致时修回字段原值；其余形态语料为零（D:\\游戏
    42303 文件 0 例 gen<9），宁漏勿坏原样返回。

布局参照（resources/AssetRipper-2.0.0，SerializedFileHeader.cs）：
    gen 9-21：``<meta:u32, file_size:u32, version:u32, data_offset:u32>``
    @0x00 + endian bool @0x10 + 3 reserved = 20 字节头，metadata 随后。
    gen >= 22：0x00-0x13 零（version:u32 @0x08）+ endian @0x10 + reserved
    + ``meta:u32 @0x14, file_size:i64 @0x18, data_offset:i64 @0x20,
    unknown:i64 @0x28`` = 48 字节头（HasLargeFilesSupport = gen>=22）。
    gen < 9：16 字节头（无 endian 块，HasEndianess = gen>=9），数据紧随
    头部，endian 字节 + metadata 在文件尾（IsMetadataAtTheEnd = gen<9，
    endian 位置 = file_size - metadata_size，metadata_size 含 endian 字节）。

修复：
    保存后按原文件头重建字节流——原 data_offset 大于 UnityPy 计算的
    自然布局终点且 16 字节对齐时，用零填充恢复原 data_offset 布局。
    其中 ``file_size' = data_offset + (新文件长 - 新 data_offset)``。
    对象 byte_start 在 metadata 里是数据区相对偏移（读侧统一
    ``+= data_offset``），填充对 metadata 透明。

    实证（fake-it level3，gen 21，原 data_offset=4096）：重建后 UnityPy
    重开正常，128 对象全量字节逐一恒等，对象集合恒等。

    原布局不满足条件（data_offset 更小 / 未对齐 / 元数据长大撑破原
    布局）时原样返回——错误的 data_offset 恢复比不恢复更危险。
"""
from __future__ import annotations

import struct
from pathlib import Path

# gen 9..21：头部 = 4*4 + 1 endian + 3 reserved = 20 字节
_GEN9_HEADER_BYTES = 20
# gen >= 22：0x14 零前导 + 4 + 3*8 = 48 字节（LargeFilesSupport）
_GEN22_HEADER_BYTES = 48
# gen < 9：16 字节头（无 endian 块），数据紧随头部
_LEGACY_HEADER_BYTES = 16


def original_header(raw: bytes, *,
                    file_size: int | None = None,
                    endian_byte: bytes | None = None,
                    ) -> tuple[int, int, int, int] | None:
    """读原文件头 (metadata_size, file_size, version, data_offset)。

    支持三个世代：
    - gen 9..21：20 字节头，UnityPy 保存重算 data_offset（A10）；
    - gen >= 22：48 字节头（meta @0x14 / file_size:i64 / data_offset:i64），
      UnityPy 保存同样重算 data_offset（A13）；
    - gen < 9：16 字节头 + 文件尾 metadata，UnityPy 保存硬编码
      data_offset=32（A13）。

    不满足自洽校验返回 None（不处理）。

    file_size 用于头部字段自检（真实文件长度）：流式调用（restore_from_path）
    只读头部若干字节时必须传，否则 ``头长+metadata_size > 缓冲区长度``
    恒成立 → 全部误判非法返回 None → data_offset 恢复静默失效（A10b）。

    endian_byte（单字节）是 gen<9 必需的尾部探针：legacy 布局 metadata
    在文件尾，endian 字节位于 ``file_size - metadata_size``——拿不到该
    字节（值非 0/1）就无法确认「metadata 在尾」这一前提，返回 None。
    全文件驻留调用（saved_container_bytes）可自动从缓冲区取；流式调用
    （restore_from_path）由调用方 seek 读取后传入。
    """
    total = file_size if file_size is not None else len(raw)
    if len(raw) >= _GEN22_HEADER_BYTES:
        version, = struct.unpack(">I", raw[8:12])
        if version >= 22:
            return _header_gen22(raw, total)
    if len(raw) >= 16:
        meta, fsize_field, version, doff = struct.unpack(">IIII", raw[:16])
        if 9 <= version < 22:
            return _header_gen9(raw, meta, fsize_field, version, doff, total)
        if 2 <= version < 9:
            return _header_legacy(
                raw, meta, fsize_field, version, doff, total, endian_byte)
    return None


def _header_gen9(raw: bytes, meta: int, fsize_field: int, version: int,
                 doff: int, total: int) -> tuple[int, int, int, int] | None:
    # 合法性自检：头部字段必须自洽（真实文件至少容纳头部 + 元数据）。
    # 参照长度优先取调用方给出的真实文件长度；只有整文件驻留的调用
    # （saved_container_bytes）才可用缓冲区自身长度。
    if fsize_field != total:
        # header 的 file_size 与真实长度恒等是后续一切重建的前提
        # （restore_data_offset 直接用 s_size 推导新长度）。原文件
        # 被裁剪/追加过（如 bundle 内嵌流）→ 不处理，交 UnityPy 原生
        # 布局（宁漏勿坏：错误的 data_offset 恢复比不恢复更危险）。
        return None
    if 20 + meta > total:
        return None
    return meta, fsize_field, version, doff


def _header_gen22(raw: bytes,
                  total: int) -> tuple[int, int, int, int] | None:
    # gen>=22 头部：0x00-0x07 零、version @0x08、0x0c-0x0f 零、
    # endian @0x10、reserved、meta:u32 @0x14、file_size:i64 @0x18、
    # data_offset:i64 @0x20（unknown @0x28 保存时原样透传，不参与重建）
    if len(raw) < _GEN22_HEADER_BYTES:
        return None
    if raw[:8] != b"\x00" * 8 or raw[0x0c:0x10] != b"\x00" * 4:
        return None
    version, = struct.unpack(">I", raw[8:12])
    meta, = struct.unpack(">I", raw[0x14:0x18])
    fsize, doff = struct.unpack(">qq", raw[0x18:0x28])
    if fsize != total:
        return None
    if _GEN22_HEADER_BYTES + meta > total:
        return None
    if not _GEN22_HEADER_BYTES <= doff <= total:
        return None
    return meta, fsize, version, doff


def _header_legacy(raw: bytes, meta: int, fsize_field: int, version: int,
                   doff: int, total: int, endian_byte: bytes | None,
                   ) -> tuple[int, int, int, int] | None:
    # gen<9：metadata 在文件尾（endian 字节位于 file_size - metadata_size，
    # metadata_size 含该字节）。endian 字节必须可探且值为 0/1——这是
    # 「metadata 真在尾部」的唯一形态学证据，拿不到就不处理。
    if fsize_field != total:
        return None
    if meta < 1 or total - meta < _LEGACY_HEADER_BYTES:
        return None
    if not _LEGACY_HEADER_BYTES <= doff <= total - meta:
        return None
    if endian_byte is None:
        # 全文件驻留且缓冲区覆盖尾部时自动探；否则（流式未传参）拒绝
        if fsize_field - meta < len(raw):
            endian_byte = raw[fsize_field - meta:fsize_field - meta + 1]
        else:
            return None
    if endian_byte not in (b"\x00", b"\x01"):
        return None
    return meta, fsize_field, version, doff


def restore_data_offset(saved: bytes, orig_header: tuple[int, int, int, int]
                        ) -> bytes:
    """按原 data_offset 布局重建 saved 字节流（条件不满足原样返回）。

    saved 是 UnityPy ``container.save()`` 的产物；orig_header 是同一
    文件保存前 ``original_header()`` 的结果。按 version 分派三个世代：
    gen >= 22 → 48 字节头重建；gen 9-21 → 20 字节头重建；gen < 9 →
    data_offset 字段修正。重建只在原 data_offset 超出 UnityPy 自然布局
    终点且 16 字节对齐时进行——其余情况 UnityPy 的布局即与原文件一致
    或更保守，不动。
    """
    if orig_header is None:
        return saved
    _meta, _size, version, _off = orig_header
    if version >= 22:
        return _restore_gen22(saved, orig_header)
    if version < 9:
        return _restore_legacy(saved, orig_header)
    return _restore_gen9(saved, orig_header)


def _restore_gen9(saved: bytes,
                  orig_header: tuple[int, int, int, int]) -> bytes:
    o_meta, _o_size, version, o_offset = orig_header
    if len(saved) < 24:
        return saved
    s_meta, s_size, _s_ver, s_offset = struct.unpack(">IIII", saved[:16])
    # 保存产物头与原头必须同版本同元数据长（save 不改这两项）
    if _s_ver != version or s_meta != o_meta:
        return saved
    natural_end = _GEN9_HEADER_BYTES + s_meta
    # 原布局必须 ≥ 自然终点（否则原文件本就是紧凑布局，UnityPy 对齐
    # 后的 s_offset 可能略大于原值——那是 16 对齐导致的确定性偏移，
    # 不是重排；此时 s_offset == 原布局对齐值即视为一致）
    if o_offset < natural_end:
        return saved
    if o_offset % 16:
        return saved
    if s_offset > o_offset:
        # UnityPy 对齐后的 data_offset 反超原值：元数据长大撑破了原
        # 布局（如 typetree 重建变长）。零填充无法收缩——原样返回，
        # 但这属于罕见形态（save_typetree 不改 types 长度），交给
        # 上层验证把关。
        return saved
    if s_offset == o_offset:
        return saved  # 布局已一致
    new_size = o_offset + (s_size - s_offset)
    header = struct.pack(">IIII", s_meta, new_size, version, o_offset)
    # 原文件 [16:20+meta] 是 endian+reserved，saved 同位置即 UnityPy
    # 写出的同 4 字节（endian 不变、reserved 保留），直接复用。
    pad = o_offset - natural_end
    return (header + saved[16:natural_end]
            + b"\x00" * pad + saved[s_offset:])


def _restore_gen22(saved: bytes,
                   orig_header: tuple[int, int, int, int]) -> bytes:
    """gen>=22：48 字节头（LargeFilesSupport）data_offset 恢复。

    UnityPy 保存产物布局：[0x00-0x13 零+version+endian+reserved]
    [meta:u32][file_size:i64][data_offset:i64][unknown:i64][metadata]
    [16 对齐零填充][对象数据]。重建 = 头部 file_size/data_offset 字段
    改回原布局值 + 元数据自然终点到原 data_offset 之间零填充。
    unknown 字段保存时原样透传，复用 saved 字节即可。
    """
    o_meta, _o_size, version, o_offset = orig_header
    if len(saved) < _GEN22_HEADER_BYTES:
        return saved
    s_meta, = struct.unpack(">I", saved[0x14:0x18])
    s_size, s_offset = struct.unpack(">qq", saved[0x18:0x28])
    # 保存产物头与原头必须同元数据长（save 不改这项；version 在
    # saved[8:12]，restore_data_offset 已按 orig_header 分派到这里，
    # 再校验一次防产物形态错位）
    s_ver, = struct.unpack(">I", saved[8:12])
    if s_ver != version or s_meta != o_meta:
        return saved
    # s_offset 即 UnityPy 自然布局终点（48+meta 16 对齐）
    if s_offset < _GEN22_HEADER_BYTES + s_meta:
        return saved
    if o_offset < s_offset:
        # 原布局比自然终点还小：原文件本就是紧凑布局（或仅差 16 对齐
        # 的确定性偏移），不动
        return saved
    if o_offset % 16:
        return saved
    if s_offset > o_offset:
        # 元数据长大撑破原布局——零填充无法收缩，原样返回交上层把关
        return saved
    if s_offset == o_offset:
        return saved  # 布局已一致
    new_size = o_offset + (s_size - s_offset)
    out = bytearray(saved[:s_offset])
    struct.pack_into(">q", out, 0x18, new_size)
    struct.pack_into(">q", out, 0x20, o_offset)
    return bytes(out) + b"\x00" * (o_offset - s_offset) + saved[s_offset:]


def _restore_legacy(saved: bytes,
                    orig_header: tuple[int, int, int, int]) -> bytes:
    """gen<9：data_offset 头部字段修正（metadata 在文件尾）。

    UnityPy gen<9 保存写序与原布局同构：[16 字节头][对象数据]
    [endian 字节][metadata]（file_size = 16 + metadata_size + data_size，
    metadata_size 含 endian 字节）——唯一漂移是头部 data_offset 字段
    **硬编码 32**。原文件按 AssetRipper 写序 data_offset=16（16 字节头
    后即 16 对齐，数据紧邻头部）。仅当原 doff==16（数据物理位置与
    保存产物一致，都是紧邻头部）且字段确实漂移时，把字段修回原值；
    其余形态（原 doff>16 头后带填充 / doff 异常）语料为零，原样返回。
    修字段同时修复 UnityPy 自身重开（byte_start += data_offset）的
    16 字节整体偏移。
    """
    _o_meta, _o_size, version, o_offset = orig_header
    if len(saved) < _LEGACY_HEADER_BYTES:
        return saved
    s_meta, s_size, s_ver, s_doff = struct.unpack(">IIII", saved[:16])
    if s_ver != version or s_doff == o_offset:
        return saved
    if o_offset != _LEGACY_HEADER_BYTES:
        # 原 doff 非 16：数据物理起始无法从保存产物对应（UnityPy 数据
        # 固定写在 16），修字段会造成「字段声称 doff、数据在 16」的
        # 自相矛盾布局——宁漏勿坏
        return saved
    header = struct.pack(">IIII", s_meta, s_size, version, o_offset)
    return header + saved[16:]


def saved_container_bytes(container, raw_original: bytes):
    """容器保存 + data_offset 布局恢复（统一入口，gen 2-8/9-21/>=22）。

    BundleFile/WebFile 不适用（布局由 bundle 结构决定），仅对
    SerializedFile 生效。返回保存字节流。raw_original 是保存前的
    原文件字节（大文件注意：仅在需要时读——SerializedFile 路径
    用流式读头部即可，见 restore_from_path）。
    """
    if type(container).__name__ != "SerializedFile":
        if type(container).__name__ == "BundleFile":
            return container.save(packer="original")
        return container.save()
    return restore_data_offset(container.save(), original_header(raw_original))


def restore_from_path(container, path: Path) -> bytes:
    """saved_container_bytes 的流式头读取变体（E5 内存纪律）。

    只读原文件头部（48 字节覆盖三个世代）+ gen<9 尾部 endian 探针
    （1 字节 seek 读），不整文件驻留。
    """
    if type(container).__name__ != "SerializedFile":
        if type(container).__name__ == "BundleFile":
            return container.save(packer="original")
        return container.save()
    with open(path, "rb") as fh:
        head = fh.read(_GEN22_HEADER_BYTES)
        fh.seek(0, 2)
        total = fh.tell()
        # A10b：头部自检必须参照真实文件长度（流式头永远装不下
        # metadata——全文件误判 None → data_offset 恢复静默失效）
        # gen<9 还需尾部 endian 探针：头部 16 字节里的 version 在
        # [5,9) 时 seek 到 file_size - metadata_size 读 1 字节
        endian_byte = None
        if len(head) >= 16:
            _m, _f, _v, _d = struct.unpack(">IIII", head[:16])
            if 2 <= _v < 9 and _f == total and 1 <= _m and total - _m >= 16:
                fh.seek(total - _m)
                endian_byte = fh.read(1)
    return restore_data_offset(
        container.save(),
        original_header(head, file_size=total, endian_byte=endian_byte))
