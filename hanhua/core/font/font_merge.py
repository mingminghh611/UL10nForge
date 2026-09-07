"""字体合并与字符集裁剪（工具移植任务 4，2026-08-16）。

来源：Warcraft-Font-Merger（cmap/glyf 合并 + TrimGlyph 裁剪逻辑）
+ FilterRepeatCharacter（文本字符集提取去重——CSDN 文章，核心为
按需求集去重）。用 fontTools 重新实现（runtime 已安装）。

能力：
1. collect_needed_chars(entries)：从译文+原文提取字符集（去重）——
   FilterRepeatCharacter 逻辑（只保留真正需要的字符）；
2. merge_fonts(primary, fallback, needed_chars)：合并两个 TTF——
   primary 的字形优先，fallback 补齐 needed_chars 中 primary 缺的
   字符（cmap 并集 + glyf 并集 + 引用字形递归复制），并裁剪到
   需求集（TrimGlyph 等价：只保留 needed + 引用字形）；
3. 输出新 TTF 字节（fontTools TTFont.save）。

集成：font 管线缺字场景（legacy 字体缺码点，如希伯来文/▶）——
用合并字体作为补全（发布时部署合并字体替代）。
"""
from __future__ import annotations

from pathlib import Path

try:
    from fontTools.ttLib import TTFont
except ImportError:  # pragma: no cover
    TTFont = None


def collect_needed_chars(
        texts, *, include_ascii: bool = True) -> set[str]:
    """从文本集合提取所需字符集（FilterRepeatCharacter 逻辑——去重）。

    - 中文译文 + 原文全部字符；include_ascii 时含 ASCII（拉丁/数字/
      标点——游戏 UI 常见）。
    - 排除控制字符（\n\t\r 等——字体不需要）。
    """
    needed: set[str] = set()
    for t in texts or ():
        for ch in str(t):
            if ord(ch) < 0x20 and ch not in ("\t", "\n", "\r"):
                continue
            if not include_ascii and ord(ch) < 0x80:
                continue
            needed.add(ch)
    return needed


def _iter_cmap_chars(font: TTFont) -> set[str]:
    chars: set[str] = set()
    if "cmap" not in font:
        return chars
    for table in font["cmap"].tables:
        for code, _name in getattr(table, "cmap", {}).items():
            try:
                chars.add(chr(code))
            except ValueError:
                continue
    return chars


def _needed_glyph_names(font: TTFont, needed_chars: set[str]) -> set[str]:
    """需求字符 → 字形名（cmap 映射；找不到的字符跳过）。"""
    names: set[str] = set()
    if "cmap" not in font:
        return names
    cmap = {}
    for table in font["cmap"].tables:
        cmap.update(getattr(table, "cmap", {}))
    for ch in needed_chars:
        name = cmap.get(ord(ch))
        if name:
            names.add(name)
    return names


def _collect_references(font: TTFont, names: set[str]) -> set[str]:
    """递归收集复合字形的引用字形（TrimGlyph 的 AddRef 等价）。"""
    glyf = getattr(font, "glyf", None)
    if glyf is None:
        return set()
    collected = set(names)
    stack = list(names)
    while stack:
        name = stack.pop()
        glyph = glyf.get(name)
        if glyph is None:
            continue
        refs = getattr(glyph, "components", None) or []
        for comp in refs:
            gname = getattr(comp, "glyphName", None)
            if gname and gname not in collected:
                collected.add(gname)
                stack.append(gname)
    return collected


def merge_fonts(
        primary: str | Path | bytes,
        fallback: str | Path | bytes,
        needed_chars: set[str],
        *,
        rename_postfix: str = "Merged") -> bytes:
    """合并两个字体（primary 优先 + fallback 补缺）并裁剪到需求集。

    返回合并后 TTF 字节。无 fontTools 或字体无法解析 → 抛 ValueError。
    """
    if TTFont is None:
        raise ValueError("fontTools 未安装，无法合并字体")
    primary_font = _load(primary)
    fallback_font = _load(fallback)
    try:
        return _merge(primary_font, fallback_font, needed_chars,
                      rename_postfix)
    finally:
        primary_font.close()
        fallback_font.close()


def _load(src: str | Path | bytes) -> TTFont:
    try:
        if isinstance(src, (str, Path)):
            font = TTFont(str(src))
        else:
            font = TTFont(__import__("io").BytesIO(src))
        return font
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"字体解析失败：{exc}") from exc


def _trim_cmap(font: TTFont, needed_chars: set[str]) -> None:
    """cmap 裁剪（TrimGlyph 等价）：需求外字符从 cmap 移除。

    必须先于 CFF→glyf 转换——转换只转需求字形，cmap 若仍引用
    未转换字形，保存/加载会 KeyError（'cid00002' 实证）。
    """
    keep = _needed_glyph_names(font, needed_chars)
    keep |= _collect_references(font, keep)
    # 全部子表都裁（含非 Unicode symbol 表——SourceHanSans 实证：
    # 非 Unicode 子表残留引用未转换字形 → 保存 KeyError）；format14
    # 变体选择器（uvDict）直接删除（游戏极少用，且其字形引用会漏裁）
    font["cmap"].tables = [
        t for t in font["cmap"].tables
        if getattr(t, "format", None) != 14]
    for subtable in font["cmap"].tables:
        try:
            subtable.cmap = {
                code: name for code, name in subtable.cmap.items()
                if name in keep}
        except (AttributeError, TypeError):
            continue


def _ensure_glyf(font: TTFont, needed_chars: set[str]) -> None:
    """CFF（OTF）字体按需求集转换为 TrueType glyf。

    完整中文字体（SourceHanSans 6.5 万字形）全量转换超 TTF 上限
    （numGlyphs 65535）——只转换需求字形 + 引用字形（裁剪先行，
    转换后字形数 = 需求集规模）。合并逻辑基于 glyf 表。
    """
    if "glyf" in font:
        return
    if "CFF " not in font:
        return
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib.tables._g_l_y_f import table__g_l_y_f
    from fontTools.ttLib.tables._l_o_c_a import table__l_o_c_a
    glyph_set = font.getGlyphSet()
    keep = _needed_glyph_names(font, needed_chars)
    keep |= _collect_references(font, keep)
    keep |= {font.getGlyphOrder()[0]}  # .notdef
    glyf = table__g_l_y_f()
    glyf.setGlyphOrder([])          # 空表——__setitem__ 自动 append
    glyf.glyphs = {}
    converted = []
    for name in font.getGlyphOrder():
        if name not in keep or name not in glyph_set:
            continue
        pen = TTGlyphPen(None)
        glyph_set[name].draw(pen)
        glyf[name] = pen.glyph()
        converted.append(name)
    # 注意：glyf.glyphOrder 只含转换的字形（全量会引未转换字形
    # KeyError——'cid00002' 实证）；全局 glyphOrder 同步（maxp recalc
    # 遍历 font.getGlyphOrder——不一致同样 KeyError）
    font.setGlyphOrder(converted)
    font["glyf"] = glyf
    font["loca"] = table__l_o_c_a()
    # 删除 CFF 表（转换后不再需要——保存时避免重复表冲突）
    if "CFF " in font:
        del font["CFF "]


def _merge(primary: TTFont, fallback: TTFont, needed_chars: set[str],
           rename_postfix: str) -> bytes:
    # 先裁 primary 的 cmap 到需求集（CFF 转换只转需求字形——cmap
    # 若仍引用未转换字形，保存/加载 KeyError 'cid00002' 实证）
    _trim_cmap(primary, needed_chars)
    # CFF（OTF）主字体按需求集转 glyf（在合并之前——转换后
    # 字形数 = 需求集规模，不超 TTF 上限）
    _ensure_glyf(primary, needed_chars)
    # 需求字形（primary 优先；缺的从 fallback 找）
    p_names = _needed_glyph_names(primary, needed_chars)
    f_names = _needed_glyph_names(fallback, needed_chars)
    missing = {n for n in f_names if n not in p_names}
    refs = _collect_references(fallback, missing)
    # fallback 需要复制：需求字形 + 引用字形；同步 hmtx 度量
    # （否则保存时 hmtx 缺字形键 KeyError——真实 OTF 合并实证）
    to_copy = missing | refs
    p_glyf = primary["glyf"]
    f_glyf = fallback["glyf"]
    p_hmtx = primary.get("hmtx")
    f_hmtx = fallback.get("hmtx")
    for name in to_copy:
        if name not in f_glyf:
            continue
        p_glyf[name] = f_glyf[name]
        if p_hmtx is not None and f_hmtx is not None \
                and name in f_hmtx.metrics:
            p_hmtx.metrics[name] = f_hmtx.metrics[name]
    # cmap 合并：primary 缺的字符从 fallback cmap 补
    p_cmap = primary.getBestCmap()
    f_cmap = fallback.getBestCmap()
    added_codes: dict[int, str] = {}
    for ch in needed_chars:
        code = ord(ch)
        if code in p_cmap:
            continue
        fname = f_cmap.get(code)
        if fname and fname in p_glyf:
            added_codes[code] = fname
    if added_codes:
        cmap_table = primary["cmap"]
        for subtable in cmap_table.tables:
            try:
                is_unicode = subtable.isUnicode()
            except TypeError:
                is_unicode = True
            if is_unicode:
                subtable.cmap.update(added_codes)
    # 字形序同步（A10 0.46.0 实证修复）：CFF 主字体经 _ensure_glyf 后，
    # font 全局 glyphOrder 只含转换字形；上面从 fallback 复制进 glyf 表的
    # 新字形只 append 进 glyf 表自身顺序——cmap/loca 编译按全局
    # glyphOrder 查键，缺了就 KeyError（fake-it sharedassets4 内嵌
    # OTTO 字体 'uni3002'/'uni5141' 实证）。所有新增字形并入全局序。
    _go = primary.getGlyphOrder()
    _table_order = list(getattr(p_glyf, "glyphOrder", None) or [])
    _new = [n for n in _table_order if n not in set(_go)]
    if _new:
        primary.setGlyphOrder(list(_go) + _new)
    # 重命名（防重复字体名冲突）——仅 ASCII 值（中文名表记录是
    # UTF-16，直接改 string 会破坏编码；跳过非 ASCII 防损坏）
    name_table = primary["name"]
    for record in name_table.names:
        if record.nameID not in (1, 4, 6):
            continue
        try:
            value = record.toUnicode()
        except Exception:  # noqa: BLE001
            continue
        if not value or not value.isascii():
            continue
        try:
            record.string = (value + f"-{rename_postfix}").encode(
                "utf-16-be" if record.isUnicode() else "latin-1")
        except Exception:  # noqa: BLE001 编码失败跳过
            continue
    out = __import__("io").BytesIO()
    primary.save(out)
    return out.getvalue()
