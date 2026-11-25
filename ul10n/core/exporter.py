"""Multi-format localization text exporter (JSON, TXT, Bilingual TXT, Markdown, CSV)."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from ul10n.core.models import Entry, GameProject, STATUS_EDITED, STATUS_TRANSLATED
from ul10n.core.xuat_codec import encode_xuat_entry

_STATUS_ZH = {
    "pending": "待翻译",
    "translated": "已翻译",
    "edited": "已精修",
    "flagged": "需复核",
    "failed": "失败",
    "skipped": "已忽略",
}


def filter_export_entries(entries: list[Entry], scope: str = "done") -> list[Entry]:
    """Filter entries by requested export scope: done | all | live | edited."""
    sc = (scope or "done").lower()
    if sc == "all":
        return list(entries)
    if sc == "live":
        return [e for e in entries if e.source == "xuat_live"]
    if sc == "edited":
        return [e for e in entries if e.status == STATUS_EDITED]
    # Default "done": all translated & manually edited entries with non-empty translation
    return [e for e in entries if e.status in (STATUS_TRANSLATED, STATUS_EDITED) and (e.translation or "").strip()]


def export_entries_as_format(
    project: GameProject,
    entries: list[Entry],
    fmt: str = "json",
    *,
    scope: str = "done",
) -> tuple[bytes, str, str]:
    """Render `(content_bytes, media_type, filename_ext)` for the requested format.

    Supported `fmt`:
    - `json`: structured UTF-8 JSON with metadata + original/translation pairs
    - `txt`: XUAT-compatible `原文=译文` plain text (UTF-8 no BOM)
    - `bilingual`: human-readable paired `原文 / 译文` plain text
    - `md`: clean Markdown table document
    - `csv`: Excel-friendly UTF-8-SIG CSV (`原文,译文,状态,来源`)
    """
    selected = filter_export_entries(entries, scope)
    f = (fmt or "json").lower()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if f == "json":
        payload = {
            "game": project.name,
            "unity_version": project.unity_version,
            "exported_at": now_str,
            "scope": scope,
            "count": len(selected),
            "entries": [
                {
                    "original": e.original,
                    "translation": e.translation,
                    "status": e.status,
                    "source": e.source,
                    "locked": e.locked,
                }
                for e in selected
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return raw, "application/json; charset=utf-8", "json"

    if f == "txt":
        lines = [f"// {project.name} 汉化词典导出 ({now_str})，共 {len(selected)} 条"]
        for e in selected:
            encoded = encode_xuat_entry(e.original, e.translation) if e.translation else None
            if encoded:
                lines.append(encoded)
            elif e.original:
                orig = e.original.replace("\n", "\\n")
                tr = (e.translation or "").replace("\n", "\\n")
                lines.append(f"{orig}={tr}")
        return ("\n".join(lines) + "\n").encode("utf-8"), "text/plain; charset=utf-8", "txt"

    if f == "bilingual":
        blocks = [f"# {project.name} 原文与译文对照（导出时间：{now_str}，共 {len(selected)} 条）\n"]
        for idx, e in enumerate(selected, start=1):
            blocks.append(f"[{idx}] 原文：\n{e.original}\n译文：\n{e.translation or '（未翻译）'}\n")
        return ("\n---\n\n".join(blocks) + "\n").encode("utf-8"), "text/plain; charset=utf-8", "txt"

    if f == "md":
        def _esc_md(s: str) -> str:
            return (s or "").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")

        lines = [
            f"# {project.name} 汉化文本对照表",
            "",
            f"- 导出时间：{now_str}",
            f"- 词条数量：{len(selected)} 条",
            "",
            "| # | 原文 | 译文 | 状态 |",
            "|---:|---|---|---|",
        ]
        for idx, e in enumerate(selected, start=1):
            st = _STATUS_ZH.get(e.status, e.status)
            lines.append(f"| {idx} | {_esc_md(e.original)} | {_esc_md(e.translation)} | {st} |")
        return ("\n".join(lines) + "\n").encode("utf-8"), "text/markdown; charset=utf-8", "md"

    if f == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["原文", "译文", "状态", "来源", "已锁定"])
        for e in selected:
            writer.writerow([
                e.original,
                e.translation,
                _STATUS_ZH.get(e.status, e.status),
                "实时翻译" if e.source == "xuat_live" else "静态/词典",
                "是" if e.locked else "否",
            ])
        return buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "csv"

    raise ValueError(f"不支持的导出格式：{fmt}（支持 json / txt / bilingual / md / csv）")
