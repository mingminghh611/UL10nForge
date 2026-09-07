# -*- coding: utf-8 -*-
"""P2（0.42.1 审计）：审计→修复有界闭环回归测试。

repair_writeback 分诊四路：占位符机械自愈 / 磁盘分歧重渲染 / 模型
质量 flag 反馈重译（服务在场）/ 结构性失败直接人工。铁律：修复只改
store + 从 store 重渲染，绝不直接改磁盘字节；有界轮次 + 收敛判定
（失败集合不收缩即停）；无模型服务绝不盲改译文。

注意：源文件一律 write_bytes 写 LF——Windows 下 write_text 默认把
\n 翻成 \r\n，源 CRLF vs 写回 LF 会假报 eol_conserved False。
"""
import sys

sys.path.insert(0, "")

from pathlib import Path  # noqa: E402

import json as _json  # noqa: E402


def _project(tmp_path: Path, rel_path: str, content: str, fmt: str,
             meta: dict | None = None):
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


# ── 分诊 1：占位符丢失 → 机械自愈 + 重渲染 ──────────────────────

def test_repair_placeholder_healed_and_rewritten(tmp_path):
    """占位符丢失：self_heal 补全译文落库 + 重渲染该文件 → 收敛通过。

    self_heal 口径与翻译管线一致：缺口补全只在「译文保留部分占位符
    （子序列锚点）」时确定性生效（全丢锚点不足不乱插，句中丢失同理
    ——self_heal_format_tags docstring 实证规则），故用留 {0} 丢 {1}
    的形态。全丢形态由 test_repair_placeholder_not_healable_to_manual
    覆盖（转人工/重译，绝不乱补）。
    """
    from hanhua.core.writeback_audit import audit_writeback
    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Take {0} and {1}\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1",
         "original": "Take {0} and {1}",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "拿{0}和")  # 丢 {1}
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audit = audit_writeback(store, game, out_dir, run_model=False)
    assert audit.needs_rewrite                       # 占位符丢失被抓

    report = repair_writeback(store, game, out_dir, audit)
    assert report["converged"] is True
    assert report["healed_entries"] == 1
    assert report["repaired_files"] == ["t.csv"]
    assert report["needs_manual"] == []
    out_text = (out_dir / "t.csv").read_text(encoding="utf-8")
    assert "{1}" in out_text and "拿{0}和" in out_text  # 自愈后完整落盘


def test_repair_placeholder_not_healable_to_manual(tmp_path):
    """全丢占位符（锚点不足，self_heal 按设计拒绝乱补）→ 修不动 →
    needs_manual（绝不把标记堆到句尾）。"""
    from hanhua.core.writeback_audit import audit_writeback
    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,{0} items\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "{0} items",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "件物品")  # 全丢 {0}
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audit = audit_writeback(store, game, out_dir, run_model=False)
    assert audit.needs_rewrite

    report = repair_writeback(store, game, out_dir, audit)
    assert report["converged"] is False
    assert report["healed_entries"] == 0               # 没乱补
    assert any("t.csv" in m for m in report["needs_manual"])
    # 译文未被破坏（原样保留）
    assert store.get_entries()[0]["translation"] == "件物品"


# ── 分诊 2：磁盘分歧 → 重渲染恢复 ───────────────────────────────

def test_repair_render_inconsistent_rewritten(tmp_path):
    """渲染不一致（外部篡改磁盘）→ 从 store 重渲染恢复 → 收敛。"""
    from hanhua.core.writeback_audit import audit_writeback
    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    # 外部篡改磁盘（写回后产物被改）
    (out_dir / "t.csv").write_bytes(b"key,en\na,GOODBYE\n")
    audit = audit_writeback(store, game, out_dir, run_model=False)
    assert audit.needs_rewrite

    report = repair_writeback(store, game, out_dir, audit)
    assert report["converged"] is True
    out_text = (out_dir / "t.csv").read_text(encoding="utf-8")
    assert "你好" in out_text                     # 恢复 store 渲染


def test_repair_translation_missing_store_side_root_cause_to_manual(tmp_path):
    """译文未落盘且根因在 store（行号 meta 过期）→ 重渲染复现失败 →
    走满轮数收敛判定停 → needs_manual（绝不伪造通过）。"""
    from hanhua.core.writeback_audit import audit_writeback
    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 9, "target_col": 1,
                              "delimiter": ","})},   # 行号越界
    ])
    store.update_translation("t.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audit = audit_writeback(store, game, out_dir, run_model=False)
    assert audit.needs_rewrite

    report = repair_writeback(store, game, out_dir, audit)
    assert report["converged"] is False
    assert report["final_failed"] == ["t.csv"]
    assert any("t.csv" in m for m in report["needs_manual"])
    # 宁漏勿坏：修复不改磁盘内容（重渲染产物 == 原写回产物）
    assert (out_dir / "t.csv").read_text(encoding="utf-8") == "key,en\na,Hello\n"


# ── 分诊 3：模型质量 flag → 反馈重译（服务在场）/ 跳过记录（无服务）──

class _StubTranslator:
    """反馈重译测试替身：把译文改成固定中文（模拟重译通过质量门）。"""

    def __init__(self, new_translation: str = "重译好的译文"):
        self.new_translation = new_translation
        self.calls: list[str] = []

    def retranslate_with_feedback(self, entry, feedback, round_no=1):
        self.calls.append(entry.key_path)
        return True, self.new_translation


def test_repair_quality_flag_retranslated_with_service(tmp_path):
    """模型质量 flag + 服务在场 → 反馈重译落库 + 重渲染。"""
    from types import SimpleNamespace

    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)     # 确定性层全过
    # 模拟第 2 层软复核 flag（SEMANTIC_ERROR，行对含原文前缀）
    audit = SimpleNamespace(
        files=[], model_flags=[("t.csv", "SEMANTIC_ERROR",
                                "[0] 语义颠倒 | Hello → 你好")])
    stub = _StubTranslator("哈喽")
    report = repair_writeback(store, game, out_dir, audit,
                              repair_service=stub)
    assert stub.calls == ["row/1"]
    assert report["retranslated"] == 1
    assert report["repaired_files"] == ["t.csv"]
    assert report["converged"] is True
    # 重译落库 + 重渲染：磁盘出现新译文
    assert "哈喽" in (out_dir / "t.csv").read_text(encoding="utf-8")


def test_repair_quality_flag_skipped_without_service(tmp_path):
    """模型质量 flag + 无服务 → 跳过只记录，绝不盲改译文。"""
    from types import SimpleNamespace

    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audit = SimpleNamespace(
        files=[], model_flags=[("t.csv", "SEMANTIC_ERROR",
                                "[0] 语义颠倒 | Hello → 你好")])
    report = repair_writeback(store, game, out_dir, audit)
    assert report["retranslated"] == 0
    assert len(report["skipped_quality"]) == 1
    # store 译文未被盲改
    e = store.get_entries()[0]
    assert e["translation"] == "你好"


# ── 分诊 4：结构性守恒失败 → 直接人工 ───────────────────────────

def test_repair_structure_failure_direct_to_manual(tmp_path):
    """行数/结构守恒失败 → 不自动修（重渲染只复现坏数据）→ needs_manual。"""
    from types import SimpleNamespace

    from hanhua.core.writeback_audit import FileAudit
    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    # 伪造结构性失败审计（line_conserved False）
    audit = SimpleNamespace(
        files=[FileAudit(rel_path="t.csv", format="csv",
                         line_conserved=False, render_consistent=True)],
        model_flags=[])
    report = repair_writeback(store, game, out_dir, audit, max_rounds=1)
    assert report["converged"] is False
    assert any("结构性" in m for m in report["needs_manual"])


# ── 有界轮次与收敛判定 ──────────────────────────────────────────

def test_repair_bounded_rounds_no_infinite_loop(tmp_path):
    """不可修复失败（行号过期）→ 收敛判定提前停，不空转满轮。"""
    from hanhua.core.writeback_audit import audit_writeback
    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 9, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    audit = audit_writeback(store, game, out_dir, run_model=False)
    report = repair_writeback(store, game, out_dir, audit, max_rounds=5)
    # 收敛判定：第 1 轮修复后失败集合不收缩 → 第 2 轮开头即停
    assert report["rounds"] <= 2
    assert report["rounds"] < 5


def test_repair_clean_audit_noop(tmp_path):
    """干净审计 → 零修复动作（rounds=0，无 repaired_files）。"""
    from types import SimpleNamespace

    from hanhua.core.writeback_repair import repair_writeback
    from hanhua.core.writer import write_back

    game, store = _project(
        tmp_path, "t.csv", "key,en\na,Hello\n", "csv",
        {"delimiter": ",", "target_col": 1})
    store.upsert_entries([
        {"file_id": "t.csv", "key_path": "row/1", "original": "Hello",
         "status": "pending",
         "meta": _json.dumps({"kind": "csv", "row": 1, "target_col": 1,
                              "delimiter": ","})},
    ])
    store.update_translation("t.csv", "row/1", "你好")
    out_dir = tmp_path / "out"
    write_back(store, game, out_dir)
    before = (out_dir / "t.csv").read_bytes()
    audit = SimpleNamespace(files=[], model_flags=[])
    report = repair_writeback(store, game, out_dir, audit)
    assert report["rounds"] == 0
    assert report["repaired_files"] == []
    assert report["converged"] is True
    assert (out_dir / "t.csv").read_bytes() == before   # 磁盘未动


# ── 辅助函数口径 ────────────────────────────────────────────────

def test_missing_flag_key_parses_both_formats():
    """两种译文缺失消息的 key_path 解析（含 csv_target_residue 行格式）。"""
    from hanhua.core.writeback_repair import _missing_flag_key

    m1 = "translation_missing_in_output: row/2（World → 世界）"
    assert _missing_flag_key(m1) == "row/2"
    m2 = "csv_target_residue: row 2: Hello（条目 row/2 已有译文未落盘）"
    assert _missing_flag_key(m2) == "row/2"
    m3 = "其他消息"
    assert _missing_flag_key(m3) is None


# ── runner 集成（P2(3)：审计 FAIL 先修复再判）─────────────────────

def test_runner_wires_repair_between_audit_and_block():
    """all_record_runner 写回审计块必须先 repair_writeback 再判阻断。

    runner 逻辑在 main() 内联（不可直接调用，同 test_p6_cloud_chain 的
    结构锁定口径）：静态锁三件事——
    1. needs_rewrite 分支里先调 repair_writeback（自动修复优先于人工）；
    2. 修复后重跑 audit_writeback 复检（复用旧结果会把已修复的 FAIL
       错误地留给阻断分支）；
    3. 最终 needs_rewrite 判定在修复块之后（修好了不阻断）。
    """
    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "all_record_runner.py").read_text(encoding="utf-8")
    # 修复调用在 needs_rewrite 分支内、复检新跑、终判在后
    assert "if audit_res.needs_rewrite:" in src
    assert "from hanhua.core.writeback_repair import repair_writeback" in src
    assert "repair_report = repair_writeback(" in src
    # 复检必须是新跑的 audit_writeback（修复块内的第二次调用）
    first = src.index("repair_report = repair_writeback(")
    recheck = src.index("audit_res = audit_writeback(", first)
    final = src.index("if audit_res.needs_rewrite:", recheck)
    assert first < recheck < final
    # 反馈重译服务透传 translator（None 安全：--no-translate 只记录跳过）
    assert "repair_service=(translator if translator" in src
