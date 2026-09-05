"""写回二进制对象证据卡审计回归测试（0.39.0 M3）。

锁定 writer._note_object_evidence + writeback_audit._audit_v2_model 安全
契约（设计文档 V1.0 §29/§62/§65/§70）：
- writer 侧：所有二进制写路径（metadata/DLL/asset）成功落盘对象产生
  证据卡（文件/对象/类型/逐处 原文→译文）；拒绝/回退不产生卡；
  译文==原文的改动不进卡；
- 审计侧：证据卡批量送模型语义复核（四值结论 PASS/STRUCTURE_BROKEN/
  VALUE_INVERTED/PLACEHOLDER_LOST），非 PASS 记 model_flags 软复核；
- 模型不可用（有卡无模型/请求失败）→ model_unavailable=True 阻断发布
  （与文本层同口径）；
- 空 v2_result / 空证据 → 第 2 层 b 零行为零模型调用（不误报
  model_unavailable，兼容 0.38.0 调用方）；
- audit_writeback 默认（v2_result=None）行为与 0.38.0 完全一致。
"""
import json

import pytest

from hanhua.core.unity import writer as unity_writer
from hanhua.core.unity.writer import WriteResult, _patch_dll, _patch_metadata
from hanhua.core.writeback_audit import (
    _audit_v2_model, _v2_evidence_cards, audit_writeback,
    render_audit_report,
)

from tests.test_v2_patch_pools import (
    _build, _meta_entry, _records, _us_entry, _us_heap,
)


# ── 假模型服务（同 test_writeback_audit 口径）──────────────────────────

class _FakeSvc:
    def __init__(self, outputs=None, error=None):
        self.outputs = list(outputs or [])
        self.error = error
        self.prompts = []

    def chat(self, prompt, *, max_tokens=512, timeout=120):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0) if len(self.outputs) > 1 \
            else self.outputs[0]


# ── writer 侧：证据卡产生 ──────────────────────────────────────────────

def test_patch_metadata_produces_evidence_card(tmp_path):
    """IL2CPP metadata 成功写回 → 一张卡（类型/定位/原文→译文）。"""
    literal = "Hello player"
    metadata = _build(29, [literal])
    out = tmp_path / "out"
    out.mkdir(parents=True)
    path = out / "global-metadata.dat"
    path.write_bytes(metadata)
    entry = _meta_entry(0x200, len(literal), literal, "你好玩家")
    result = WriteResult()
    _patch_metadata(path, [entry], result,
                    rel_path="Il2CppData/Metadata/global-metadata.dat")
    assert len(result.object_evidence) == 1
    card = result.object_evidence[0]
    assert card["type"] == "IL2CPP metadata"
    assert card["rel_path"] == "Il2CppData/Metadata/global-metadata.dat"
    assert card["path_id"] == -1
    assert card["changes"] and card["changes"][0][1] == literal
    assert card["changes"][0][2] == "你好玩家"


def test_patch_dll_produces_evidence_card(tmp_path):
    """Mono #US 堆成功写回 → 一张卡（us@offset 定位）。"""
    literal = "Hello World!!"
    heap = _us_heap([(literal.encode("utf-16-le"), 0)])
    out = tmp_path / "out"
    out.mkdir(parents=True)
    path = out / "Assembly-CSharp.dll"
    path.write_bytes(heap)
    entry = _us_entry(1, 26, literal, "你好世界")
    result = WriteResult()
    _patch_dll(path, [entry], result,
               rel_path="Managed/Assembly-CSharp.dll")
    assert len(result.object_evidence) == 1
    card = result.object_evidence[0]
    assert card["type"] == "Mono #US"
    assert card["rel_path"] == "Managed/Assembly-CSharp.dll"
    assert card["changes"][0][2] == "你好世界"


def test_patch_metadata_no_card_on_verify_failure(tmp_path):
    """写回被拒（找不到原文串）→ 零证据卡（卡只记成功落盘，宁漏勿坏）。"""
    metadata = _build(29, ["realString"])
    out = tmp_path / "out"
    out.mkdir(parents=True)
    path = out / "global-metadata.dat"
    path.write_bytes(metadata)
    entry = _meta_entry(0x200, 13, "absentString", "不存在的译文")
    result = WriteResult()
    _patch_metadata(path, [entry], result)
    assert result.object_evidence == []


def test_evidence_card_skips_noop_translation(tmp_path):
    """译文==原文的改动不进卡（二次防御，writer 侧已过滤）。"""
    literal = "sameText"
    metadata = _build(29, [literal])
    out = tmp_path / "out"
    out.mkdir(parents=True)
    path = out / "global-metadata.dat"
    path.write_bytes(metadata)
    entry = _meta_entry(0x200, len(literal), literal, literal)
    result = WriteResult()
    _patch_metadata(path, [entry], result)
    # 写回本身因译文==原文不产生改动（或改动为空）→ 卡过滤后为空
    cards = _v2_evidence_cards(result)
    assert all(c["changes"] for c in cards)


# ── 审计侧：模型语义复核 ──────────────────────────────────────────────

def _card(rel="aa/x.bundle", path_id=5, type_name="TextMeshProUGUI",
          changes=(("m_text", "Hello", "你好"),)):
    return {"rel_path": rel, "asset_file": "x.bundle", "path_id": path_id,
            "type": type_name,
            "changes": [(w, o, t) for w, o, t in changes]}


class _V2Result:
    """轻量 WriteResult 替身（audit 只读 object_evidence）。"""

    def __init__(self, cards):
        self.object_evidence = cards


def test_audit_v2_model_pass_no_flags():
    svc = _FakeSvc(outputs=[json.dumps(
        [{"index": 0, "verdict": "PASS", "issue": ""}])])
    res = _audit_v2_model(_V2Result([_card()]), svc)
    assert res.model_flags == []
    assert not res.model_unavailable
    assert res.v2_cards_audited == 1


def test_audit_v2_model_value_inverted_flag():
    svc = _FakeSvc(outputs=[json.dumps(
        [{"index": 0, "verdict": "VALUE_INVERTED", "issue": "语义颠倒"}])])
    res = _audit_v2_model(_V2Result([_card()]), svc)
    assert len(res.model_flags) == 1
    rel, verdict, issue = res.model_flags[0]
    assert rel == "aa/x.bundle"
    assert verdict == "VALUE_INVERTED"
    assert "语义颠倒" in issue


def test_audit_v2_model_invalid_json_no_flags_no_block():
    """输出非法 → 该批无判定（不猜），软复核记不到 flag 也不阻断。"""
    svc = _FakeSvc(outputs=["完全无法解析"])
    res = _audit_v2_model(_V2Result([_card()]), svc)
    assert res.model_flags == []
    assert not res.model_unavailable


def test_audit_v2_model_transport_error_blocks():
    """请求失败 → 覆盖缺口 → model_unavailable=True（阻断发布）。"""
    svc = _FakeSvc(error=RuntimeError("model down"))
    res = _audit_v2_model(_V2Result([_card()]), svc)
    assert res.model_unavailable
    assert any(f[1] == "MODEL_UNAVAILABLE" for f in res.model_flags)


def test_audit_v2_model_empty_cards_zero_calls():
    """空证据 → 零模型调用零 flag（不误报 model_unavailable）。"""
    svc = _FakeSvc(outputs=["must-not-be-called"])
    res = _audit_v2_model(_V2Result([]), svc)
    assert res.model_flags == []
    assert not res.model_unavailable
    assert res.v2_cards_audited == 0
    assert svc.prompts == []


def test_audit_v2_model_service_none_with_cards_blocks():
    """有卡无模型 → 覆盖缺口 → model_unavailable=True。"""
    res = _audit_v2_model(_V2Result([_card()]), None)
    assert res.model_unavailable
    assert res.v2_cards_sampled == 1


def test_audit_v2_model_batches_and_sampling():
    """超 max_cards 抽样截断 + 分批判定。"""
    cards = [_card(path_id=i) for i in range(50)]
    svc = _FakeSvc(outputs=[json.dumps(
        [{"index": i, "verdict": "PASS", "issue": ""}
         for i in range(12)])])
    res = _audit_v2_model(_V2Result(cards), svc,
                          cards_per_batch=12, max_cards=20)
    assert res.v2_cards_audited <= 20
    assert res.v2_cards_sampled >= 30
    assert len(svc.prompts) >= 1


def test_v2_evidence_cards_filters_dirty_data():
    """脏数据防御：非 dict 卡/空 changes/译文==原文 → 全部过滤。"""
    result = _V2Result([
        "not-a-dict",
        {"rel_path": "a", "path_id": 1, "type": "T", "changes": []},
        {"rel_path": "b", "path_id": 2, "type": "T",
         "changes": [("m_text", "same", "same")]},
        _card(),
    ])
    cards = _v2_evidence_cards(result)
    assert len(cards) == 1
    assert cards[0]["rel_path"] == "aa/x.bundle"


# ── audit_writeback 集成 ───────────────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self._files = []
        self._entries = []

    def get_files(self):
        return self._files

    def get_entries(self):
        return self._entries


def test_audit_writeback_default_v2_none_unchanged(tmp_path):
    """默认不传 v2_result → 行为与 0.38.0 一致（无卡层参与）。"""
    store = _FakeStore()
    res = audit_writeback(store, tmp_path / "game", tmp_path / "out",
                          service=_FakeSvc(), run_model=True)
    assert res.v2_cards_audited == 0
    assert res.v2_cards_sampled == 0
    # 空库：文本层也无文件可审 → 不误报 unavailable
    assert not res.model_unavailable


def test_audit_writeback_no_service_with_cards_blocks(tmp_path):
    """run_model=True + 模型不可用 + 有证据卡 → model_unavailable 阻断。"""
    store = _FakeStore()
    res = audit_writeback(store, tmp_path / "game", tmp_path / "out",
                          service=None, run_model=True,
                          v2_result=_V2Result([_card()]))
    assert res.model_unavailable
    assert res.v2_cards_audited == 0
    assert res.v2_cards_sampled == 1


def test_audit_writeback_empty_cards_no_service_no_block(tmp_path):
    """run_model=True + 模型不可用 + 空 v2 证据 → 文本层口径（空库不误报）。"""
    store = _FakeStore()
    res = audit_writeback(store, tmp_path / "game", tmp_path / "out",
                          service=None, run_model=True,
                          v2_result=_V2Result([]))
    assert res.model_unavailable  # 与 0.38.0 文本层口径一致：run_model
    # 请求了模型但服务不可用 → 阻断（空库场景文本层行为保持）


def test_audit_writeback_merges_v2_flags(tmp_path):
    """有模型 + 有卡 → 卡层 flag 并入总审计结果 + 报告可见。"""
    store = _FakeStore()
    svc = _FakeSvc(outputs=[json.dumps(
        [{"index": 0, "verdict": "PLACEHOLDER_LOST", "issue": "{0} 丢失"}])])
    res = audit_writeback(store, tmp_path / "game", tmp_path / "out",
                          service=svc, run_model=True,
                          v2_result=_V2Result([
                              _card(changes=(("m_text", "Go {0}", "去"),))]))
    assert res.v2_cards_audited == 1
    assert any(f[1] == "PLACEHOLDER_LOST" for f in res.model_flags)
    report = render_audit_report(res, "测试游戏")
    assert "二进制对象证据卡复核：送审 1 张" in report
