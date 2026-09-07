"""SerializedFile 布局保真：gen 9-21 data_offset 恢复（0.46.0）。

问题（场景写坏根因 B，问题集 A10）：
    UnityPy ``SerializedFile.save()`` 对 gen 9-21 文件重算
    ``data_offset = 20 + metadata_size``（再 16 字节对齐），**丢弃原文件
    的 data_offset**。Unity 2019 及更早（version < 22）的真实资产常把
    data_offset 页对齐（如 4096），UnityPy 保存后 4096 → 3904/1328/720
    等任意值。这是对「最小变更」原则的破坏：写回只改了文本字段，
    文件头布局却被重排。此外头部长度也随 metadata_size 变化——
    ``file_size`` 字段偏移在文件间/读写间可能不同，任何假定固定偏移
    的逻辑都不可靠。

修复：
    保存后按原文件头重建字节流——原 data_offset 大于 UnityPy 计算的
    自然布局终点且 16 字节对齐时，用零填充恢复原 data_offset 布局：
    头部 (metadata_size, file_size', version, data_offset) + 元数据 +
    零填充 (data_offset - (20+metadata_size)) + 对象数据。
    其中 ``file_size' = data_offset + (新文件长 - 新 data_offset)``。

    实证（fake-it level3，gen 21，原 data_offset=4096）：
    重建后 UnityPy 重开正常，128 对象全量字节逐一恒等，对象集合恒等。

    原布局不满足条件（data_offset 更小 / 未对齐 / gen>=22）时原样
    返回——gen>=22 布局不同（头在前、值在后），UnityPy 已保真。

    gen < 9 走 legacy 布局（gen<14 头部无 endian 块，gen<9 metadata
    在文件尾），本函数不做处理原样返回。
"""
from __future__ import annotations

import struct
from pathlib import Path

# gen 9..21：头部 = 4*4 + 1 endian + 3 reserved = 20 字节
_GEN9_HEADER_BYTES = 20


def original_header(raw: bytes, *,
                    file_size: int | None = None) -> tuple[int, int, int, int] | None:
    """读原文件头 (metadata_size, file_size, version, data_offset)。

    仅 gen 9..21 返回值（这些版本 UnityPy 会重算 data_offset）；gen>=22
    布局不同（metadata_size 在 33+ 偏移），gen<9 metadata 在文件尾——
    都返回 None（不处理）。

    file_size 用于头部字段自检（真实文件长度）：流式调用（restore_from_path）
    只读 24 字节时必须传，否则 20+metadata_size 恒大于 24 → 全部误判
    非法返回 None → data_offset 恢复静默失效（A10b，fake-it 全部 gen21
    文件 4096→3904/1328/720 实证）。
    """
    if len(raw) < 24:
        return None
    metadata_size, file_size_field, version, data_offset = struct.unpack(
        ">IIII", raw[:16])
    if not 9 <= version < 22:
        return None
    # 合法性自检：头部字段必须自洽（真实文件至少容纳头部 + 元数据）。
    # 参照长度优先取调用方给出的真实文件长度；只有整文件驻留的调用
    # （saved_container_bytes）才可用缓冲区自身长度。
    total = file_size if file_size is not None else len(raw)
    if file_size_field != total:
        # header 的 file_size 与真实长度恒等是后续一切重建的前提
        # （restore_data_offset 直接用 s_size 推导新长度）。原文件
        # 被裁剪/追加过（如 bundle 内嵌流）→ 不处理，交 UnityPy 原生
        # 布局（宁漏勿坏：错误的 data_offset 恢复比不恢复更危险）。
        return None
    if 20 + metadata_size > total:
        return None
    return metadata_size, file_size_field, version, data_offset


def restore_data_offset(saved: bytes, orig_header: tuple[int, int, int, int]
                        ) -> bytes:
    """按原 data_offset 布局重建 saved 字节流（条件不满足原样返回）。

    saved 是 UnityPy ``container.save()`` 的产物；orig_header 是同一
    文件保存前 ``original_header()`` 的结果。重建只在原 data_offset
    超出 UnityPy 自然布局终点且 16 字节对齐时进行——其余情况 UnityPy
    的布局即与原文件一致或更保守，不动。
    """
    if orig_header is None:
        return saved
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


def saved_container_bytes(container, raw_original: bytes):
    """容器保存 + gen 9-21 data_offset 布局恢复（统一入口）。

    BundleFile/WebFile 不适用（布局由 bundle 结构决定），仅对
    SerializedFile 生效。返回保存字节流。raw_original 是保存前的
    原文件字节（大文件注意：仅在需要时读——SerializedFile 路径
    用流式读头部 24 字节即可，见 restore_from_path）。
    """
    if type(container).__name__ != "SerializedFile":
        if type(container).__name__ == "BundleFile":
            return container.save(packer="original")
        return container.save()
    return restore_data_offset(container.save(), original_header(raw_original))


def restore_from_path(container, path: Path) -> bytes:
    """saved_container_bytes 的流式头读取变体（E5 内存纪律）。

    只读原文件前 24 字节做头解析，不整文件驻留。
    """
    if type(container).__name__ != "SerializedFile":
        if type(container).__name__ == "BundleFile":
            return container.save(packer="original")
        return container.save()
    with open(path, "rb") as fh:
        head = fh.read(24)
        # A10b：头部自检必须参照真实文件长度（24 字节流式头永远装不下
        # metadata——全文件误判 None → data_offset 恢复静默失效）
        fh.seek(0, 2)
        total = fh.tell()
    return restore_data_offset(
        container.save(), original_header(head, file_size=total))
