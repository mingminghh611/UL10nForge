"""任务一阶段 1 测试（T1-8）：四级审核闭环全链路。

覆盖：四级判定解析、ReviewResult 扩展与 apply_verdict 分发、
风险分流决策表全分支、反馈重译注入、记忆门禁、再审收敛上限、
审核日志生成。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from hanhua.core.batch_translator import BatchTranslator
from hanhua.core.models import TextEntry
from hanhua.core.reviewer import (
    ReviewResult,
    _memory_apply,
    _parse_level,
    _parse_result,
    _retranslate_with_feedback,
    write_review_report,
)
from hanhua.core.risk_gate import RiskSignals, evaluate_entry, gate_entries


def _entry(original="Save the game", translation="保存游戏",
           status="translated", meta=None) -> TextEntry:
    return TextEntry(
        "f", "k1", original, translation=translation, status=status,
        meta=meta or {"role": "display", "disposition": "translate",
                      "confidence": "high"})


# ── 四级判定解析（T1-1） ───────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    (("PASS", "PASS"), ("pass", "PASS"), ("Pass", "PASS"),
     ("MINOR", "MINOR"), ("minor", "MINOR"),
     ("MAJOR", "MAJOR"), ("major", "MAJOR"),
     ("CRITICAL", "CRITICAL"), ("critical", "CRITICAL"),
     ("CRITICAL: 语义完全错误", "CRITICAL"),        # 前缀
     ("(MAJOR) 术语误用", "MAJOR"),                # 括号子串
     ("[MINOR] 语序", "MINOR"),                    # 方括号子串
     ("incorrect", "MAJOR"), ("flag", "MAJOR"),    # 旧词形兼容
     ("不合格", "MAJOR"), ("需要优化", "MAJOR"),
     ("", "PASS"), (None, "PASS"), ("？？？", "PASS"),  # 兜底
     ("PASS 完全正确", "PASS"), ("MINOR only", "MINOR")))
def test_parse_level(raw, expected):
    assert _parse_level(raw) == expected


def test_parse_result_valid_json():
    r = _parse_result(
        '{"level": "CRITICAL", "reason": "否定颠倒", '
        '"issues": [{"type": "否定", "detail": "don\'t 被漏译", '
        '"suggestion": "不要打开门"}]}', "e0")
    assert r is not None
    assert r.level == "CRITICAL"
    assert r.reason == "否定颠倒"
    assert r.issues[0]["type"] == "否定"
    assert r.issue == "否定"
    assert r.suggestion == "不要打开门"
    assert r.needs_optimization


def test_parse_result_fence_stripped():
    r = _parse_result('```json\n{"level": "MAJOR"}\n```', "e1")
    assert r is not None and r.level == "MAJOR"


def test_parse_result_non_json_fallback():
    r = _parse_result("这句话译得不对，Resume 应该是继续。", "e2")
    assert r is not None
    assert r.level == "PASS"           # 非 JSON 无四级标记词 → 保守 PASS
    assert r.reviewed is False
    assert "Resume" in r.reason        # 原文保留为 reason 供人工核查


def test_parse_result_non_json_plain_pass():
    r = _parse_result("翻译质量可以。", "e3")
    assert r is not None and r.level == "PASS"


# ── ReviewResult 扩展与分发（T1-2） ────────────────────────────────
@pytest.mark.parametrize(
    ("level", "optimization"),
    (("PASS", False), ("MINOR", False), ("MAJOR", True),
     ("CRITICAL", True)))
def test_needs_optimization_by_level(level, optimization):
    assert ReviewResult("e0", level=level).needs_optimization is optimization


def test_apply_verdict_pass_writes():
    entry = _entry()
    assert ReviewResult("e0", level="PASS").apply_verdict(entry) == "write"
    assert entry.meta["review_level"] == "PASS"
    assert "need_revision" not in entry.meta
    assert "need_retranslate" not in entry.meta


def test_apply_verdict_minor_records_and_passes():
    entry = _entry()
    r = ReviewResult("e0", level="MINOR", reason="语序略生硬")
    assert r.apply_verdict(entry) == "pass_minor"
    assert entry.meta["review_level"] == "MINOR"
    assert entry.meta["review_reason"] == "语序略生硬"
    assert "need_revision" not in entry.meta


def test_apply_verdict_major_revise():
    entry = _entry()
    r = ReviewResult("e0", level="MAJOR", reason="术语误用",
                     issues=({
                         "type": "术语错误", "detail": "Resume 应为继续",
                         "suggestion": "继续游戏"},))
    assert r.apply_verdict(entry) == "revise"
    assert entry.meta["review_level"] == "MAJOR"
    assert entry.meta["need_revision"] is True
    assert entry.meta["review_suggestion"] == "继续游戏"


def test_apply_verdict_critical_retranslate():
    entry = _entry()
    r = ReviewResult("e0", level="CRITICAL", reason="否定颠倒")
    assert r.apply_verdict(entry) == "retranslate"
    assert entry.meta["need_retranslate"] is True
    assert entry.meta["review_reason"] == "否定颠倒"


# ── 风险分流决策表全分支（T1-3） ───────────────────────────────────
def test_gate_quality_failed_forced():
    entry = _entry(status="failed")
    sig = evaluate_entry(entry)
    assert sig.risky
    assert "quality_failed" in sig.signals
    assert sig.priority == 6


def test_gate_glossary_conflict():
    entry = _entry(original="Press START to begin", translation="按开始键开始")
    sig = evaluate_entry(entry, [("START", "开始")])
    # START 命中词对且译文含标准译法「开始」→ 无冲突
    assert "glossary_conflict" not in sig.signals
    entry2 = _entry(original="Press START to begin", translation="按播放键")
    sig2 = evaluate_entry(entry2, [("START", "开始")])
    # 词对命中但译文未用标准译法 → 冲突送审
    assert "glossary_conflict" in sig2.signals


def test_gate_polysemy_wordlist():
    for word in ("Resume", "save", "CHARGE", "Load", "Quit"):
        sig = evaluate_entry(_entry(original=f"Press {word} now"))
        assert "polysemy" in sig.signals
    sig = evaluate_entry(_entry(original="Hello world"))
    assert "polysemy" not in sig.signals


def test_gate_long_text():
    long_text = " ".join(f"word{i}" for i in range(70))
    sig = evaluate_entry(_entry(original=long_text))
    assert "long_text" in sig.signals
    sig2 = evaluate_entry(_entry(original="short text"))
    assert "long_text" not in sig2.signals


@pytest.mark.parametrize("word", ["not", "no", "never", "if", "unless",
                                  "only", "more", "than", "but"])
def test_gate_negation_conditional(word):
    sig = evaluate_entry(_entry(original=f"You {word} open the door"))
    assert "negation_conditional" in sig.signals


def test_gate_character_text_dialogue_role():
    entry = _entry(meta={"role": "dialogue", "disposition": "translate",
                         "confidence": "high"})
    sig = evaluate_entry(entry)
    assert "character_text" in sig.signals


def test_gate_plain_passes_through():
    entry = _entry(original="Open the door", translation="打开门")
    sig = evaluate_entry(entry)
    assert not sig.risky
    assert sig.priority == 0


# ── 语境证据消歧（审计 Phase C，P1-3） ─────────────────────────────

class _CtxEv:
    """轻量语境证据（鸭子类型，与 RetrievalEvidence 同构）。"""

    def __init__(self, kind, translation, confidence=0.8):
        self.kind = kind
        self.translation = translation
        self.confidence = confidence


def test_gate_context_evidence_supported_removes_polysemy():
    """证据支持候选译文 → 多义词已消歧，不再因歧义送审。"""
    entry = _entry(original="Press Resume now", translation="按继续键")
    sig = evaluate_entry(
        entry, context_evidence=[
            _CtxEv("context_exact", "按继续键", confidence=0.9)])
    assert sig.context == "supported"
    # 消歧只消除多义风险，不掩盖其他信号（character_text 如实保留）
    assert "polysemy" not in sig.signals
    assert "context_conflict" not in sig.signals


def test_gate_context_evidence_conflict_adds_signal():
    """证据全部反对候选译文 → 歧义未决，追加 context_conflict 送审。"""
    entry = _entry(original="Press Resume now", translation="按简历键")
    sig = evaluate_entry(
        entry, context_evidence=[
            _CtxEv("context_exact", "按继续键", confidence=0.9)])
    assert sig.context == "conflict"
    assert "context_conflict" in sig.signals
    assert sig.priority == 4                # 与 polysemy 同级
    assert sig.risky


def test_gate_context_evidence_low_confidence_ignored():
    """低于直填门禁的证据只参考不裁决（polysemy 保留）。"""
    entry = _entry(original="Press Resume now", translation="按继续键")
    sig = evaluate_entry(
        entry, context_evidence=[
            _CtxEv("context_exact", "按继续键", confidence=0.1)])
    assert sig.context == ""
    assert "polysemy" in sig.signals


def test_gate_context_evidence_vector_kind_ignored():
    """向量召回证据（kind=vector）置信链较弱，不参与消歧裁决。"""
    entry = _entry(original="Press Resume now", translation="按继续键")
    sig = evaluate_entry(
        entry, context_evidence=[
            _CtxEv("vector", "按继续键", confidence=0.95)])
    assert sig.context == ""
    assert "polysemy" in sig.signals


def test_gate_context_evidence_none_keeps_old_behavior():
    """不传 context_evidence → 行为与旧版完全一致。"""
    entry = _entry(original="Press Resume now", translation="按继续键")
    sig = evaluate_entry(entry)
    assert sig.context == ""
    assert "polysemy" in sig.signals
    assert sig.risky


def test_gate_priority_ordering():
    # 同时命中 quality_failed + long_text → 最高优先级 6
    long_text = " ".join(f"word{i}" for i in range(70))
    sig = evaluate_entry(_entry(original=long_text, status="failed"))
    assert sig.priority == 6
    assert sig.signals[0] == "quality_failed"


def test_gate_entries_budget_truncation():
    # 100 条全可疑（15% 预算 = 15 条）→ 高优先级先保，低优先级截断
    entries = [_entry(original=f"Press Resume word{i}") for i in range(100)]
    to_review, passed, deferred, stats = gate_entries(
        entries, max_send_rate=0.15)
    assert stats["sent"] == 15
    assert stats["truncated"] == 85
    assert stats["deferred_due_to_budget"] == 85
    assert len(to_review) == 15
    assert len(passed) == 0
    assert len(deferred) == 85          # 截断条目归入人工队列，不叫 passed
    # 全部同信号 → 信号数排序稳定，无崩溃
    assert all(e.original for e in to_review)


def test_gate_entries_priority_preserved():
    # failed 条目（优先级 6，mandatory）必须入选，即使排在列表尾部
    entries = [_entry(original=f"Press Resume word{i}") for i in range(99)]
    entries.append(_entry(original="Press Resume fail", status="failed"))
    to_review, _passed, _deferred, stats = gate_entries(
        entries, max_send_rate=0.15)
    # 1 条 mandatory（failed 强制）+ 15 条 discretionary = 16 条送审
    assert stats["sent"] == 16
    assert stats["mandatory"] == 1
    # failed 优先级最高 → 排序最前（截断只作用于 discretionary）
    assert to_review[0].status == "failed"
    assert to_review[0].original == "Press Resume fail"


def test_gate_entries_mandatory_never_truncated():
    """双通道（审计 §5 P0-5）：mandatory（quality_failed/glossary_conflict）
    不受预算，预算为 0 时仍强制送审；discretionary 才受截断。"""
    # 预算 0（显式关闭）——mandatory 仍全量送审，discretionary 全 deferred
    entries = [_entry(original=f"Press Resume word{i}") for i in range(50)]
    entries.append(_entry(original="Press START to begin", status="failed"))
    to_review, passed, deferred, stats = gate_entries(
        entries, max_send_rate=0.0)
    assert stats["sent"] == 1
    assert stats["mandatory"] == 1
    assert stats["discretionary"] == 0
    assert len(deferred) == 50
    assert to_review[0].status == "failed"
    # 术语硬冲突（glossary_conflict）同样强制
    conflict = _entry(original="Press START to begin", translation="按播放键")
    to_review2, _p, _d, stats2 = gate_entries(
        [conflict] + entries, [("START", "开始")], max_send_rate=0.0)
    assert stats2["sent"] == 2           # conflict + failed 都强制
    assert stats2["mandatory"] == 2
    assert len(to_review2) == 2
    assert to_review2[0].status == "failed"   # 优先级 6 > 冲突的 5


def test_gate_entries_empty():
    to_review, passed, deferred, stats = gate_entries([])
    assert to_review == [] and passed == [] and deferred == []
    assert stats["total"] == 0


def test_gate_entries_rate_zero_means_nothing_sent():
    entries = [_entry(original="Resume game") for _ in range(10)]
    to_review, _passed, _deferred, stats = gate_entries(
        entries, max_send_rate=0.0)
    assert to_review == []
    assert stats["sent"] == 0
    assert stats["rate"] == 0.0


# ── 反馈重译注入（T1-4） ───────────────────────────────────────────
class _FakeChatClient:
    """按调用序号返回译文的假客户端（首坏后好）。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.config = None

    def chat(self, system, messages):
        content = "".join(m["content"] for m in messages
                          if m["role"] == "user")
        self.calls.append(content)
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return out, type("Usage", (), {"prompt_tokens": 5,
                                       "completion_tokens": 5})()


def test_retranslate_injects_feedback_prompt():
    client = _FakeChatClient(["保存游戏"])
    bt = BatchTranslator(client, batch_size=1, concurrency=1,
                         lang="en→zh-CN")
    entry = _entry(translation="存档游戏")
    ok, out = bt.retranslate_with_feedback(entry, "术语误用：Save 应译为保存")
    assert ok and out == "保存游戏"
    assert entry.translation == "保存游戏"
    assert "[审核反馈]" in client.calls[0]
    assert "术语误用：Save 应译为保存" in client.calls[0]
    assert entry.meta["review_round"] == 1


def test_retranslate_quality_gate_rejects_echo():
    # 回显原文 = 质量门失败 → 重译失败，attempt 记账
    client = _FakeChatClient(["Save the game"])
    bt = BatchTranslator(client, batch_size=1, concurrency=1,
                         lang="en→zh-CN")
    entry = _entry(translation="存档")
    ok, out = bt.retranslate_with_feedback(entry, "译文未翻译")
    assert ok is False
    assert int(entry.meta.get("attempt_count", 0)) >= 1


def test_retranslate_request_failure_returns_false():
    class _BrokenClient:
        config = None

        def chat(self, system, messages):
            raise RuntimeError("服务不可用")

    bt = BatchTranslator(_BrokenClient(), batch_size=1, concurrency=1)
    entry = _entry()
    ok, out = bt.retranslate_with_feedback(entry, "问题")
    assert ok is False and out == ""


# ── 记忆门禁（T1-6） ───────────────────────────────────────────────
class _FakeMemory:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_memory(self, original, translation, model, lang):
        self.added.append((original, translation, model, lang))

    def remove_memory(self, original, model, lang):
        self.removed.append((original, model, lang))


def test_memory_gate_blocks_major_critical():
    mem = _FakeMemory()
    for level in ("MAJOR", "CRITICAL"):
        entry = _entry()
        _memory_apply(mem, entry, level, "model", "zh-CN")
        assert mem.removed, f"{level} 应移除坏记忆"
        assert mem.added == []
    mem2 = _FakeMemory()
    _memory_apply(mem2, _entry(), "CRITICAL", "m", "zh-CN")
    assert mem2.removed == [("Save the game", "m", "zh-CN")]


def test_memory_gate_pass_minor_enters_memory():
    mem = _FakeMemory()
    for level in ("PASS", "MINOR"):
        _memory_apply(mem, _entry(), level, "m", "zh-CN")
    assert len(mem.added) == 2
    assert mem.removed == []


def test_memory_gate_blocks_builtin_conflict_on_pass():
    """BUILTIN 冲突门禁（2026-09-01 污染系统性根治）：审核模型把
    Disabled 判「残疾人士」PASS（审核端未注入 BUILTIN 强制）→ promote
    前拦截，坏译文不得成为可命中记忆。"""
    mem = _FakeMemory()
    _memory_apply(mem, _entry(original="Disabled", translation="残疾人士"),
                  "PASS", "m", "zh-CN")
    assert mem.added == []
    assert mem.removed == []
    # 非冲突词对照常进入记忆
    _memory_apply(mem, _entry(original="Start Game", translation="开始游戏"),
                  "PASS", "m", "zh-CN")
    assert mem.added == [("Start Game", "开始游戏", "m", "zh-CN")]


def test_memory_gate_none_memory_is_noop():
    _memory_apply(None, _entry(), "CRITICAL", "m", "zh-CN")  # 不抛


# ── 再审收敛上限（T1-5） ───────────────────────────────────────────
@dataclass
class _FakeTranslator:
    rounds_ok: list[tuple[bool, str]]   # 每轮 (ok, translation)
    calls: int = 0

    def retranslate_with_feedback(self, entry, feedback, round_no=1):
        self.calls += 1
        return self.rounds_ok[min(self.calls - 1, len(self.rounds_ok) - 1)]


def test_convergence_after_one_round(monkeypatch):
    def fake_review(entry, reviewer=None, app_dir=None, term_hint="",
                context_hint="", game_context_hint=""):
        return ReviewResult("re", level="PASS")

    monkeypatch.setattr("hanhua.core.reviewer._re_review", fake_review)
    tr = _FakeTranslator([(True, "继续游戏")])
    entry = _entry()
    result = _retranslate_with_feedback(
        tr, entry, ReviewResult("e0", level="CRITICAL", reason="错译"), None)
    assert result == "converged"
    assert tr.calls == 1
    assert entry.meta["review_level"] == "PASS"
    assert "review_blocked" not in entry.meta
    # #47：重译收敛 → 已重译标记（审校页「已重译」筛选 + 状态列透出）
    assert entry.meta.get("retranslated") is True


def test_convergence_minor_after_retranslate(monkeypatch):
    def fake_review(entry, reviewer=None, app_dir=None, term_hint="",
                context_hint="", game_context_hint=""):
        return ReviewResult("re", level="MINOR", reason="语序略生硬")

    monkeypatch.setattr("hanhua.core.reviewer._re_review", fake_review)
    tr = _FakeTranslator([(True, "继续游戏")])
    entry = _entry()
    result = _retranslate_with_feedback(
        tr, entry, ReviewResult("e0", level="MAJOR", reason="术语误用"), None)
    assert result == "converged"
    assert entry.meta["review_level"] == "MINOR"


def test_blocked_after_two_rounds(monkeypatch):
    def fake_review(entry, reviewer=None, app_dir=None, term_hint="",
                context_hint="", game_context_hint=""):
        return ReviewResult("re", level="CRITICAL", reason="仍错译")

    monkeypatch.setattr("hanhua.core.reviewer._re_review", fake_review)
    tr = _FakeTranslator([(True, "译1"), (True, "译2")])
    entry = _entry()
    result = _retranslate_with_feedback(
        tr, entry, ReviewResult("e0", level="CRITICAL", reason="错译"), None)
    assert result == "blocked"
    assert tr.calls == 2                    # 上限 2 轮即停
    assert entry.meta["review_blocked"] is True
    assert entry.meta["review_blocked_rounds"] == 2
    # P3（2026-09-06 fromivan 实证）：BLOCKED 必须带具体原因——
    # 否则 blocked.txt 只剩「BLOCKED（CRITICAL）」无任何理由，
    # 人工无从复核（「莫名卡住」）。再审理由随轮次更新进 reason。
    assert entry.meta.get("review_reason"), "BLOCKED 必须写 review_reason"
    assert "仍错译" in entry.meta["review_reason"]


def test_blocked_when_retranslate_fails(monkeypatch):
    tr = _FakeTranslator([(False, "")])
    entry = _entry()
    result = _retranslate_with_feedback(
        tr, entry, ReviewResult("e0", level="CRITICAL", reason="错译"), None)
    assert result == "blocked"
    assert tr.calls == 1
    assert entry.meta["review_blocked"] is True
    # P3：首条 BLOCKED 出口同样必须带原因（首审理由兜底）
    assert entry.meta.get("review_reason"), "BLOCKED 必须写 review_reason"
    assert "错译" in entry.meta["review_reason"]


def test_blocked_reason_carries_mechanical_gate_evidence():
    """P3：机械门拒绝导致 BLOCKED 时，理由必须含机械失败证据。

    fromivan blocked.txt 33 条 0 条审核理由的根因即两个 BLOCKED 出口
    不传 reason。机械失败条目（quality_reasons 落在条目上）走重译
    全败 → BLOCKED，理由应含机械原因与修正指引，而非只有语义理由。
    """
    tr = _FakeTranslator([(False, "")])
    entry = _entry()
    entry.quality_reasons = ("newline_mismatch", "placeholder_mismatch")
    entry.meta["quality_reasons"] = ["newline_mismatch",
                                     "placeholder_mismatch"]
    result = _retranslate_with_feedback(
        tr, entry, ReviewResult("e0", level="CRITICAL", reason="错译"), None)
    assert result == "blocked"
    reason = entry.meta.get("review_reason") or ""
    assert "newline_mismatch" in reason
    assert "placeholder_mismatch" in reason
    assert "换行" in reason          # _QUALITY_FIX_HINTS 的具体修正指引
    assert "占位符" in reason


def test_review_never_reviews_failed_translation():
    # 回显译文不进送审池（review_entries 过滤——经 _entry_for 语义）
    entry = _entry(original="Save the game", translation="Save the game")
    from hanhua.core.reviewer import review_entries
    summary = review_entries([entry], None, app_dir=".")
    assert summary["used"] is False
    assert summary["sent"] == 0


def test_force_send_reviews_plain_entries(monkeypatch):
    """#38：force_send 时无风险信号条目也送审（默认分流直放）。

    人工「重新审核」语义是无条件再判：plain 条目默认直放（used=False），
    若按钮不强制送审会点了没反应。PASS 判定经 apply_verdict 终态化为
    APPROVED（translator=None 不重译）。

    2026-08-14 全量送审变更：review_entries 默认 max_send_rate=1.0 即
    全量送审（无风险直放条目也进 4B——设置页「全部译文」承诺）——
    plain 条目默认也送审；force_send 仍强制无条件再判（语义等价，
    前者也覆盖直放）。
    """
    from hanhua.core.reviewer import review_entries

    def _plain() -> TextEntry:
        # 无信号条目：无多义词/否定/长句/专名，role 非 display/dialog
        return TextEntry("f", "k1", "Loading", translation="加载中",
                         status="translated",
                         meta={"role": "other", "confidence": "high"})

    seen: list = []
    class _StubReviewer:
        usable = True
        def __init__(self, app_dir=None, service=None, online_cfg=None,
                     config=None):
            pass
        def review_batch(self, items, *, on_progress=None,
                         cancellation_event=None):
            seen.extend(items)
            return {it.entry_id: ReviewResult(it.entry_id, level="PASS")
                    for it in items}, 0
    monkeypatch.setattr("hanhua.core.reviewer.SemanticReviewer",
                        _StubReviewer)

    s_default = review_entries([_plain()], None, app_dir=".")
    assert s_default["used"] is True           # 默认 100% 预算：全量送审
    assert s_default["sent"] == 1              # 直放条目也进 4B
    assert seen, "全量送审下 plain 条目必须送审"

    seen.clear()
    s_force = review_entries([_plain()], None, app_dir=".",
                             force_send=True)
    assert s_force["used"] is True
    assert s_force["sent"] == 1
    assert s_force["mandatory"] == 0
    assert s_force["outcomes"].get("APPROVED") == 1
    assert seen, "force_send 必须真正送审（fake reviewer 被调用）"


# ── 审核日志（T1-7） ───────────────────────────────────────────────
def test_write_review_report(tmp_path):
    summary = {
        "sent": 40, "rate": 0.12, "reviewed": 40,
        "levels": {"PASS": 30, "MINOR": 5, "MAJOR": 3, "CRITICAL": 2,
                   "PARSE_FAIL": 0},
        "retranslated": 4, "converged": 3, "blocked": 1,
        "pairs_added": 1, "pairs_rejected": {"miss": "拒绝"},
        "flagged": [
            ReviewResult("e1", level="CRITICAL", reason="PRESS 译成媒体",
                         issues=({"type": "术语错误",
                                  "suggestion": "按开始键开始"},)),
            ReviewResult("e2", level="CRITICAL", reason="否定颠倒",
                         issues=()),
        ],
        "originals": {"e1": "PRESS TO START", "e2": "Don't open it"},
        "locators": {"e1": "f:k1", "e2": "f:k2"},
    }
    path = write_review_report(summary, tmp_path / "review_report.md",
                               game_name="hickory")
    text = path.read_text(encoding="utf-8")
    assert "hickory" in text
    assert "送审：40 条" in text
    assert "CRITICAL 2" in text
    assert "收敛 3" in text
    assert "PRESS TO START" in text
    assert "按开始键开始" in text
    assert "f:k1" in text


def test_write_review_report_no_critical(tmp_path):
    summary = {"sent": 1, "rate": 0.01, "reviewed": 1,
               "levels": {"PASS": 1, "MINOR": 0, "MAJOR": 0,
                          "CRITICAL": 0, "PARSE_FAIL": 0},
               "retranslated": 0, "converged": 0, "blocked": 0,
               "pairs_added": 0, "pairs_rejected": {}, "flagged": [],
               "originals": {}, "locators": {}}
    path = write_review_report(summary, tmp_path / "r.md")
    assert "无（本轮无 CRITICAL 级错译）" in path.read_text(encoding="utf-8")


def test_write_review_report_stage_f_fields(tmp_path):
    """#43 阶段 F：风险分布/结构化失败/维度分透出（旧字段兼容）。"""
    summary = {
        "sent": 40, "rate": 0.12, "reviewed": 40,
        "levels": {"PASS": 30, "MINOR": 5, "MAJOR": 3, "CRITICAL": 2,
                   "PARSE_FAIL": 0},
        "retranslated": 4, "converged": 3, "blocked": 1,
        "pairs_added": 1, "pairs_rejected": {}, "flagged": [
            ReviewResult("e1", level="CRITICAL", reason="PRESS 译成媒体",
                         overall_score=42,
                         dimensions={"语义准确": 40, "自然度": 60},
                         issues=({"type": "术语错误",
                                  "suggestion": "按开始键开始"},)),
        ],
        "originals": {"e1": "PRESS TO START"},
        "locators": {"e1": "f:k1"},
        "risk_levels": {"LOW": 30, "MEDIUM": 5, "HIGH": 3, "CRITICAL": 2},
        "review_failures": [
            {"game": "hickory", "locator": "f:k1", "level": "CRITICAL",
             "original": "PRESS TO START", "wrong_translation": "媒体开始",
             "correct_translation": "按开始键开始", "reason": "PRESS 误译"},
        ],
    }
    path = write_review_report(summary, tmp_path / "rr.md", game_name="hickory")
    text = path.read_text(encoding="utf-8")
    assert "风险分布：LOW 30 / MEDIUM 5 / HIGH 3 / CRITICAL 2" in text
    assert "结构化失败：1 条" in text
    assert "综合分：42/100" in text
    assert "维度分：语义准确 40、自然度 60" in text
    assert "PRESS 误译" in text
    assert "按开始键开始" in text
    # 旧字段仍在
    assert "送审：40 条" in text


def test_write_review_report_legacy_summary_compat(tmp_path):
    """旧 summary（无风险/失败/维度字段）→ 不崩溃、不出现新区块。"""
    summary = {"sent": 1, "rate": 0.01, "reviewed": 1,
               "levels": {"PASS": 1, "MINOR": 0, "MAJOR": 0,
                          "CRITICAL": 0, "PARSE_FAIL": 0},
               "retranslated": 0, "converged": 0, "blocked": 0,
               "pairs_added": 0, "pairs_rejected": {}, "flagged": [],
               "originals": {}, "locators": {}}
    path = write_review_report(summary, tmp_path / "legacy.md")
    text = path.read_text(encoding="utf-8")
    assert "风险分布" not in text
    assert "结构化失败" not in text


def test_retranslate_reuses_passed_reviewer_not_cwd_builder(monkeypatch):
    """#20：重译闭环的再审必须复用主审核 reviewer 实例——此前每轮新建
    SemanticReviewer 且回退 cwd 找模型（#17 已修主路径，再审路径漏掉），
    模型在 resource_dir 时再审必挂 TRANSPORT_ERROR，重译永不收敛。"""
    calls = []
    class _StubReviewer:
        def review_one(self, item):
            calls.append(item.entry_id)
            return ReviewResult("re", level="PASS")
    tr = _FakeTranslator([(True, "继续游戏")])
    entry = _entry()
    stub = _StubReviewer()
    result = _retranslate_with_feedback(
        tr, entry, ReviewResult("e0", level="CRITICAL", reason="错译"), None,
        reviewer=stub)
    assert result == "converged"
    assert calls == ["re_e0"] or len(calls) == 1   # 走传入的 reviewer
    # 未传 reviewer 时仍可用 app_dir 定位（不新建 cwd 实例）
    seen = []
    monkeypatch.setattr("hanhua.core.reviewer.SemanticReviewer",
                        lambda app_dir=None, service=None,
                        online_cfg=None: (
                            seen.append(app_dir) or _StubReviewer()))
    tr2 = _FakeTranslator([(True, "继续游戏")])
    entry2 = _entry()
    result2 = _retranslate_with_feedback(
        tr2, entry2,
        ReviewResult("e0", level="CRITICAL", reason="错译"), None,
        app_dir="/x/models")
    assert result2 == "converged"
    assert seen and seen[0] == "/x/models"


def test_write_review_report_detail_section(tmp_path):
    """#48：全量送审明细章节——每条原文/译文/AI 判定/未通过原因/终态。"""
    summary = {
        "sent": 2, "rate": 0.2, "reviewed": 2,
        "levels": {"PASS": 1, "MINOR": 0, "MAJOR": 1, "CRITICAL": 0,
                   "PARSE_FAIL": 0},
        "retranslated": 1, "converged": 1, "blocked": 0,
        "pairs_added": 0, "pairs_rejected": {}, "flagged": [],
        "originals": {}, "locators": {},
        "detail": [
            {"locator": "f:k1", "text_type": "UI 显示文本",
             "original": "Removes 5 <b>clicks</b> at the start of each "
                         "<b>stage</b>.",
             "translation": "移除每个阶段开头的 5 次点击。",
             "final_translation": "移除每个阶段开头的 5 <b>次点击</b>。",
             "level": "MAJOR", "reason": "标签缺失",
             "suggestion": "保留 <b> 标签", "issues": [],
             "overall_score": 0, "dimensions": {}, "error": "",
             "quality_reasons": ["rich_text_mismatch"],
             "outcome": "APPROVED_MINOR", "review_round": 1},
            {"locator": "f:k2", "text_type": "游戏文本",
             "original": "Good job", "translation": "干得好",
             "final_translation": "", "level": "PASS", "reason": "",
             "suggestion": "", "issues": [],
             "overall_score": 88, "dimensions": {"语义准确": 90,
                                                 "自然度": 86},
             "error": "", "quality_reasons": [],
             "outcome": "APPROVED", "review_round": None},
        ],
    }
    path = write_review_report(summary, tmp_path / "detail.md")
    text = path.read_text(encoding="utf-8")
    assert "## 全量送审明细（2 条）" in text
    assert "Removes 5 <b>clicks</b>" in text          # 原文保留标签
    assert "送审译文：移除每个阶段开头的 5 次点击。" in text
    assert "终译（重译收敛）：移除每个阶段开头的 5 <b>次点击</b>。" in text
    assert "AI 判定：较大问题" in text
    assert "未通过原因（机械质量门）：rich_text_mismatch" in text
    assert "终态：APPROVED_MINOR · 重译轮次：1" in text
    assert "干得好" in text
    assert "AI 判定：通过（88/100）" in text
    assert "维度分：语义准确 90、自然度 86" in text
    assert "终态：APPROVED" in text


def test_write_review_report_legacy_summary_no_detail(tmp_path):
    """旧 summary（无 detail）→ 不输出全量明细章节（零破坏）。"""
    summary = {"sent": 1, "rate": 0.01, "reviewed": 1,
               "levels": {"PASS": 1, "MINOR": 0, "MAJOR": 0,
                          "CRITICAL": 0, "PARSE_FAIL": 0},
               "retranslated": 0, "converged": 0, "blocked": 0,
               "pairs_added": 0, "pairs_rejected": {}, "flagged": [],
               "originals": {}, "locators": {}}
    path = write_review_report(summary, tmp_path / "legacy2.md")
    text = path.read_text(encoding="utf-8")
    assert "全量送审明细" not in text
