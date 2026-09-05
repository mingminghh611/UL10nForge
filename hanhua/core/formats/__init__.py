from __future__ import annotations
from pathlib import Path

import chardet

#: 编码检测样本上限：chardet.detect 是 O(N) 统计分析，全量检测几十 MB
#: 的大文本（对话库/语料库）会占住 raw 副本数秒——2026-08-19 扫描性能
#: 修复，编码判断只需头部统计（BOM/前 64KB 足够稳定）。
_ENCODING_SAMPLE_BYTES = 65536


def read_text(path: str | Path) -> str:
    """按 chardet 检测的编码读取文本文件（含 BOM 处理）。

    chardet 只喂头部样本（性能说明见 _ENCODING_SAMPLE_BYTES）。"""
    p = Path(path)
    raw = p.read_bytes()
    sample = raw[:_ENCODING_SAMPLE_BYTES] if len(raw) > _ENCODING_SAMPLE_BYTES else raw
    det = chardet.detect(sample)
    encoding = (det.get("encoding") or "utf-8").lower()
    if raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif encoding in ("ascii",):
        encoding = "utf-8"
    try:
        return raw.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError):
        # 兜底：gbk → latin-1
        for fallback in ("gbk", "latin-1"):
            try:
                return raw.decode(fallback, errors="strict")
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")


def detect_eol(raw: bytes) -> str:
    """\r\n 计数超过 \n 一半时视为 CRLF。"""
    crlf = raw.count(b"\r\n")
    if raw.count(b"\n") > 0 and crlf > raw.count(b"\n") / 2:
        return "\r\n"
    return "\n"


def apply_format_text(fmt: str, entries, text: str, meta: dict) -> str:
    """按格式名把译文渲染回文本（writer 与 zip 内层共用）。

    meta 需含 csv 的 delimiter/target_col 等格式写回参数。
    """
    from hanhua.core.formats import (csv_format, json_format, txt_format,
                                     xml_format, yaml_format, subtitle_format,
                                     po_format, ink_yarn_format)
    if fmt == "kv":
        from hanhua.core.formats.kv_format import apply_kv
        return apply_kv(entries, text)
    if fmt == "json":
        return json_format.apply_json(entries, text)
    if fmt == "csv":
        suffix = meta.get("source_suffix")
        # delimiter 三级取值：file 级 meta → 条目级 meta（B16c 实证
        # 2026-09-05：TextAsset 内嵌 '|' 表 writer 只传 {"kind":"textasset"}，
        # 缺省按 ',' 重建 → 译文追加成 ',,,,' 尾巴破坏行结构）→ 按后缀缺省
        delimiter = meta.get("delimiter")
        if not delimiter:
            for e in entries:
                d = getattr(e, "meta", {}).get("delimiter")
                if d:
                    delimiter = d
                    break
        if not delimiter:
            delimiter = {".tsv": "\t", ".psv": "|", None: ",", "": ",",
                         }.get(suffix, ",")
        # 写回列优先取条目级 meta 的 target_col（Rendezvous 实证 2026-08
        # -17：writer 传参 meta 只有 {"kind": "textasset"}，target_col 缺失
        # → apply_csv 走 new_col 追加列——译文写进第 14 列（Chinese
        # Simplified），游戏读 CHN 第 13 列——UI 全部显示英文，汉化白写）
        tc = meta.get("target_col")
        if tc is None:
            for e in entries:
                if getattr(e, "meta", {}).get("target_col") is not None:
                    tc = e.meta["target_col"]
                    break
        return csv_format.apply_csv(entries, text, delimiter, "zh-CN", tc)
    if fmt == "xml":
        return xml_format.apply_xml(entries, text)
    if fmt == "yaml":
        body = yaml_format.apply_yaml(entries)
        # 行数守恒保护（Rendezvous 实证 2026-08-17）：yaml 按行号重建，
        # 任何一行条目缺失（提取过滤/行号错位）→ 重建丢行 → 游戏解析
        # 越界黑屏。行数不一致 → 拒绝重建返回原文（宁漏勿坏），由
        # 调用方（writer）逐条记 rejected 供审计。
        if len(body.splitlines()) != len(text.splitlines()):
            return text
        return body
    if fmt in ("srt", "vtt", "ass", "ssa", "lrc"):
        return subtitle_format.apply_subtitle(entries)
    if fmt == "po":
        return po_format.apply_po(entries, text)
    if fmt in ("ink", "yarn"):
        return ink_yarn_format.apply_ink_yarn(entries)
    return txt_format.apply_txt(entries)
