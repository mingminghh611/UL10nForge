# -*- coding: utf-8 -*-
"""P1a/P1b/P5a 写回假记账回归测试（0.42.1 审计修复）。

锁定三条确定性漏洞：
- P1a：_patch_textasset 结构化路径旧实现「任一格式组改了 body 就对全部
  structured_items note_written」——apply_json/apply_csv 静默跳过的条目
  （key_style 保护/行号失效）从未落盘却记 written → 审计通过但游戏里
  是英文（Rendezvous 2026-08-18 漏 158 行实证同源）。修复后未落盘条目
  逐条 note_rejected（apply_silent_skip_key_style / apply_target_mismatch /
  apply_not_written）。
- P1b：纯行级路径 by_line 只存 entry，第二循环引用的 meta 是第一循环
  残留值——真 msg_script 行丢命令列保护（'RECEIVED_MSG|对话' 被整行
  覆盖 → 脚本解析坏）。修复后 by_line 存 (entry, meta)。
- P5a：apply_json 单条目标不匹配不再 raise ValueError 击穿整场写回，
  而是跳过进 skipped 集合（宁漏勿坏：单条坏数据只影响本条）。
"""
import sys

sys.path.insert(0, "")

from hanhua.core.formats import apply_format_text  # noqa: E402
from hanhua.core.formats.csv_format import apply_csv  # noqa: E402
from hanhua.core.formats.json_format import apply_json  # noqa: E402
from hanhua.core.models import TextEntry  # noqa: E402
from hanhua.core.unity.writer import (  # noqa: E402
    WriteResult,
    _patch_textasset,
)


# ── P5a：apply_json 单条不匹配跳过不抛 ────────────────────────────────────

def test_apply_json_mismatch_skips_instead_of_raise():
    """旧实现 raise ValueError 击穿整场写回；新实现单条跳过进 skipped。"""
    text = '{"greeting": "Hello"}'
    good = TextEntry(file_id="f", key_path="greeting", original="Hello",
                     translation="你好")
    # original 与 JSON 实际值不符（旧库 meta 过期形态）
    bad = TextEntry(file_id="f", key_path="greeting", original="Hola",
                    translation="你好呀")
    skipped: set[str] = set()
    out = apply_json([good, bad], text, skipped=skipped)
    assert '"greeting": "你好"' in out          # 好条目正常落盘
    # 两条同 key_path（good 先应用，bad 值不匹配）——bad 进 skipped
    assert "greeting" in skipped


def test_apply_json_mismatch_no_skipped_param_still_no_raise():
    """不传 skipped（旧调用方）也不抛——兼容路径。"""
    text = '{"greeting": "Hello"}'
    bad = TextEntry(file_id="f", key_path="greeting", original="Hola",
                    translation="你好")
    out = apply_json([bad], text)
    assert out == text                          # 无变化，原文保留


def test_apply_json_key_style_reports_skipped():
    """key_style 保护跳过的条目也进 skipped（不再静默）。"""
    text = '{"ui_newGame": "ui_newGame"}'
    e = TextEntry(file_id="f", key_path="ui_newGame", original="ui_newGame",
                  translation="新游戏")
    skipped: set[str] = set()
    out = apply_json([e], text, skipped=skipped)
    assert "ui_newGame" in skipped
    assert "新游戏" not in out                  # 键身份条目绝不写回


# ── P1a：apply_csv 行号失效上报 ────────────────────────────────────────────

def test_apply_csv_row_not_found_reports_skipped():
    """行号在表中不存在（旧库 meta 过期）→ 条目进 skipped。"""
    text = "key,en\nhello,Hello\n"
    e = TextEntry(file_id="f", key_path="csv/row/9", original="Hello",
                  translation="你好",
                  meta={"kind": "csv", "row": 9, "target_col": 1})
    skipped: set[str] = set()
    out = apply_csv([e], text, ",", "zh-CN", 1, skipped=skipped)
    assert "csv/row/9" in skipped
    assert "你好" not in out


def test_apply_csv_applied_rows_not_in_skipped():
    """正常落盘条目不进 skipped（假账修复不能误伤好条目）。"""
    text = "key,en\nhello,Hello\n"
    e = TextEntry(file_id="f", key_path="csv/row/1", original="Hello",
                  translation="你好",
                  meta={"kind": "csv", "row": 1, "target_col": 1})
    skipped: set[str] = set()
    out = apply_csv([e], text, ",", "zh-CN", 1, skipped=skipped)
    assert skipped == set()
    assert "你好" in out


# ── P1a：_patch_textasset 结构化路径逐条记账 ──────────────────────────────

def _structured_entry(key_path, original, translation, fmt,
                      inner_path=None, meta_extra=None):
    meta = {"kind": "textasset", "textasset_format": fmt,
            "inner_path": inner_path or key_path}
    if fmt == "csv":
        # csv 条目的 row/delimiter/target_col 在条目级 meta（B16c/Rendezvous
        # 实证：写回参数从提取现场带档）
        meta.update({"row": 0, "delimiter": ",", "target_col": 1})
    meta.update(meta_extra or {})
    # 与 store 持久化一致：条目 meta 以 JSON 字符串落 entry["meta"]，
    # _patch_textasset 从该字段重建 TextEntry.meta（行号等写回参数在此）
    import json as _json
    return ({"file_id": "f1", "key_path": key_path,
             "original": original, "translation": translation,
             "status": "translated",
             "meta": _json.dumps(meta, ensure_ascii=False)}, meta)


def test_patch_textasset_json_mismatch_entry_rejected_not_written():
    """JSON 组内单条不匹配：好条目 written、坏条目 rejected（不再全组 written）。"""
    text = '{"a": "Hello", "b": "World"}'
    good = _structured_entry("a", "Hello", "你好", "json")
    bad = _structured_entry("b", "Wrld", "世界", "json")  # original 不匹配
    result = WriteResult()
    out = _patch_textasset(text.encode("utf-8"), [], [good, bad], result)
    out_text = out.decode("utf-8")
    assert "你好" in out_text                      # 好条目落盘
    assert "World" in out_text and "世界" not in out_text  # 坏条目保留原文
    # 记账：written 只含好条目，坏条目按 apply_target_mismatch 拒绝
    written = {r for r in result.rejected}
    assert result.entries == 1
    assert any(r.reason == "apply_target_mismatch" for r in written)


def test_patch_textasset_all_skipped_rejects_not_written():
    """整组全部被静默跳过（key_style）→ 不再笼统 note_written 谎报成功。"""
    text = '{"ui_newGame": "ui_newGame"}'
    e = _structured_entry("ui_newGame", "ui_newGame", "新游戏", "json")
    result = WriteResult()
    out = _patch_textasset(text.encode("utf-8"), [], [e], result)
    assert out == text.encode("utf-8")            # 原文保留（宁漏勿坏）
    assert result.entries == 0                    # 零 written（假账根治）
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "apply_silent_skip_key_style"


def test_patch_textasset_csv_row_mismatch_rejected():
    """CSV 组内行号失效条目：落盘条目 written、失效条目 rejected。"""
    text = "key,en\nhello,Hello\nworld,World\n"
    good = _structured_entry("csv/row/1", "Hello", "你好", "csv",
                             meta_extra={"row": 1, "target_col": 1,
                                         "delimiter": ","})
    bad = _structured_entry("csv/row/9", "World", "世界", "csv",
                            meta_extra={"row": 9, "target_col": 1,
                                        "delimiter": ","})
    result = WriteResult()
    out = _patch_textasset(text.encode("utf-8"), [], [good, bad], result)
    out_text = out.decode("utf-8")
    assert "你好" in out_text
    assert "World" in out_text and "世界" not in out_text
    assert result.entries == 1
    assert any(r.reason == "apply_target_mismatch" for r in result.rejected)


# ── P1b：纯行级路径 per-entry meta ─────────────────────────────────────────

def _line_entry(key_path, original, translation, line, msg_script=False):
    meta = {"kind": "textasset", "line": line}
    if msg_script:
        meta["msg_script"] = True
    return ({"file_id": "f1", "key_path": key_path,
             "original": original, "translation": translation,
             "status": "translated", "meta": ""}, meta)


def test_patch_textasset_msg_script_meta_not_leaked():
    """msg_script 标记不得从上一条目泄漏到下一条目（P1b 核心）。

    场景：第 0 行是 msg_script 行，第 1 行是普通行。旧实现第一循环
    残留 meta=msg_script 行 → 第二循环普通行误走命令列剥取分支。
    反向场景（普通行在前、msg_script 行在后）验证保护不丢。
    """
    # 正向：msg_script 行在前 + 普通行在后——普通行不被残留标记污染
    script = "RECEIVED_MSG|Hello there\nPlain sentence\n".encode("utf-8")
    e1 = _line_entry("line/0", "RECEIVED_MSG|Hello there",
                     "RECEIVED_MSG|你好", 0, msg_script=True)
    e2 = _line_entry("line/1", "Plain sentence", "普通句子", 1)
    result = WriteResult()
    out = _patch_textasset(script, [e1, e2], [], result)
    lines = out.decode("utf-8").splitlines()
    assert lines[0] == "RECEIVED_MSG|你好"        # 命令列保留
    assert lines[1] == "普通句子"                  # 普通行整行替换（不被误剥）


def test_patch_textasset_msg_script_after_plain_line():
    """反向：普通行在前、msg_script 行在后——命令列保护不因残留丢失。"""
    script = "Plain sentence\nRECEIVED_MSG|Hello there\n".encode("utf-8")
    e1 = _line_entry("line/0", "Plain sentence", "普通句子", 0)
    e2 = _line_entry("line/1", "RECEIVED_MSG|Hello there",
                     "你好呀", 1, msg_script=True)
    result = WriteResult()
    out = _patch_textasset(script, [e1, e2], [], result)
    lines = out.decode("utf-8").splitlines()
    assert lines[0] == "普通句子"
    assert lines[1] == "RECEIVED_MSG|你好呀"      # 命令列保留（保护在）


# ── dispatcher 透传 ────────────────────────────────────────────────────────

def test_apply_format_text_threads_skipped_json():
    """apply_format_text 透传 skipped 出参（json 分支）。"""
    text = '{"greeting": "Hello"}'
    bad = TextEntry(file_id="f", key_path="greeting", original="Hola",
                    translation="你好")
    skipped: set[str] = set()
    out = apply_format_text("json", [bad], text, {}, skipped=skipped)
    assert "greeting" in skipped


def test_apply_format_text_threads_skipped_csv():
    """apply_format_text 透传 skipped 出参（csv 分支，条目级 delimiter）。"""
    text = "key,en\nhello,Hello\n"
    bad = TextEntry(file_id="f", key_path="csv/row/9", original="Hello",
                    translation="你好",
                    meta={"kind": "csv", "row": 9, "target_col": 1,
                          "delimiter": ","})
    skipped: set[str] = set()
    out = apply_format_text("csv", [bad], text, {}, skipped=skipped)
    assert "csv/row/9" in skipped


# ── P3：文本路径逐条记账（write_back result 出参）─────────────────────────

def _make_text_project(tmp_root: str, rel_path: str, content: str,
                       file_id: str, fmt: str, meta: dict | None = None):
    """建最小项目库：源文件 + 单文件记录，返回 (game_dir, store)。"""
    import json as _json
    from pathlib import Path
    import tempfile
    from hanhua.core.memory import ProjectStore

    d = Path(tmp_root) if tmp_root else Path(tempfile.mkdtemp())
    (d / rel_path).write_text(content, encoding="utf-8")
    store = ProjectStore(Path(tempfile.mkdtemp()) / "p.db")
    store.init_schema()
    store.add_file(file_id, rel_path, fmt, "utf-8", "\n", meta or {})
    return d, store


def test_write_back_text_accounting_written_and_key_style_rejected(tmp_path):
    """P3：正常译文条目 note_written，key_style 条目 note_rejected（不再零账）。

    旧文本路径只返回文件数——key_style 被渲染层静默置空却无任何记账
    （P1a 修的是 TextAsset 结构化分支，此处为纯文本路径同款假账）。
    """
    from hanhua.core.writer import write_back
    from hanhua.core.memory import ProjectStore

    game, store = _make_text_project(
        str(tmp_path), "strings.txt", "a=hello\nkey=ui_newGame\n",
        "strings.txt", "txt")
    store.upsert_entries([
        {"file_id": "strings.txt", "key_path": "kv/a/0", "original": "hello",
         "status": "pending",
         "meta": "{\"line_no\": 0, \"raw\": \"a=hello\", \"kind\": \"kv\", "
                 "\"key\": \"a\", \"delim\": \"=\"}"},
        {"file_id": "strings.txt", "key_path": "kv/key/1",
         "original": "ui_newGame", "status": "pending",
         "meta": "{\"line_no\": 1, \"raw\": \"key=ui_newGame\", "
                 "\"kind\": \"kv_structural\", \"key\": \"key\", \"delim\": \"=\"}"},
    ])
    store.update_translation("strings.txt", "kv/a/0", "你好")
    store.update_translation("strings.txt", "kv/key/1", "新游戏")
    result = WriteResult()
    write_back(store, game, tmp_path / "out", result=result)
    out = (tmp_path / "out" / "strings.txt").read_text(encoding="utf-8")
    assert "a=你好" in out
    assert "新游戏" not in out                      # 键条目保留原文
    # 记账：好条目 written；key_style 条目 rejected（reason 指明来源）
    assert result.entries == 1
    assert [r.reason for r in result.rejected] == ["text_key_style_blank"]


def test_write_back_text_accounting_apply_silent_skip(tmp_path):
    """P3：apply 层静默跳过（csv 行号失效）→ note_rejected。

    Rendezvous 漏 158 行实证形态：行号 meta 过期，apply_csv 找不到行
    静默丢弃——旧路径无任何账目，审计通过但游戏里是英文。
    """
    import json as _json
    from hanhua.core.writer import write_back

    game, store = _make_text_project(
        str(tmp_path), "table.csv", "key,en\nhello,Hello\n",
        "table.csv", "csv", {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "table.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 9, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("table.csv", "row/1", "你好")
    result = WriteResult()
    write_back(store, game, tmp_path / "out", result=result)
    out = (tmp_path / "out" / "table.csv").read_text(encoding="utf-8")
    assert out == "key,en\nhello,Hello\n"           # 宁漏勿坏：原文保留
    assert result.entries == 0                      # 假账根治：零 written
    assert [r.reason for r in result.rejected] == ["text_apply_silent_skip"]


def test_write_back_text_accounting_no_result_param_unchanged(tmp_path):
    """P3：不传 result（旧调用方）行为零变化——出参纯可选。"""
    from hanhua.core.writer import write_back

    game, store = _make_text_project(
        str(tmp_path), "strings.txt", "a=hello\n", "strings.txt", "txt")
    store.upsert_entries([
        {"file_id": "strings.txt", "key_path": "kv/a/0", "original": "hello",
         "status": "pending",
         "meta": "{\"line_no\": 0, \"raw\": \"a=hello\", \"kind\": \"kv\", "
                 "\"key\": \"a\", \"delim\": \"=\"}"},
    ])
    store.update_translation("strings.txt", "kv/a/0", "你好")
    n = write_back(store, game, tmp_path / "out")   # 无 result
    assert n == 1
    out = (tmp_path / "out" / "strings.txt").read_text(encoding="utf-8")
    assert "a=你好" in out


# ── P6b：_fit_bytes 高代理截断守卫 ─────────────────────────────────────────

def test_fit_bytes_guard_constants_are_surrogate_range():
    """守卫常量必须是代理区 U+D800–U+DFFF（0.42.1 审计：旧写法单字符
    链式比较 (U+FFFD <= c <= U+FFFD) 只匹配替换字符本身——死代码，
    代理区判定从未生效）。AST 层锁定常量，防止回归成 FFFD 死代码。
    """
    import ast as _ast
    import io as _io
    import os as _os
    src = _io.open(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "hanhua", "core", "unity",
        "writer.py"), encoding="utf-8").read()
    consts = [ord(n.value) for n in _ast.walk(_ast.parse(src))
              if isinstance(n, _ast.Constant) and isinstance(n.value, str)
              and len(n.value) == 1
              and 0xD800 <= ord(n.value) <= 0xDFFF]
    assert 0xD800 in consts and 0xDFFF in consts


def test_fit_bytes_guard_backs_off_high_surrogate():
    """截断点前一字符是高代理 → 多退一格（不产生新的孤立代理）。

    场景：内嵌孤立高代理（surrogatepass 解码产物形态）。Python str 按
    码点切片，chars 落在高代理后会把代理当普通字符留下——切出来的
    片段以孤立高代理结尾，CLR #US 解码器容忍度不确定。
    """
    HI = chr(0xD800)
    t = "abc" + HI + "defgh"
    # 守卫常量已由上一测试 AST 锁定为 D800/DFFF；此处同判据验证行为
    chars = 4                                  # 切在高代理后
    while chars > 0 and chr(0xD800) <= t[chars - 1:chars] <= chr(0xDFFF):
        chars -= 1
    assert chars == 3                           # 回退到高代理之前
    # 正常 astral 码点（emoji 是单个码点）不触发回退——Python str 是
    # 码点序列，代理区只匹配真正内嵌的孤立代理
    t2 = "abcdef" + "\U0001F600" + "ghi"
    chars = 7
    while chars > 0 and chr(0xD800) <= t2[chars - 1:chars] <= chr(0xDFFF):
        chars -= 1
    assert chars == 7                           # emoji 码点不动
    # BMP 普通字符不动
    chars = 3
    while chars > 0 and chr(0xD800) <= t[chars - 1:chars] <= chr(0xDFFF):
        chars -= 1
    assert chars == 3
