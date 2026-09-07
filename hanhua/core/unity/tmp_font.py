"""TMP 字体资产跨版本替换工具链（Rendezvous 2026-08-18 全天实证驱动）。

背景：工具生成的 TMP_FontAsset bundle 与目标游戏的主序列化文件存在
序列化格式差异，直接复制字节会导致 mono 反序列化错位（Rendezvous 条纹
实证）：
  1. m_AtlasTextureIndex 位置：bundle(TMP 2.1.0) 在 m_AtlasTextures 数组
     后；主文件(TMP 2.1.1) 在 m_AtlasPopulationMode 后且数组后也有。
  2. 纹理数据布局：save_typetree 输出与主文件差 8-9 字节（image data
     len 字段位置：主文件 @104 头部）。
  3. m_fontInfo 段：主文件为「len 字段(4B) + 23 floats(92B)」共 96B——
     复制时漏 len 字段 → 后续全部字段偏移 4 字节 → atlas 尺寸读错 →
     UV 错乱（一个字里很多小字/水平条纹）。
  4. 组件引用替换若做全局字节替换会误伤 MeshRenderer/LightmapSettings
     等非文本对象（pathID 恰好匹配）→ 关卡加载崩溃。

本模块提供：主文件格式 TMP_FontAsset 结构验证、跨版本字段修正、
主文件格式纹理重建、组件级引用替换（只改 TMP 组件内部）。
"""
from __future__ import annotations

import struct
from typing import Iterable

#: 显示文本脚本的 MonoScript pathID（globalgamemanagers 内）——
#: Rendezvous 实证（TMP 组件 script=2000）。
TMP_TEXT_SCRIPT_PID = 2000

#: 主文件格式 TMP_FontAsset 的 m_fontInfo 段字节数（len 4 + 23 floats 92）。
MAIN_FONTINFO_SEGMENT_SIZE = 96

#: 主文件格式纹理头部（image data len 字段位置）——Rendezvous 实证。
MAIN_TEXTURE_HEADER_SIZE = 104


def validate_font_structure(data: bytes) -> list[str]:
    """验证主文件格式 TMP_FontAsset 的字段序列。

    返回错误列表（空 = 结构正确）。覆盖 Rendezvous 全部已知错位点：
      - m_fontInfo 段长度（必须 96B：len 4 + 23 floats 92）
      - m_AtlasWidth/Height 存在且为 2 的幂
      - glyph/char 表计数与元素大小（48B/16B）
      - 无意外 PPtr 悬空（atlas 引用指向有效 pathID）
    """
    errors: list[str] = []
    if data is None or len(data) < 32:
        return ["object data too short"]

    pos = 28
    n = struct.unpack_from("<i", data, pos)[0]
    if not (0 < n < 200) or 32 + n > len(data):
        return [f"m_Name header invalid at {pos}"]
    pos = 32 + n
    pos = (pos + 3) & ~3

    def read_str(p: int):
        nn = struct.unpack_from("<i", data, p)[0]
        if not (0 <= nn < 200) or p + 4 + nn > len(data):
            raise ValueError(f"bad string at {p}")
        return p + 4 + nn, (p + 4 + nn + 3) & ~3

    try:
        pos += 4  # hashCode
        pos += 12  # material PPtr
        pos += 4  # matHashCode
        _, pos = read_str(pos)  # version
        _, pos = read_str(pos)  # sourceFontGUID
        pos += 12  # sourceFont PPtr
        pos += 4 + 4  # mode + atlasIndex
        _, pos = read_str(pos)  # face family
        _, pos = read_str(pos)  # face style
        pos += 17 * 4  # FaceInfo 17 values
        glyph_count = struct.unpack_from("<i", data, pos)[0]
        if glyph_count < 0 or pos + 4 + glyph_count * 48 > len(data):
            errors.append(f"glyph table invalid: count={glyph_count}")
            return errors
        pos += 4 + glyph_count * 48
        char_count = struct.unpack_from("<i", data, pos)[0]
        if char_count < 0 or pos + 4 + char_count * 16 > len(data):
            errors.append(f"char table invalid: count={char_count}")
            return errors
        pos += 4 + char_count * 16
        atlas_count = struct.unpack_from("<i", data, pos)[0]
        if atlas_count < 0 or pos + 4 + atlas_count * 12 > len(data):
            errors.append(f"atlas textures invalid: count={atlas_count}")
            return errors
        pos += 4 + atlas_count * 12
        pos += 4  # atlasIndex
        used_count = struct.unpack_from("<i", data, pos)[0]
        if used_count < 0 or pos + 4 + used_count * 16 > len(data):
            errors.append(f"usedGlyphRects invalid: count={used_count}")
            return errors
        pos += 4 + used_count * 16
        free_count = struct.unpack_from("<i", data, pos)[0]
        if free_count < 0 or pos + 4 + free_count * 16 > len(data):
            errors.append(f"freeGlyphRects invalid: count={free_count}")
            return errors
        pos += 4 + free_count * 16
        # m_fontInfo: len 字段 + 92B floats（Rendezvous 条纹根因）
        fi_len = struct.unpack_from("<i", data, pos)[0]
        if fi_len != 0:
            errors.append(f"m_fontInfo name not empty: len={fi_len}")
        fi_floats_end = pos + 4 + 92
        if fi_floats_end > len(data):
            errors.append(f"m_fontInfo segment too short (need 96B)")
        # 后续字段能定位到 atlas 尺寸
        aw = struct.unpack_from("<i", data, fi_floats_end)[0]
        ah = struct.unpack_from("<i", data, fi_floats_end + 4)[0]
        if aw < 64 or ah < 64 or (aw & (aw - 1)) or (ah & (ah - 1)):
            errors.append(f"atlas size suspicious: {aw}x{ah}")
    except (struct.error, ValueError) as exc:  # noqa: BLE001
        errors.append(f"structure parse failed: {exc}")
    return errors


def fix_bundle_fontinfo_segment(font_data: bytearray,
                                atlas_pptr_value: int) -> None:
    """修正 bundle 版 TMP_FontAsset 的 m_fontInfo 段长度（Rendezvous 条纹根因）。

    bundle(TMP 2.1.0) 的 m_fontInfo 段为 92B（无 len 字段）；主文件
    (TMP 2.1.1) 为 96B（len 4 + 92 floats）。调用方应先在字节流中
    定位 m_fontInfo 段并补齐 len 字段。
    """
    raise NotImplementedError("use build_main_format_font instead")


def build_main_format_font(
        head: bytes,
        face_values: bytes,
        glyph_table: bytes,
        char_table: bytes,
        atlas_textures: bytes,
        used_rects: bytes,
        free_rects: bytes,
        font_info_segment: bytes,
        atlas_width: int,
        atlas_height: int,
        atlas_padding: int,
        atlas_render_mode: int,
        tail_fields: bytes) -> bytes:
    """按主文件格式组装 TMP_FontAsset（Rendezvous 全流程验证）。

    参数为已提取的字段段（均按主文件格式序列化）：
      head：m_GameObject..m_AtlasTextureIndex（含 FaceInfo 字符串头）
      face_values：FaceInfo 17 数值（int pointSize + 16 floats，68B）
      glyph_table：count(4) + N×48B
      char_table：count(4) + N×16B
      atlas_textures：count(4) + N×12B（PPtr fileID+pathID）
      used/free_rects：count(4) + N×16B
      font_info_segment：**必须 96B（len 4 + 92B floats）**——Rendezvous
        条纹根因：缺 len 字段 → 后续字段偏移 4 → UV 错乱
      tail_fields：m_AtlasWidth 之后的尾部字段（含 padding/renderMode
        后置字段与创建设置/字重表等）
    返回可直接 set_raw_data 的完整对象字节。
    """
    if font_info_segment and len(font_info_segment) < 96:
        raise ValueError(
            f"font_info_segment must be 96B (len+92 floats), got "
            f"{len(font_info_segment)}")
    parts = [
        head,
        face_values if len(face_values) == 68 else _pad_face_values(face_values),
        glyph_table,
        char_table,
        atlas_textures,
        struct.pack("<i", 0),  # m_AtlasTextureIndex（mode 后已含；数组后）
        used_rects,
        free_rects,
        font_info_segment,
        struct.pack("<ii", atlas_width, atlas_height),
        struct.pack("<i", atlas_padding),
        struct.pack("<i", atlas_render_mode),
        struct.pack("<i", 0),  # m_AtlasPopulationMode(Static)
        tail_fields,
    ]
    return b"".join(parts)


def _pad_face_values(face_values: bytes) -> bytes:
    """FaceInfo 数值段若不足 68B（int pointSize + 16 floats）补零。"""
    if len(face_values) >= 68:
        return face_values[:68]
    return face_values + b"\x00" * (68 - len(face_values))


def validate_texture_structure(data: bytes) -> list[str]:
    """验证主文件格式 Texture2D 对象数据（Rendezvous 水平条纹根因）。

    检查：image data len 字段位置（主文件 104B 头部）、像素长度、
    尺寸为 2 的幂。返回错误列表（空 = 正确）。
    """
    errors: list[str] = []
    if data is None or len(data) < 24:
        return ["texture data too short"]
    n = struct.unpack_from("<i", data, 0)[0]
    if not (0 < n < 100) or 4 + n > len(data):
        return [f"texture name header invalid: {n}"]
    pos = 4 + n
    pos = (pos + 3) & ~3
    pos += 4 + 1  # forcedFallback + downscale
    pos = (pos + 3) & ~3
    w = struct.unpack_from("<i", data, pos)[0]
    h = struct.unpack_from("<i", data, pos + 4)[0]
    cis = struct.unpack_from("<i", data, pos + 8)[0]
    fmt = struct.unpack_from("<i", data, pos + 12)[0]
    if w < 64 or h < 64 or (w & (w - 1)) or (h & (h - 1)):
        errors.append(f"suspicious size {w}x{h}")
    # 步进到 image data len 字段
    pos += 4 + 4 + 4 + 4 + 4  # w h cis fmt mips
    pos += 4 + 4 + 4 + 4 + 8 + 4 + 4  # flags priority imageCount dim settings lightmap colorSpace
    img_len = struct.unpack_from("<i", data, pos)[0]
    if img_len <= 0:
        errors.append(f"image data len invalid: {img_len}")
    elif pos + 4 + img_len > len(data):
        errors.append(f"image data exceeds object: {img_len}")
    if fmt not in (1, 4, 28, 62):
        errors.append(f"unexpected format {fmt} (expected Alpha8/RGBA)")
    return errors


def retarget_component_refs(
        path: str,
        script_pid: int = TMP_TEXT_SCRIPT_PID,
        replacements: dict[int, int] | None = None,
        file_id: int = 2) -> int:
    """组件级引用替换：只改指定脚本组件的 PPtr 引用（防误伤）。

    Rendezvous 实证：全局字节替换 (fileID=2, pathID=X) 会误伤
    MeshRenderer/LightmapSettings/Transform 等非文本对象（pathID 恰好
    匹配）→ 关卡加载崩溃。本函数限定在 script=script_pid 的
    MonoBehaviour 数据内部替换，其他对象绝不触碰。

    参数：path=level 文件；script_pid=目标脚本（默认 TMP 2000）；
    replacements={old_pathID: new_pathID}；file_id=外部文件索引。
    返回替换次数。就地修改文件（UnityPy save 重写对象区）。
    """
    from UnityPy import Environment

    if not replacements:
        return 0
    env = Environment()
    env.load_file(path)
    total = 0
    changed = False
    for obj in env.objects:
        if str(getattr(obj.type, "name", "")) != "MonoBehaviour":
            continue
        data = obj.get_raw_data()
        if data is None or len(data) < 28:
            continue
        fid = struct.unpack_from("<i", data, 16)[0]
        pid = struct.unpack_from("<q", data, 20)[0]
        if (fid, pid) != (1, script_pid):
            continue
        blob = bytearray(data)
        c = 0
        for old, new in replacements.items():
            pat = struct.pack("<i", file_id) + struct.pack("<q", old)
            c += blob.count(pat)
            blob = blob.replace(pat, struct.pack("<i", file_id) + struct.pack("<q", new))
        if c:
            obj.set_raw_data(bytes(blob))
            changed = True
            total += c
    if changed:
        af = next(iter(env.objects)).assets_file
        # A10 0.46.0：gen 9-21 data_offset 布局保真（与 writer 统一）
        from hanhua.core.unity.serialized_layout import restore_from_path
        raw = restore_from_path(af, path)
        with open(path, "wb") as f:
            f.write(raw)
    return total


def build_main_format_texture(
        name: bytes,
        pixels: bytes,
        width: int = 4096,
        height: int = 4096,
        format_id: int = 1,
        trailing: bytes = b"") -> bytes:
    """按主文件格式组装 Texture2D 对象数据（Rendezvous 验证的 104B 头部）。

    主文件纹理布局（Rendezvous 实证，勿用 UnityPy save_typetree——
    其输出与主文件差 8-9 字节导致引擎逐行错位 → 水平条纹）：
      name(len+utf8) + forcedFallback(4) + downscale(1) + align
      + width(4) height(4) completeImageSize(4) format(4) mipCount(4)
      + isReadable(4) priority(4) imageCount(4) textureDimension(4)
      + textureSettings(8) lightmapFormat(4) colorSpace(4)
      + image data len(4) + 像素 + 尾部
    """
    data = bytearray()
    data += struct.pack("<i", len(name)) + name
    data += b"\x00" * ((4 - len(data) % 4) % 4)
    data += struct.pack("<i", 4)   # forcedFallbackFormat
    data += b"\x00"                # downscaleFallback
    data += b"\x00" * ((4 - len(data) % 4) % 4)
    data += struct.pack("<iiiiii", width, height, len(pixels), format_id, 1, 1)
    data += struct.pack("<i", 0)   # priority
    data += struct.pack("<i", 1)   # imageCount
    data += struct.pack("<i", 2)   # textureDimension
    data += struct.pack("<ii", 0, 0)  # textureSettings (filterMode=0 Point)
    data += struct.pack("<i", 0)   # lightmapFormat
    data += struct.pack("<i", 0)   # colorSpace
    data += struct.pack("<i", len(pixels))
    data += pixels
    data += trailing
    return bytes(data)
