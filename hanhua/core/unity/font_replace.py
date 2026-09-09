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
    #: bundle 材质 m_SavedProperties.m_Floats 的 (name, value) 对——
    #: 图集耦合浮点（_TextureWidth/_TextureHeight/_GradientScale）的
    #: 权威取值来源（A15：材质浮点与替换后图集不同步 → SDF 采样错位
    #: → UI 整体不可见但射线仍命中）
    material_floats: tuple = ()


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


#: TMP 布局特征字段（根级）：tmp2=m_GlyphTable，tmp1=m_glyphInfoList。
_TMP_FIELD_NAMES = ("m_GlyphTable", "m_glyphInfoList")


def _node_has_tmp_fields(node) -> bool:
    """节点树根字段是否含 TMP 布局特征字段。"""
    for child in getattr(node, "m_Children", None) or []:
        if getattr(child, "m_Name", None) in _TMP_FIELD_NAMES:
            return True
    return False


def _mono_script_key(obj):
    """MonoBehaviour 的 m_Script 指针签名（预筛局部缓存键）。

    同类对象的 m_Script PPtr 恒等——按 (assets_file id, FileID, PathID)
    memo 可让同类对象只做一次 deref+get_nodes。缓存是调用方局部 dict，
    env 存活期内 assets_file 对象被 env.files 持有、id 稳定，无复用风险。
    头部解析失败返回 None（此时 read_typetree 同样失败，不缓存）。
    """
    try:
        mb = obj.parse_monobehaviour_head()
        pptr = getattr(mb, "m_Script", None)
        if pptr is None:
            return None
        return (id(getattr(obj, "assets_file", None)),
                int(getattr(pptr, "m_FileID", 0) or 0),
                int(getattr(pptr, "m_PathID", 0) or 0))
    except Exception:  # noqa: BLE001
        return None


def _tmp_candidate_prescreen(obj, cache: dict | None = None) -> bool:
    """MonoBehaviour 是否可能是 TMP 字体（不做 body 解析的预筛）。

    E6 根因（drova 实证 2026-09-07）：字体阶段对每个 MonoBehaviour 全量
    ``read_typetree``——UnityPyBoost 对部分游戏类（DialogueSystem.
    DS_EndNode 等 NodePort 字典嵌套类）的 body 解析会误读数组长度，
    构造 GB 级瞬时列表后以 EOFError 失败，单对象 2.4-8s；× 容器内全部
    MonoBehaviour × 45 容器 × 2 遍（主遍历 + 重开验证）= 写回期间内存
    50-90% 反复横跳（驻留 RSS 正常，纯瞬时分配振荡）。

    预筛原理（语义等价，不退化漏检）：read_typetree 按节点树解析，产物
    dict 的顶层键集合 == 节点树根字段名集合。节点树根字段无
    m_GlyphTable/m_glyphInfoList 时解析产物必然也没有——与「全量解析后
    _typetree_layout_version 判 None → 跳过」完全等价；节点树不可得
    （_get_typetree_node 抛异常）时 read_typetree 必然同样抛异常，
    两边都跳过。只有特征字段在场的候选对象才做 body 解析。

    内嵌 typetree（serialized_type.node 在场）直接查节点树；typeless
    bundle 走 generate_monobehaviour_node（头部固定布局解析 + m_Script
    deref + get_nodes，均不触碰 body，实测毫秒级），并按脚本签名 memo。

    返回 False=确定非候选（跳过）；True=候选或无法预筛（对象无
    _get_typetree_node API——测试桩，回退全量解析，旧行为不变）。
    """
    getter = getattr(obj, "_get_typetree_node", None)
    if getter is None:
        return True
    # 内嵌 typetree：节点树现成（与 ObjectReader._get_typetree_node 同判据）
    st = getattr(obj, "serialized_type", None)
    node = getattr(st, "node", None) if st is not None else None
    if node:
        return _node_has_tmp_fields(node)
    # typeless：按脚本签名 memo（同类对象只 deref+get_nodes 一次）
    key = _mono_script_key(obj) if cache is not None else None
    if key is not None and key in cache:
        return cache[key]
    try:
        node = getter()
    except Exception:  # noqa: BLE001  # 节点树不可得 → read_typetree 同样失败
        if key is not None:
            cache[key] = False
        return False
    result = _node_has_tmp_fields(node)
    if key is not None:
        cache[key] = result
    return result


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
    material_floats: tuple = ()
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
                floats = (tree.get("m_SavedProperties")
                          or {}).get("m_Floats") or []
                if isinstance(floats, list):
                    # A15：bundle 材质浮点是图集耦合参数的权威配对值
                    material_floats = tuple(
                        (str(n), float(v)) for n, v in floats
                        if isinstance(n, str)
                        and isinstance(v, (int, float)))
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
            material_floats=material_floats,
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

# A10（0.46.0）：目标 TTF 相对原内嵌字体的最大膨胀倍数。游戏内嵌
# Font 常是 30-110KB 的拉丁字体，白名单 CJK 全字库 52.6MB——无上限
# 全量写入是 fake-it 黑屏排查中确认的资产暴涨根因（sharedassets4
# 2.6MB→212MB）。裁剪字体（subset）通常 1-2MB，允许 12 倍裕量：
# 覆盖小原字体 + 需求字符集扩展的正常场景，拒绝整字库量级的写入。
_FONT_DATA_MAX_GROWTH = 12


def _font_subset_bytes(font_obj, ttf_bytes: bytes,
                       required_chars: set[int],
                       cache: dict[int, bytes]) -> bytes | None:
    """A10（0.46.0）：按需求集构建 legacy Font 的裁剪替换字体。

    全量 TTF 在 _FONT_DATA_MAX_GROWTH 上限内时直接返回 None（调用方
    用全量——零行为变化）。超限时用 merge_fonts 以**原内嵌字体为
    primary、目标 TTF 为 fallback** 构建 subset：原字体字形全保留
    （拉丁/数字/UI 符号不回退），目标字体只补需求字符中缺失部分，
    并裁剪到需求集。

    subset 按「原字体内容」缓存（同容器多 Font 对象共享，键 = 原字体
    字节长度——同长度不同内容的碰撞会导致错误复用，但同容器内嵌字体
    重复共享是常态，碰撞概率与代价（字符集略偏）可接受）。

    任何失败（fontTools 缺失/解析失败/产物仍超限）→ 返回 None 交调用
    方走全量路径（_patch_font_object 会再拦一次，最终进 skipped 诚实
    记录）。绝不抛出——字体是增强项不阻断写回。
    """
    try:
        tree = font_obj.read_typetree()
        font_data = tree.get("m_FontData")
        if not isinstance(font_data, list) or len(font_data) < 256:
            return None
        current = bytes(font_data)
        if not _ttf_has_magic(current):
            return None
        if len(ttf_bytes) <= len(current) * _FONT_DATA_MAX_GROWTH:
            return None  # 全量在上限内——不需要 subset
        key = len(current)
        if key not in cache:
            from hanhua.core.font.font_merge import merge_fonts
            needed = set(chr(c) for c in required_chars)
            merged = merge_fonts(current, ttf_bytes, needed)
            # 产物仍超限 → 不用（宁漏勿坏）
            if len(merged) > len(current) * _FONT_DATA_MAX_GROWTH:
                return None
            cache[key] = merged
        return cache[key]
    except Exception:  # noqa: BLE001
        return None


def _patch_font_object(env, font_obj, ttf_bytes: bytes) -> bool:
    """把单个 Font 对象的内嵌 TTF 换成目标 TTF。返回是否替换。

    度量换算（0.40.0 fromivan 修正）：等比缩放替换 TTF 的自然度量，
    使 m_LineSpacing 对齐原字体声明值——度量间比例保持替换 TTF 的
    自然比（deadbeat 模糊根因是比例失真，不复现），幅度对齐原字体
    行距（布局节奏/BestFit 拟合口径不变）。此前直接写替换 TTF 自然值，
    CJK 字体行距 1.437em 把原紧凑字体（0.93em）放大 1.55 倍 → BestFit
    组件字号被压到极小（「部分文本非常小」根因）。
    并把像素字体渲染模式（HintedRaster）改为 Smooth——矢量 TTF 锯齿。

    A10（0.46.0）：全量 TTF 无上限写入是资产暴涨根因——白名单字体
    52.6MB 写进每个 36KB 级的内嵌 Font，fake-it sharedassets4 从
    2.6MB 涨到 212MB。目标 TTF 超出原内嵌字体 _FONT_DATA_MAX_GROWTH
    倍时拒绝替换（宁漏勿坏：超大字体不做静态替换，交给 TMP/发布
    验证兜底，绝不无声撑爆资产文件）。
    """
    tree = font_obj.read_typetree()
    font_data = tree.get("m_FontData")
    if not isinstance(font_data, list) or len(font_data) < 256:
        # 无内嵌字体数据（静态位图字体/外部引用）→ 不替换
        return False
    current = bytes(font_data)
    if not _ttf_has_magic(current):
        return False
    # A10 尺寸守恒闸门：全量替换目标必须与原内嵌字体同量级
    if len(ttf_bytes) > len(current) * _FONT_DATA_MAX_GROWTH:
        raise ValueError(
            f"目标 TTF {len(ttf_bytes) // 1024}KB 超出原内嵌字体 "
            f"{len(current) // 1024}KB 的 {_FONT_DATA_MAX_GROWTH} 倍"
            "——拒绝替换（资产暴涨防线，详见问题集 A10）")
    # 注意：不在此处跳过 current == ttf_bytes —— UnityPy typetree 解析器
    # 对同类型对象可能返回共享缓存，前一个对象已改则后续读到的就是目标字节；
    # 跳过会漏计数（替换本身无害）。save_typetree 幂等。
    metrics = _ttf_metrics(ttf_bytes)
    if metrics is not None:
        ascent, descent, line_gap = metrics
        font_size = tree.get("m_FontSize") or 16
        natural_line = (ascent - descent + line_gap) * font_size
        orig_line = tree.get("m_LineSpacing")
        if (isinstance(orig_line, (int, float)) and orig_line > 0
                and natural_line > 0):
            scale = orig_line / natural_line
            tree["m_Ascent"] = round(ascent * font_size * scale, 2)
            tree["m_Descent"] = round(descent * font_size * scale, 2)
            tree["m_LineSpacing"] = round(natural_line * scale, 2)
        else:
            # 原字体无行距声明（异常资产）→ 退回替换 TTF 自然度量
            tree["m_Ascent"] = round(ascent * font_size, 2)
            tree["m_Descent"] = round(descent * font_size, 2)
            tree["m_LineSpacing"] = round(natural_line, 2)
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
    from hanhua.core.unity.serialized_layout import restore_from_path
    with tempfile.TemporaryDirectory(
        prefix=f".{path.name}.", dir=path.parent,
    ) as tmp:
        saved = Path(tmp) / path.name
        # A10：gen 9-21 SerializedFile 保存后恢复原 data_offset 布局
        # （UnityPy 重算 4096→3904 破坏最小变更；零填充重建已实证恒等）
        saved.write_bytes(restore_from_path(container, path))
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


def _object_key(obj) -> tuple[str, int]:
    """对象全局标识（资产文件名, path_id）。mock/无 assets_file 回退空名
    （单文件 env 下所有对象同 key 由调用方 seen 去重兜底）。"""
    assets_file = getattr(obj, "assets_file", None)
    name = getattr(assets_file, "name", "") or "" if assets_file else ""
    return str(name), int(obj.path_id)


def _collect_baseline(env) -> dict[tuple[str, int], tuple[int, bytes]]:
    """打补丁前采集全对象原始字节指纹（writer._patch_asset 同模型）。

    写回安全核心口径：写回前后，除允许变化的对象外，游戏结构没有发生
    非预期变化。container.save() 是整容器重建——旧验证只查被替换对象的
    目标字段，整容器重建对非目标对象的任何改动（丢对象/改字节）都不会
    被发现。此处为每个对象记 (size, sha256)，重开后全量比对。
    get_raw_data 失败的对象跳过（与 writer 同容错——不可读基线不比对）。
    """
    import hashlib
    baseline: dict[tuple[str, int], tuple[int, bytes]] = {}
    for obj in env.objects:
        key = _object_key(obj)
        if key in baseline:
            continue
        try:
            raw = obj.get_raw_data()
        except Exception:  # noqa: BLE001
            continue
        if raw is None:
            continue
        baseline[key] = (len(raw), hashlib.sha256(raw).digest())
    return baseline


def _verify_object_baseline(verify, baseline: dict, patched_keys: set,
                            saved: Path) -> None:
    """重开容器后全对象基线比对（writer._verify_saved_bundle 同模型）。

    两层检查：
    1. 对象集合恒等——saved 容器里对象多了/少了都是结构变化，直接拒绝；
    2. 非目标对象字节恒等——本次允许变化的只有 patched_keys（被替换的
       Font/TMP/图集对象），其余任何对象的 (size, sha256) 必须与基线
       完全一致，否则容器重建引入了非预期改动。

    E5（2026-09-07 内存暴涨复现）：指纹流式采集——逐对象算 (size,
    sha256) 即弃，绝不把整容器全对象字节同时收进 dict（大 level bundle
    实测把进程推到 GB 级峰值）。语义不变：集合恒等 + 逐对象指纹恒等。
    """
    import hashlib
    sizes: dict[object, int] = {}
    digests: dict[object, bytes] = {}
    for obj in verify.objects:
        key = _object_key(obj)
        if key in sizes:
            continue
        try:
            raw = obj.get_raw_data()
        except Exception:  # noqa: BLE001
            continue
        sizes[key] = len(raw)
        digests[key] = hashlib.sha256(raw).digest()
    if set(sizes) != set(baseline):
        changed = sorted(set(baseline) ^ set(sizes))
        raise ValueError(
            f"容器对象集合变化（非预期结构改动）: {saved.name} "
            f"差异对象数={len(changed)} 首个={changed[0] if changed else '?'}")
    for key, (size, digest) in baseline.items():
        if key in patched_keys:
            continue
        if key not in sizes or sizes[key] != size or digests[key] != digest:
            raise ValueError(
                f"非目标对象字节变化: {saved.name} {key} "
                f"expected=({size}, {digest.hex()[:8]}…) "
                f"actual=({sizes.get(key, '缺失')}, …)")


def _verify_legacy_saved(saved: Path, ttf_bytes: bytes, replaced: int,
                         source_dir: Path | None = None,
                         baseline: dict | None = None,
                         patched_keys: set | None = None,
                         expected_fonts: set[bytes] | None = None
                         ) -> None:
    """重开临时容器验证全部 Font 的 m_FontData 均已被替换。

    0.45.0：baseline 给定时追加全对象基线比对——除被替换的 Font 对象外，
    容器内任何对象不得增删或字节变化（整容器重建防非预期改动）。

    0.46.0（A10）：subset 路径下不同 Font 对象可能写入不同 TTF 字节
    （各自原内嵌字体为 primary 的裁剪产物）。ttf_bytes 单一比对改为
    expected_fonts 集合匹配——全量路径集合只有一个元素，行为等价。
    """
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
            if not (isinstance(fd, list) and fd):
                continue
            data = bytes(fd)
            if expected_fonts is not None:
                if data in expected_fonts:
                    matched += 1
            elif data == ttf_bytes:
                matched += 1
        if matched < replaced:
            raise ValueError(
                f"Font 替换重开验证不一致: {saved.name} "
                f"replaced={replaced} matched={matched}")
        if baseline is not None:
            _verify_object_baseline(
                verify, baseline, patched_keys or set(), saved)
    finally:
        _dispose_environment(verify)


def replace_legacy_fonts_in_container(
    path: Path,
    ttf_bytes: bytes,
    progress: int = 0,
    typetree_generator: Any | None = None,
    source_dir: Path | None = None,
    required_chars: set[int] | None = None,
) -> tuple[int, list[str], list]:
    """替换单个 Unity 容器（.assets/level/bundle）内全部 Font 对象的内嵌 TTF。

    返回 (替换数, 跳过原因列表, 消费者记录列表)。Phase 2：每个 Font 对象
    都进消费者清单——已替换的附目标 TTF 真实字符集（cmap 解析），未替换的
    记为 STATIC_NOT_REPLACED（不得静默消失）。

    source_dir：外部引用解析根（写回副本路径时传原游戏目录——副本临时
    替换文件同目录无兄弟文件，Mono 游戏 m_Script PPtr deref 需在原目录
    解析 external；与 writer._verify_saved_bundle 同语义）。

    required_chars（A10 0.46.0）：本次翻译真实需求码点集。提供时对每个
    Font 对象先试全量 TTF；全量超出 _FONT_DATA_MAX_GROWTH 倍上限时按
    「原内嵌字体优先 + 目标 TTF 补缺 + 裁剪到需求集」构建 subset 再试
    ——原字体字形保留（拉丁/数字/UI 符号不回退），目标字体只补 CJK。
    subset 仍超限或构建失败 → 跳过并记录（宁漏勿坏），绝不无声撑爆。
    """
    from hanhua.core.font import FontConsumer
    from hanhua.core.font.ttf_charset import ttf_charset
    env = _make_env(typetree_generator, path=str(source_dir or path.parent))
    replaced = 0
    skipped: list[str] = []
    consumers: list[FontConsumer] = []
    ttf_chars: frozenset[int] = ttf_charset(ttf_bytes)
    subset_cache: dict[int, bytes] = {}   # 原字体长度 → subset 字节
    used_fonts: set[bytes] = set()        # 实际写入的字体字节集合
    try:
        env.load([str(path)])
        # 打补丁前采集全对象字节基线（0.45.0 写回安全闸门）
        baseline = _collect_baseline(env)
        patched_keys: set[tuple[str, int]] = set()
        seen: set[tuple[str, str, int]] = set()
        for obj in env.objects:
            if obj.type.name != "Font":
                continue
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                used = None
                if required_chars is not None:
                    used = _font_subset_bytes(
                        obj, ttf_bytes, required_chars, subset_cache)
                effective = used or ttf_bytes
                if _patch_font_object(env, obj, effective):
                    replaced += 1
                    used_fonts.add(effective)
                    patched_keys.add(_object_key(obj))
                    consumers.append(FontConsumer(
                        f"{path.name}#Font#{obj.path_id}", "legacy_font",
                        static_replaced=True,
                        font_scalars=frozenset(required_chars)
                        if used is not None else ttf_chars,
                        atlas_resolved=True,
                        ref="内嵌 TTF 已替换（"
                        + ("按需求集裁剪" if used is not None
                           else "字符集按 cmap 解析") + "）"))
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
                saved, ttf_bytes, replaced, source_dir=source_dir,
                baseline=baseline, patched_keys=patched_keys,
                expected_fonts=used_fonts or None),
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


#: 图集耦合材质浮点（A15）——SDF 采样与图集几何直接耦合的参数名。
#: _TextureWidth/_TextureHeight：SDF 梯度采样按图集尺寸归一化；
#: _GradientScale：SDF 等值线过渡带宽（≈ padding+1）。图集换成
#: bundle 4096² 后这些值若仍描述游戏原图集（512² 等），shader 采样
#: 完全错位 → 文本渲染为空（UI 消失但可点击，射线不依赖渲染）。
_ATLAS_COUpled_FLOATS = ("_TextureWidth", "_TextureHeight", "_GradientScale")


def _resolve_material_obj(env, tree: dict, anchor=None):
    """解析 TMP 字体引用的 Material（必须与字体同 SerializedFile）。

    tmp2 布局引用字段为 ``material``（小写，TMP_FontAsset 序列化字段），
    部分世代为 ``m_Material``。解析口径与 _resolve_atlas_obj 同源：
    m_FileID=0 同文件、path_id 按 anchor 资产文件限定（跨文件同号对象
    是错误目标）。
    """
    ref = tree.get("material")
    if not isinstance(ref, dict):
        ref = tree.get("m_Material")
    if not isinstance(ref, dict):
        return None
    file_id = ref.get("m_FileID")
    path_id = ref.get("m_PathID")
    if isinstance(file_id, str):
        same_file = file_id.strip() in {"", "0", "0:0"}
    else:
        same_file = not file_id or int(file_id) == 0
    if not same_file:
        return None  # 跨文件引用：不支持（宁漏勿坏）
    try:
        path_id = int(path_id)
    except (TypeError, ValueError):
        return None
    anchor_file = _obj_file_key(anchor) if anchor is not None else None
    for other in env.objects:
        if other.type.name != "Material":
            continue
        if anchor_file is not None and _obj_file_key(other) != anchor_file:
            continue
        if int(other.path_id) == path_id:
            return other
    return None


def _sync_material_floats(material_obj, payload: TmpBundlePayload,
                          atlas_width: int, atlas_height: int) -> bool:
    """把游戏侧 TMP Material 的图集耦合浮点同步为 bundle 配对值。

    返回是否有变化（False = 材质本来已同步或无耦合字段，无需保存）。

    A15 根因（UI 消失但可点击）：_patch_atlas_texture 把图集换成
    bundle 4096² 后，游戏侧 Material 的 m_SavedProperties.m_Floats 里
    _TextureWidth/_TextureHeight/_GradientScale 仍描述原图集（512² 等）
    → SDF shader 按旧尺寸归一化采样 → 文本渲染为空。取值权威来源是
    bundle 自带材质（与 bundle 图集同批生成，Rendezvous 手工修复实证
    必须同步）；bundle 材质浮点缺失时按替换后图集几何推导（_Gradient-
    Scale 回退 padding+1，与 SDF 生成惯例一致）。
    """
    bundle_floats = dict(payload.material_floats)
    fallbacks = {
        "_TextureWidth": float(atlas_width),
        "_TextureHeight": float(atlas_height),
        "_GradientScale": float(
            int(payload.font_typetree.get("m_AtlasPadding") or 0) + 1),
    }
    targets = {}
    for name in _ATLAS_COUpled_FLOATS:
        if name in bundle_floats:
            targets[name] = bundle_floats[name]
        elif name in fallbacks:
            targets[name] = fallbacks[name]
    if not targets:
        return False
    tree = material_obj.read_typetree()
    saved = tree.get("m_SavedProperties")
    if not isinstance(saved, dict):
        return False
    floats = saved.get("m_Floats")
    if not isinstance(floats, list):
        return False
    changed = False
    seen_names = set()
    for i, pair in enumerate(floats):
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2
                and isinstance(pair[0], str)):
            continue
        name = pair[0]
        if name in targets:
            seen_names.add(name)
            if float(pair[1]) != targets[name]:
                floats[i] = [name, targets[name]]
                changed = True
    # 材质浮点表缺失耦合字段时补齐（shader 按属性默认值读取会导致
    # 同样的采样错位——与 Rendezvous 手工修复字段集对齐）
    for name, value in targets.items():
        if name not in seen_names:
            floats.append([name, value])
            changed = True
    if changed:
        material_obj.save_typetree(tree)
    return changed


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
        # 打补丁前采集全对象字节基线（0.45.0 写回安全闸门）
        baseline = _collect_baseline(env)
        patched_keys: set[tuple[str, int]] = set()
        seen: set[tuple[str, str, int]] = set()
        prescreen_cache: dict = {}
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            # E6：节点树预筛——非 TMP 候选不做 body 解析（语义等价，
            # 见 _tmp_candidate_prescreen；drova DS_EndNode 类 boost 解析
            # 单对象 GB 级瞬时分配是内存振荡根因）
            if not _tmp_candidate_prescreen(obj, prescreen_cache):
                continue
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
                # A15：图集耦合材质浮点同步（material 未解析不阻断替换
                # ——运行时插件兜底，但消费者记录诚实标注）
                material_obj = _resolve_material_obj(env, tree, obj)
                material_synced = False
                if material_obj is not None:
                    try:
                        material_synced = _sync_material_floats(
                            material_obj, payload,
                            payload.atlas_width, payload.atlas_height)
                    except Exception:  # noqa: BLE001
                        material_synced = False
                patched.append((obj, atlas_obj))
                patched_keys.add(_object_key(obj))
                patched_keys.add(_object_key(atlas_obj))
                if material_synced and material_obj is not None:
                    patched_keys.add(_object_key(material_obj))
                replaced += 1
                consumers.append(FontConsumer(
                    cid, "tmp_font", static_replaced=True,
                    font_scalars=payload.charset,
                    layout_ok=True, unity_version=unity_version,
                    material_resolved=material_obj is not None,
                    ref=f"bundle {payload.font_name} 已替换动态字体 · "
                        f"{len(payload.charset)} 字符"
                        + ("" if material_synced
                           else " · 材质浮点未同步（图集-材质失配风险）")))
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
            # A15：图集耦合材质浮点同步（UI 消失但可点击根因）
            material_obj = _resolve_material_obj(env, tree, obj)
            material_synced = False
            if material_obj is not None:
                try:
                    material_synced = _sync_material_floats(
                        material_obj, payload,
                        payload.atlas_width, payload.atlas_height)
                except Exception:  # noqa: BLE001
                    material_synced = False
            patched.append((obj, atlas_obj))
            patched_keys.add(_object_key(obj))
            patched_keys.add(_object_key(atlas_obj))
            if material_synced and material_obj is not None:
                patched_keys.add(_object_key(material_obj))
            replaced += 1
            consumers.append(FontConsumer(
                cid, "tmp_font", static_replaced=True,
                font_scalars=payload.charset,   # 替换后真实字形 = bundle 字符集
                layout_ok=True, unity_version=unity_version,
                material_resolved=material_obj is not None,
                ref=f"bundle {payload.font_name} 已替换 · "
                    f"{len(payload.charset)} 字符"
                    + ("" if material_synced
                       else " · 材质浮点未同步（图集-材质失配风险）")))
        if not patched:
            return 0, skipped, consumers
        _replace_and_swap(
            path, env,
            verify_fn=lambda saved: _verify_tmp_saved(
                saved, payload, replaced, typetree_generator,
                source_dir=source_dir,
                baseline=baseline, patched_keys=patched_keys))
    finally:
        _dispose_environment(env)
    return replaced, skipped, consumers


def _verify_tmp_saved(saved: Path, payload: TmpBundlePayload, replaced: int,
                      typetree_generator: Any | None = None,
                      source_dir: Path | None = None,
                      baseline: dict | None = None,
                      patched_keys: set | None = None) -> None:
    """重开临时容器验证 TMP 字形表 + 图集像素均已替换。

    只验字形数量会漏掉「元数据更新但流没写入」的假通过——旧实现正是如此
    （同尺寸分支只改 typetree 不写像素）。图集流数据必须逐字节等于
    payload.atlas_stream。

    0.45.0：baseline 给定时追加全对象基线比对——除被替换的 TMP 对象
    与其图集 Texture2D 外，容器内任何对象不得增删或字节变化。
    """
    verify = _make_env(typetree_generator, path=str(source_dir or saved.parent))
    try:
        verify.load([str(saved)])
        seen: set[tuple[str, str, int]] = set()
        prescreen_cache: dict = {}
        matched = 0
        atlas_verified = 0
        for obj in verify.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            key = (_obj_file_key(obj), obj.type.name, obj.path_id)
            if key in seen:
                continue
            seen.add(key)
            # E6：与主遍历同一预筛——非 TMP 候选不做 body 解析
            if not _tmp_candidate_prescreen(obj, prescreen_cache):
                continue
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
            # A15：材质图集耦合浮点必须已同步（_GradientScale 等描述
            # 旧图集 → SDF 采样错位 → UI 不可见但可点击）
            material_obj = _resolve_material_obj(verify, tree, obj)
            if material_obj is not None:
                saved_props = (material_obj.read_typetree()
                               .get("m_SavedProperties") or {})
                floats = saved_props.get("m_Floats") or []
                pairs = {str(n): float(v) for n, v in floats
                         if isinstance(n, str)
                         and isinstance(v, (int, float))}
                bundle_floats = dict(payload.material_floats)
                for name in _ATLAS_COUpled_FLOATS:
                    if name not in pairs:
                        continue  # 材质本无该字段：未注入，不误杀
                    if pairs[name] <= 0:
                        raise ValueError(
                            f"TMP 材质浮点非法（{name}="
                            f"{pairs[name]}）: {saved.name}")
                    if name in bundle_floats:
                        expected = bundle_floats[name]
                    elif name == "_TextureWidth":
                        expected = float(payload.atlas_width)
                    elif name == "_TextureHeight":
                        expected = float(payload.atlas_height)
                    else:
                        continue  # _GradientScale 无权威值时不强检
                    if pairs[name] != expected:
                        raise ValueError(
                            f"TMP 材质图集耦合浮点未同步（{name}="
                            f"{pairs[name]}，期望 {expected}）: {saved.name}")
        if matched < replaced:
            raise ValueError(
                f"TMP 替换重开验证不一致: {saved.name} "
                f"replaced={replaced} matched={matched}")
        if atlas_verified < replaced:
            raise ValueError(
                f"TMP 图集像素验证不一致: {saved.name} "
                f"replaced={replaced} atlas_verified={atlas_verified}")
        if baseline is not None:
            _verify_object_baseline(
                verify, baseline, patched_keys or set(), saved)
    finally:
        _dispose_environment(verify)


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
                    source_dir=source_dir,
                    required_chars=required_scalars)
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
