"""font_replace.py 静态字体替换单元测试。

覆盖：版本→bundle 映射、TMP 布局代判定、字体字段复制、
legacy Font TTF 替换判定、候选文件筛选、manifest 完整性。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from UnityPy.exceptions import TypeTreeError

from hanhua.core.models import FontConfig
from hanhua.core.unity.font_replace import (
    TmpBundlePayload,
    _asset_candidates,
    _copy_font_fields,
    _font_ttf_candidate,
    _patch_font_object,
    _ttf_has_magic,
    _ttf_metrics,
    _typetree_layout_version,
    install_static_fonts,
    select_tmp_bundle,
)

FONTS_DIR = Path(__file__).resolve().parents[1] / "fonts"
BUNDLE_DIR = FONTS_DIR / "TMP_Font_AssetBundles"


# ── 版本映射 ────────────────────────────────────────────────

@pytest.mark.parametrize("version,expected", [
    ("5.6.1", None),      # TMP1 走 legacy 路径，无 bundle
    ("2017.4.40", None),
    ("2018.4.36", None),
    ("2019.4.40", "notoserif_sdf_u2019"),
    ("2020.3.48", "notoserif_sdf_u2019"),
    ("2021.3.33", "notoserif_sdf_u2021"),
    ("2022.3.20", "notoserif_sdf_u2022"),
    ("6000.3.32f1", "notoserif_sdf_u6000"),
    ("2050.1.0", None),
])
def test_select_tmp_bundle(version, expected):
    bundle = select_tmp_bundle(version)
    if expected is None:
        assert bundle is None
    else:
        assert bundle is not None
        assert bundle.name == expected
        assert bundle.is_file()


def test_select_tmp_bundle_none():
    assert select_tmp_bundle(None) is None
    assert select_tmp_bundle("") is None
    assert select_tmp_bundle("invalid") is None


# ── 布局代判定 ──────────────────────────────────────────────

def test_layout_version():
    assert _typetree_layout_version({"m_GlyphTable": []}) == "tmp2"
    assert _typetree_layout_version({"m_glyphInfoList": []}) == "tmp1"
    assert _typetree_layout_version({"m_Name": "x"}) is None
    assert _typetree_layout_version({}) is None


# ── TTF magic ───────────────────────────────────────────────

def test_ttf_has_magic():
    assert _ttf_has_magic(b"\x00\x01\x00\x00" + b"x" * 20)
    assert _ttf_has_magic(b"OTTO" + b"x" * 20)
    assert not _ttf_has_magic(b"ABCDEFGH")
    assert not _ttf_has_magic(b"")


# ── legacy Font 对象替换 ────────────────────────────────────

class _StubFontObj:
    def __init__(self, font_data):
        self._tree = {
            "m_Name": "f", "m_FontData": font_data, "m_FontSize": 16.0,
            "m_Ascent": 12.0, "m_Descent": -4.0, "m_LineSpacing": 16.0,
            "m_FontRenderingMode": 2,
        }
        self.saved = None

    def read_typetree(self):
        return self._tree

    def save_typetree(self, tree):
        self.saved = tree
        return b"raw"


def _make_font_ttf(n=4096):
    # 伪造一个有效的 TTF 头 + 数据
    return b"\x00\x01\x00\x00" + bytes((i % 251 for i in range(n - 4)))


# 构造含真实 head/hhea 表的迷你 TTF：upm=1000, ascent=860, descent=-200, gap=0
def _make_metric_ttf():
    head = b"\x00\x01\x00\x00" + b"\x00" * 14 + (1000).to_bytes(2, "big") \
        + b"\x00" * 36
    hhea = b"\x00" * 4 + (860).to_bytes(2, "big", signed=True) \
        + (-200).to_bytes(2, "big", signed=True) + b"\x00" * 4 + b"\x00" * 12
    body = head + hhea
    body += bytes((i % 251 for i in range(4096 - len(body))))
    num_tables = 2
    # sfnt 头 12 字节：magic(4) + numTables(2) + searchRange/entrySelector/rangeShift(6)
    header = b"\x00\x01\x00\x00" + num_tables.to_bytes(2, "big") + b"\x00" * 6
    table_entries = b""
    # head 表：紧随目录；hhea 表：跟在 head 后
    head_len = len(head)
    for i, (tag, length, data) in enumerate([
            (b"head", head_len, head), (b"hhea", len(hhea), hhea)]):
        offset = 12 + 16 * num_tables + (head_len if i == 1 else 0)
        table_entries += tag + b"\x00" * 4 \
            + offset.to_bytes(4, "big") + length.to_bytes(4, "big")
    return header + table_entries + body


def test_ttf_metrics_real_fonts():
    import os
    ttf = Path(os.path.join(
        str(Path(__file__).resolve().parents[1]), "fonts",
        "SimplifiedChinese", "NotoSerifCJKsc-Medium.otf"))
    if ttf.is_file():
        ascent, descent, gap = _ttf_metrics(ttf.read_bytes())
        # Noto Serif CJK SC hhea: ascent≈0.92em, descent≈-0.24em
        assert 0.7 < ascent < 1.2
        assert -0.35 < descent < -0.1
        assert -0.2 < gap < 0.2


def test_ttf_metrics_synthetic():
    ascent, descent, gap = _ttf_metrics(_make_metric_ttf())
    assert ascent == 0.86
    assert descent == -0.2
    assert gap == 0.0
    assert _ttf_metrics(b"") is None
    assert _ttf_metrics(b"NOTATTF" + b"\x00" * 40) is None


def test_patch_font_object_replaces():
    obj = _StubFontObj(list(_make_font_ttf()))
    ttf = _make_font_ttf(8192)
    assert _patch_font_object(None, obj, ttf) is True
    assert obj.saved is not None
    assert bytes(obj.saved["m_FontData"]) == ttf


def test_patch_font_object_syncs_metrics():
    # 0.40.0：行距等比缩放——m_LineSpacing 对齐原字体声明值（16.0），
    # 度量间保持替换 TTF 自然比（ascent:descent:line = 0.86:-0.2:0），
    # 缩放系数 = 16.0 / ((0.86+0.2)*16) = 16/16.96。
    obj = _StubFontObj(list(_make_font_ttf()))
    assert _patch_font_object(None, obj, _make_metric_ttf()) is True
    assert obj.saved["m_LineSpacing"] == 16.0
    # scale = 16.0/16.96；ascent = 0.86*16*scale = 12.98
    assert obj.saved["m_Ascent"] == 12.98
    # descent 为负值乘 scale 后四舍五入：-0.2*16*0.9434 → -3.02
    assert obj.saved["m_Descent"] == -3.02
    # 像素字体渲染模式（2=HintedRaster）→ Smooth(0) 提高矢量 TTF 清晰度
    assert obj.saved["m_FontRenderingMode"] == 0


def test_patch_font_object_metrics_scaled_not_natural():
    """fromivan B21 回归：原紧凑行距（0.93em）遇 CJK 替换字体自然行距
    （1.437em）不得写自然值——统一放大致 BestFit 组件字号被压小。"""
    obj = _StubFontObj(list(_make_font_ttf()))
    obj._tree["m_LineSpacing"] = 14.86   # Kroftsmann 原值（16px 字号）
    assert _patch_font_object(None, obj, _make_metric_ttf()) is True
    # 缩放后行距 = 原行距 14.86（而非自然值 16.96）
    assert obj.saved["m_LineSpacing"] == 14.86


def test_patch_font_object_no_orig_linespacing_falls_back():
    # 原字体异常资产无行距声明 → 退回替换 TTF 自然度量（旧行为）
    obj = _StubFontObj(list(_make_font_ttf()))
    del obj._tree["m_LineSpacing"]
    assert _patch_font_object(None, obj, _make_metric_ttf()) is True
    assert obj.saved["m_LineSpacing"] == 16.96
    assert obj.saved["m_Ascent"] == 13.76
    assert obj.saved["m_Descent"] == -3.2


def test_patch_font_object_keeps_smooth_mode():
    obj = _StubFontObj(list(_make_font_ttf()))
    obj._tree["m_FontRenderingMode"] = 0
    assert _patch_font_object(None, obj, _make_metric_ttf()) is True
    assert obj.saved["m_FontRenderingMode"] == 0


def test_patch_font_object_skips_small_data():
    obj = _StubFontObj(list(b"\x00\x01\x00\x00" + b"\x00" * 100))
    assert _patch_font_object(None, obj, _make_font_ttf()) is False
    assert obj.saved is None


def test_patch_font_object_skips_non_ttf():
    obj = _StubFontObj(list(b"NOTAFONT" + b"\x00" * 300))
    assert _patch_font_object(None, obj, _make_font_ttf()) is False
    assert obj.saved is None


def test_patch_font_object_skips_empty():
    obj = _StubFontObj([])
    assert _patch_font_object(None, obj, _make_font_ttf()) is False


def test_patch_font_object_rejects_font_bloat():
    """A10（0.46.0）：全量 CJK 字库写入小内嵌字体 = 资产暴涨（fake-it
    sharedassets 2.6MB→212MB 实证根因）。超出 12 倍增长上限必须拒绝——
    ValueError 由调用方 except 捕获记入 skipped（宁漏勿坏）。"""
    import pytest as _pytest
    from hanhua.core.unity.font_replace import _FONT_DATA_MAX_GROWTH
    obj = _StubFontObj(list(_make_font_ttf(4096)))   # 原字体 4KB
    huge = _make_font_ttf(4096 * (_FONT_DATA_MAX_GROWTH + 1))
    with _pytest.raises(ValueError, match="倍"):
        _patch_font_object(None, obj, huge)
    # 边界内（正好 12 倍）仍正常替换
    ok = _make_font_ttf(4096 * _FONT_DATA_MAX_GROWTH)
    assert _patch_font_object(None, obj, ok) is True
    assert bytes(obj.saved["m_FontData"]) == ok


# ── TMP 字段复制 ────────────────────────────────────────────

def _payload(layout, fields):
    return TmpBundlePayload(
        bundle_path=Path("b"),
        font_name="test",
        glyph_count=100,
        layout_version=layout,
        font_typetree=fields,
        atlas_texture={},
        atlas_stream=b"",
        atlas_width=8,
        atlas_height=8,
        atlas_format=1,
    )


def test_copy_fields_tmp2():
    payload = _payload("tmp2", {
        "m_GlyphTable": [{"i": 1}], "m_CharacterTable": [{"c": 65}],
    })
    game = {"m_GlyphTable": [], "m_Name": "game"}
    assert _copy_font_fields(game, payload) is True
    assert game["m_GlyphTable"] == [{"i": 1}]
    assert game["m_CharacterTable"] == [{"c": 65}]
    # 未出现在 bundle 的字段不动
    assert game["m_Name"] == "game"


def test_copy_fields_tmp1():
    payload = _payload("tmp1", {"m_glyphInfoList": [{"id": 1}]})
    game = {"m_glyphInfoList": []}
    assert _copy_font_fields(game, payload) is True
    assert game["m_glyphInfoList"] == [{"id": 1}]


def test_copy_fields_no_change():
    payload = _payload("tmp2", {"m_GlyphTable": [{"i": 1}]})
    game = {"m_GlyphTable": [{"i": 1}]}
    assert _copy_font_fields(game, payload) is False


def test_copy_fields_layout_mismatch_fields():
    # 布局匹配由调用方保证（replace_tmp_fonts_in_container 内 layout 检查）；
    # _copy_font_fields 只复制 bundle 里存在的字段，tmp1 字段写入即可
    payload = _payload("tmp1", {"m_glyphInfoList": [{"id": 1}]})
    game = {"m_GlyphTable": []}
    assert _copy_font_fields(game, payload) is True
    assert game["m_glyphInfoList"] == [{"id": 1}]
    assert game["m_GlyphTable"] == []


# ── 字体候选 ────────────────────────────────────────────────

def test_font_ttf_candidate_whitelist():
    # legacy 白名单：CFF OTF 已弃用（D1 根治 2026-09-04），走别名映射。
    # _font_ttf_candidate 是 legacy Font 路径的候选解析，TMP SDF 路径不受影响。
    cfg = FontConfig(filename="SimplifiedChinese/NotoSerifCJKsc-Medium.otf")
    cand = _font_ttf_candidate(cfg)
    assert cand is None or cand.is_file()


def test_font_ttf_candidate_unknown():
    cfg = FontConfig(filename="不存在.ttf")
    assert _font_ttf_candidate(cfg) is None
    assert _font_ttf_candidate(FontConfig(filename="")) is None


# ── 候选文件筛选 ────────────────────────────────────────────

def test_asset_candidates_filters(tmp_path):
    big = b"x" * 4096
    (tmp_path / "data.unity3d").write_bytes(big)
    (tmp_path / "globalgamemanagers.assets").write_bytes(big)
    (tmp_path / "mainData").write_bytes(big)
    (tmp_path / "level1").write_bytes(big)
    (tmp_path / "resources.assets").write_bytes(big)
    (tmp_path / "readme.txt").write_bytes(big)
    (tmp_path / "il2cpp_data" / "Metadata").mkdir(parents=True)
    (tmp_path / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(big)
    (tmp_path / "MonoBleedingEdge").mkdir()
    (tmp_path / "MonoBleedingEdge" / "x.bundle").write_bytes(big)
    names = {p.name for p in _asset_candidates(tmp_path)}
    assert names == {"data.unity3d", "globalgamemanagers.assets",
                     "mainData", "level1", "resources.assets"}
    assert "global-metadata.dat" not in names


def test_asset_candidates_skips_tiny_placeholder(tmp_path):
    # <256 字节的占位文件（如测试 fixture 的 10 字节 globalgamemanagers）
    # 不是真实 Unity 容器，必须跳过，避免 UnityPy 以未知格式持有句柄
    (tmp_path / "globalgamemanagers").write_bytes(b"2022.3.34f1")
    assert _asset_candidates(tmp_path) == []


# ── manifest 完整性 ─────────────────────────────────────────

def test_manifest_matches_bundles():
    manifest = json.loads((BUNDLE_DIR / "manifest.json").read_text(
        encoding="utf-8"))
    integrity = manifest["integrity"]
    # manifest 里的每个 bundle 都必须真实存在且 sha256 匹配
    # （单字体 NotoSerif × 4 版本，共 4 个）
    assert len(integrity) == 4
    for name, meta in integrity.items():
        p = BUNDLE_DIR / name
        assert p.is_file(), f"{name} missing"
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        assert sha == meta["sha256"], f"{name} sha256 mismatch"


def test_manifest_versions_cover_all_bundles():
    manifest = json.loads((BUNDLE_DIR / "manifest.json").read_text(
        encoding="utf-8"))
    integrity = manifest["integrity"]
    for name in ["notoserif_sdf_u2019", "notoserif_sdf_u2021",
                 "notoserif_sdf_u2022", "notoserif_sdf_u6000"]:
        assert name in integrity


# ── Phase 2：字符表提取与需求集覆盖判定 ─────────────────────

def test_tmp_chars_extraction():
    from hanhua.core.unity.font_replace import _tmp_chars
    tmp2 = {"m_CharacterTable": [
        {"m_Unicode": 0x4E00, "m_GlyphIndex": 0},
        {"m_Unicode": 0x41, "m_GlyphIndex": 1},
    ]}
    assert _tmp_chars(tmp2) == [0x4E00, 0x41]
    tmp1 = {"m_glyphInfoList": [
        {"m_characterCode": 0x9F99}, {"m_characterCode": 0x42},
    ]}
    assert _tmp_chars(tmp1) == [0x9F99, 0x42]
    assert _tmp_chars({}) == []


def test_tmp_covers_required_subset():
    from hanhua.core.unity.font_replace import _tmp_covers_required
    tree = {"m_CharacterTable": [
        {"m_Unicode": ord(c)} for c in "继续游戏"
    ]}
    assert _tmp_covers_required(tree, {ord(c) for c in "继续"}) is True
    assert _tmp_covers_required(tree, {ord(c) for c in "继续游戏"}) is True


def test_tmp_covers_required_missing_codepoint():
    """实现重点 2 缺陷锁：需求集缺任何码点 → 必须替换（旧样本启发式漏检）。"""
    from hanhua.core.unity.font_replace import _tmp_covers_required
    tree = {"m_CharacterTable": [
        {"m_Unicode": ord(c)} for c in "继续游戏"
    ]}
    # 字形很多、只是缺需求集里的生僻字——样本启发式会误判已覆盖
    assert _tmp_covers_required(tree, {ord(c) for c in "继续游戏饕"}) is False
    assert _tmp_covers_required(tree, {ord("X")}) is False


# ── E6：TMP 候选预筛（节点树特征字段，不做 body 解析） ──

class _Node:
    def __init__(self, name, children=()):
        self.m_Name = name
        self.m_Children = list(children)


class _PrescreenObj:
    """预筛桩：节点树可控 + 可选头部解析失败。"""

    def __init__(self, node=None, head_raises=False, no_api=False):
        self._node = node
        self._head_raises = head_raises
        if not no_api:
            self._get_typetree_node = self._getter
        self.serialized_type = SimpleNamespace(
            node=node) if node is not None else SimpleNamespace(node=None)

    def _getter(self):
        if self._node is None:
            raise TypeTreeError("no nodes")
        return self._node

    def parse_monobehaviour_head(self):
        if self._head_raises:
            raise ValueError("head parse failed")
        return SimpleNamespace(m_Script=SimpleNamespace(
            m_FileID=0, m_PathID=42))


def test_prescreen_node_tree_tmp2_candidate():
    """节点树根含 m_GlyphTable → 候选（放行 body 解析）。"""
    from hanhua.core.unity.font_replace import _tmp_candidate_prescreen
    obj = _PrescreenObj(node=_Node("MonoBehaviour", [
        _Node("m_Name"), _Node("m_GlyphTable"), _Node("m_CharacterTable")]))
    assert _tmp_candidate_prescreen(obj, {}) is True


def test_prescreen_node_tree_tmp1_candidate():
    from hanhua.core.unity.font_replace import _tmp_candidate_prescreen
    obj = _PrescreenObj(node=_Node("MonoBehaviour", [
        _Node("m_fontInfo"), _Node("m_glyphInfoList")]))
    assert _tmp_candidate_prescreen(obj, {}) is True


def test_prescreen_node_tree_non_tmp_rejected():
    """非 TMP 类（如 DialogueSystem 节点）→ 拒绝——这是内存根治主路径：
    boost body 解析（GB 级瞬时分配 + 9s）只发生在候选对象上。"""
    from hanhua.core.unity.font_replace import _tmp_candidate_prescreen
    obj = _PrescreenObj(node=_Node("MonoBehaviour", [
        _Node("m_GameObject"), _Node("m_position"), _Node("ports")]))
    assert _tmp_candidate_prescreen(obj, {}) is False


def test_prescreen_no_node_rejected():
    """节点树不可得（generator 失败/无 typetree）→ 拒绝——read_typetree
    对同一对象同样抛异常，语义等价（两边都跳过）。"""
    from hanhua.core.unity.font_replace import _tmp_candidate_prescreen
    obj = _PrescreenObj(node=None)
    assert _tmp_candidate_prescreen(obj, {}) is False


def test_prescreen_no_api_falls_back_true():
    """对象无 _get_typetree_node（测试桩/旧 UnityPy）→ 回退全量解析，
    旧行为不变（不因预筛缺 API 而漏检）。"""
    from hanhua.core.unity.font_replace import _tmp_candidate_prescreen
    obj = _PrescreenObj(node=None, no_api=True)
    assert _tmp_candidate_prescreen(obj, {}) is True


def test_prescreen_script_memo_cache():
    """同类对象按 m_Script 签名 memo：第二次不做 deref/get_nodes。
    头部解析失败的对象不缓存（返回 True 回退全量解析，不误杀）。"""
    from hanhua.core.unity.font_replace import _tmp_candidate_prescreen, \
        _mono_script_key
    calls = []

    class _CountingObj(_PrescreenObj):
        def _getter(self):
            calls.append(1)
            return super()._getter()

    cache: dict = {}
    node = _Node("MonoBehaviour", [_Node("m_GlyphTable")])
    a = _CountingObj(node=node)
    a.serialized_type = SimpleNamespace(node=None)   # 强制走 generator 路径
    b = _CountingObj(node=node)
    b.serialized_type = SimpleNamespace(node=None)
    assert _tmp_candidate_prescreen(a, cache) is True
    assert _tmp_candidate_prescreen(b, cache) is True
    assert len(calls) == 1                            # memo 命中
    # 头部解析失败 → key None → 不缓存，对象回退全量解析
    head_fail = _PrescreenObj(node=node, head_raises=True)
    head_fail.serialized_type = SimpleNamespace(node=None)
    assert _mono_script_key(head_fail) is None


def test_prescreen_uses_serialized_type_node_directly():
    """内嵌 typetree 在场 → 直接查节点树，不触碰 generator（serialized_
    type.node 即 _get_typetree_node 的首选来源，判据同源）。"""
    from hanhua.core.unity.font_replace import _tmp_candidate_prescreen
    calls = []

    class _CountingObj(_PrescreenObj):
        def _getter(self):
            calls.append(1)
            return super()._getter()

    obj = _CountingObj(node=_Node("MonoBehaviour", [
        _Node("m_Name"), _Node("m_GlyphTable")]))
    obj.serialized_type = SimpleNamespace(node=obj._node)
    assert _tmp_candidate_prescreen(obj, {}) is True
    assert calls == []                                # 未走 generator


# ── Phase 2：容器级消费者记录（replace_tmp_fonts_in_container） ──

class _FakeTmpObj:
    """最小对象桩：read/save typetree + path_id + 类型名 + 所属文件。"""

    def __init__(self, path_id, tree, type_name="MonoBehaviour",
                 assets_file=None):
        self.path_id = path_id
        self.type = SimpleNamespace(name=type_name)
        self._tree = tree
        # assets_file 缺省时 _obj_file_key 回退 ""（单文件 env 全相同 → anchor
        # 限定自然放宽），旧 fixture 无需改动。
        self.assets_file = (SimpleNamespace(name=assets_file)
                            if assets_file else None)

    def read_typetree(self):
        return self._tree

    def save_typetree(self, tree):
        self._tree = tree


class _FakeTmpEnv:
    def __init__(self, objs):
        self.objects = objs
        self.files = {}

    def load(self, paths):
        pass


def _run_container(path, objs, payload, monkeypatch,
                   required=None, unity_version="2021.3"):
    import UnityPy
    import hanhua.core.unity.font_replace as fr
    env = _FakeTmpEnv(objs)
    monkeypatch.setattr(UnityPy, "Environment", lambda: env)   # 函数内 import
    monkeypatch.setattr(fr, "_replace_and_swap", lambda *a, **kw: None)
    monkeypatch.setattr(fr, "_dispose_environment", lambda *a, **kw: None)
    return fr.replace_tmp_fonts_in_container(
        path, payload, required=required, unity_version=unity_version), env


def _full_payload(**kw):
    tree = {
        "m_Name": "g", "m_CharacterTable": [
            {"m_Unicode": 0x4E00, "m_GlyphIndex": 0},
        ],
        "m_GlyphTable": [{"m_Index": 0, "m_GlyphRect":
                          {"m_X": 0, "m_Y": 0, "m_Width": 4, "m_Height": 4}}],
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}],
    }
    base = dict(
        bundle_path=Path("b"), font_name="arialuni", glyph_count=1,
        layout_version="tmp2", font_typetree=tree,
        atlas_texture={}, atlas_stream=b"\x00" * 256,
        atlas_width=8, atlas_height=8, atlas_format=62,
        charset=frozenset({0x4E00, 0x4E01, 0x9F99}),
        material_name="TMP SDF", shader_name="TextMeshPro/Distance Field",
    )
    base.update(kw)
    return TmpBundlePayload(**base)


def test_container_replaced_consumer_records(tmp_path, monkeypatch):
    """替换成功 → 消费者 static_replaced=True，font_scalars = bundle 字符集。"""
    from hanhua.core.font import COVERED
    payload = _full_payload()
    game = {"m_Name": "g", "m_CharacterTable": [
        {"m_Unicode": 0x41, "m_GlyphIndex": 0}],     # 只覆盖 ASCII → 必须替换
        "m_GlyphTable": [{"m_Index": 0, "m_GlyphRect": {}}],
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}],
    }
    atlas = _FakeTmpObj(100, {"m_Name": "a", "m_TextureSettings": {}},
                        type_name="Texture2D")
    objs = [_FakeTmpObj(1, game), atlas]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), _ = _run_container(
        bundle, objs, payload, required={0x4E00}, monkeypatch=monkeypatch)
    assert replaced == 1
    assert consumers and consumers[0].static_replaced is True
    assert consumers[0].font_scalars == payload.charset
    assert consumers[0].unity_version == "2021.3"
    assert game["m_CharacterTable"][0]["m_Unicode"] == 0x4E00  # 字形表已复制
    assert atlas._tree["image data"] == payload.atlas_stream    # 图集已替换


def test_container_already_covers_consumer(tmp_path, monkeypatch):
    """游戏字体已覆盖需求集 → 不替换，消费者记录游戏字符集（覆盖证据）。"""
    payload = _full_payload()
    game_chars = [0x4E00, 0x4E01, 0x9F99]
    game = {"m_Name": "g", "m_CharacterTable": [
        {"m_Unicode": c, "m_GlyphIndex": 0} for c in game_chars],
        "m_GlyphTable": [{"m_Index": 0, "m_GlyphRect": {}}],
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}],
    }
    objs = [_FakeTmpObj(1, game),
            _FakeTmpObj(100, {"m_Name": "a", "m_TextureSettings": {}},
                       type_name="Texture2D")]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), env = _run_container(
        bundle, objs, payload, required=set(game_chars),
        monkeypatch=monkeypatch)
    assert replaced == 0
    assert "already covers required" in skipped[0]
    assert consumers[0].static_replaced is True
    assert consumers[0].font_scalars == frozenset(game_chars)
    assert env.objects[0]._tree == game          # 原样保留，无修改


def test_container_dynamic_with_atlas_replaced(tmp_path, monkeypatch):
    """hickory 实证回归：0-glyph 动态字体带图集引用 → 静态替换为 bundle。

    用户 SDF 字体方案无 TTF 数据源，运行时插件兜底不可部署
    （FontInstallError）——0-glyph 字体必须静态替换（bundle 字符表
    全量覆盖需求集，查表渲染），否则动态消费者永久 BLOCKED。"""
    payload = _full_payload()
    dynamic = {"m_Name": "d", "m_GlyphTable": [],
               "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}]}
    atlas = _FakeTmpObj(100, {"m_Name": "a", "m_TextureSettings": {}},
                        type_name="Texture2D")
    objs = [_FakeTmpObj(1, dynamic), atlas]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), _ = _run_container(
        bundle, objs, payload, required={0x4E00}, monkeypatch=monkeypatch)
    assert replaced == 1
    assert consumers[0].kind == "tmp_font"
    assert consumers[0].static_replaced is True
    assert consumers[0].font_scalars == payload.charset
    assert dynamic["m_CharacterTable"][0]["m_Unicode"] == 0x4E00  # 表已复制
    assert atlas._tree["image data"] == payload.atlas_stream       # 图集已替换


def test_container_cross_file_pathid_no_mismatch(tmp_path, monkeypatch):
    """hickory 实证回归：多 SerializedFile bundle 里 path_id 跨文件重号，
    图集解析必须锚定字体同文件——否则无关纹理被撑爆、真图集未替换
    （部分笔画 + 无关纹理 4096×16MB 卡顿）。无关纹理排最前模拟误匹配。"""
    payload = _full_payload()
    game = {"m_Name": "g", "m_CharacterTable": [
        {"m_Unicode": 0x41, "m_GlyphIndex": 0}],
        "m_GlyphTable": [{"m_Index": 0, "m_GlyphRect": {}}],
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}],
    }
    # 无关纹理与真图集 path_id 相同（=100），分属不同文件；无关者排最前
    unrelated = _FakeTmpObj(
        100, {"m_Name": "FalloffLookupTexture", "m_TextureSettings": {}},
        type_name="Texture2D", assets_file="globalgamemanagers.assets")
    real_atlas = _FakeTmpObj(
        100, {"m_Name": "a", "m_TextureSettings": {}},
        type_name="Texture2D", assets_file="sharedassets0.assets")
    font = _FakeTmpObj(1, game, assets_file="sharedassets0.assets")
    objs = [unrelated, real_atlas, font]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), _ = _run_container(
        bundle, objs, payload, required={0x4E00}, monkeypatch=monkeypatch)
    assert replaced == 1
    assert consumers[0].static_replaced is True
    # 真图集（同文件）被替换；无关纹理未被碰
    assert real_atlas._tree["image data"] == payload.atlas_stream
    assert unrelated._tree.get("image data") is None
    assert "FalloffLookupTexture" in unrelated._tree.get("m_Name", "")


def test_container_dynamic_replace_forces_static_mode(tmp_path, monkeypatch):
    """0-glyph 动态字体静态替换后必须 m_AtlasPopulationMode=0——保留
    Dynamic（mode=1）会在运行时注入字形破坏静态 8361 表 + FreeGlyphRects
    （启动卡顿 + 部分笔画，hickory 实证根源之一）。"""
    payload = _full_payload()
    dynamic = {"m_Name": "d", "m_GlyphTable": [],
               "m_AtlasPopulationMode": 1,
               "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}]}
    atlas = _FakeTmpObj(100, {"m_Name": "a", "m_TextureSettings": {}},
                        type_name="Texture2D")
    objs = [_FakeTmpObj(1, dynamic), atlas]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), _ = _run_container(
        bundle, objs, payload, required={0x4E00}, monkeypatch=monkeypatch)
    assert replaced == 1
    assert consumers[0].static_replaced is True
    assert dynamic["m_AtlasPopulationMode"] == 0          # 强制静态
    assert atlas._tree["image data"] == payload.atlas_stream


def test_container_atlas_dimensions_copied(tmp_path, monkeypatch):
    """hickory 实证回归：_TMP2_COPY_FIELDS 必须包含 m_AtlasWidth/Height/
    Padding——bundle 图集 4096 而游戏原 512，不复制则 TMP 按 512 计算
    UV（rect/512 而非 rect/4096）→ 8 倍采样偏移 → 文本部分笔画。"""
    payload = _full_payload()
    game = {"m_Name": "g", "m_CharacterTable": [
        {"m_Unicode": 0x41, "m_GlyphIndex": 0}],
        "m_GlyphTable": [{"m_Index": 0, "m_GlyphRect": {}}],
        "m_AtlasWidth": 512, "m_AtlasHeight": 512, "m_AtlasPadding": 5,
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}],
    }
    payload = _full_payload(
        font_typetree=dict(payload.font_typetree,
                           m_AtlasWidth=4096, m_AtlasHeight=4096,
                           m_AtlasPadding=9))
    atlas = _FakeTmpObj(100, {"m_Name": "a", "m_TextureSettings": {}},
                        type_name="Texture2D")
    objs = [_FakeTmpObj(1, game), atlas]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), _ = _run_container(
        bundle, objs, payload, required={0x4E00}, monkeypatch=monkeypatch)
    assert replaced == 1
    assert game["m_AtlasWidth"] == 4096
    assert game["m_AtlasHeight"] == 4096
    assert game["m_AtlasPadding"] == 9


def test_container_dynamic_without_atlas_stays_dynamic(tmp_path, monkeypatch):
    """0-glyph 且图集引用不可解析 → 保持 dynamic_tmp（诚实阻断，不假覆盖）。"""
    payload = _full_payload()
    dynamic = {"m_Name": "d", "m_GlyphTable": []}     # 无 m_AtlasTextures
    objs = [_FakeTmpObj(1, dynamic)]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), _ = _run_container(
        bundle, objs, payload, required={0x4E00}, monkeypatch=monkeypatch)
    assert replaced == 0
    assert consumers[0].kind == "dynamic_tmp"
    assert consumers[0].runtime_provider_available is False
    assert any("dynamic font (0 glyphs)" in s for s in skipped)


def test_container_dynamic_and_layout_consumers(tmp_path, monkeypatch):
    """dynamic 0 glyph 与布局不匹配对象各自进消费者终态（不消失）。"""
    from hanhua.core.font import BLOCKED, CANDIDATE_ONLY
    payload = _full_payload()
    dynamic = {"m_Name": "d", "m_GlyphTable": []}         # 0 glyph
    legacy = {"m_Name": "l", "m_glyphInfoList": []}       # tmp1 布局
    objs = [_FakeTmpObj(1, dynamic), _FakeTmpObj(2, legacy)]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    (replaced, skipped, consumers), _ = _run_container(
        bundle, objs, payload, required={0x4E00}, monkeypatch=monkeypatch)
    assert replaced == 0
    kinds = {(c.consumer_id.split("#TMP#")[1], c.kind): c
             for c in consumers}
    assert kinds["1", "dynamic_tmp"].runtime_provider_available is False
    assert kinds["2", "tmp_font"].layout_ok is False
    # 注意：all(any(... for s in ...)) 的 for 子句会绑到最外层 all 上，
    # 内层 any(bool) 抛 "bool not iterable"——genexp 体需括号包裹
    assert all(("dynamic" in s) or ("layout" in s) for s in skipped)


# ── install_static_fonts 容错 ───────────────────────────────

def test_install_static_fonts_empty_dir(tmp_path):
    result = install_static_fonts(tmp_path, FontConfig(enabled=True))
    assert result.replaced == 0
    assert result.skipped == []
    assert result.warnings == []


def test_install_static_fonts_disabled_tmp(tmp_path):
    # enabled=False 时 TMP 路径不执行，legacy 仍按 TTF 替换
    result = install_static_fonts(tmp_path, FontConfig(enabled=False))
    assert result.replaced == 0


def test_install_static_fonts_rejects_corrupt_target_ttf(
        tmp_path, monkeypatch):
    """#42 防复发自检：目标 TTF 损坏（坏 magic）→ 拒绝替换 + 明确告警。

    旧行为：损坏 TTF 照样写进游戏且 replaced>0——用户以为字体已替换，
    方框问题复发时无法归因（静默假 PASS）。自检后 replaced=0、warning
    明示原因，覆盖证明如实为未覆盖。
    """
    calls = []

    bad_ttf = tmp_path / "broken.otf"
    bad_ttf.write_bytes(b"NOTATTF" + b"\x00" * 2000)

    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._font_ttf_candidate",
        lambda _cfg: bad_ttf)
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._asset_candidates",
        lambda _out_dir: [tmp_path / "x.bundle"])
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace.replace_legacy_fonts_in_container",
        lambda _asset, _ttf_bytes, **kw: calls.append(1) or (1, []))

    result = install_static_fonts(tmp_path, FontConfig(enabled=True))

    assert calls == []                      # 损坏 TTF 不进入任何替换
    assert result.replaced == 0
    assert any("内容无效" in w for w in result.warnings)


def test_install_static_fonts_rejects_empty_target_ttf(
        tmp_path, monkeypatch):
    """#42 防复发自检：空壳 TTF（<1KB）同样拒绝，不把坏数据写进游戏。"""
    empty_ttf = tmp_path / "empty.otf"
    empty_ttf.write_bytes(b"\x00\x01\x00\x00" + b"\x00" * 200)

    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._font_ttf_candidate",
        lambda _cfg: empty_ttf)
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._asset_candidates",
        lambda _out_dir: [tmp_path / "x.bundle"])
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace.replace_legacy_fonts_in_container",
        lambda _asset, _ttf_bytes, **kw: (1, []))

    result = install_static_fonts(tmp_path, FontConfig(enabled=True))

    assert result.replaced == 0
    assert any("内容无效" in w for w in result.warnings)


def test_install_static_fonts_collects_replaced_paths(
        tmp_path, monkeypatch):
    """C5：整容器重建的 bundle 必须记下相对路径，供 catalog CRC 二次同步。"""
    ttf = tmp_path / "f.otf"
    ttf.write_bytes(_make_font_ttf())
    bundle = tmp_path / "StreamingAssets" / "aa" / "fonts.bundle"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"bundle")

    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._font_ttf_candidate",
        lambda _cfg: ttf)
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._asset_candidates",
        lambda _out_dir: [bundle])
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace.replace_legacy_fonts_in_container",
        lambda _asset, _ttf_bytes, **kw: (2, []))

    result = install_static_fonts(tmp_path, FontConfig(enabled=True))

    assert result.replaced == 2
    assert result.replaced_paths == ["StreamingAssets/aa/fonts.bundle"]


# ── Phase 2（审计 §9 样本 1）：部分命中必须 INCOMPLETE 而非 PASS ──

def test_partial_hit_is_incomplete_not_pass(tmp_path, monkeypatch):
    """审计 §1 核心缺陷修复锁定：replaced > 0 不再代表全局成功。

    容器内「一个 TMP 可替换、一个 dynamic 0 glyph」——旧逻辑全局 PASS。
    Phase 2 后：容器函数返回逐对象消费者记录（dynamic 0 glyph →
    dynamic_tmp 消费者），install_static_fonts 汇总结构化覆盖：
    replaced=1 但整体 BLOCKED，result.incomplete=True。
    """
    from hanhua.core.font import BLOCKED, FontConsumer
    from hanhua.core.font.glyph_set import build_required_glyph_set
    from hanhua.core.models import TextEntry

    ttf = tmp_path / "f.otf"
    ttf.write_bytes(_make_font_ttf())
    bundle = tmp_path / "assets" / "fonts.bundle"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"bundle")

    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._font_ttf_candidate",
        lambda _cfg: None)                       # 只走 TMP 路径
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace._asset_candidates",
        lambda _out_dir: [bundle])
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace.select_tmp_bundle",
        lambda _version, **kw: bundle)
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace.load_tmp_bundle",
        lambda _path: SimpleNamespace(font_name="arialuni", glyph_count=0))
    # 容器内：替换 1 个（带消费者记录），跳过 1 个 dynamic 0 glyph
    monkeypatch.setattr(
        "hanhua.core.unity.font_replace.replace_tmp_fonts_in_container",
        lambda _asset, _payload, **kw: (1, ["dynamic 0 glyph 跳过"], [
            FontConsumer("tmp_replaced", "tmp_font", static_replaced=True,
                         font_scalars=frozenset(ord(c) for c in "继续游戏"),
                         unity_version="2021.3"),
            FontConsumer("tmp_dynamic", "dynamic_tmp",
                         runtime_provider_available=False),
        ]))

    entry = TextEntry("f", "k1", "Continue", translation="继续游戏",
                      status="translated")
    result = install_static_fonts(
        tmp_path, FontConfig(enabled=True), unity_version="2021.3",
        required=build_required_glyph_set([entry]))

    # 修复后：replaced=1 但结果层带覆盖信号 → INCOMPLETE 而非 PASS
    assert result.replaced == 1
    assert result.skipped == ["dynamic 0 glyph 跳过"]
    assert result.coverage is not None
    assert result.overall == BLOCKED.name
    assert result.incomplete is True
    assert "未覆盖" in result.summary_text()


# ── 0.45.0 全对象基线验证（写回安全闸门：除允许变化的对象外，容器
#    内任何对象不得增删或字节变化） ─────────────────────────────

class _RawTmpObj(_FakeTmpObj):
    """带原始字节的对象桩：get_raw_data 返回构造时给定的 bytes。"""

    def __init__(self, path_id, tree, raw: bytes, type_name="MonoBehaviour",
                 assets_file=None):
        super().__init__(path_id, tree, type_name, assets_file)
        self._raw = raw

    def get_raw_data(self):
        return self._raw


def _baseline_objs():
    """字体对象（将被替换）+ 图集 + 无关对象（必须字节恒等）。

    payload.atlas_texture 给出目标尺寸/格式，_patch_atlas_texture 会把它
    与游戏图集的 m_Name/m_TextureSettings 合并——图集树不需要预置字段
    （与其它容器测试同形态）。
    """
    payload = _full_payload(atlas_texture={
        "m_Width": 8, "m_Height": 8, "m_TextureFormat": 62})
    game = {"m_Name": "g", "m_CharacterTable": [
        {"m_Unicode": 0x41, "m_GlyphIndex": 0}],
        "m_GlyphTable": [{"m_Index": 0, "m_GlyphRect": {}}],
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 100}],
    }
    font = _RawTmpObj(1, game, b"font-original")
    atlas = _RawTmpObj(100, {"m_Name": "a", "m_TextureSettings": {}},
                       b"atlas-original", type_name="Texture2D")
    bystander = _RawTmpObj(7, {"m_Name": "bystander"}, b"bystander-original")
    return payload, game, font, atlas, bystander


def test_object_baseline_pass_untouched(tmp_path, monkeypatch):
    """非目标对象字节恒等 → 基线验证通过（不抛异常）。"""
    import hanhua.core.unity.font_replace as fr
    payload, game, font, atlas, bystander = _baseline_objs()
    objs = [font, atlas, bystander]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    captured = {}

    def fake_replace_and_swap(path, env, verify_fn=None):
        # 重开容器返回同一批对象（font/atlas 已改、bystander 原样）
        captured["verify_fn"] = verify_fn

    monkeypatch.setattr(fr, "_replace_and_swap", fake_replace_and_swap)
    monkeypatch.setattr(fr, "_dispose_environment", lambda *a, **kw: None)
    import UnityPy
    monkeypatch.setattr(UnityPy, "Environment", lambda: _FakeTmpEnv(objs))

    replaced, skipped, consumers = fr.replace_tmp_fonts_in_container(
        bundle, payload, required={0x4E00})
    assert replaced == 1
    # 同一批对象重开：bystander 未动 → 基线比对通过
    captured["verify_fn"](bundle)


def test_object_baseline_rejects_bystander_change(tmp_path, monkeypatch):
    """非目标对象字节变化 → ValueError（整容器重建引入非预期改动）。"""
    import hanhua.core.unity.font_replace as fr
    payload, game, font, atlas, bystander = _baseline_objs()
    objs = [font, atlas, bystander]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    captured = {}

    def fake_replace_and_swap(path, env, verify_fn=None):
        captured["verify_fn"] = verify_fn

    monkeypatch.setattr(fr, "_replace_and_swap", fake_replace_and_swap)
    monkeypatch.setattr(fr, "_dispose_environment", lambda *a, **kw: None)
    import UnityPy
    monkeypatch.setattr(UnityPy, "Environment", lambda: _FakeTmpEnv(objs))

    replaced, _, _ = fr.replace_tmp_fonts_in_container(
        bundle, payload, required={0x4E00})
    assert replaced == 1
    # 容器重建把 bystander 字节改了 → 基线比对必须拒绝
    bystander._raw = b"bystander-TAMPERED"
    with pytest.raises(ValueError, match="非目标对象字节变化"):
        captured["verify_fn"](bundle)


def test_object_baseline_rejects_object_set_change(tmp_path, monkeypatch):
    """对象集合变化（多/少对象）→ ValueError（结构级非预期改动）。"""
    import hanhua.core.unity.font_replace as fr
    payload, game, font, atlas, bystander = _baseline_objs()
    objs = [font, atlas, bystander]
    bundle = tmp_path / "fonts.bundle"
    bundle.write_bytes(b"x")
    captured = {}

    def fake_replace_and_swap(path, env, verify_fn=None):
        captured["verify_fn"] = verify_fn

    monkeypatch.setattr(fr, "_replace_and_swap", fake_replace_and_swap)
    monkeypatch.setattr(fr, "_dispose_environment", lambda *a, **kw: None)
    import UnityPy
    monkeypatch.setattr(UnityPy, "Environment", lambda: _FakeTmpEnv(objs))

    replaced, _, _ = fr.replace_tmp_fonts_in_container(
        bundle, payload, required={0x4E00})
    assert replaced == 1
    # 容器重建丢了一个对象 → 基线比对必须拒绝
    saved_objs = _FakeTmpEnv([font, atlas])   # bystander 消失
    monkeypatch.setattr(UnityPy, "Environment", lambda: saved_objs)
    with pytest.raises(ValueError, match="容器对象集合变化"):
        captured["verify_fn"](bundle)


def test_object_baseline_legacy_path(tmp_path, monkeypatch):
    """legacy Font 路径同样走全对象基线：Font 本身豁免、其余恒等。"""
    import hanhua.core.unity.font_replace as fr
    payload = _full_payload()
    ttf_bytes = _make_font_ttf()
    font_tree = {"m_FontData": list(ttf_bytes), "m_FontSize": 16,
                 "m_LineSpacing": 1.0}
    font = _RawTmpObj(1, font_tree, b"font-original", type_name="Font")
    bystander = _RawTmpObj(7, {"m_Name": "bystander"}, b"bystander-original")
    objs = [font, bystander]
    bundle = tmp_path / "level"
    bundle.write_bytes(b"x")
    captured = {}

    def fake_replace_and_swap(path, env, verify_fn=None):
        captured["verify_fn"] = verify_fn

    monkeypatch.setattr(fr, "_replace_and_swap", fake_replace_and_swap)
    monkeypatch.setattr(fr, "_dispose_environment", lambda *a, **kw: None)
    import UnityPy
    monkeypatch.setattr(UnityPy, "Environment", lambda: _FakeTmpEnv(objs))

    replaced, _, _ = fr.replace_legacy_fonts_in_container(bundle, ttf_bytes)
    assert replaced == 1
    # bystander 未动 → 通过
    captured["verify_fn"](bundle)
    # bystander 被动 → 拒绝
    bystander._raw = b"tampered"
    with pytest.raises(ValueError, match="非目标对象字节变化"):
        captured["verify_fn"](bundle)
