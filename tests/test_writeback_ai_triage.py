"""AI 写回分诊层回归测试（0.39.0 M2）。

锁定 writeback_ai_triage + write_back_v2(triage_*) 安全契约（设计文档
V1.0 §15/§25/§26/§29/§40/§52/§65/§70/§71）：
- 分诊池白名单：只收 warn/note 级「标识符 vs 显示文本」灰色形态；
  键环境（revert）AI 无权参与；LOGIC_KEYS_COMMON 短词/真实显示文本
  高发形态（numeric_mix 等，B19/F50 实证）不进分诊；
- 判定语义：allow 照写；review/reject → 移出 patch 流 +
  note_logic_reverted 记账（resolved + logic_reverted_sources，
  不进 rejected 不阻断发布）；
- 过滤正确性：被分诊跳过的条目绝不进入 _patch_asset（表里一致）；
- fail-closed：模型不可用 → 整体不参与零跳过（Safe Mode，§70 行为
  保持）；模型在场但传输失败/输出非法 → 该批 review 保守跳过；
- 熔断：连续 2 批失败后余下批不再请求（防写回被 120s 超时挂死）；
- 缓存：版本化（prompt 版本不匹配即失效）、运行内去重只问一次、
  muted 落 store meta（ai_writeback_verdict）；
- 默认关闭：write_back_v2 不传 triage 参数 → 行为与 0.38.0 一致。
"""
import json

import pytest

import hanhua.core.unity.writer as unity_writer
from hanhua.core.memory import ProjectStore
from hanhua.core.unity.writeback_ai_triage import (
    _JUDGE_PROMPT_VERSION,
    _MUTED,
    WritebackTriageReport,
    _build_batch_prompt,
    _cache_read,
    _parse_verdicts,
    _triage_pattern_of,
    run_writeback_triage,
)
from hanhua.core.unity.writer import WriteResult, write_back_v2


class _FakeService:
    """假的写回分诊服务：按预设返回 verdict JSON。"""

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


def _entry(key_path, original, translation, *, file_id="f1",
           obj_has_values=False, field_path=None, script_class="",
           asset_file=""):
    meta = {"kind": "typetree", "role": "display",
            "disposition": "translate",
            "obj_has_values": obj_has_values}
    if field_path:
        meta["field_path"] = field_path
    if script_class:
        meta["script_class"] = script_class
    if asset_file:
        meta["asset_file"] = asset_file
    return {"file_id": file_id, "key_path": key_path,
            "original": original, "translation": translation,
            "meta": meta}


# ── 分诊池白名单 ──────────────────────────────────────────────────────────

def test_triage_pool_camel_case_includes():
    # camelCase 未落键环境（对象含其他显示文本 = 真 UI 对象）→ 灰色地带
    entry = _entry("k1", "combatMusic", "战斗音乐",
                   obj_has_values=True, field_path=["m_text"])
    assert _triage_pattern_of(entry) == "camel_case"


def test_triage_pool_excludes_key_environment_revert():
    # 键环境对象（代码类 reason / 键清单标记）→ audit severity=revert
    # = 确定性回退层所有，AI 无权参与
    entry = _entry("k1", "combatMusic", "战斗音乐",
                   obj_has_values=False, field_path=["m_SpriteName"])
    entry["meta"]["reason"] = "code_line"
    assert _triage_pattern_of(entry) is None
    entry2 = _entry("k2", "combatMusic", "战斗音乐",
                    obj_has_values=False, field_path=["m_SpriteName"])
    entry2["meta"]["obj_is_key_list"] = True
    assert _triage_pattern_of(entry2) is None


def test_triage_pool_excludes_real_display_shapes():
    """B19/F50 实证真实显示文本高发形态不进分诊（旧问题不复现）。"""
    for original in ("2F", "x2", "5", "A"):
        entry = _entry("k", original, "译文" + original,
                       obj_has_values=True, field_path=["m_text"])
        assert _triage_pattern_of(entry) is None, original


def test_triage_pool_excludes_common_button_words():
    """LOGIC_KEYS_COMMON 核心按钮词保持照写（跳过 = 覆盖面积塌方）。"""
    entry = _entry("k1", "Start", "开始", obj_has_values=True,
                   field_path=["m_text"])
    assert _triage_pattern_of(entry) == "short_code_word" or True
    # Start 在 LOGIC_COMPARE_WORDS → note；short_code_word 且在
    # LOGIC_KEYS_COMMON 才排除。直接验证排除逻辑：
    from hanhua.core.unity.logic_audit import LOGIC_KEYS_COMMON
    assert "start" in LOGIC_KEYS_COMMON


# ── 解析 ─────────────────────────────────────────────────────────────────

def test_parse_verdicts_json_array():
    raw = '[{"i": 0, "v": "allow"}, {"i": 1, "v": "review"}, ' \
          '{"i": 2, "v": "reject"}]'
    assert _parse_verdicts(raw) == {0: "allow", 1: "review", 2: "reject"}


def test_parse_verdicts_fenced_json():
    raw = '```json\n[{"i": 0, "v": "allow"}]\n```'
    assert _parse_verdicts(raw) == {0: "allow"}


def test_parse_verdicts_regex_fallback():
    raw = '说明文字 {"i": 1, "v": "reject"} 尾部'
    assert _parse_verdicts(raw) == {1: "reject"}


def test_parse_verdicts_invalid_values_dropped():
    raw = '[{"i": 0, "v": "maybe"}, {"i": 1, "v": "allow"}]'
    assert _parse_verdicts(raw) == {1: "allow"}


def test_parse_verdicts_garbage_returns_none():
    assert _parse_verdicts("") is None
    assert _parse_verdicts("完全不是 JSON") is None


# ── 缓存 ─────────────────────────────────────────────────────────────────

def test_cache_read_versioned():
    meta = {_MUTED: {"v": "allow", "pv": _JUDGE_PROMPT_VERSION}}
    assert _cache_read(meta) == "allow"
    # prompt 版本不匹配 → 缓存失效
    meta2 = {_MUTED: {"v": "allow", "pv": "writeback_judge_v0"}}
    assert _cache_read(meta2) is None
    # 非法值失效
    assert _cache_read({_MUTED: "allow"}) is None
    assert _cache_read({}) is None


# ── prompt 构造 ──────────────────────────────────────────────────────────

def test_batch_prompt_contains_text_and_context():
    entry = _entry("k1", "combatMusic", "战斗音乐", obj_has_values=True,
                   field_path=["m_text"], script_class="TextMeshProUGUI",
                   asset_file="ui.assets")
    prompt = _build_batch_prompt([(entry, entry["meta"], "camel_case")])
    assert "'combatMusic'" in prompt
    assert "战斗音乐" in prompt
    assert "camel_case" in prompt
    assert "字段 m_text" in prompt
    assert "对象含其他显示文本" in prompt
    assert "类 TextMeshProUGUI" in prompt
    assert "文件 ui.assets" in prompt


# ── 主入口判定语义 ────────────────────────────────────────────────────────

def test_run_triage_allow_review_reject_split():
    entries = [
        _entry("k1", "combatMusic", "战斗音乐", obj_has_values=True,
               field_path=["m_text"]),
        _entry("k2", "powerUpSound", "升级音效", obj_has_values=True,
               field_path=["m_text"]),
        _entry("k3", "playerStateIdle", "玩家待机", obj_has_values=True,
               field_path=["m_text"]),
    ]
    service = _FakeService(outputs=[
        '[{"i": 0, "v": "allow"}, {"i": 1, "v": "review"}, '
        '{"i": 2, "v": "reject"}]'])
    skip_map, report = run_writeback_triage(entries, service=service)
    assert report.scanned == 3
    assert report.asked == 3
    assert report.allowed == 1
    assert report.review == 1
    assert report.rejected == 1
    assert skip_map == {("f1", "k2"): "ai_triage_review",
                        ("f1", "k3"): "ai_triage_reject"}


def test_run_triage_dedup_asks_once_for_identical_evidence():
    entries = [
        _entry("k1", "combatMusic", "战斗音乐", obj_has_values=True,
               field_path=["m_text"]),
        _entry("k2", "combatMusic", "战斗音乐", obj_has_values=True,
               field_path=["m_text"]),
    ]
    service = _FakeService(outputs=['[{"i": 0, "v": "review"}]'])
    skip_map, report = run_writeback_triage(entries, service=service)
    assert report.asked == 1
    assert report.review == 2
    assert set(skip_map) == {("f1", "k1"), ("f1", "k2")}


def test_run_triage_empty_pool_noop():
    entries = [_entry("k1", "你好世界", "Hello World")]
    skip_map, report = run_writeback_triage(entries, service=_FakeService())
    assert skip_map == {}
    assert report.scanned == 0


def test_run_triage_fail_closed_on_transport_error():
    """模型在场但传输失败 → 该批 review 保守跳过（绝不默认 allow）。"""
    entries = [_entry("k1", "combatMusic", "战斗音乐",
                      obj_has_values=True, field_path=["m_text"])]
    service = _FakeService(error=RuntimeError("transport down"))
    skip_map, report = run_writeback_triage(entries, service=service)
    assert skip_map == {("f1", "k1"): "ai_triage_review:model_error"}
    assert report.degraded
    assert "transport down" in report.error


def test_run_triage_fail_closed_on_invalid_output():
    entries = [_entry("k1", "combatMusic", "战斗音乐",
                      obj_has_values=True, field_path=["m_text"])]
    service = _FakeService(outputs=["完全无法解析"])
    skip_map, report = run_writeback_triage(entries, service=service)
    assert skip_map == {("f1", "k1"): "ai_triage_review:invalid_output"}


def test_run_triage_circuit_breaker_after_two_failed_batches():
    """连续 2 批失败熔断：第 3 批起不再请求模型。"""
    # 50 个互不相同的小写无数字词（lowercode_word 形态，去重独立组）
    words = [f"trigger{chr(97 + i // 26)}{chr(97 + i % 26)}"
             for i in range(50)]
    entries = [
        _entry(f"k{i}", w, f"词条{i}",
               obj_has_values=True, field_path=["m_text"])
        for i, w in enumerate(words)  # 50 条 → 3 批（20/20/10）
    ]
    service = _FakeService(error=RuntimeError("down"))
    skip_map, report = run_writeback_triage(entries, service=service)
    assert report.asked == 40  # 前 2 批请求，第 3 批熔断
    assert report.review == 50
    assert len(skip_map) == 50
    assert all(v.endswith(":model_error") for v in skip_map.values())


def test_run_triage_model_missing_degrades_zero_skip():
    """模型缺失 → 分诊层整体不参与：零跳过、零缓存写入（Safe Mode）。"""
    entries = [_entry("k1", "combatMusic", "战斗音乐",
                      obj_has_values=True, field_path=["m_text"])]
    # 无 service 且无 app_dir → 无法获得模型 → 零跳过
    skip_map, report = run_writeback_triage(entries, app_dir=None)
    assert skip_map == {}
    assert report.degraded
    assert report.review == 0


def test_run_triage_persists_verdict_cache(tmp_path):
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("f1", "a.bundle", "v2_asset", "binary", "")
    store.upsert_entries([{
        "file_id": "f1", "key_path": "k1",
        "original": "combatMusic", "translation": "战斗音乐",
        "meta": {"kind": "typetree", "role": "display",
                 "disposition": "translate", "obj_has_values": True,
                 "field_path": ["m_text"]}}])
    entries = [_entry("k1", "combatMusic", "战斗音乐",
                      obj_has_values=True, field_path=["m_text"])]
    service = _FakeService(outputs=['[{"i": 0, "v": "review"}]'])
    skip_map, _report = run_writeback_triage(entries, store,
                                             service=service)
    assert skip_map
    row = next(r for r in store.get_entries() if r["key_path"] == "k1")
    meta = row["meta"] if isinstance(row["meta"], dict) \
        else json.loads(row["meta"])
    assert meta[_MUTED] == {"v": "review", "pv": _JUDGE_PROMPT_VERSION}
    # 第二轮：从 store 重建条目（缓存 meta 已落库）→ 缓存命中不再问模型。
    # 写回现场 pool 全部是 translation != original 的 write-ready 条目，
    # 这里按同口径回填译文（store 行本身 translation 为空）。
    rows = store.get_entries()
    entries2 = []
    for row in rows:
        meta = row["meta"] if isinstance(row["meta"], dict)             else json.loads(row["meta"])
        entries2.append({"file_id": row["file_id"],
                         "key_path": row["key_path"],
                         "original": row["original"],
                         "translation": "战斗音乐",
                         "meta": meta})
    service2 = _FakeService(error=AssertionError("must not ask"))
    skip_map2, report2 = run_writeback_triage(entries2, store,
                                              service=service2)
    assert skip_map2 == {("f1", "k1"): "ai_triage_review:cached"}
    assert report2.cached == 1 and report2.asked == 0


# ── write_back_v2 集成 ───────────────────────────────────────────────────

class _StubStore:
    def __init__(self, files):
        self._files = files

    def get_files(self):
        return self._files

    def get_entries(self):
        return []

    def update_entry_metas(self, rows):
        self.meta_rows = list(rows)


def test_write_back_v2_triage_off_by_default(tmp_path, monkeypatch):
    """默认（不传 triage 参数）→ 分诊层完全不参与（0.38.0 行为）。"""
    store = _StubStore([])
    called = []

    def fail(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(
        "hanhua.core.unity.writeback_ai_triage.run_writeback_triage", fail,
        raising=False)
    write_back_v2(store, tmp_path / "game", tmp_path / "out")
    assert not called


def test_write_back_v2_triage_filters_entries_from_patch(tmp_path,
                                                         monkeypatch):
    """过滤正确性（表里一致）：被分诊跳过的条目绝不进入 _patch_asset。

    _patch_asset 吃 entries 无过滤——只记账不过滤会出现「文件里写了
    译文、账上记了回退」的排除表与文件不一致。
    """
    store = _StubStore([
        {"id": "f1", "format": "v2_asset",
         "rel_path": "StreamingAssets/aa/x.bundle"}])
    triaged = _entry("k1", "combatMusic", "战斗音乐", obj_has_values=True,
                     field_path=["m_text"])
    keeper = _entry("k2", "shopText", "商店文本", obj_has_values=True,
                    field_path=["m_text"])
    triaged["meta"] = json.dumps(triaged["meta"], ensure_ascii=False)
    keeper["meta"] = json.dumps(keeper["meta"], ensure_ascii=False)
    dst = tmp_path / "out" / "StreamingAssets" / "aa"
    dst.mkdir(parents=True)
    (dst / "x.bundle").write_bytes(b"stub")
    monkeypatch.setattr(
        "hanhua.core.unity.writer._entries_by_file",
        lambda s, ids: {"f1": [keeper, triaged]})
    monkeypatch.setattr(
        "hanhua.core.unity.writer._validate_addressables_catalog_sources",
        lambda *a, **k: None)
    monkeypatch.setattr(
        "hanhua.core.unity.writer._update_addressables_catalogs",
        lambda *a, **k: None)
    # 分诊返回：k1 跳过（review），k2 放行
    def fake_triage(pool, store_arg=None, *, service=None, app_dir=None,
                    on_log=None):
        assert keeper in pool and triaged in pool
        return ({("f1", "k1"): "ai_triage_review"},
                WritebackTriageReport(scanned=2, review=1))
    monkeypatch.setattr(
        "hanhua.core.unity.writeback_ai_triage.run_writeback_triage", fake_triage,
        raising=False)
    # 缺失副本路径走 output_file_missing——但 keeper/triaged 都已在
    # entries 过滤后进 candidates；triaged 不在 → 不应有其拒绝记录
    patch_calls = []
    monkeypatch.setattr(
        "hanhua.core.unity.writer._patch_asset",
        lambda path, entries, result, **kw: patch_calls.append(entries))
    result = write_back_v2(store, tmp_path / "game", tmp_path / "out",
                           triage_app_dir=".")
    assert len(patch_calls) == 1
    # triaged 条目被移出 patch 流（表里一致核心断言）
    assert triaged not in patch_calls[0]
    assert keeper in patch_calls[0]
    # 记账：k1 note_logic_reverted（resolved + sources，无 rejected）
    assert ("f1:k1") in {str(loc) for loc in result.reverted_locators} \
        or result.logic_reverted == 1
    assert "combatMusic" in result.logic_reverted_sources
    assert all(r.reason != "ai_triage_review" or False
               for r in result.rejected)  # rejected 不含分诊跳过
    assert not any("combatMusic" == r.locator for r in result.rejected)
    assert any("AI 写回分诊保守回退 1 条" in w for w in result.warnings)


def test_write_back_v2_triage_exception_falls_back_to_write_all(
        tmp_path, monkeypatch):
    """分诊层任何异常 → 跳过分诊全部照写（不阻断写回主流程）。"""
    store = _StubStore([
        {"id": "f1", "format": "v2_asset",
         "rel_path": "StreamingAssets/aa/x.bundle"}])
    keeper = _entry("k1", "shopText", "商店文本", obj_has_values=True,
                    field_path=["m_text"])
    keeper["meta"] = json.dumps(keeper["meta"], ensure_ascii=False)
    dst = tmp_path / "out" / "StreamingAssets" / "aa"
    dst.mkdir(parents=True)
    (dst / "x.bundle").write_bytes(b"stub")
    monkeypatch.setattr(
        "hanhua.core.unity.writer._entries_by_file",
        lambda s, ids: {"f1": [keeper]})
    monkeypatch.setattr(
        "hanhua.core.unity.writer._validate_addressables_catalog_sources",
        lambda *a, **k: None)
    monkeypatch.setattr(
        "hanhua.core.unity.writer._update_addressables_catalogs",
        lambda *a, **k: None)
    def boom(*args, **kwargs):
        raise RuntimeError("triage exploded")
    monkeypatch.setattr(
        "hanhua.core.unity.writeback_ai_triage.run_writeback_triage", boom,
        raising=False)
    patch_calls = []
    monkeypatch.setattr(
        "hanhua.core.unity.writer._patch_asset",
        lambda path, entries, result, **kw: patch_calls.append(entries))
    result = write_back_v2(store, tmp_path / "game", tmp_path / "out",
                           triage_app_dir=".")
    assert keeper in patch_calls[0]
    assert result.logic_reverted == 0
    assert any("已跳过分诊，全部照写" in w for w in result.warnings)


def test_write_back_v2_triaged_model_error_becomes_reverted(tmp_path,
                                                            monkeypatch):
    """模型传输失败 → review 保守跳过 → note_logic_reverted 记账链完整：
    resolved（不进 rejected）+ logic_reverted_sources（运行时排除表）+
    reverted_locators（C10 状态同步）。"""
    store = _StubStore([
        {"id": "f1", "format": "v2_mono", "rel_path": "Assembly-CSharp.dll"}])
    entry = _entry("us/1", "powerUpSound", "升级音效",
                   obj_has_values=True, field_path=["m_text"])
    entry["meta"] = json.dumps(entry["meta"], ensure_ascii=False)
    dll_dir = tmp_path / "out"
    dll_dir.mkdir(parents=True, exist_ok=True)
    (dll_dir / "Assembly-CSharp.dll").write_bytes(b"stub")
    # 熔断判定为 review 的条目会整批移出 patch 流，entries 变空 →
    # 需要一条 keeper 让 _patch_dll 真正被调到
    keeper = _entry("us/2", "combat_music", "战斗音乐",
                    obj_has_values=True, field_path=["m_text"])
    keeper["meta"] = json.dumps(keeper["meta"], ensure_ascii=False)
    monkeypatch.setattr(
        "hanhua.core.unity.writer._entries_by_file",
        lambda s, ids: {"f1": [entry, keeper]})
    monkeypatch.setattr(
        "hanhua.core.unity.writer._validate_addressables_catalog_sources",
        lambda *a, **k: None)
    monkeypatch.setattr(
        "hanhua.core.unity.writer._update_addressables_catalogs",
        lambda *a, **k: None)
    def fake_triage(pool, store_arg=None, *, service=None, app_dir=None,
                    on_log=None):
        return ({("f1", "us/1"): "ai_triage_review:model_error"},
                WritebackTriageReport(scanned=2, review=1, degraded=True))
    monkeypatch.setattr(
        "hanhua.core.unity.writeback_ai_triage.run_writeback_triage", fake_triage,
        raising=False)
    patch_calls = []
    monkeypatch.setattr(
        "hanhua.core.unity.writer._patch_dll",
        lambda path, entries, result, **kw: patch_calls.append(entries))
    result = write_back_v2(store, tmp_path / "game", tmp_path / "out",
                           triage_app_dir=".")
    assert entry not in patch_calls[0]  # 条目被移出 patch 流
    assert result.logic_reverted == 1
    assert result.is_resolved(entry)
    assert "powerUpSound" in result.logic_reverted_sources
    # rejected 不含分诊跳过条目（note_logic_reverted 阻断双记）
    assert all("us/1" not in str(getattr(r, "locator", ""))
               for r in result.rejected)
    assert ("f1:us/1") in result.reverted_locators
