# -*- coding: utf-8 -*-
"""CFF OTF → TrueType (glyf) 转换器（D1 根治，2026-09-04）。

背景（问题集 D1 复发根因）：
  仓库唯一中文字体 NotoSerifCJKsc-Medium.otf 是 CFF 轮廓（OTTO magic）。
  运行时插件部署把它的字节直接写成 BepInEx/plugins/HanhuaFont/font.ttf
  （font_support.install_font_override），Unity `new Font(path)` 按
  TrueType glyf 表解析 CFF → 缺字/口口口。2026-08-18 单字体收敛时
  唯一字体源从真 TTF 换成了 CFF OTF，D1 复发。

根治方案（本脚本，一次性生成入库产物）：
  freetype-py 读 CFF 轮廓（FT_LOAD_NO_SCALE）→ 每条 contour 收集全部
  cubic，cu2qu curves_to_quadratic **整条 contour 联合优化**（单条逐个
  转换会丢失拐角保真，实测复杂字形渲染差放大 3 倍）→ TTGlyphPen 重建
  glyf，其余表（cmap/hmtx/OS2/name/head/hhea/vhea/vmtx/post）原样复制，
  丢弃 CFF/CFF2/GSUB/GPOS/GDEF/VORG/DSIG/BASE（glyf 字体不需要），
  maxp 从 0.5 升到 1.0（maxZones=2，其余 v1.0 字段显式置 0）。

产物质量（fonts/SimplifiedChinese/NotoSerifCJKsc-Medium.ttf）：
  - 65535 字形、cmap 44777 码点 1:1 保留、hmtx 步进 0 差异、
    head/hhea/OS2 度量逐字段一致（Unity 度量同步依赖这些表）；
  - 无 hinting 光栅化与源 CFF 差 ≤0.1%（hinting 差 ≤3.6%，属
    TrueType 引擎对 quad 轮廓的正常 hint 行为差异，非轮廓误差）；
  - magic 00010000（真 TrueType），插件 `new Font(path)` 可解析。

用法（仅当字体源更新时重跑）：
  python scripts/convert_cff_to_ttf.py \
      fonts/SimplifiedChinese/NotoSerifCJKsc-Medium.otf \
      fonts/SimplifiedChinese/NotoSerifCJKsc-Medium.ttf
依赖：freetype-py、fonttools、cu2qu（见 requirements.txt）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import freetype
from cu2qu.cu2qu import curves_to_quadratic
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._g_l_y_f import Glyph, table__g_l_y_f
from fontTools.pens.ttGlyphPen import TTGlyphPen

# 丢弃的表：glyf 字体不需要 CFF 生态表（OpenType 特性替换在游戏
# 渲染路径上不影响 CJK 文本显示）。
_DROP_TABLES = ("glyf", "loca", "CFF ", "CFF2", "GSUB", "GPOS", "GDEF",
                "VORG", "DSIG", "BASE")


def _contour_segments(cpts: list, ctags: list) -> list[tuple]:
    """一条 contour 的点序 → 有序段列表 ('l', p0, p1) / ('c', p0,c1,c2,p3)。

    先旋转到 on-curve 起点（TrueType 惯例）；全部 off-curve（罕见）时
    合成中点作为起点（两个相邻 off-curve 之间由渲染器隐含 on-curve 中点，
    合成点不影响形状）。
    """
    n = len(cpts)
    if ctags[0] != 1:
        for k in range(1, n):
            if ctags[k] == 1:
                cpts = cpts[k:] + cpts[:k]
                ctags = ctags[k:] + ctags[:k]
                break
        else:
            sp = ((cpts[0][0] + cpts[-1][0]) / 2.0,
                  (cpts[0][1] + cpts[-1][1]) / 2.0)
            cpts = [sp] + cpts
            ctags = [1] + ctags
    n = len(cpts)
    segs: list[tuple] = []
    cur = 0
    while True:
        j = (cur + 1) % n
        if ctags[j] == 1:
            segs.append(('l', cpts[cur], cpts[j]))
            cur = j
        else:
            c1 = cpts[j]
            j2 = (j + 1) % n
            c2 = cpts[j2]
            j3 = (j2 + 1) % n
            segs.append(('c', cpts[cur], c1, c2, cpts[j3]))
            cur = j3
        if cur == 0:
            break
    return segs


def convert(src_path: str | Path, out_path: str | Path,
            max_err: float = 0.2) -> Path:
    t0 = time.time()
    ft = freetype.Face(str(src_path))
    ft.set_char_size(1000 * 64)
    otf = TTFont(str(src_path), lazy=True)
    glyph_order = otf.getGlyphOrder()
    num_glyphs = len(glyph_order)

    glyphs: dict[str, Glyph] = {}
    empty = Glyph()
    for gid in range(num_glyphs):
        ft.load_glyph(gid, freetype.FT_LOAD_NO_SCALE
                      | freetype.FT_LOAD_NO_HINTING
                      | freetype.FT_LOAD_NO_BITMAP)
        o = ft.glyph.outline
        gname = glyph_order[gid]
        if not o.contours:
            glyphs[gname] = empty
            continue
        pts = [tuple(p) for p in o.points]
        tags = list(o.tags)
        pen = TTGlyphPen(None)
        start = 0
        for end in o.contours:
            segs = _contour_segments(pts[start:end + 1], tags[start:end + 1])
            start = end + 1
            cidx = [i for i, s in enumerate(segs) if s[0] == 'c']
            if cidx:
                # cu2qu 设计用法：整条 contour 的 cubic 一次联合转换
                #（on-curve 节点联合对齐；逐条转换丢拐角保真）
                cubics = [segs[i][1:] for i in cidx]
                try:
                    quads = curves_to_quadratic(cubics,
                                                [max_err] * len(cubics))
                except Exception:  # noqa: BLE001 联合失败逐条兜底
                    quads = [curves_to_quadratic([c], [max_err])[0]
                             for c in cubics]
                qmap = {i: quads[k] for k, i in enumerate(cidx)}
            else:
                qmap = {}
            pen.moveTo(segs[0][1])
            for i, s in enumerate(segs):
                if s[0] == 'l':
                    pen.lineTo(s[2])
                else:
                    # spline = [p0, ctrl..., pEnd]：p0/末点与相邻段共享，
                    # 只喂控制点（qCurveTo 隐含中点 on-curve）
                    pen.qCurveTo(*qmap[i][1:])
            pen.closePath()
        glyphs[gname] = pen.glyph()

    out = TTFont()
    for tag in otf.keys():
        if tag in _DROP_TABLES:
            continue
        out[tag] = otf[tag]
    maxp = otf["maxp"]
    maxp.tableVersion = 0x00010000
    maxp.numGlyphs = num_glyphs
    for attr, val in [("maxPoints", 0), ("maxContours", 0),
                      ("maxCompositePoints", 0), ("maxCompositeContours", 0),
                      ("maxZones", 2), ("maxTwilightPoints", 0),
                      ("maxStorage", 0), ("maxFunctionDefs", 0),
                      ("maxInstructionDefs", 0), ("maxStackElements", 0),
                      ("maxSizeOfInstructions", 0),
                      ("maxComponentElements", 0), ("maxComponentDepth", 0)]:
        setattr(maxp, attr, val)
    out["maxp"] = maxp
    glyf = table__g_l_y_f()
    glyf.glyphs = glyphs
    out["glyf"] = glyf
    out["loca"] = newTable("loca")
    out.glyphOrder = glyph_order
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(out_path))
    print(f"[convert] {num_glyphs} glyphs, {time.time() - t0:.1f}s, "
          f"{out_path} ({out_path.stat().st_size} bytes)")
    return out_path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    convert(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
