"""AI 辅助识别回归测试（0.38.0 任务二④）。

锁定 ai_recognition 的安全契约：
- 候选收集白名单（只收 typetree_candidate/typetree_prefilter 的
  role=candidate status=skipped，muted 短路）；
- 升格路径（模型判 display → pending/display/translate，通过
  is_actionable_translation 终检）；
- 键风格预校验（模型幻觉防线：MENU_PLAY 判 display 也不放行）；
- 结构终检（URL/程序集名 AI 无权推翻——宁漏勿坏）；
- 确认跳过（structural verdict 留档 muted，防重复询问）；
- fail-closed（传输错误/空输出/解析失败 → 不改任何 meta）；
- 模型缺失降级（degraded=True，不触发启动）。
"""
import pytest

from hanhua.core.ai_recognition import (
    _apply_verdicts,
    _parse_verdicts,
    _PendingWrites,
    _verify_upgradeable,
    collect_candidates,
    run_ai_recognition,
)
from hanhua.core.memory import ProjectStore
from hanhua.core.models import (
    STATUS_SKIPPED,
    TextEntry,
    entry_from_row,
    is_actionable_translation,
)


class _FakeService:
    """假的识别服务：按预设返回 verdict JSON。"""

    def __init__(self, outputs=None, error=None):
        self.outputs = list(outputs or [])
        self.error = error
        self.prompts = []

    def chat(self, prompt, *, max_tokens=1024, temperature=0.1,
             timeout=120.0):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0) if len(self.outputs) > 1 \
            else self.outputs[0]


def _candidate_row(key_path, original, *, kind="typetree_candidate",
                   obj_has_values=False, extra_meta=None):
    meta = {"kind": kind, "role": "candidate", "confidence": "low",
            "disposition": "structural",
            "obj_has_values": obj_has_values}
    if extra_meta:
        meta.update(extra_meta)
    return {"file_id": "f1", "key_path": key_path, "original": original,
            "status": "skipped", "meta": meta}


def _write_candidates(store, rows):
    store.upsert_entries([
        {"file_id": row["file_id"], "key_path": row["key_path"],
         "original": row["original"], "status": row["status"],
         "meta": row["meta"]}
        for row in rows])


@pytest.fixture
def store(tmp_path):
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    return store


# ── 候选收集 ────────────────────────────────────────────────────────────

def test_collect_candidates_whitelist_kinds_only():
    rows = [
        _candidate_row("k1", "Combat Music"),
        _candidate_row("k2", "ui_newGame", kind="typetree_prefilter",
                       extra_meta={"prefilter": "key_identifier"}),
        # display 层不收（提取器已判定的不动）
        {"file_id": "f1", "key_path": "k3", "original": "Hello World",
         "status": "pending",
         "meta": {"kind": "typetree", "role": "display"}},
        # 引擎串等其他 skipped kind 不收（AI 无权推翻确定性判定）
        {"file_id": "f1", "key_path": "k4", "original": "...<sentence>",
         "status": "skipped",
         "meta": {"kind": "il2cpp_sentence", "role": "candidate"}},
        # muted 短路
        _candidate_row("k5", "Start Menu",
                       extra_meta={"ai_verdict": "structural"}),
        # 非 candidate role 不收
        {"file_id": "f1", "key_path": "k6", "original": "Something",
         "status": "skipped",
         "meta": {"kind": "typetree_candidate", "role": "structural"}},
    ]
    picked = collect_candidates(rows)
    assert [e.key_path for e in picked] == ["k1", "k2"]


def test_collect_candidates_ranks_valueless_spaced_first():
    rows = [
        _candidate_row("a", "sometoken", obj_has_values=True),
        _candidate_row("b", "Some Phrase", obj_has_values=False),
        _candidate_row("c", "some_token", obj_has_values=False),
    ]
    picked = collect_candidates(rows)
    # 无值证据 + 含空格的最优先（漏网概率最高）
    assert picked[0].key_path == "b"
    assert picked[-1].key_path == "a"


def test_collect_candidates_respects_limit():
    rows = [_candidate_row(f"k{i}", f"Phrase Number {i}")
            for i in range(50)]
    assert len(collect_candidates(rows, limit=10)) == 10


# ── verdict 解析 ────────────────────────────────────────────────────────

def test_parse_verdicts_plain_and_fenced_and_regex_fallback():
    plain = '[{"i": 0, "v": "display"}, {"i": 1, "v": "structural"}]'
    assert _parse_verdicts(plain) == {0: "display", 1: "structural"}
    fenced = "```json\n" + plain + "\n```"
    assert _parse_verdicts(fenced) == {0: "display", 1: "structural"}
    # 截断/夹带说明文字 → 逐项正则兜底
    broken = '结果如下 [{"i": 0, "v": "display"}, {"i": 1, "v'
    assert _parse_verdicts(broken) == {0: "display"}
    # 完全不可解析 → 空（fail-closed）
    assert _parse_verdicts("complete garbage") == {}
    # 非法 verdict 值丢弃
    assert _parse_verdicts('[{"i": 0, "v": "maybe"}]') == {}


# ── 升格预校验（AI 无权推翻的确定性闸门） ───────────────────────────────

def test_verify_upgradeable_blocks_key_style_and_structural():
    def entry(text):
        return TextEntry(file_id="f", key_path="k", original=text,
                         status=STATUS_SKIPPED,
                         meta={"kind": "typetree_candidate",
                               "role": "candidate"})

    # 真显示文本可升格
    assert _verify_upgradeable(entry("Combat Music"))
    assert _verify_upgradeable(entry("Open the gate"))
    # 键风格标识符：模型判 display 也不放行（写回 immutable 拦截前置）
    assert not _verify_upgradeable(entry("ui_newGame"))
    assert not _verify_upgradeable(entry("MENU_PLAY"))
    assert not _verify_upgradeable(entry("icon_sword_01"))
    # 结构串终检（_final_structural_backstop）：URL/程序集名 AI 无权推翻
    assert not _verify_upgradeable(entry("https://example.com/cfg"))
    assert not _verify_upgradeable(entry("GameMaster, Assembly-CSharp"))
    assert not _verify_upgradeable(entry("12345"))


def test_upgraded_entry_passes_actionable_gate():
    """升格后的条目必须真正进入翻译池（is_actionable_translation）。"""
    entry = TextEntry(
        file_id="f", key_path="k", original="Combat Music",
        status="pending",
        meta={"kind": "ai_upgraded", "role": "display",
              "disposition": "translate", "confidence": "medium",
              "confidence_promoted": True, "ai_verdict": "display"})
    assert is_actionable_translation(entry)


# ── verdict 应用 ────────────────────────────────────────────────────────

def test_apply_verdicts_upgrade_and_confirm_and_precheck():
    items = [
        TextEntry(file_id="f", key_path="k1", original="Combat Music",
                  status=STATUS_SKIPPED,
                  meta={"kind": "typetree_candidate", "role": "candidate",
                        "field_path": ["m_Text"], "obj": 7}),
        TextEntry(file_id="f", key_path="k2", original="MENU_PLAY",
                  status=STATUS_SKIPPED,
                  meta={"kind": "typetree_prefilter", "role": "candidate"}),
        TextEntry(file_id="f", key_path="k3", original="Canvas/HUD",
                  status=STATUS_SKIPPED,
                  meta={"kind": "typetree_candidate", "role": "candidate"}),
    ]
    pending = _PendingWrites()
    _apply_verdicts(items, {0: "display", 1: "display", 2: "structural"},
                    pending)
    # k1 升格缓冲：meta 含 role=display/disposition=translate，status→pending
    assert ("f", "k1") in [(fid, kp) for fid, kp, _ in pending.meta_rows]
    upgrade_row = next(row for row in pending.meta_rows if row[1] == "k1")
    assert upgrade_row[2]["role"] == "display"
    assert upgrade_row[2]["disposition"] == "translate"
    assert upgrade_row[2]["confidence_promoted"] is True
    assert ("f", "k1", "pending") in pending.status_rows
    # k2 键风格被预校验拦下（状态不动 + muted 留档）
    assert pending.precheck_blocked == 1
    assert ("f", "k2") in [(fid, kp) for fid, kp, _ in pending.meta_rows]
    assert not any(kp == "k2" for _, kp, _ in pending.status_rows)
    # k3 确认跳过：muted 留档，状态不动
    assert pending.confirmed == 1
    assert not any(kp == "k3" for _, kp, _ in pending.status_rows)
    assert pending.upgraded == 1


def test_apply_verdicts_missing_index_keeps_silent():
    """模型漏判的条目：不缓冲任何写入（宁漏勿坏）。"""
    items = [TextEntry(file_id="f", key_path="k1", original="Combat Music",
                       status=STATUS_SKIPPED,
                       meta={"kind": "typetree_candidate",
                             "role": "candidate"})]
    pending = _PendingWrites()
    _apply_verdicts(items, {}, pending)
    assert not pending.meta_rows and not pending.status_rows
    assert pending.upgraded == 0


# ── 主入口端到端 ────────────────────────────────────────────────────────

def test_run_ai_recognition_upgrades_and_mutes(store):
    _write_candidates(store, [
        _candidate_row("k1", "Combat Music"),
        _candidate_row("k2", "MENU_PLAY"),
        _candidate_row("k3", "https://example.com/cfg"),
        _candidate_row("k4", "Canvas/HUD"),
    ])
    service = _FakeService(outputs=[
        '[{"i": 0, "v": "display"}, {"i": 1, "v": "display"}, '
        '{"i": 2, "v": "display"}, {"i": 3, "v": "structural"}]'])
    report = run_ai_recognition(store, ".", service=service)
    assert not report.degraded
    assert report.scanned == 4 and report.asked == 4
    assert report.upgraded == 1
    assert report.precheck_blocked == 2
    assert report.confirmed_skipped == 1
    # k1 真正进入翻译池
    row1 = next(r for r in store.get_entries() if r["key_path"] == "k1")
    entry1 = entry_from_row(row1)
    assert row1["status"] == "pending"
    assert is_actionable_translation(entry1)
    assert entry1.meta["role"] == "display"
    assert entry1.meta["disposition"] == "translate"
    # k2/k3/k4 保持 skipped
    for key in ("k2", "k3", "k4"):
        row = next(r for r in store.get_entries() if r["key_path"] == key)
        assert row["status"] == "skipped"
    # 第二轮：muted 短路，零询问
    report2 = run_ai_recognition(store, ".", service=_FakeService())
    assert report2.scanned == 0 and report2.asked == 0


def test_run_ai_recognition_fail_closed_on_transport_error(store):
    _write_candidates(store, [_candidate_row("k1", "Combat Music")])
    service = _FakeService(error=RuntimeError("transport down"))
    report = run_ai_recognition(store, ".", service=service)
    assert not report.degraded   # 单批失败不计 degraded（偶发可重试）
    assert report.upgraded == 0
    row = next(r for r in store.get_entries() if r["key_path"] == "k1")
    assert row["status"] == "skipped"
    assert "ai_verdict" not in str(row["meta"])


def test_run_ai_recognition_fail_closed_on_empty_output(store):
    _write_candidates(store, [_candidate_row("k1", "Combat Music")])
    report = run_ai_recognition(store, ".",
                                service=_FakeService(outputs=[""]))
    assert report.upgraded == 0
    row = next(r for r in store.get_entries() if r["key_path"] == "k1")
    assert row["status"] == "skipped"


def test_run_ai_recognition_degraded_when_model_missing(store, tmp_path,
                                                        monkeypatch):
    _write_candidates(store, [_candidate_row("k1", "Combat Music")])
    # service=None 分支：ReviewModelService 是函数内延迟导入——
    # patch 源头 review_server.ReviewModelService 的 _spec 让模型
    # 「缺失」，触发 degraded（不触发启动/探测）
    monkeypatch.setattr(
        "hanhua.core.review_server.ReviewModelService._spec",
        lambda self: type("S", (), {"is_available": False,
                                    "path": tmp_path / "missing.gguf"})())
    report = run_ai_recognition(store, tmp_path)
    assert report.degraded
    assert "缺失" in report.error
    assert report.upgraded == 0
    row = next(r for r in store.get_entries() if r["key_path"] == "k1")
    assert row["status"] == "skipped"
    assert "ai_verdict" not in str(row["meta"])


def test_run_ai_recognition_empty_pool_noop(store):
    report = run_ai_recognition(store, ".", service=_FakeService())
    assert report.scanned == 0 and report.asked == 0
    assert not report.degraded
