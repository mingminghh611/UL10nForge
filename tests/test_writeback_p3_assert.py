# -*- coding: utf-8 -*-
"""P3（0.42.1 审计）：译文存在性独立断言回归测试。

writeback_audit 新增独立断言：store 条目 is_write_ready 且译文≠原文时，
译文（或截断版本）必须出现在写回产物里——csv 查目标列单元格、
json/txt/kv 查子串。找不到 → FileAudit.translation_missing 非空 →
needs_rewrite。不依赖渲染一致性链路，专抓「译了但没落盘」的静默丢失
（Rendezvous 2026-08-18 漏 158 行实证形态：行号 meta 过期，apply 层
静默跳过，渲染层零异常、旧审计通过、游戏里全是英文）。

注意：源文件一律 write_bytes 写 LF——Windows 下 write_text 默认把
\n 翻译成 \r\n，源 CRLF vs 写回 LF 会假报 eol_conserved False（测试
工件，非产品 bug）。
"""
import sys

sys.path.insert(0, "")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


def _p3_project(tmp_path: Path, rel_path: str, content: str, fmt: str,
                meta: dict | None = None):
    """建最小项目库：game/<rel_path> 源文件（LF 字节）+ 单文件 store。"""
    import tempfile
    from hanhua.core.memory import ProjectStore

    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / rel_path).write_bytes(content.encode("utf-8"))
    store = ProjectStore(Path(tempfile.mkdtemp()) / "p.db")
    store.init_schema()
    store.add_file(rel_path, rel_path, fmt, "utf-8", "\n", meta or {})
    return game, store


def _entries_by_file(store) -> dict:
    grouping: dict[str, list[dict]] = {}
    for e in store.get_entries():
        grouping.setdefault(e["file_id"], []).append(e)
    return grouping


# ── 检出：csv 行号失效静默未落盘 ─────────────────────────────────

def test_p3_translation_missing_csv_stale_row(tmp_path):
    """P3 核心：csv 行号 meta 失效 → 条目静默未落盘，独立断言必须抓到。"""
    import json as _json
    from hanhua.core.writer import write_back
    from hanhua.core.writeback_audit import audit_deterministic

    game, store = _p3_project(
        tmp_path, "table.csv", "key,en\nhello,Hello\nworld,World\n",
        "csv", {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "table.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
        {"file_id": "table.csv", "key_path": "row/2", "original": "World",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 9, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("table.csv", "row/1", "你好")
    store.update_translation("table.csv", "row/2", "世界")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert len(audits) == 1
    a = audits[0]
    assert not a.passed                              # 审计失败
    assert len(a.translation_missing) == 1           # 只有 row/2 未落盘
    assert a.translation_missing[0].startswith(
        "translation_missing_in_output: row/2")
    assert "World → 世界" in a.translation_missing[0]
    # 好条目 row/1（你好已落盘）不得误报
    assert not any("row/1" in m for m in a.translation_missing)


def test_p3_translation_present_passes(tmp_path):
    """P3 happy path：译文落盘 → translation_missing 空、审计通过。"""
    import json as _json
    from hanhua.core.writer import write_back
    from hanhua.core.writeback_audit import audit_deterministic

    game, store = _p3_project(
        tmp_path, "table.csv", "key,en\nhello,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "table.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("table.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert audits[0].translation_missing == []
    assert audits[0].passed


# ── 豁免：不算漏译的合法形态 ─────────────────────────────────────

def test_p3_translation_equal_original_exempt(tmp_path):
    """译文==原文（术语回退/保留词，合法回退）不触发断言。"""
    import json as _json
    from hanhua.core.writer import write_back
    from hanhua.core.writeback_audit import audit_deterministic

    game, store = _p3_project(
        tmp_path, "table.csv", "key,en\nhello,KoiKoi\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "table.csv", "key_path": "row/1", "original": "KoiKoi",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 9, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("table.csv", "row/1", "KoiKoi")  # 回退=原文
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert audits[0].translation_missing == []       # 不触发（合法回退）


def test_p3_key_style_entry_exempt(tmp_path):
    """key_style 条目（键身份保护，渲染层刻意置空）不触发断言。"""
    import json as _json
    from hanhua.core.writer import write_back
    from hanhua.core.writeback_audit import audit_deterministic

    game, store = _p3_project(
        tmp_path, "strings.txt", "key=ui_newGame\n", "txt")
    store.upsert_entries([
        {"file_id": "strings.txt", "key_path": "kv/key/0",
         "original": "ui_newGame", "status": "pending",
         "meta": _json.dumps({"line_no": 0, "raw": "key=ui_newGame",
                              "kind": "kv_structural", "key": "key",
                              "delim": "="})},
    ])
    store.update_translation("strings.txt", "kv/key/0", "新游戏")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    out = (out_dir / "strings.txt").read_text(encoding="utf-8")
    assert "ui_newGame" in out                      # 键身份保留原文
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert audits[0].translation_missing == []       # 刻意置空不算漏


# ── json / txt 子串分派 ──────────────────────────────────────────

def test_p3_txt_translation_missing_caught(tmp_path):
    """txt/kv 分派：有译文但产物里找不到（子串断言）→ 抓到。"""
    import json as _json
    from hanhua.core.writeback_audit import audit_deterministic

    # 不跑 write_back——out 就是源文件拷贝（写回被外部跳过的形态）
    game, store = _p3_project(
        tmp_path, "strings.txt", "a=hello\n", "txt")
    store.upsert_entries([
        {"file_id": "strings.txt", "key_path": "kv/a/0", "original": "hello",
         "status": "pending",
         "meta": _json.dumps({"line_no": 0, "raw": "a=hello", "kind": "kv",
                              "key": "a", "delim": "="})},
    ])
    store.update_translation("strings.txt", "kv/a/0", "你好")
    out_dir = tmp_path / "out"
    (out_dir / "strings.txt").parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "strings.txt").write_bytes(b"a=hello\n")
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert not audits[0].passed
    assert any(m.startswith("translation_missing_in_output: kv/a/0")
               for m in audits[0].translation_missing)


def test_p3_json_translation_missing_caught(tmp_path):
    """json 分派：write-ready 译文不在 JSON 产物里 → 抓到。"""
    import json as _json
    from hanhua.core.writeback_audit import audit_deterministic

    game, store = _p3_project(
        tmp_path, "data.json", '{"greeting": "Hello"}', "json")
    store.upsert_entries([
        {"file_id": "data.json", "key_path": "greeting", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "json"})},
    ])
    store.update_translation("data.json", "greeting", "你好")
    out_dir = tmp_path / "out"
    (out_dir / "data.json").parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "data.json").write_bytes(b'{"greeting": "Hello"}')
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert any(m.startswith("translation_missing_in_output: greeting")
               for m in audits[0].translation_missing)


def test_p3_json_translation_present_passes(tmp_path):
    """json happy path：json.dumps 译文子串在产物里 → 不误报。"""
    import json as _json
    from hanhua.core.writer import write_back
    from hanhua.core.writeback_audit import audit_deterministic

    game, store = _p3_project(
        tmp_path, "data.json", '{"greeting": "Hello"}', "json")
    store.upsert_entries([
        {"file_id": "data.json", "key_path": "greeting", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "json"})},
    ])
    store.update_translation("data.json", "greeting", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert audits[0].translation_missing == []
    assert audits[0].passed


# ── 整链路：audit_writeback needs_rewrite ────────────────────────

def test_p3_audit_writeback_needs_rewrite(tmp_path):
    """漏译文 → audit_writeback 整链路 needs_rewrite=True（run_model=False）。"""
    import json as _json
    from hanhua.core.writer import write_back
    from hanhua.core.writeback_audit import audit_writeback

    game, store = _p3_project(
        tmp_path, "table.csv", "key,en\nhello,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "table.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 9, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("table.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)   # 行号 9 越界 → 静默未落盘
    result = audit_writeback(store, game, out_dir, run_model=False)
    assert result.needs_rewrite is True
    assert any(not a.passed for a in result.files)


def test_p3_audit_writeback_clean_needs_no_rewrite(tmp_path):
    """干净写回 → needs_rewrite=False（断言不产生误报）。"""
    import json as _json
    from hanhua.core.writer import write_back
    from hanhua.core.writeback_audit import audit_writeback

    game, store = _p3_project(
        tmp_path, "table.csv", "key,en\nhello,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "table.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("table.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    result = audit_writeback(store, game, out_dir, run_model=False)
    assert result.needs_rewrite is False
    assert all(a.passed for a in result.files)


# ── csv 截断容忍：前缀匹配 ───────────────────────────────────────

def test_p3_csv_truncated_translation_prefix_matches(tmp_path):
    """csv 目标列单元格是译文截断前缀（字节预算截断形态）→ 不误报。

    截断容忍判据：cell 含译文前缀（len / len-1 / len//2 / len//4）。
    """
    from hanhua.core.writeback_audit import _translation_missing_entries

    f = {"id": "t", "format": "csv", "rel_path": "t.csv"}
    translation = "这是一段比较长的中文译文用于截断前缀匹配测试"
    # 手工构造 write-ready meta（store.update_translation 同款字段：
    # quality_passed=True + confidence 非 low → 过机械质量门）
    entry_meta = {"kind": "csv", "row": 1, "target_col": 1,
                  "delimiter": ",", "quality_passed": True,
                  "confidence": "medium"}
    entries = [{"key_path": "row/1", "original": "Hello",
                "translation": translation,
                "status": "translated",
                "meta": entry_meta}]
    half = translation[:len(translation) // 2]
    out_text = f"key,en\nhello,{half}\n"
    missing = _translation_missing_entries(f, entries, out_text)
    assert missing == []                          # 截断前缀命中 → 不算漏
    # 完全无关的英文残留 → 报漏
    out_bad = "key,en\nhello,Hello\n"
    missing2 = _translation_missing_entries(f, entries, out_bad)
    assert len(missing2) == 1
    assert missing2[0].startswith("translation_missing_in_output: row/1")


# ── P2：CSV 目标列英文残留双通道（csv_target_residue）─────────────

def _residue_file(fmt_meta: dict | None = None):
    import json as _json
    return {"id": "t.csv", "format": "csv", "rel_path": "t.csv",
            "meta": _json.dumps(fmt_meta if fmt_meta is not None
                                else {"delimiter": ",", "target_col": 1})}


def _residue_entry(kp: str, orig: str, trans: str, row: int,
                   target_col: int | None = 1):
    import json as _json
    meta = {"kind": "csv", "row": row, "delimiter": ",",
            "quality_passed": True, "confidence": "medium"}
    if target_col is not None:
        meta["target_col"] = target_col
    return {"file_id": "t.csv", "key_path": kp, "original": orig,
            "status": "translated", "translation": trans,
            "meta": _json.dumps(meta, ensure_ascii=False)}


def test_p2_csv_residue_duplicate_row_caught():
    """P2 核心：重复英文行一条落盘一条静默丢失 → csv_target_residue 抓到。

    translation_missing 子串断言被别处同译文掩护（你好已在场）→ 残留
    通道是唯一防线（Rendezvous 漏 158 行的重复行形态）。
    """
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file()
    entries = [_residue_entry("row/1", "Hello", "你好", 1),
               _residue_entry("row/2", "Hello", "你好", 9)]  # 行号越界
    out_text = "key,en\na,你好\nb,Hello\n"
    residue = _csv_target_residue(f, entries, out_text, set())
    assert len(residue) == 1
    assert residue[0].startswith("csv_target_residue: row 2: Hello")
    assert "row/2" in residue[0]


def test_p2_csv_residue_duplicate_row_end_to_end(tmp_path):
    """P2 端到端：audit_deterministic 双通道并拦 → needs_rewrite。"""
    import json as _json
    from hanhua.core.writeback_audit import audit_deterministic

    game, store = _p3_project(
        tmp_path, "t.csv", "key,en\na,Hello\nb,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
        {"file_id": "t.csv", "key_path": "row/2", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 2, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "你好")
    store.update_translation("t.csv", "row/2", "你好")
    out_dir = tmp_path / "out"          # row/2 静默未落盘形态
    out_dir.mkdir()
    (out_dir / "t.csv").write_bytes("key,en\na,你好\nb,Hello\n".encode("utf-8"))
    audits = audit_deterministic(store, game, out_dir,
                                 _entries_by_file(store))
    assert not audits[0].passed
    assert any("csv_target_residue" in m and "row/2" in m
               for m in audits[0].translation_missing)


def test_p2_csv_residue_tech_word_no_false_positive():
    """残留行无对应条目（技术词 V-Sync）→ 不误报。"""
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file()
    entries = [_residue_entry("row/1", "Hello", "你好", 1)]
    out_text = "key,en\na,你好\nb,V-Sync enabled\n"
    assert _csv_target_residue(f, entries, out_text, set()) == []


def test_p2_csv_residue_equal_translation_no_false_positive():
    """译文==原文（KoiKoi 术语回退）→ 合法保留原文，不误报。"""
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file()
    entries = [_residue_entry("row/1", "KoiKoi", "KoiKoi", 1)]
    out_text = "key,en\na,KoiKoi\n"
    assert _csv_target_residue(f, entries, out_text, set()) == []


def test_p2_csv_residue_ascii_translation_landed_no_false_positive():
    """纯 ASCII 译文（OK）已落盘 → 单元格==译文 → 不误报。"""
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file()
    entries = [_residue_entry("row/1", "OK", "OK!", 1)]
    out_text = "key,en\na,OK!\n"
    assert _csv_target_residue(f, entries, out_text, set()) == []


def test_p2_csv_residue_append_column_skipped():
    """追加列模式（无 target_col）→ 列位未知不猜，不扫描。"""
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file({"delimiter": ","})   # 无 target_col
    entries = [_residue_entry("row/1", "Hello", "你好", 1,
                              target_col=None)]
    out_text = "key,en\na,你好\nb,Hello\n"
    assert _csv_target_residue(f, entries, out_text, set()) == []


def test_p2_csv_residue_ambiguous_in_range_no_guess():
    """多候选行号均有效且译文已各自落盘 → 残留行另有其主，不猜（宁漏勿坏）。"""
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file()
    entries = [_residue_entry("row/1", "Hello", "你好", 1),
               _residue_entry("row/3", "Hello", "你好呀", 3)]
    # 4 行表：残留行 2 无映射，两候选行号 1/3 均有效且译文已落自身行
    out_text = "key,en\na,你好\nb,Hello\nc,你好呀\n"
    assert _csv_target_residue(f, entries, out_text, set()) == []


def test_p2_csv_residue_single_candidate_already_landed_no_flag():
    """单候选译文已落自身行（row/1=你好）→ 残留行 2 是无条目重复行，不误报。"""
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file()
    entries = [_residue_entry("row/1", "Hello", "你好", 1)]
    out_text = "key,en\na,你好\nb,Hello\n"
    assert _csv_target_residue(f, entries, out_text, set()) == []


def test_p2_csv_residue_dedup_with_translation_missing():
    """已在 translation_missing 报过的条目（key_path 集合传入）→ 不双报。"""
    from hanhua.core.writeback_audit import _csv_target_residue

    f = _residue_file()
    entries = [_residue_entry("row/1", "Hello", "你好", 1),
               _residue_entry("row/2", "Hello", "你好", 9)]
    out_text = "key,en\na,你好\nb,Hello\n"
    assert _csv_target_residue(f, entries, out_text, {"row/2"}) == []
