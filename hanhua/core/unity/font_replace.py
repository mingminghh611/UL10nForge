"""静态字体替换：写回阶段把游戏字体资源换成 CJK 全字库。

两条路径（都作用于写回副本，不碰原游戏）：

1. legacy Font 替换（主路径，覆盖 uGUI Text / 3D TextMesh 主流样本）：
   把 Font 对象内嵌的 ``m_FontData`` TTF 字节整体换成白名单中文字体 TTF。
   Unity 对 dynamic Font 在运行时按 TTF 生成字形图集，替换后拉丁+中文全部可渲染。

2. TMP_FontAsset 替换（版本化 bundle 路径）：
   按游戏 Unity 版本选择 ``fonts/TMP_Font_AssetBundles`` 中
   Noto Serif CJK SC Medium（宋体中等字重，单字体收敛）SDF 字体 bundle
   ``notoserif_sdf_u<2019|2021|2022|6000>``
   （u2019/u2021/u2022=TMP 2.x，u6000=TMP 3.x；TMP 1.x 2018 及更早无
   中文 SDF bundle，仅 legacy Font 路径可替换），把游戏内 TMP_FontAsset
   的字形表/字符表/面信息替换为 bundle 字体的，图集 Texture2D 数据同步替换。

安全语义：任何失败只跳过该对象并记录，绝不阻断文本写回；替换后重开验证
（m_FontData == 目标 TTF / m_GlyphTable 数量一致）；外部流图集以追加方式
写入游戏 .resS 文件（不破坏既有流偏移）。
"""
from __future__ import annotations

import os
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from hanhua.core.models import FontConfig
from hanhua.core.unity.writer import _dispose_environment


_MAJOR_VERSION = re.compile(r"^(\d+)")


@dataclass
class FontReplaceResult:
    replaced: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # C5：被整容器重建（os.replace）的文件相对路径——Addressables 管线
    # 下 bundle CRC 已变，catalog.bin 中的 CRC 必须二次同步，否则运行时
    # CRC Mismatch 拒载（write_back_v2 末尾的 catalog 更新早于字体替换）。
    replaced_paths: list[str] = field(default_factory=list)
    # ── Phase 2：结构化覆盖（replaced > 0 不再代表全局成功） ──
    # 逐对象消费者记录 + 逐码点覆盖结果。调用方传入 RequiredGlyphSet 后，
    # overall/incomplete 才有意义；未传时保持 None（旧行为兼容）。
    consumers: list = field(default_factory=list)
    coverage: object | None = None          # FontCoverageOutcome
    overall: str | None = None              # CoverageState 名
    incomplete: bool = False                # 存在未覆盖/阻断消费者

    def summary_text(self) -> str:
        """审计报告行：替换数 + 覆盖终态（有覆盖计算时）。"""
        if self.coverage is None:
            return f"字体替换 {self.replaced} 个对象，{len(self.skipped)} 个跳过"
        text = f"字体替换 {self.replaced} 个对象，整体 {self.overall}"
        if self.incomplete:
            text += "（未覆盖——禁止称全局成功）"
        return text


@dataclass(frozen=True)
class TmpBundlePayload:
    """一个版本化 TMP 字体 bundle 解析后的载荷。"""
    bundle_path: Path
    font_name: str
    glyph_count: int
    layout_version: str  # "tmp1" | "tmp2" | "tmp3"
    font_typetree: dict
    atlas_texture: dict          # 图集 Texture2D typetree
    atlas_stream: bytes          # 图集像素流数据（原始字节）
    atlas_width: int
    atlas_height: int
    atlas_format: int
    #: Phase 2：载荷真实字符集（character table 提取）——替换后消费者
    #: 的字形覆盖按此逐码点验证，不再只比总 glyph 数
    charset: frozenset[int] = frozenset()
    #: material/shader 契约（tmp_contract 校验用；缺失可降级）
    material_name: str = ""
    shader_name: str = ""


# ── 版本映射 ────────────────────────────────────────────────

def _make_env(typetree_generator=None, path: str | None = None):
    """UnityPy Environment 构造：Mono 游戏 typetree 生成器透传。

    资产构建未带 typetree 时（DisableWriteTypeTree），MonoBehaviour 读取
    全部失败——TMP_FontAsset 找不到、静态替换与重开验证全空跑（hickory
    实证：2 个 Spectral SDF 字体 99 字形纯 ASCII 未被发现 → 中文缺字
    口口口）。挂上生成器后对象可读，替换/验证才能命中。
    """
    from UnityPy import Environment
    env = Environment()
    if typetree_generator is not None:
        env.typetree_generator = typetree_generator
    # 外部引用解析根=游戏目录（默认是 os.getcwd() 工具目录，Mono 游戏
    # m_Script PPtr deref 需加载同目录兄弟文件 globalgamemanagers.assets）
    if path:
        env.path = str(path)
    return env


def _bundle_dir() -> Path:
    return (Path(__file__).resolve().parents[3] / "fonts"
            / "TMP_Font_AssetBundles")


#: TMP2 布局可用的 Unity 主版本（2019+，tmp2 骨架）→ bundle 后缀。
#: 2018 及更早是 TMP1 布局（m_glyphInfoList），用户的中文 SDF 资产
#: （TMP 1.1.0，tmp2 布局）不兼容 → 无可用 bundle，返回 None。
#: 2026-08-18 单字体收敛：只保留 NotoSerifCJKsc-Medium，无档位维度。
def select_tmp_bundle(unity_version: str | None) -> Path | None:
    """按 Unity 主版本选 TMP 字体 bundle；未知返回 None。

    返回 notoserif_sdf_u<ver>（Noto Serif CJK SC Medium，用户导出的
    中文字库，覆盖 CJK 基本区，中文不缺字）。TMP1（2018 及更早）无
    中文 SDF bundle，返回 None（仅 legacy Font 路径可替换）。
    """
    if not unity_version:
        return None
    major = _MAJOR_VERSION.match(unity_version.strip())
    if not major:
        return None
    major_num = int(major.group(1))
    if major_num <= 2018:
        return None
    if major_num <= 2020:
        suffix = "u2019"
    elif major_num == 2021:
        suffix = "u2021"
    elif major_num == 2022:
        suffix = "u2022"
    elif major_num >= 6000:
        suffix = "u6000"
    else:
        return None
    bundle = _bundle_dir() / f"notoserif_sdf_{suffix}"
    return bundle if bundle.is_file() else None


def _typetree_layout_version(tree: dict) -> str | None:
    """判定 TMP_FontAsset 布局代：tmp1（m_fontInfo/m_glyphInfoList）/ tmp2/3。"""
    if "m_GlyphTable" in tree:
        return "tmp2"
    if "m_glyphInfoList" in tree:
        return "tmp1"
    return None


def _atlas_stream_meta(tree: dict) -> tuple[str, int, int]:
    """返回图集流 (path, offset, size)；无流数据时 path 为空。"""
    stream = tree.get("m_StreamData") or {}
    path = str(stream.get("path") or "")
    offset = int(stream.get("offset") or 0)
    size = int(stream.get("size") or 0)
    return path, offset, size


def _extract_atlas_bytes(env, atlas_tex, bundle: Path, atlas_tree: dict | None = None) -> bytes:
    """提取图集原始像素字节（仅流数据覆盖的区间，不含同流其他纹理）。

    优先从 bundle 的 ``CAB-xxx.resS`` 子文件按 m_StreamData 区间读取（保真无损）；
    无 resS 子文件/无流时回退 ``image_data``。
    """
    bundle_file = None
    for item in env.files.values():
        if type(item).__name__ == "BundleFile":
            bundle_file = item
            break
    path, offset, size = _atlas_stream_meta(atlas_tree or {})
    if bundle_file is not None and path:
        res_name = Path(path).name
        res = bundle_file.files.get(res_name)
        if res is not None:
            reader = res.read() if callable(res.read) else res
            data = reader if isinstance(reader, bytes) else bytes(reader)
            if data and offset + size <= len(data):
                return data[offset:offset + size]
    reader = atlas_tex.read()
    return reader.image_data or b""


def load_tmp_bundle(bundle: Path) -> TmpBundlePayload:
    """解析版本化 TMP 字体 bundle，返回载荷。"""
    from UnityPy import Environment
    env = Environment()
    env.path = str(bundle.parent)
    font_obj = atlas_obj = None
    try:
        env.load([str(bundle)])
        seen: set[tuple[str, str, int]] = set()
        material_name = shader_name = ""
        for obj in env.objects:
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            if obj.type.name == "MonoBehaviour" and font_obj is None:
                tree = obj.read_typetree()
                if _typetree_layout_version(tree) is not None:
                    font_obj = (obj, tree)
            elif obj.type.name == "Texture2D" and atlas_obj is None:
                atlas_obj = (obj, obj.read_typetree())
            elif obj.type.name == "Material" and not material_name:
                tree = obj.read_typetree()
                material_name = str(tree.get("m_Name", "") or "")
                shader_name = str(tree.get("m_ShaderName", "") or "")
        if font_obj is None or atlas_obj is None:
            raise ValueError(
                f"TMP 字体 bundle 缺少字体或图集对象: {bundle.name}")
        _, font_tree = font_obj
        atlas_tex, atlas_tree = atlas_obj
        layout = _typetree_layout_version(font_tree)
        glyphs = len(font_tree.get("m_GlyphTable")
                     or font_tree.get("m_glyphInfoList") or [])
        atlas_bytes = _extract_atlas_bytes(env, atlas_tex, bundle, atlas_tree)
        if not atlas_bytes:
            raise ValueError(f"TMP 字体 bundle 图集数据缺失: {bundle.name}")
        # Phase 2：真实字符集 = 字符表码点（tmp1: m_glyphInfoList 内嵌）
        if layout == "tmp2":
            charset = frozenset(_tmp_chars(font_tree))
        else:
            charset = frozenset(_tmp1_codes(font_tree))
        return TmpBundlePayload(
            bundle_path=bundle,
            font_name=str(font_tree.get("m_Name", "ARIALUNI SDF")),
            glyph_count=glyphs,
            layout_version=layout,
            font_typetree=font_tree,
            atlas_texture=atlas_tree,
            atlas_stream=atlas_bytes,
            atlas_width=int(atlas_tree.get("m_Width") or 0),
            atlas_height=int(atlas_tree.get("m_Height") or 0),
            atlas_format=int(atlas_tree.get("m_TextureFormat") or 0),
            charset=charset,
            material_name=material_name,
            shader_name=shader_name,
        )
    finally:
        _dispose_environment(env)


# ── legacy Font 替换 ────────────────────────────────────────

def _font_ttf_candidate(config: FontConfig) -> Path | None:
    """白名单中文字体 TTF（写回方负责校验存在性）。"""
    from hanhua.core.font_support import (FONT_OPTIONS,
                                          _normalize_font_filename)
    if not config.filename:
        return None
    # 旧库兼容：弃用字体路径映射新字体（与运行时部署同源更正）
    filename = _normalize_font_filename(config.filename)
    if filename not in FONT_OPTIONS:
        return None
    fonts_dir = _bundle_dir().parent
    candidate = fonts_dir / filename
    return candidate if candidate.is_file() else None


def _ttf_has_magic(data: bytes) -> bool:
    return (data[:4] in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"}
            or data[:2] == b"\x00\x01")


def _ttf_metrics(data: bytes) -> tuple[float, float, float] | None:
    """解析 TTF head/hhea 表 → (ascent, descent, lineGap)，单位 em（除以 unitsPerEm）。

    用这些值同步 legacy Font 的 m_Ascent/m_Descent/m_LineSpacing：
    Unity 按原字体的度量渲染替换后的 TTF，指标不匹配会把字形错位缩放
    （deadbeat 原 m_Ascent=12 vs 联想小新黑体实际 0.86em）→ 字体模糊。
    """
    if len(data) < 12 or not _ttf_has_magic(data):
        return None
    try:
        num_tables = struct.unpack(">H", data[4:6])[0]
        tables: dict[str, tuple[int, int]] = {}
        for i in range(num_tables):
            off = 12 + i * 16
            if off + 16 > len(data):
                return None
            tag = data[off:off + 4].decode("latin1")
            _checksum, toffset, tlength = struct.unpack(
                ">III", data[off + 4:off + 16])
            tables[tag] = (toffset, tlength)
        head_off, head_len = tables.get("head", (0, 0))
        hhea_off, hhea_len = tables.get("hhea", (0, 0))
        if not (head_off and hhea_off and head_off + 20 <= len(data)
                and hhea_off + 10 <= len(data)):
            return None
        upm = struct.unpack(">H", data[head_off + 18:head_off + 20])[0]
        if not upm:
            return None
        ascent = struct.unpack(">h", data[hhea_off + 4:hhea_off + 6])[0] / upm
        descent = struct.unpack(">h", data[hhea_off + 6:hhea_off + 8])[0] / upm
        line_gap = struct.unpack(">h", data[hhea_off + 8:hhea_off + 10])[0] / upm
        return ascent, descent, line_gap
    except (IndexError, struct.error):
        return None


# 像素字体渲染模式（HintedRaster）：对矢量 TTF 会产生锯齿/块状模糊。
# 替换为平滑渲染（Smooth）提高清晰度。
_FONT_RENDERING_MODE_HINTED_RASTER = 2
_FONT_RENDERING_MODE_SMOOTH = 0


def _patch_font_object(env, font_obj, ttf_bytes: bytes) -> bool:
    """把单个 Font 对象的内嵌 TTF 换成目标 TTF。返回是否替换。

    同时按目标 TTF 的真实度量修正 m_Ascent/m_Descent/m_LineSpacing，
    并把像素字体渲染模式（HintedRaster）改为 Smooth——原字体指标
    与替换 TTF 不匹配是「汉化后字体模糊」的直接根因。
    """
    tree = font_obj.read_typetree()
    font_data = tree.get("m_FontData")
    if not isinstance(font_data, list) or len(font_data) < 256:
        # 无内嵌字体数据（静态位图字体/外部引用）→ 不替换
        return False
    current = bytes(font_data)
    if not _ttf_has_magic(current):
        return False
    # 注意：不在此处跳过 current == ttf_bytes —— UnityPy typetree 解析器
    # 对同类型对象可能返回共享缓存，前一个对象已改则后续读到的就是目标字节；
    # 跳过会漏计数（替换本身无害）。save_typetree 幂等。
    metrics = _ttf_metrics(ttf_bytes)
    if metrics is not None:
        ascent, descent, line_gap = metrics
        font_size = tree.get("m_FontSize") or 16
        tree["m_Ascent"] = round(ascent * font_size, 2)
        tree["m_Descent"] = round(descent * font_size, 2)
        tree["m_LineSpacing"] = round(
            (ascent - descent + line_gap) * font_size, 2)
        if tree.get("m_FontRenderingMode") == _FONT_RENDERING_MODE_HINTED_RASTER:
            tree["m_FontRenderingMode"] = _FONT_RENDERING_MODE_SMOOTH
    tree["m_FontData"] = list(ttf_bytes)
    font_obj.save_typetree(tree)
    return True


def _replace_and_swap(path: Path, env, verify_fn=None) -> None:
    """容器序列化 → 验证临时文件 → 释放句柄 → 原子替换目标文件。

    与 writer._patch_asset 同一顺序：验证发生在替换前（对临时文件），
    目标文件只在初次 env.load 时被打开，且替换前已 dispose，避免 Windows
    句柄锁定导致 PermissionError。
    """
    import gc
    import time as _time
    containers = {
        id(item): item for item in env.files.values()
        if type(item).__name__ in ("BundleFile", "SerializedFile")
    }
    if len(containers) != 1:
        raise ValueError(
            f"预期恰好一个顶层 Unity 容器，实际为 {len(containers)}: {path.name}")
    container = next(iter(containers.values()))
    with tempfile.TemporaryDirectory(
        prefix=f".{path.name}.", dir=path.parent,
    ) as tmp:
        saved = Path(tmp) / path.name
        if type(container).__name__ == "BundleFile":
            saved.write_bytes(container.save(packer="original"))
        else:
            saved.write_bytes(container.save())
        if verify_fn is not None:
            verify_fn(saved)
        _dispose_environment(env)
        gc.collect()
        # 兜底：Defender 扫描锁定窗口短重试
        for attempt in range(5):
            try:
                os.replace(saved, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                _time.sleep(0.8)


def _verify_legacy_saved(saved: Path, ttf_bytes: bytes, replaced: int,
                         source_dir: Path | None = None) -> None:
    """重开临时容器验证全部 Font 的 m_FontData 均已被替换。"""
    from UnityPy import Environment
    verify = Environment()
    # 临时副本同目录无兄弟文件——外部引用在原游戏目录解析
    verify.path = str(source_dir or saved.parent)
    try:
        verify.load([str(saved)])
        seen: set[tuple[str, str, int]] = set()
        matched = 0
        for obj in verify.objects:
            if obj.type.name != "Font":
                continue
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            tree = obj.read_typetree()
            fd = tree.get("m_FontData")
            if isinstance(fd, list) and bytes(fd) == ttf_bytes:
                matched += 1
        if matched < replaced:
            raise ValueError(
                f"Font 替换重开验证不一致: {saved.name} "
                f"replaced={replaced} matched={matched}")
    finally:
        _dispose_environment(verify)


def replace_legacy_fonts_in_container(
    path: Path,
    ttf_bytes: bytes,
    progress: int = 0,
    typetree_generator: Any | None = None,
    source_dir: Path | None = None,
) -> tuple[int, list[str], list]:
    """替换单个 Unity 容器（.assets/level/bundle）内全部 Font 对象的内嵌 TTF。

    返回 (替换数, 跳过原因列表, 消费者记录列表)。Phase 2：每个 Font 对象
    都进消费者清单——已替换的附目标 TTF 真实字符集（cmap 解析），未替换的
    记为 STATIC_NOT_REPLACED（不得静默消失）。

    source_dir：外部引用解析根（写回副本路径时传原游戏目录——副本临时
    替换文件同目录无兄弟文件，Mono 游戏 m_Script PPtr deref 需在原目录
    解析 external；与 writer._verify_saved_bundle 同语义）。
    """
    from hanhua.core.font import FontConsumer
    from hanhua.core.font.ttf_charset import ttf_charset
    env = _make_env(typetree_generator, path=str(source_dir or path.parent))
    replaced = 0
    skipped: list[str] = []
    consumers: list[FontConsumer] = []
    ttf_chars: frozenset[int] = ttf_charset(ttf_bytes)
    try:
        env.load([str(path)])
        seen: set[tuple[str, str, int]] = set()
        for obj in env.objects:
            if obj.type.name != "Font":
                continue
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                if _patch_font_object(env, obj, ttf_bytes):
                    replaced += 1
                    consumers.append(FontConsumer(
                        f"{path.name}#Font#{obj.path_id}", "legacy_font",
                        static_replaced=True, font_scalars=ttf_chars,
                        atlas_resolved=True,
                        ref="内嵌 TTF 已替换 · 字符集按 cmap 解析"))
                    continue
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{path.name}#Font#{obj.path_id}: {exc}")
            consumers.append(FontConsumer(
                f"{path.name}#Font#{obj.path_id}", "legacy_font",
                static_replaced=False,
                ref="无内嵌 TTF（静态位图/外部引用）未替换"))
        if not replaced:
            return 0, skipped, consumers
        _replace_and_swap(
            path, env,
            verify_fn=lambda saved: _verify_legacy_saved(
                saved, ttf_bytes, replaced, source_dir=source_dir),
        )
    finally:
        _dispose_environment(env)
    return replaced, skipped, consumers


# ── TMP_FontAsset 替换 ──────────────────────────────────────

# 常用汉字样本（GB2312 一级字）：字符表全覆盖样本且总量充足 → 视为已覆盖
# CJK，不替换（避免把游戏自带中文字体如 chi_NotoSansCH 换成 ARIALUNI SDF：
# 既没必要，又会把 bundle 撑到数百 MB）。
_CJK_SAMPLE_CODES = tuple(ord(c) for c in (
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞"
))
_CJK_MIN_TOTAL = 2000  # 字符表 CJK 码点数门槛（覆盖样本外生僻字的余量）


def _tmp_chars(tree: dict) -> list[int]:
    """从 TMP 字符表提取 unicode 码点（tmp1/tmp2 兼容）。

    tmp2：m_CharacterTable[].m_Unicode；tmp1：m_glyphInfoList[].m_characterCode。
    m_Unicode 缺失时回退 m_characterCode（兼容不同序列化命名）。
    """
    chars: list[int] = []
    table = next((tree[k] for k in
                  ("m_CharacterTable", "m_characterTable", "m_glyphInfoList")
                  if isinstance(tree.get(k), list)), None)
    for item in table or []:
        if not isinstance(item, dict):
            continue
        u = item.get("m_Unicode")
        if isinstance(u, str):
            try:
                u = int(u, 16)
            except ValueError:
                continue
        if not isinstance(u, int):
            u = item.get("m_characterCode")
        if isinstance(u, int):
            chars.append(u)
    return chars


def _tmp1_codes(tree: dict) -> list[int]:
    """tmp1 布局：m_glyphInfoList 内嵌 m_characterCode（载荷字符集提取）。"""
    codes: list[int] = []
    for glyph in tree.get("m_glyphInfoList") or []:
        code = glyph.get("m_characterCode") if isinstance(glyph, dict) \
            else None
        if isinstance(code, int):
            codes.append(code)
    return codes


def _tmp_covers_cjk(tree: dict) -> bool:
    """TMP 字体字符表是否已覆盖常用汉字（是则跳过替换）。"""
    chars = _tmp_chars(tree)
    cjk = {c for c in chars if 0x4E00 <= c <= 0x9FFF}
    if len(cjk) < _CJK_MIN_TOTAL:
        return False
    return all(c in cjk for c in _CJK_SAMPLE_CODES)


def _tmp_covers_required(tree: dict, required: set[int]) -> bool:
    """Phase 2：按本次真实译文需求集验证游戏自带字体（实现重点 2）。

    旧逻辑（样本启发式 _tmp_covers_cjk）只验常用字子集——字形很多但缺
    译文生僻字照样跳过替换 → 方框。现在：游戏字体字符表 ⊇ 需求集才算
    「已覆盖」；缺任何需求码点都必须替换。
    """
    chars = set(_tmp_chars(tree))
    return required <= chars


_TMP2_COPY_FIELDS = (
    "m_FaceInfo", "m_GlyphTable", "m_CharacterTable",
    "m_AtlasTextureIndex", "m_IsMultiAtlasTexturesEnabled",
    "m_UsedGlyphRects", "m_FreeGlyphRects",
    # 图集尺寸/padding：UV 坐标系依赖（hickory 实证缺陷）——bundle 图集
    # 4096×4096 而游戏原 512/1024，不复制则 TMP 按 m_AtlasWidth=512 计算
    # UV（rect/512 而非 rect/4096）→ 8 倍采样偏移 → 文本部分笔画。
    "m_AtlasWidth", "m_AtlasHeight", "m_AtlasPadding",
)
_TMP1_COPY_FIELDS = (
    "m_fontInfo", "m_glyphInfoList", "m_kerningInfo",
    "m_kerningPair", "m_characterSpacing", "m_characterPadding",
)


def _copy_font_fields(game_tree: dict, payload: TmpBundlePayload) -> bool:
    """把 bundle 字体的字形数据复制进游戏字体 typetree。返回是否有变化。"""
    if payload.layout_version == "tmp2":
        fields = _TMP2_COPY_FIELDS
    else:
        fields = _TMP1_COPY_FIELDS
    changed = False
    for field_name in fields:
        if field_name not in payload.font_typetree:
            continue
        value = payload.font_typetree[field_name]
        if game_tree.get(field_name) != value:
            game_tree[field_name] = value
            changed = True
    return changed


def _resolve_atlas_obj(env, tree: dict, anchor=None) -> object | None:
    """解析 TMP 字体引用的图集 Texture2D（必须与字体同 SerializedFile）。

    Unity 引用 `m_FileID=0` 表示同一 SerializedFile 内的对象 —— 旧代码把
    "0" 当作资产文件名比对导致永远找不到图集（project-arrhythmia 真实失败）。

    **path_id 只在 SerializedFile 内唯一**：data.unity3d 这类多文件 bundle
    里跨文件会重号（hickory 实证：sharedassets0 与 globalgamemanagers 都有
    pid=31）。全局找第一个同号对象会误选无关纹理（副本实证：FalloffLookup
    /Large02/LDR_LLL1_9 四个无关纹理被撑成 4096×16MB，真图集从未替换 →
    字形表 8361 配 512 原图 → 部分笔画 + 无关纹理撑爆卡顿）。anchor 为字体
    对象时按同 assets_file 限定。
    """
    refs = tree.get("m_AtlasTextures") or tree.get("atlas")
    ref = None
    if isinstance(refs, list) and refs:
        ref = refs[0] if isinstance(refs[0], dict) else None
    elif isinstance(refs, dict):
        ref = refs
    if not isinstance(ref, dict):
        return None
    file_id = ref.get("m_FileID")
    path_id = ref.get("m_PathID")
    if isinstance(file_id, str):
        same_file = file_id.strip() in {"", "0", "0:0"}
    else:
        same_file = not file_id or int(file_id) == 0
    if not same_file:
        return None  # 跨文件引用（同 bundle 其他 SerializedFile）：不支持
    try:
        path_id = int(path_id)
    except (TypeError, ValueError):
        return None
    anchor_file = _obj_file_key(anchor) if anchor is not None else None
    for other in env.objects:
        if other.type.name != "Texture2D":
            continue
        if anchor_file is not None and _obj_file_key(other) != anchor_file:
            continue
        if int(other.path_id) == path_id:
            return other
    return None


def _obj_file_key(obj) -> str:
    """对象所在 SerializedFile 名；mock/异常时回退空串（单文件 env 下
    所有对象 key 相同 → anchor 限定自然放宽，测试 fixture 兼容）。"""
    assets_file = getattr(obj, "assets_file", None)
    if assets_file is None:
        return ""
    return getattr(assets_file, "name", "") or ""


def _patch_atlas_texture(env, atlas_obj, payload: TmpBundlePayload) -> dict | None:
    """把游戏图集 Texture2D 替换为 bundle 图集（含真实像素）。

    旧实现两个缺陷（导致替换后 TMP 汉字仍是口口口口/花屏）：
    1. 图集引用 m_FileID=0 被当作文件名比对 → 永远找不到图集（已修）；
    2. 图集走共享 resS 流文件：多个 TMP 字体共用一个 resS，按 offset 覆盖/
       追加会把彼此刚写入的 64MB 数据互相踩掉（实测 resS 内 9 段数据互相
       重叠）。现在改为**内嵌数据**（m_StreamData.size=0 + typetree 的
       "image data" 字节）：Unity 加载时流为空则读内嵌字节 —— 每个图集
       独立携带像素，无需共享流、无需改任何引用。
    """
    tree = atlas_obj.read_typetree()
    # 图集整体换为 bundle 图集的内容；保留游戏图集的名称与采样/包裹设置
    # （wrap/filter 影响渲染，保留游戏原有设置最稳）。
    new_tree = dict(payload.atlas_texture)
    for keep in ("m_Name", "m_TextureSettings"):
        if keep in tree:
            new_tree[keep] = tree[keep]
    new_tree["image data"] = payload.atlas_stream
    new_tree["m_StreamData"] = {"offset": 0, "size": 0, "path": ""}
    new_tree["m_CompleteImageSize"] = len(payload.atlas_stream)
    return new_tree


def replace_tmp_fonts_in_container(
    path: Path,
    payload: TmpBundlePayload,
    *,
    required: set[int] | None = None,
    unity_version: str | None = None,
    typetree_generator: Any | None = None,
    source_dir: Path | None = None,
) -> tuple[int, list[str], list]:
    """替换单个容器内全部 TMP_FontAsset 对象。

    返回 (替换数, 跳过列表, 消费者记录列表)。Phase 2：
    - required 给定 → 「已覆盖」按真实译文需求集验证（实现重点 2），
      缺任何需求码点都必须替换；
    - 每个 TMP 对象都进消费者清单：布局不匹配 / dynamic 0 glyph /
      atlas 未解析 / 已覆盖 / 已替换 各有明确终态（实现重点 3/4/5）。

    source_dir：外部引用解析根（写回副本路径时传原游戏目录，同
    replace_legacy_fonts_in_container）。
    """
    from hanhua.core.font import FontConsumer
    env = _make_env(typetree_generator, path=str(source_dir or path.parent))
    replaced = 0
    skipped: list[str] = []
    patched: list = []
    consumers: list[FontConsumer] = []
    try:
        env.load([str(path)])
        seen: set[tuple[str, str, int]] = set()
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                tree = obj.read_typetree()
            except Exception:  # noqa: BLE001
                continue
            if _typetree_layout_version(tree) is None:
                continue
            layout = _typetree_layout_version(tree)
            cid = f"{path.name}#TMP#{obj.path_id}"
            if layout != payload.layout_version:
                skipped.append(
                    f"{path.name}#TMP#{obj.path_id}: layout {layout} "
                    f"!= bundle {payload.layout_version}")
                consumers.append(FontConsumer(
                    cid, "tmp_font", static_replaced=False,
                    layout_ok=False, unity_version=unity_version,
                    ref=f"布局 {layout} != bundle {payload.layout_version}"))
                continue
            glyphs = len(tree.get("m_GlyphTable")
                         or tree.get("m_glyphInfoList") or [])
            if glyphs <= 0:
                # 动态/空字体（0 glyph）：字形本由运行时生成。仍可静态
                # 替换——bundle 字符表全量覆盖需求集时 TMP 查表渲染，
                # 未收录码点才走原动态路径（源字体引用保留、动态 mode
                # 字段不在复制清单内），无行为冲突。hickory 实证：用户
                # SDF 字体方案无 TTF 数据源，插件兜底不可部署
                # （FontInstallError），0-glyph 字体不静态替换 → 动态
                # 消费者永久 BLOCKED。图集引用不可解析时归 dynamic_tmp
                # （运行时路径，诚实阻断）。
                atlas_obj = _resolve_atlas_obj(env, tree, obj)
                if atlas_obj is None:
                    skipped.append(
                        f"{path.name}#TMP#{obj.path_id}: dynamic font (0 glyphs)")
                    consumers.append(FontConsumer(
                        cid, "dynamic_tmp",
                        runtime_provider_available=False,
                        ref="dynamic 0 glyph——静态无法证明覆盖"))
                    continue
                # 静态替换后必须关掉动态注入：mode=1（Dynamic）会在运行时
                # 按源字体生成字形注入图集——静态 8361 字符表 + 静态
                # FreeGlyphRects 下注入会破坏图集布局（启动卡顿 + 部分笔画，
                # hickory 实测根源之一）。静态替换即全部字形查表渲染，
                # mode=0（Static）无副作用。
                if isinstance(tree.get("m_AtlasPopulationMode"), int) \
                        and tree["m_AtlasPopulationMode"] != 0:
                    tree["m_AtlasPopulationMode"] = 0
                changed = _copy_font_fields(tree, payload)
                atlas_tree = _patch_atlas_texture(env, atlas_obj, payload)
                if atlas_tree is None:
                    skipped.append(
                        f"{path.name}#TMP#{obj.path_id}: "
                        "dynamic atlas replace failed")
                    consumers.append(FontConsumer(
                        cid, "tmp_font", static_replaced=False,
                        layout_ok=True, unity_version=unity_version,
                        atlas_resolved=False, ref="动态字体图集替换失败"))
                    continue
                atlas_obj.save_typetree(atlas_tree)
                if changed:
                    obj.save_typetree(tree)
                patched.append((obj, atlas_obj))
                replaced += 1
                consumers.append(FontConsumer(
                    cid, "tmp_font", static_replaced=True,
                    font_scalars=payload.charset,
                    layout_ok=True, unity_version=unity_version,
                    ref=f"bundle {payload.font_name} 已替换动态字体 · "
                        f"{len(payload.charset)} 字符"))
                continue
            game_chars = set(_tmp_chars(tree))
            if required is not None:
                covers = required <= game_chars
            else:
                covers = _tmp_covers_cjk(tree)
            if covers:
                # 游戏自带字体已覆盖需求集（或常用汉字样本）→ 保留原字体，
                # 避免无谓替换与 bundle 膨胀；其字符集即真实字形覆盖证据
                skipped.append(
                    f"{path.name}#TMP#{obj.path_id}: already covers "
                    f"{'required' if required is not None else 'CJK'}")
                consumers.append(FontConsumer(
                    cid, "tmp_font", static_replaced=True,
                    font_scalars=frozenset(game_chars),
                    layout_ok=True, unity_version=unity_version,
                    ref="游戏自带字体字符表已覆盖需求集"))
                continue
            changed = _copy_font_fields(tree, payload)
            # 图集：游戏字体引用的 Texture2D 需要同步替换（m_FileID=0 为
            # 同文件引用；anchor 限定同 SerializedFile——跨文件同号对象
            # 是错误目标，hickory 实证误替换撑爆无关纹理）
            atlas_obj = _resolve_atlas_obj(env, tree, obj)
            if atlas_obj is None:
                skipped.append(f"{path.name}#TMP#{obj.path_id}: atlas not found")
                consumers.append(FontConsumer(
                    cid, "tmp_font", static_replaced=False,
                    layout_ok=True, unity_version=unity_version,
                    atlas_resolved=False,
                    ref="图集引用未解析（跨文件引用）"))
                continue
            atlas_tree = _patch_atlas_texture(env, atlas_obj, payload)
            if atlas_tree is None:
                skipped.append(
                    f"{path.name}#TMP#{obj.path_id}: atlas replace failed")
                consumers.append(FontConsumer(
                    cid, "tmp_font", static_replaced=False,
                    layout_ok=True, unity_version=unity_version,
                    atlas_resolved=False, ref="图集替换失败"))
                continue
            atlas_obj.save_typetree(atlas_tree)
            if changed:
                obj.save_typetree(tree)
            patched.append((obj, atlas_obj))
            replaced += 1
            consumers.append(FontConsumer(
                cid, "tmp_font", static_replaced=True,
                font_scalars=payload.charset,   # 替换后真实字形 = bundle 字符集
                layout_ok=True, unity_version=unity_version,
                ref=f"bundle {payload.font_name} 已替换 · "
                    f"{len(payload.charset)} 字符"))
        if not patched:
            return 0, skipped, consumers
        _replace_and_swap(
            path, env,
            verify_fn=lambda saved: _verify_tmp_saved(
                saved, payload, replaced, typetree_generator,
                source_dir=source_dir))
    finally:
        _dispose_environment(env)
    return replaced, skipped, consumers


def _verify_tmp_saved(saved: Path, payload: TmpBundlePayload, replaced: int,
                      typetree_generator: Any | None = None,
                      source_dir: Path | None = None) -> None:
    """重开临时容器验证 TMP 字形表 + 图集像素均已替换。

    只验字形数量会漏掉「元数据更新但流没写入」的假通过——旧实现正是如此
    （同尺寸分支只改 typetree 不写像素）。图集流数据必须逐字节等于
    payload.atlas_stream。
    """
    verify = _make_env(typetree_generator, path=str(source_dir or saved.parent))
    try:
        verify.load([str(saved)])
        seen: set[tuple[str, str, int]] = set()
        matched = 0
        atlas_verified = 0
        for obj in verify.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                tree = obj.read_typetree()
            except Exception:  # noqa: BLE001
                continue
            if _typetree_layout_version(tree) != payload.layout_version:
                continue
            glyphs = len(tree.get("m_GlyphTable")
                         or tree.get("m_glyphInfoList") or [])
            if glyphs == payload.glyph_count:
                matched += 1
            atlas_obj = _resolve_atlas_obj(verify, tree, obj)
            if atlas_obj is None:
                continue
            atlas_tree = atlas_obj.read_typetree()
            if (atlas_tree.get("m_Width") == payload.atlas_width
                    and atlas_tree.get("m_Height") == payload.atlas_height
                    and atlas_tree.get("m_TextureFormat") == payload.atlas_format):
                data = atlas_tree.get("image data")
                if isinstance(data, (bytes, bytearray, list)) \
                        and bytes(data) == payload.atlas_stream:
                    atlas_verified += 1
        if matched < replaced:
            raise ValueError(
                f"TMP 替换重开验证不一致: {saved.name} "
                f"replaced={replaced} matched={matched}")
        if atlas_verified < replaced:
            raise ValueError(
                f"TMP 图集像素验证不一致: {saved.name} "
                f"replaced={replaced} atlas_verified={atlas_verified}")
    finally:
        _dispose_environment(verify)


def _object_key(obj) -> tuple[str, int]:
    return str(obj.assets_file.name), int(obj.path_id)


# ── 整目录入口 ──────────────────────────────────────────────

_ASSET_SUFFIXES = {".assets", ".bundle", ".unity3d", ".u3d", ".dat"}
_NO_EXT_NAMES = {"level", "maindata", "globalgamemanagers"}
# 真实 Unity 容器最小也有数 KB；更小的是占位/假文件（如测试 fixture 的
# 10 字节 globalgamemanagers），加载无意义且 UnityPy 会以未知格式持有句柄。
_MIN_CONTAINER_BYTES = 256


def _asset_candidates(out_dir: Path) -> list[Path]:
    """收集写回副本中值得做字体替换的 Unity 容器。"""
    candidates: list[Path] = []
    for root in (out_dir.rglob("*")):
        if not root.is_file():
            continue
        if root.stat().st_size < _MIN_CONTAINER_BYTES:
            continue
        name = root.name.casefold()
        is_level = name.startswith("level") and name[5:].isdigit()
        if (root.suffix.casefold() in _ASSET_SUFFIXES
                or name in _NO_EXT_NAMES or is_level):
            candidates.append(root)
    # 排除引擎/工具目录
    excluded = {"monobleedingedge", "il2cpp_data", "bee_data", "resources/unity"
                "_builtin_extra", "streamingassets/aa/catalogs"}
    return [c for c in candidates
            if not any(part.casefold() in excluded for part in c.parts)]


def _split_parts(parts: tuple):
    """(replaced, skipped, consumers) → (replaced, skipped, consumers)。

    容器函数返回 3-tuple（第三个元素是消费者列表）；旧 2-tuple 返回
    （无消费者记录）也容忍。star-unpacking 会把列表整体收进一个元素，
    这里归一化。
    """
    replaced, skipped, *consumer_list = parts
    if len(consumer_list) == 1 and isinstance(consumer_list[0], list):
        consumer_list = consumer_list[0]
    return replaced, skipped, consumer_list


def install_static_fonts(out_dir, config, *, unity_version=None,
                         required=None,
                         typetree_generator: Any | None = None,
                         source_dir: Path | None = None,
                         ) -> FontReplaceResult:
    """在写回副本上执行静态字体替换（legacy Font + TMP_FontAsset）。

    任何单项失败只跳过并记录；绝不抛出（字体是增强项，不阻断写回）。

    Phase 2：required（RequiredGlyphSet）给定——本次真实译文需求集时，
    逐对象消费者记录汇总为结构化覆盖：replaced > 0 不再代表全局成功；
    任何消费者 CANDIDATE_ONLY/BLOCKED → result.incomplete=True
    （「一个成功一个失败」的 fixture 结果必须是 INCOMPLETE 而非 PASS）。

    source_dir：外部引用解析根（staging 副本路径传原游戏目录——副本
    asset 加载/重开验证的 Mono m_Script deref 需在原目录解析兄弟文件）。
    """
    from hanhua.core.font import compute_coverage
    required_scalars = set(required.scalars) if required is not None else None
    result = FontReplaceResult()
    ttf = _font_ttf_candidate(config)
    if ttf is not None:
        ttf_bytes = ttf.read_bytes()
        # #42 防复发自检：候选文件存在但内容无效（坏 magic/空壳）时
        # 拒绝替换并明确告警——旧行为把损坏 TTF 照样写进游戏且
        # replaced>0，用户以为字体已替换（方框问题复发源头：静默假 PASS）。
        if not _ttf_has_magic(ttf_bytes) or len(ttf_bytes) < 1024:
            result.warnings.append(
                f"目标字体 {config.filename} 内容无效（损坏或过小）——"
                "拒绝静态替换；请重新下载字体文件")
            ttf = None
    if ttf is not None:
        ttf_bytes = ttf.read_bytes()
        for asset in _asset_candidates(out_dir):
            try:
                parts = replace_legacy_fonts_in_container(
                    asset, ttf_bytes, typetree_generator=typetree_generator,
                    source_dir=source_dir)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"{asset.name}: {exc}")
                continue
            replaced, skipped, consumers = _split_parts(parts)
            result.replaced += replaced
            result.skipped.extend(skipped)
            result.consumers.extend(consumers)
            if replaced:
                result.replaced_paths.append(
                    asset.relative_to(out_dir).as_posix())
    # TMP 路径（单字体 NotoSerifCJKsc-Medium，按 Unity 版本选择）
    bundle = select_tmp_bundle(unity_version)
    if bundle is not None and config.enabled:
        try:
            payload = load_tmp_bundle(bundle)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"TMP bundle {bundle.name} 解析失败: {exc}")
            payload = None
        if payload is not None:
            for asset in _asset_candidates(out_dir):
                try:
                    parts = replace_tmp_fonts_in_container(
                        asset, payload, required=required_scalars,
                        unity_version=unity_version,
                        typetree_generator=typetree_generator,
                        source_dir=source_dir)
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"{asset.name}: {exc}")
                    continue
                replaced, skipped, consumers = _split_parts(parts)
                result.replaced += replaced
                result.skipped.extend(skipped)
                result.consumers.extend(consumers)
                if replaced:
                    result.replaced_paths.append(
                        str(asset.relative_to(out_dir)))
    # Phase 2：结构化覆盖（有需求集 + 有消费者记录时）
    if required is not None and result.consumers:
        outcome = compute_coverage(result.consumers, required)
        result.coverage = outcome
        result.overall = outcome.overall.name
        result.incomplete = outcome.blocks_publish()
    return result
