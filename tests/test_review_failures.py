# -*- coding: utf-8 -*-
"""Phase B-5（审计 P1-7）：结构化 review failure 闭环测试。

- review_failure_v1 版本化 JSON schema：错误译文/正确译文/错误类型/
  审核理由结构化留档（runner 机械 quality_reasons 聚合之外的语义错误
  专属通道）
- 收敛与未收敛均记录；只有终态 APPROVED 系（二审收敛 PASS / 人工确认）
  译文才写入 correct_translation——BLOCKED/NEEDS_REVISION/REVIEW_ERROR
  一律留空（坏译文不当正确例学习）
- REVIEW_ERROR 管线错误单独 error_type（语义错误 vs 管线错误分开记账）
- record_review_failure 幂等（game:locator pattern，重审只 hits+1），
  match_case 按原文召回同类失败（KnowledgeRetrieval 反例接入点）
"""
import json

import pytest

from hanhua.core.glossary import CANDIDATE, DepositResult
from hanhua.core.knowledge import KnowledgeBase
from hanhua.core.models import TextEntry
from hanhua.core.review_failures import (ERROR_CRITICAL, ERROR_MAJOR,
                                         ERROR_REVIEW, SCHEMA_VERSION,
                                         build_review_failure,
                                         failure_pattern)
from hanhua.core.reviewer import ReviewResult, review_entries


class _FakeGlossary:
    def add_reviewed(self, term, trans, context="", game=""):
        return DepositResult(CANDIDATE, term=term)


class _FakeTranslator:
    """重译替身：按调用次数返回 (ok, translation)。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def retranslate_with_feedback(self, entry, feedback, round_no=1):
        self.calls += 1
        return self.rounds[min(self.calls - 1, len(self.rounds) - 1)]


def _fake_review(monkeypatch, results: dict[str, ReviewResult]):
    monkeypatch.setattr(
        "hanhua.core.reviewer.SemanticReviewer.usable",
        property(lambda self: True))
    monkeypatch.setattr(
        "hanhua.core.reviewer.SemanticReviewer.review_batch",
        lambda self, items, timeout=None, **kwargs: (results, 0))
    # 4B 重译通道禁用（2026-08-14 起 _retranslate_with_feedback 4B 优先
    # 且 reviewer 已 usable——本文件验证失败留档语义，必须走 translator
    # fake 确定性路径；否则本地 4B 模型运行时会真实重译，断言不稳）
    monkeypatch.setattr(
        "hanhua.core.reviewer.SemanticReviewer.retranslate_with_feedback",
        lambda self, original, translation, feedback, **kwargs: "")


def _entry(original="Press Resume to continue",
           translation="按下继续继续游戏",
           key_path="k1") -> TextEntry:
    return TextEntry(
        "f", key_path, original, translation=translation,
        status="translated",
        meta={"role": "display", "disposition": "translate",
              "confidence": "high"})


def _entries() -> list[TextEntry]:
    return [_entry()]


# ── review_failure_v1 schema ───────────────────────────────────────

def test_build_review_failure_shape():
    """13 字段版本化 JSON：schema 标记 + 错误/正确译文分离 + 收敛状态。"""
    failure = build_review_failure(
        game="G", model="m", error_type=ERROR_CRITICAL,
        original="Press Resume to continue",
        wrong_translation="按下继续继续游戏",
        correct_translation="按下继续继续",
        review_reason="否定颠倒", suggestion="继续游戏",
        converged=True, final_outcome="APPROVED", locator="f:k1")
    assert failure["schema"] == SCHEMA_VERSION
    assert failure["error_type"] == ERROR_CRITICAL
    assert failure["converged"] is True
    assert failure["wrong_translation"] == "按下继续继续游戏"
    assert failure["correct_translation"] == "按下继续继续"
    # 可 JSON 序列化（落库前提）
    json.dumps(failure, ensure_ascii=False)


def test_failure_pattern_includes_game():
    """幂等 pattern = game:locator——跨游戏同名 locator 不串。"""
    a = build_review_failure(game="g1", model="", error_type=ERROR_MAJOR,
                             original="", wrong_translation="",
                             review_reason="", suggestion="",
                             converged=False, final_outcome="BLOCKED",
                             locator="shared/1.strings:1")
    b = build_review_failure(game="g2", model="", error_type=ERROR_MAJOR,
                             original="", wrong_translation="",
                             review_reason="", suggestion="",
                             converged=False, final_outcome="BLOCKED",
                             locator="shared/1.strings:1")
    assert failure_pattern(a) != failure_pattern(b)
    assert failure_pattern(a) == "g1:shared/1.strings:1"


# ── review_entries：结构化失败构建 ─────────────────────────────────

def test_critical_converged_records_correct_translation(monkeypatch):
    """CRITICAL 重译二审收敛 → 失败留档，正确例 = 终译（APPROVED 系）。"""
    _fake_review(monkeypatch, {
        "e0": ReviewResult("e0", level="CRITICAL", reason="否定颠倒",
                           suggestion="继续游戏"),
    })
    monkeypatch.setattr(
        "hanhua.core.reviewer._re_review",
        lambda entry, reviewer=None, app_dir=None, term_hint="",
        context_hint="", game_context_hint="":
        ReviewResult("re", level="PASS", reason="已收敛"))
    tr = _FakeTranslator([(True, "按下继续继续")])
    summary = review_entries(_entries(), _FakeGlossary(), game_name="G",
                             translator=tr, max_send_rate=1.0)
    monkeypatch.undo()

    failures = summary["review_failures"]
    assert len(failures) == 1
    f = failures[0]
    assert f["error_type"] == ERROR_CRITICAL
    assert f["converged"] is True
    assert f["final_outcome"] == "APPROVED"
    assert f["original"] == "Press Resume to continue"
    assert f["wrong_translation"] == "按下继续继续游戏"   # 送审快照（坏译文）
    assert f["correct_translation"] == "按下继续继续"     # 终译 = 正确例
    assert f["locator"] == "f:k1"
    assert f["game"] == "G"


def test_critical_blocked_never_has_correct_translation(monkeypatch):
    """重译 2 轮未收敛 → BLOCKED：记录未收敛，正确例留空。"""
    _fake_review(monkeypatch, {
        "e0": ReviewResult("e0", level="CRITICAL", reason="否定颠倒",
                           suggestion="继续游戏"),
    })
    monkeypatch.setattr(
        "hanhua.core.reviewer._re_review",
        lambda entry, reviewer=None, app_dir=None, term_hint="",
        context_hint="", game_context_hint="":
        ReviewResult("re", level="CRITICAL", reason="仍错译"))
    tr = _FakeTranslator([(True, "译1"), (True, "译2")])
    summary = review_entries(_entries(), _FakeGlossary(), game_name="G",
                             translator=tr, max_send_rate=1.0)
    monkeypatch.undo()

    failures = summary["review_failures"]
    assert len(failures) == 1
    f = failures[0]
    assert f["error_type"] == ERROR_CRITICAL
    assert f["converged"] is False
    assert f["final_outcome"] == "BLOCKED"
    assert f["correct_translation"] == ""      # 无正确例——坏译文不学习
    assert f["wrong_translation"] == "按下继续继续游戏"


def test_major_without_translator_records_needs_revision(monkeypatch):
    """MAJOR 无重译通道 → NEEDS_REVISION：记录但正确例留空
    （未二审收敛/人工确认，不得当正确例）。"""
    _fake_review(monkeypatch, {
        "e0": ReviewResult("e0", level="MAJOR", reason="术语误用",
                           suggestion="Resume→继续"),
    })
    summary = review_entries(_entries(), _FakeGlossary(), game_name="G",
                             max_send_rate=1.0)
    monkeypatch.undo()

    failures = summary["review_failures"]
    assert len(failures) == 1
    f = failures[0]
    assert f["error_type"] == ERROR_MAJOR
    assert f["converged"] is False
    assert f["final_outcome"] == "NEEDS_REVISION"
    assert f["correct_translation"] == ""
    assert f["review_reason"] == "术语误用"


def test_review_error_recorded_separately(monkeypatch):
    """REVIEW_ERROR 管线错误 → 单独 error_type，不进 flagged。"""
    _fake_review(monkeypatch, {
        "e0": ReviewResult("e0", error="TRANSPORT_ERROR",
                           reason="传输失败"),
    })
    summary = review_entries(_entries(), _FakeGlossary(), game_name="G",
                             max_send_rate=1.0)
    monkeypatch.undo()

    assert summary["flagged"] == []
    failures = summary["review_failures"]
    assert len(failures) == 1
    f = failures[0]
    assert f["error_type"] == ERROR_REVIEW
    assert f["final_outcome"] == "REVIEW_ERROR"
    assert "TRANSPORT_ERROR" in f["review_reason"]   # 错误种类留档
    assert f["converged"] is False
    assert f["correct_translation"] == ""


def test_pass_and_minor_produce_no_failures(monkeypatch):
    """PASS/MINOR 不构成失败——review_failures 为空。"""
    _fake_review(monkeypatch, {
        "e0": ReviewResult("e0", level="MINOR", reason="语序略生硬"),
    })
    summary = review_entries(_entries(), _FakeGlossary(), game_name="G",
                             max_send_rate=1.0)
    monkeypatch.undo()
    assert summary["flagged"] == []
    assert summary["review_failures"] == []


def test_no_review_sent_yields_empty_failures(monkeypatch):
    """无可审条目 → summary 仍含 review_failures 空列表（API 稳定）。"""
    summary = review_entries([], _FakeGlossary(), game_name="G")
    assert summary["review_failures"] == []


# ── record_review_failure：fail_case 域落库 ────────────────────────

def _kb(tmp_path) -> KnowledgeBase:
    kb = KnowledgeBase(tmp_path / "knowledge.db")
    kb.store.init_schema()
    return kb


def _failure(**overrides) -> dict:
    data = dict(game="G", model="m", error_type=ERROR_CRITICAL,
                original="Press Resume to continue",
                wrong_translation="按下继续继续游戏",
                correct_translation="按下继续继续",
                review_reason="否定颠倒", suggestion="继续游戏",
                converged=True, final_outcome="APPROVED",
                locator="f:k1")
    data.update(overrides)
    return build_review_failure(**data)


def test_record_review_failure_idempotent(tmp_path):
    """同条目重审 → 单行 hits+1，note 刷新为 review_failure_v1 JSON。"""
    kb = _kb(tmp_path)
    assert kb.record_review_failure(_failure()) is True
    assert kb.record_review_failure(_failure()) is False   # 幂等（非新增）

    rows = kb.store.list_by_domain("fail_case")
    assert len(rows) == 1
    assert rows[0]["kind"] == "审核"
    assert rows[0]["pattern"] == "G:f:k1"
    assert rows[0]["hits"] == 2
    note = json.loads(rows[0]["note"])
    assert note["schema"] == SCHEMA_VERSION
    assert note["correct_translation"] == "按下继续继续"


def test_record_review_failure_recallable_by_original(tmp_path):
    """KnowledgeRetrieval 接入点：match_case 按原文召回同类失败作反例。"""
    kb = _kb(tmp_path)
    kb.record_review_failure(_failure())

    hits = kb.match_case("Press Resume to continue", fail_type="审核")
    assert hits, "按原文应召回审核失败案例"
    note = json.loads(hits[0]["note"])
    assert note["wrong_translation"] == "按下继续继续游戏"
    assert note["correct_translation"] == "按下继续继续"


def test_record_review_failure_empty_locator_rejected(tmp_path):
    kb = _kb(tmp_path)
    assert kb.record_review_failure(
        _failure(locator="")) is False
    assert kb.store.list_by_domain("fail_case") == []


def test_record_review_failure_cross_game_distinct(tmp_path):
    """同 locator 不同游戏 → 两条独立案例（不误合并）。"""
    kb = _kb(tmp_path)
    kb.record_review_failure(_failure(game="g1"))
    kb.record_review_failure(_failure(game="g2"))
    rows = kb.store.list_by_domain("fail_case")
    assert len(rows) == 2


def test_record_review_failure_builtin_conflict_correct_cleared(tmp_path):
    """BUILTIN 冲突门禁（2026-09-01 污染系统性根治）：失败案例
    correct_translation 若与内置 UI 权威冲突（Disabled→残疾人士，UI
    状态标签被误判「残疾」）→ 留空——坏译名不得成为可召回的正确例；
    案例本身仍记录（wrong_translation 留档）。"""
    kb = _kb(tmp_path)
    assert kb.record_review_failure(
        _failure(original="Disabled", wrong_translation="残疾人士",
                 correct_translation="残疾人士", locator="f:disabled",
                 error_type=ERROR_CRITICAL)) is True
    rows = kb.store.list_by_domain("fail_case")
    assert len(rows) == 1
    note = json.loads(rows[0]["note"])
    assert note["correct_translation"] == ""
    assert note["wrong_translation"] == "残疾人士"


def test_record_review_failure_builtin_conflict_authoritative_kept(tmp_path):
    """非冲突正确例照常落库（权威译名/多词短语不受影响）。"""
    kb = _kb(tmp_path)
    kb.record_review_failure(
        _failure(original="Disabled", wrong_translation="残疾人士",
                 correct_translation="已禁用", locator="f:disabled2",
                 error_type=ERROR_CRITICAL))
    kb.record_review_failure(
        _failure(original="Press any key", wrong_translation="按键盘",
                 correct_translation="按任意键", locator="f:key",
                 error_type=ERROR_CRITICAL))
    rows = kb.store.list_by_domain("fail_case")
    notes = {json.loads(r["note"])["original"]: json.loads(r["note"])
             for r in rows}
    assert notes["Disabled"]["correct_translation"] == "已禁用"
    assert notes["Press any key"]["correct_translation"] == "按任意键"


# ── #48 全量送审明细（审校后完整记录） ─────────────────────────────

def test_review_entries_collects_full_detail(monkeypatch):
    """#48：审校后输出完整记录——每条待审核文本的原文（保留富文本
    标签）/送审译文/AI 判定/机械未通过原因/终态/重译轮次全收集，
    与审核模型输入的原文一致。"""
    _fake_review(monkeypatch, {
        "e0": ReviewResult("e0", level="MAJOR", reason="标签缺失",
                           suggestion="保留 <b> 标签"),
    })
    entry = _entry(
        original="Removes 5 <b>clicks</b> at the start of each "
                 "<b>stage</b>.",
        translation="移除每个阶段开头的 5 次点击。", key_path="k1")
    entry.meta = dict(entry.meta)
    entry.meta["quality_reasons"] = ["rich_text_mismatch"]
    monkeypatch.setattr(
        "hanhua.core.reviewer._re_review",
        lambda entry, reviewer=None, app_dir=None, term_hint="",
        context_hint="", game_context_hint="":
        ReviewResult("re", level="MINOR", reason="已收敛"))
    tr = _FakeTranslator([(True, "移除每个阶段开头的 5 <b>次点击</b>。")])
    summary = review_entries([entry], _FakeGlossary(), game_name="G",
                             translator=tr, max_send_rate=1.0)
    monkeypatch.undo()

    detail = summary["detail"]
    assert len(detail) == 1
    d = detail[0]
    # 原文保留富文本标签（与审核模型 prompt 输入一致）
    assert d["original"] == (
        "Removes 5 <b>clicks</b> at the start of each <b>stage</b>.")
    assert d["translation"] == "移除每个阶段开头的 5 次点击。"  # 送审快照
    assert d["final_translation"] == "移除每个阶段开头的 5 <b>次点击</b>。"
    assert d["level"] == "MAJOR"
    assert d["reason"] == "标签缺失"
    assert d["suggestion"] == "保留 <b> 标签"
    assert d["quality_reasons"] == ["rich_text_mismatch"]  # 机械未通过原因
    assert d["text_type"] == "UI 显示文本"
    assert d["outcome"] in ("APPROVED", "APPROVED_MINOR")
    assert d["review_round"] == 1
    assert d["locator"] == "f:k1"
