from __future__ import annotations
import csv
import io
import re
from pathlib import Path
from hanhua.core.models import STATUS_SKIPPED, TextEntry
from hanhua.core.formats import read_text

TARGET_LANG_ALIASES = {
    "zh-CN": ("ChineseSimplified", "zh-CN", "zh_Hans", "简体中文",
              "Simplified Chinese", "cn", "zh", "chn"),
}

NON_LANG_HEADERS = {"key", "id", "type", "category", "comment", "notes"}

# 自动覆盖源列判据（snowday 实证 2026-09-05）：表 ID|Notes|ActionEvents|
# Name|ChoicesLink|Spanish 唯一语言列是 Spanish（内容是英语），游戏运行时
# 只读这一列；target_col=None 时默认走「追加 ChineseSimplified 列」游戏不读
# → 汉化白写。检测「单语言源列形态」自动置 overwrite_source。
# 安全边界（宁漏勿坏）：
# - 表内命中中文目标列（CHN/ChineseSimplified/…）→ 不触发（写中文列更安全）
# - 数据行 < 8 → 不触发（样本不足防误判）
# - 句子态列必须唯一且 == source_col：Notes/ChoicesLink 这类低填充注释列
#   即便句子态 100% 也不算（非空值数须 ≥ 数据行数一半）
_SENTENCE_LIKE = re.compile(r"[a-z]{2,}[\s,.'’\-][a-z]{2,}", re.IGNORECASE)
_IDENTIFIER_LIKE = re.compile(r"^[A-Za-z0-9_*\[\]:.\-]+$")


def _sentence_ratio(values: list[str]) -> float:
    if not values:
        return 0.0
    hits = sum(1 for v in values
               if _SENTENCE_LIKE.search(v) and not _IDENTIFIER_LIKE.match(v))
    return hits / len(values)

# 对话框消息脚本命令值（fromivan 实证 2026-09-01：TextAsset 是
# 'RECEIVED_MSG|Hey, kiddo!' 式逐行消息脚本，被 CSV 判定误当 2 列表——
# 命令列（RECEIVED_MSG/DELAY/TYPING/SENT_MSG/WAIT_FOR_PRESS）是引擎
# 解析指令，翻译写坏对话时序。真对话在 '|' 另一侧，但 CSV 分支按
# 行列对齐整行翻译会连命令一起译坏。命令形态（全大写或 TitleCase
# 单 token）命中即跳过；'|' 分隔的对话行保持原样由 line 拆分处理）
_MSG_SCRIPT_COMMAND = re.compile(r"^[A-Z]{2,}(?:_[A-Z]{2,})*$|^[A-Z][a-z]+(?:_[A-Za-z0-9]+)*$")


def pick_target_col(header: list[str], target_lang: str) -> int | None:
    # 大小写不敏感（Rendezvous 实证：I2 13 列词典表头 'ID,IND,ENG,...,CHN'，
    # CHN 未加别名导致 target_col=None → 写回走追加新列 → 游戏读 CHN 列
    # 汉化不生效；且已填行被重复翻译覆盖官方中文）
    aliases = {a.lower() for a in TARGET_LANG_ALIASES.get(target_lang, (target_lang,))}
    for i, name in enumerate(header):
        if name.strip().lower() in aliases:
            return i
    return None


def _detect_delimiter(text: str, suffix: str) -> str:
    if suffix == ".tsv":
        return "\t"
    if suffix == ".psv":
        return "|"
    head = text.splitlines()[0] if text.splitlines() else ""
    if "\t" in head:
        return "\t"
    counts = {",": head.count(","), "|": head.count("|"), ";": head.count(";")}
    best = max(counts, key=counts.get)
    # 分号分隔（key;english;russian;german 实证 incremental-rts）：只有
    # 分号时用分号；逗号存在时逗号优先（CSV 字段内的分号更常见）
    if best == ";" and counts[";"] > 0 and counts[","] == 0:
        return ";"
    if counts["|"] > counts[","]:
        return "|"
    return ","


def _read_rows(text: str, delimiter: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def extract_csv(path: str | Path, target_lang: str = "zh-CN", file_id: str | None = None
                ) -> tuple[list[TextEntry], int | None]:
    """I2 Localization 风格（Key 列 + 语言列）或两列 key,value。返回 (条目, 目标列索引或None)。"""
    p = Path(path)
    return extract_csv_text(read_text(p), file_id or p.name,
                            p.suffix.lower(), target_lang)


def looks_like_csv_text(text: str) -> bool:
    """TextAsset 内嵌 CSV 判据：≥2 行且各行列数一致（首行含分隔符）。

    空行（width=0）不计入宽度判定（incremental-rts 实证：695 行 4 列 +
    31 空行，空行会破坏 len(widths)==1）。
    """
    if "\n" not in text and "\r" not in text:
        return False
    delimiter = _detect_delimiter(text, "")
    rows = [r for r in _read_rows(text, delimiter) if r]
    if len(rows) < 2:
        return False
    widths = {len(row) for row in rows}
    return len(widths) == 1 and len(rows[0]) >= 2


def extract_csv_text(text: str, file_id: str | None = None, suffix: str = "",
                     target_lang: str = "zh-CN",
                     overwrite_source: bool = False) -> tuple[list[TextEntry], int | None]:
    """文本直取（TextAsset / zip 内层 / 伪装文件复用）。

    overwrite_source=True：覆盖源列模式（Rendezvous 实证——游戏语言设置
    只有英文，目标语言列（CHN）官方内容玩家永远读不到；汉化=翻译源列
    （ENG）写回源列）。此时目标语言列仅作官方译文参考（meta official_zh
    = 官方中文，runner 搬运优先于模型译文）。
    """
    fid = file_id or "csv"
    delimiter = _detect_delimiter(text, suffix)
    rows = _read_rows(text, delimiter)
    if not rows:
        return [], None
    header = [h.strip() for h in rows[0]]
    target_col = pick_target_col(header, target_lang)
    lang_cols = [i for i, h in enumerate(header) if h and h.lower() not in NON_LANG_HEADERS]
    # 源语言列选择：语言列中「非空行最多」者（faerie-afterlight 实证：
    # header=key,voice,en,id,sp,... 时 voice 列几乎全空，lang_cols[0] 选错
    # → 0 条目。en 列非空最多才是真正的源文本列）
    if lang_cols:
        source_col = max(
            lang_cols,
            key=lambda c: sum(1 for r in range(1, len(rows))
                              if len(rows[r]) > c and rows[r][c].strip()))
    else:
        source_col = 1
    # 自动覆盖源列（B16b）：无中文目标列 + 单一句子态源列 + 样本充足 →
    # 游戏运行时只读源列本身，追加新列玩家永远看不到（snowday 实证
    # 2026-09-05：Spanish 列内容是英语，追加 ChineseSimplified 游戏不读）
    # 句子态列唯一时它本身就是源列：source_col 若因「非空行数并列取
    # 首个」落在人名列（Name 列全标识符态与 Spanish 并列实证），以句
    # 子态列为准改选（人名/短标签不是对话文本，译文落对话列才有效）。
    if not overwrite_source and target_col is None:
        data_rows = [r for r in rows[1:] if any(c.strip() for c in r)]
        if len(data_rows) >= 8:
            filled = [
                (c, [row[c].strip() for row in data_rows
                     if len(row) > c and row[c].strip()])
                for c in lang_cols
            ]
            filled = [(c, vals) for c, vals in filled
                      if len(vals) >= len(data_rows) / 2]
            sentence_cols = [c for c, vals in filled
                             if _sentence_ratio(vals) >= 0.5]
            if len(sentence_cols) == 1:
                source_col = sentence_cols[0]
                overwrite_source = True
    # 覆盖源列模式：写回列=源列（ENG）；目标语言列（CHN）作官方译文参考
    write_col = source_col if overwrite_source else target_col
    official_col = target_col if overwrite_source else None
    entries: list[TextEntry] = []
    for r in range(1, len(rows)):
        row = rows[r]
        if len(row) <= source_col or not row[source_col].strip():
            continue
        key = row[0].strip() if row and row[0].strip() else f"row{r}"
        # delimiter 留档（B16c）：写回端 apply_format_text 的 delimiter
        # 只看 file 级 meta，TextAsset 内嵌表缺省按 ',' 重建——'|' 表会被
        # 整行当单字段，译文追加成 ',,,,' 尾巴破坏行结构（snowday 实证）
        meta = {"row": r, "key": key, "source_col": source_col,
                "target_col": write_col, "delimiter": delimiter}
        if overwrite_source:
            meta["overwrite_source"] = True
            # 官方中文参考（目标语言列已填）——搬运优先，模型不译
            if official_col is not None and len(row) > official_col \
                    and row[official_col].strip():
                meta["official_zh"] = row[official_col].strip()
        elif target_col is not None and len(row) > target_col \
                and row[target_col].strip():
            # 目标语言列已有内容 → 官方已汉化（Rendezvous 实证：I2 词典
            # CHN 列 428 行已填官方中文）——跳过不译不覆盖（宁漏勿坏；
            # skipped 留档审计可见，写回侧同保护）
            entries.append(TextEntry(
                file_id=fid, key_path=f"row/{r}",
                original=row[source_col].strip(),
                status=STATUS_SKIPPED,
                meta={**meta, "reason": "target_col_already_filled"}))
            continue
        entries.append(TextEntry(
            file_id=fid, key_path=f"row/{r}", original=row[source_col].strip(),
            meta=meta))
    return entries, write_col


def apply_csv(entries: list[TextEntry], source_text: str, delimiter: str = ",",
              target_lang: str = "zh-CN", target_col: int | None = None) -> str:
    """重建 CSV：无目标列时在表头追加目标语言列。"""
    rows = _read_rows(source_text, delimiter)
    new_col = target_col is None
    if new_col:
        target_col = len(rows[0])
        alias = TARGET_LANG_ALIASES.get(target_lang, (target_lang,))[0]
        rows[0].append(alias)
    by_row = {e.meta["row"]: e for e in entries}
    for r in range(1, len(rows)):
        e = by_row.get(r)
        if not e or not e.translation:
            if new_col:
                rows[r].append("")
            continue
        if new_col:
            rows[r].append(e.translation)
        else:
            while len(rows[r]) <= target_col:
                rows[r].append("")
            rows[r][target_col] = e.translation
    out = io.StringIO()
    # 保留原始行终止符（与 detect_eol 同判据）：CRLF 文件写回 CRLF，
    # 避免行终止符变化被版本控制/脚本误判（调查报告 2.6 新发现）
    eol = "\r\n" if source_text.count("\r\n") > source_text.count("\n") / 2 else "\n"
    writer = csv.writer(out, delimiter=delimiter, lineterminator=eol)
    writer.writerows(rows)
    return out.getvalue()


def verify_csv_writeback(source_text: str, delimiter: str = ",",
                         target_col: int = 2) -> list[str]:
    """写回后完整性校验：源列不应残留「未翻译的纯 ASCII 英文」。

    Rendezvous 2026-08-18 实证：CutsceneLocalization(TextAsset#30) 写回
    漏了 158 行（ENG 列仍是英文）——过场对话全部英文。写回后调用本
    函数逐行检查源列，返回残留的（行号, 内容）描述列表；空 = 完整。

    注意：技术词（V-Sync/SFX 等）会被判为「残留」——由调用方按
    allowlist 过滤，或人工确认后忽略。
    """
    import io as _io

    rows = _read_rows(source_text, delimiter)
    leftovers: list[str] = []
    for r, row in enumerate(rows[1:], start=1):
        if len(row) <= target_col:
            continue
        cell = row[target_col].strip()
        if not cell:
            continue
        if all(c.isascii() and (c.isprintable() or c in " ") for c in cell):
            leftovers.append(f"row {r}: {cell[:60]}")
    return leftovers


def apply_csv_by_id(entries: list, source_text: str, delimiter: str = ",",
                    target_col: int = 2, id_col: int = 0) -> str:
    """按 ID 列写回（防行号错位——Rendezvous 引号逗号行合并实证）。

    与 apply_csv 的差异：条目按「ID 列值」匹配行（不依赖行号），
    即使 CSV 解析的行号因引号内逗号/换行发生偏移也能正确写回。
    条目需在 meta 中携带 id 值（提取侧注入），否则退化为按行号。
    """
    import io as _io

    rows = _read_rows(source_text, delimiter)
    by_id: dict[str, object] = {}
    for e in entries:
        rid = (e.meta or {}).get("id")
        if rid is not None:
            by_id[str(rid).strip()] = e
    written = 0
    for r, row in enumerate(rows[1:], start=1):
        if len(row) <= id_col:
            continue
        e = by_id.get(str(row[id_col]).strip())
        if not e or not e.translation:
            continue
        while len(row) <= target_col:
            row.append("")
        row[target_col] = e.translation
        written += 1
    out = _io.StringIO()
    eol = "\r\n" if source_text.count("\r\n") > source_text.count("\n") / 2 else "\n"
    import csv as _csv
    _csv.writer(out, delimiter=delimiter, lineterminator=eol).writerows(rows)
    return out.getvalue()
