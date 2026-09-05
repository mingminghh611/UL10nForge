"""写回预演（Dry Run）回归测试（0.39.0 M4，设计文档 §46/§62）。

锁定 writeback_plan.build_writeback_plan 安全契约：
- 零副作用：不写文件、不建目录、不改 store（含 AI 分诊 store=None
  不落判定缓存）；
- 分类链与 write_back_v2 / write_back_text 单一来源：disposition 闸门
  拒绝、键保护丢弃、warn/revert 形态分类、typetree 语义回退预测、
  AI 分诊 review/reject 计数；
- §62 输出口径：预计写回 / 需要人工 / 拒绝 / 高风险（含自动回退）；
- 分诊默认关闭（不传参数零模型参与，与正式写回一致）；
- 分诊降级（模型缺席/异常）只标注不阻断预演。
"""
import json
import os

import pytest

from hanhua.core.memory import ProjectStore
from hanhua.core.unity.writeback_plan import (
    WritebackPlan, build_writeback_plan,
)

from tests.test_writeback_ai_triage import _FakeService


def _store(tmp_path):
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    return store


def _v2_entry(key_path, original, translation, *, file_id="f1", meta=None):
    m = {"kind": "typetree", "role": "display",
         "disposition": "translate", "obj_has_values": True,
         "field_path": ["m_text"],
         "quality_passed": True, "confidence": "medium"}
    m.update(meta or {})
    return {"file_id": file_id, "key_path": key_path,
            "original": original, "translation": translation,
            "status": "translated",
            "meta": json.dumps(m, ensure_ascii=False)}


def _text_entry(key_path, original, translation, *, file_id="t1"):
    return {"file_id": file_id, "key_path": key_path,
            "original": original, "translation": translation,
            "status": "translated",
            "meta": json.dumps({"disposition": "translate",
                                "quality_passed": True,
                                "confidence": "medium"},
                               ensure_ascii=False)}


def _add_files(store):
    store.add_file("f1", "aa/x.bundle", "v2_asset", "binary", "")
    store.add_file("t1", "dialog.csv", "csv", "text", "")


def _seed(store, rows):
    """upsert_entries 新行译文恒为空（memory.py upsert 协议），译文必须走
    update_translation 落库——与真实翻译管线同路径，且顺带写入
    quality_passed/confidence_promoted 质检元数据（is_write_ready 闸门依赖）。
    """
    store.upsert_entries(rows)
    for r in rows:
        if r.get("translation"):
            store.update_translation(r["file_id"], r["key_path"],
                                     r["translation"])


# ── 零副作用 ──────────────────────────────────────────────────────────────

def test_dry_run_no_disk_side_effects(tmp_path):
    """预演不建目录不写文件——§62「分析写回但不修改游戏」。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "Hello", "你好"),
        _text_entry("r/1", "Save the world", "拯救世界"),
    ])
    before = {p: p.stat().st_mtime for p in tmp_path.rglob("*")}
    game = tmp_path / "game"
    game.mkdir()
    before = {p: p.stat().st_mtime for p in tmp_path.rglob("*")}
    plan = build_writeback_plan(store, game)
    after = {p: p.stat().st_mtime for p in tmp_path.rglob("*")}
    assert plan.planned_total == 2
    assert set(before) == set(after)  # 无新文件/目录
    assert before == after            # 无文件被改写


def test_dry_run_no_store_mutation_with_triage(tmp_path):
    """AI 分诊以 store=None 运行：判定缓存不落库（预演无副作用）。"""
    from hanhua.core.unity.writeback_ai_triage import _MUTED
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐")])
    svc = _FakeService(outputs=['[{"i": 0, "v": "review"}]'])
    plan = build_writeback_plan(store, tmp_path / "game",
                                triage_service=svc)
    assert plan.needs_review == 1
    row = next(r for r in store.get_entries() if r["key_path"] == "k1")
    meta = row["meta"] if isinstance(row["meta"], dict) \
        else json.loads(row["meta"])
    assert _MUTED not in meta  # 缓存未落库


# ── 文本侧分类 ────────────────────────────────────────────────────────────

def test_text_key_protection_drops_translation(tmp_path):
    """键名/键字段条目：真实写回丢弃译文保留原文 → 预演计 text_dropped。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _text_entry("cfg/saveButtonKey", "saveButton", "保存按钮"),
        _text_entry("r/1", "Save the world", "拯救世界"),
    ])
    plan = build_writeback_plan(store)
    assert plan.text_dropped == 1
    assert plan.text_planned == 1
    assert "saveButton" in "；".join(plan.text_dropped_items)


def test_text_file_counted_once(tmp_path):
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _text_entry("r/1", "Hello", "你好"),
        _text_entry("r/2", "World", "世界"),
    ])
    plan = build_writeback_plan(store)
    assert plan.text_planned == 2
    assert plan.text_files == 1


# ── v2 侧分类 ─────────────────────────────────────────────────────────────

def test_v2_disposition_gate_rejects(tmp_path):
    """disposition != translate → 拒绝（与 _should_write_entry 同口径）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "Hello", "你好",
                  meta={"disposition": "skipped"}),
        _v2_entry("k2", "World", "世界"),
    ])
    plan = build_writeback_plan(store)
    assert plan.rejected == 1
    assert plan.v2_planned == 1
    assert "disposition_skipped" in "；".join(plan.rejected_items)


def test_v2_noop_translation_not_counted(tmp_path):
    """译文==原文 → 不产生计数（与 write_back_v2 candidates 同条件）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [_v2_entry("k1", "Hello", "Hello")])
    plan = build_writeback_plan(store)
    assert plan.v2_planned == 0
    assert plan.rejected == 0


def test_v2_warn_pattern_high_risk(tmp_path):
    """camelCase 形态（非键环境）→ warn 级高风险（真实写回照写）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐")])
    plan = build_writeback_plan(store)
    assert plan.high_risk == 1
    assert plan.v2_planned == 0
    assert "combatMusic" in "；".join(plan.high_risk_items)


def test_v2_revert_pattern_auto_revert(tmp_path):
    """warn 形态 + 键环境对象 → revert 级（真实写回确定性回退）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐",
                  meta={"obj_is_key_list": True})])
    plan = build_writeback_plan(store)
    assert plan.auto_revert == 1
    assert plan.v2_planned == 0


def test_v2_typetree_semantic_revert_predicted(tmp_path):
    """typetree 条目命中 UnityEvent 绑定字段 → 预演预测自动回退
    （真实写回对象循环里 typetree_logic_key_evidence 确定性回退）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "OnButtonClick", "按钮点击回调",
                  meta={"field_path": ["m_MethodName"]})])
    plan = build_writeback_plan(store)
    assert plan.auto_revert == 1
    assert "unityevent" in "；".join(plan.revert_items)


def test_v2_normal_display_planned(tmp_path):
    """普通显示文本（无形态命中）→ 预计写回。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "Hello, traveler!", "你好，旅行者！")])
    plan = build_writeback_plan(store)
    assert plan.v2_planned == 1
    assert plan.high_risk == 0 and plan.auto_revert == 0


# ── AI 分诊 ───────────────────────────────────────────────────────────────

def test_triage_review_counts_needs_attention(tmp_path):
    """分诊 review → 需要人工（真实写回保守回退）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐"),
        _v2_entry("k2", "shopKeeperDialogue", "店主对话")])
    svc = _FakeService(outputs=[
        '[{"i": 0, "v": "review"}, {"i": 1, "v": "allow"}]'])
    plan = build_writeback_plan(store, triage_service=svc)
    assert plan.needs_review == 1
    # allow 的 warn 级条目照旧计高风险（分诊放行 ≠ 审计形态豁免）
    assert plan.high_risk == 1
    assert plan.v2_planned == 0


def test_triage_reject_counts_rejected(tmp_path):
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐")])
    svc = _FakeService(outputs=['[{"i": 0, "v": "reject"}]'])
    plan = build_writeback_plan(store, triage_service=svc)
    assert plan.rejected == 1
    assert "AI 分诊拒绝" in "；".join(plan.rejected_items)


def test_triage_disabled_by_default(tmp_path, monkeypatch):
    """不传分诊参数 → 分诊层零参与（与正式写回默认一致）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐")])
    called = []
    monkeypatch.setattr(
        "hanhua.core.unity.writeback_ai_triage.run_writeback_triage",
        lambda *a, **k: called.append(True) or ({}, None),
        raising=False)
    plan = build_writeback_plan(store)
    assert not called
    assert plan.triage_degraded is False
    assert plan.needs_review == 0


def test_triage_degraded_noted_not_blocking(tmp_path):
    """模型传输失败 → 预演标注降级，不阻断；受影响条目按正式写回口径
    fail-closed 保守回退 → 计需要人工（非高风险）。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐")])
    svc = _FakeService(error=RuntimeError("model down"))
    plan = build_writeback_plan(store, triage_service=svc)
    assert plan.triage_degraded
    assert "model down" in plan.triage_note
    # fail-closed：请求失败条目 review 跳过 → 需要人工（保守回退保留原文）
    assert plan.needs_review == 1
    assert plan.high_risk == 0


def test_triage_exception_falls_back_to_form_classification(tmp_path):
    """分诊层异常 → run_writeback_triage 内部 fail-closed 同样 review
    跳过受影响条目（异常不外泄），预演计需要人工 + 标注降级。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "combatMusic", "战斗音乐")])
    class _Boom:
        def chat(self, *a, **k):
            raise RuntimeError("triage exploded")
    plan = build_writeback_plan(store, triage_service=_Boom())
    assert plan.triage_degraded
    assert plan.needs_review == 1
    assert plan.high_risk == 0


# ── 报告（§62 输出口径）──────────────────────────────────────────────────

def test_summary_contains_four_categories(tmp_path):
    """§62 输出：预计写回 / 需要人工 / 拒绝 / 高风险 + 未覆盖说明。"""
    store = _store(tmp_path)
    _add_files(store)
    _seed(store, [
        _v2_entry("k1", "Hello, traveler!", "你好，旅行者！"),
        _v2_entry("k2", "combatMusic", "战斗音乐"),
        _v2_entry("k3", "Skipped", "跳过",
                  meta={"disposition": "skipped"}),
        _text_entry("r/1", "Save the world", "拯救世界"),
    ])
    plan = build_writeback_plan(store)
    text = plan.summary()
    assert "预计写回：2" in text
    assert "需要人工：0" in text
    assert "拒绝：1" in text
    assert "高风险：1" in text
    assert "预演未覆盖" in text
    assert "正式写回" in text


def test_empty_store_zero_plan(tmp_path):
    store = _store(tmp_path)
    _add_files(store)
    plan = build_writeback_plan(store)
    assert plan.planned_total == 0
    assert plan.rejected == 0
    assert "预计写回：0" in plan.summary()


def test_unsafe_rel_path_raises(tmp_path):
    """路径逃逸 rel_path → 预演失败（与正式写回同源校验，提前暴露）。"""
    from hanhua.core.memory import ProjectStore as PS
    store = PS(tmp_path / "p.db")
    store.init_schema()
    store.add_file("f1", "../escape.bundle", "v2_asset", "binary", "")
    game = tmp_path / "game"
    game.mkdir()
    with pytest.raises(Exception):
        build_writeback_plan(store, game)
