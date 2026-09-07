from __future__ import annotations
import json
from pathlib import Path

from hanhua.core.formats import apply_format_text, read_text, zip_format
from hanhua.core.memory import ProjectStore
from hanhua.core.models import TextEntry
from hanhua.core.paths import resolve_relative_under
from hanhua.core.placeholders import is_key_style_identifier, looks_like_key_field
from hanhua.core.quality import is_write_ready


def write_back(store: ProjectStore, game_dir: Path, out_dir: Path,
               target_lang: str = "zh-CN",
               normalize_fallback_punctuation: bool = False,
               result=None) -> int:
    """把项目库中的译文写回 out_dir（保留相对路径/编码/EOL）。返回写回文件数。

    normalize_fallback_punctuation：中文字体启用时，未翻译条目回退的
    原文做字体标点归一化（– → —），与新 bundle 渲染字节一致（需求集
    同款变换，防 □）。字体未启用时保持 False——原文原样写回。

    result（0.42.1 P3）：可选 WriteResult（unity.writer）出参——文本
    路径逐条记账。旧文本路径只返回文件数，条目是否真正落盘完全无账
    （P1a 修复的是 TextAsset 结构化分支；纯文本 apply 层的静默跳过
    ——key_style/键字段强制置空、apply_json/csv 行号失效——同样存在
    「记 translated 但游戏里是英文」假账）。传入后：is_write_ready 且
    译文非空的条目记 note_written；被 _render 置空回退（非 write_ready/
    key_style/键字段）或 apply 层报 skipped 的记 note_rejected（带
    原因），供审计层与 needs_rewrite 判定使用。与二进制路径
    write_back_v2 的 WriteResult 同口径（locator 去重）。
    """
    files = store.get_files()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 2026-08-20 写回性能修复：旧实现循环内每文件 get_entries() 全库
    # SELECT * + Python 过滤——333 文件 × 21839 条目 = O(N×M) 聚集，
    # 大游戏写回时重复全表扫描几十次（与扫描六根因同模式）。
    # 一次全量取出按 file_id 分组，循环内 O(1) 查表。
    entries_by_file: dict[str, list[dict]] = {}
    for e in store.get_entries():
        entries_by_file.setdefault(e["file_id"], []).append(e)
    count = 0
    for f in files:
        if f["format"].startswith("v2_"):
            continue      # v2 二进制资源由 write_back_v2 处理
        src = resolve_relative_under(game_dir, f["rel_path"])
        if not src.exists():
            continue
        entries = entries_by_file.get(f["id"], [])
        # mkdir 后须重取路径：resolve_relative_under 校验 reparse 链，
        # 若 mkdir 被竞态替换成 junction，重查会抛 UnsafeRelativePathError
        out = resolve_relative_under(out_dir, f["rel_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out = resolve_relative_under(out_dir, f["rel_path"])
        if f["format"] == "sqlite":
            from hanhua.core.formats import sqlite_format
            out.write_bytes(sqlite_format.apply_sqlite(src, _entries_to_model(entries)))
        elif f["format"] == "zip":
            out.write_bytes(zip_format.apply_zip(src, _entries_to_model(entries)))
        else:
            skipped: set[str] | None = (
                set() if (result is not None and f["format"]
                          in ("kv", "json", "csv")) else None)
            body = _render(src, f, entries, target_lang,
                           normalize_fallback_punctuation, skipped=skipped)
            out.write_bytes(_encode(body, src, f))
            # P3 文本路径逐条记账：apply 层 skipped 的 key_path 是静默
            # 未落盘实证（P1a 同款），逐条记 rejected 暴露给审计；
            # 未进 skipped 且有有效译文的条目记 written。
            if result is not None:
                _account_text_entries(result, entries, f["format"], skipped)
        count += 1
    return count


def _account_text_entries(result, entries: list[dict], fmt: str,
                          skipped: set[str] | None) -> None:
    """P3 文本路径逐条记账：written/rejected 与二进制路径同口径。

    记账规则（宁漏勿坏，只记有翻译意图的条目）：
    - 无译文（非 write_ready / 译文为空）→ 不记——本就没有写回意图，
      混入会稀释 rejected 告警信号；
    - 有译文但被 _render 置空（key_style/json 键字段）或 apply 层报
      skipped（key_style 强制跳过、目标值不匹配、行号/行集失效）→
      note_rejected（reason 带来源，区分 renderer 置空 vs apply 层跳过）；
    - 其余有译文条目 → note_written。
    key_style/键字段判定与 _render 同源重复执行（_render 只改模型副本
    不回写 store 行），保证口径一致。fmt 取文件级 format（与 _render
    的 json 键字段判定同源）。
    """
    for e in entries:
        translation = e.get("translation") or ""
        if not translation:
            continue
        key_path = str(e.get("key_path", ""))
        original = str(e.get("original") or "")
        if is_key_style_identifier(original):
            result.note_rejected(e, "text_key_style_blank")
            continue
        if fmt == "json" and looks_like_key_field(key_path.rsplit("/", 1)[-1]):
            result.note_rejected(e, "text_json_key_field_blank")
            continue
        if skipped and key_path in skipped:
            result.note_rejected(e, "text_apply_silent_skip")
            continue
        result.note_written(e)


def _entries_to_model(entries: list[dict]) -> list[TextEntry]:
    return [_dict_to_entry(d) for d in entries]


def _encode(body: str, src: Path, f: dict) -> bytes:
    raw = src.read_bytes()
    enc = (f.get("encoding") or "utf-8").lower()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    data = body.replace("\r\n", "\n")
    if (f.get("eol") or "\n") == "\r\n":
        data = data.replace("\n", "\r\n")
    if has_bom or enc == "utf-8-sig":
        return data.encode("utf-8-sig")
    if enc.startswith("utf"):
        return data.encode("utf-8")
    try:
        return data.encode(enc)
    except UnicodeEncodeError:
        # 文档1 §5.1：无法编码的中文必须阻断写回，不能静默替换成
        # UTF-8——文件编码被改变后游戏按原编码读取会乱码，且重开验证
        # 按新编码读取仍会通过，掩盖问题
        raise RuntimeError(
            f"文件无法以 {enc} 编码写回（译文含编码表外字符），"
            f"已阻断发布避免静默改变文件编码：{src}") from None
    except LookupError:
        raise RuntimeError(
            f"文件编码名无法识别（{enc}），已阻断发布避免静默改变"
            f"文件编码：{src}") from None


def _render(src: Path, f: dict, entries: list[dict], target_lang: str,
            normalize_fallback_punctuation: bool = False,
            skipped: set[str] | None = None) -> str:
    fmt = f["format"]
    model_entries = [_dict_to_entry(d) for d in entries]
    # 写回保护：键名/键字段条目即使曾被（误）翻译也不写回。
    # 只丢弃译文（txt 按条目重建，条目必须保留才能保住原行）。
    from hanhua.core.models import STATUS_SKIPPED
    for e in model_entries:
        if not is_write_ready(e.status, e.translation, e.meta):
            if normalize_fallback_punctuation:
                # 回退原文 = 实际渲染字节：字体替换后 TMP 文本全由新
                # bundle 渲染，原文含 bundle 缺字标点（– → —）会渲染为
                # □——与 _font_required_glyph_set 需求集同款归一化，防缺字
                from hanhua.core.font.punct_normalize import (
                    normalize_font_punctuation)
                e.translation = normalize_font_punctuation(e.original)
            else:
                e.translation = ""
        if is_key_style_identifier(e.original) or (
                fmt == "json" and looks_like_key_field(e.key_path.rsplit("/", 1)[-1])):
            e.translation = ""
            e.status = STATUS_SKIPPED
    meta = json.loads(f.get("meta") or "{}")
    text = read_text(src)
    # P3：kv/json/csv 支持 skipped 出参（P1a），把 apply 层静默未落盘
    # 的 key_path 逐条暴露给 write_back 记账；其余行级格式条目 meta
    # 自带行参数，跳过场景由提取期结构判定覆盖（P1a 结论）。
    body = apply_format_text(fmt, model_entries, text, {
        **meta,
        "source_suffix": src.suffix.lower(),
        "target_col": meta.get("target_col"),
    }, skipped=skipped)
    # 行重建格式需还原原文件末尾换行
    if fmt in ("txt", "yaml", "ink_yarn", "subtitle", "po") and text.endswith(("\n", "\r")):
        body += "\n"
    return body


def _dict_to_entry(d: dict) -> TextEntry:
    return TextEntry(
        file_id=d["file_id"], key_path=d["key_path"], original=d["original"],
        translation=d.get("translation") or "", status=d.get("status", "pending"),
        locked=bool(d.get("locked")), meta=json.loads(d.get("meta") or "{}"))
