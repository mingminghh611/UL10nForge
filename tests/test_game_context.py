# -*- coding: utf-8 -*-
"""游戏语境识别模块测试（设计文档 §3-24，2026-08-21）。

覆盖：代表性文本抽样（§4 程序自动分类，宁缺毋滥不跨类补数）、
识别 prompt 构建（§5 schema + §19 保守模式）、结构化 JSON 解析
（§5-10 保守容错，dict 形态角色名容忍）、Game Context 三态判定
（§23：未建立/已建立/需要更新）、本地/云端统一识别器（§18，
create_client 分发，识别参数走 ApiConfig 副本）。
"""
import json

import pytest

from hanhua.core import game_context as gc
from hanhua.core.memory import ProjectStore
from hanhua.core.models import ApiConfig


# ── 代表性文本抽样（§4） ─────────────────────────────────────

def _rows_ui(n=100):
    return [{"file_id": "f", "key_path": str(i), "original": f"UI Button {i}",
             "status": "pending", "locked": 0,
             "meta": {"reason": "single_visible_string"}} for i in range(n)]


def _rows_dialogue(n=100):
    return [{"file_id": "f", "key_path": f"d{i}", "original": f"Hello friend {i}",
             "status": "pending", "locked": 0,
             "meta": {"role": "dialogue"}} for i in range(n)]


def test_sample_entries_budget_per_category():
    """§4：每类按预算采样；类别不足取该类全部，绝不跨类补数。"""
    samples = gc.sample_entries(_rows_ui(100) + _rows_dialogue(100))
    cats = {}
    for s in samples:
        cats[s["category"]] = cats.get(s["category"], 0) + 1
    # 测试数据只有 ui/dialogue 两类：各按预算封顶，总数 30（≠90）
    assert cats == {"ui": 10, "dialogue": 20}, cats


def test_sample_entries_missing_category_never_cross_fills():
    """§4 宁缺毋滥：quest/item/… 无样本不跨类补数。"""
    samples = gc.sample_entries(_rows_ui(100))
    assert {s["category"] for s in samples} == {"ui"}
    assert len(samples) == 10


def test_sample_entries_long_natural_language_is_story():
    """natural_language 长叙述（>60 字）归剧情，短对白归对白。"""
    rows = [
        {"file_id": "f", "key_path": "k1", "original": "x" * 80,
         "status": "pending", "locked": 0,
         "meta": {"reason": "natural_language"}},
        {"file_id": "f", "key_path": "k2", "original": "hello",
         "status": "pending", "locked": 0,
         "meta": {"reason": "natural_language"}},
    ]
    cats = {gc._category_of(r["meta"], str(r["original"])) for r in rows}
    assert cats == {"story", "dialogue"}


def test_sample_entries_text_truncated():
    """text 截断 max_text_len（§12 不膨胀上下文）。"""
    rows = [{"file_id": "f", "key_path": "0", "original": "y" * 500,
             "status": "pending", "locked": 0,
             "meta": {"reason": "single_visible_string"}}]
    samples = gc.sample_entries(rows)
    assert len(samples[0]["text"]) == 200


# ── 识别 prompt（§5 schema + §19 保守模式） ───────────────────

def test_build_recognition_user_prompt_has_categories_and_conservative():
    samples = [{"category": "ui", "text": "Start"}, {"category": "dialogue", "text": "hi"}]
    prompt = gc.build_recognition_user_prompt(samples, source_lang="auto")
    assert "【UI 界面文本】" in prompt
    assert "【角色对白】" in prompt
    assert "不要猜测" in prompt
    prompt_zh = gc.build_recognition_user_prompt(samples, source_lang="zh")
    assert "原文语言：zh" in prompt_zh


# ── 结构化解析（§5-10 保守容错） ─────────────────────────────

def test_parse_game_context_dict_characters():
    """容忍 {name, type} 形态角色 → 「名字：类型」；未知字段/空值回落。"""
    raw = json.dumps({
        "game_name": "Test", "genre": "RPG", "setting": "未知",
        "summary": "玩家冒险", "style": "自然",
        "characters": [{"name": "Alice", "type": "教师"}, "Bob：士兵"],
        "terms": ["Mana：游戏机制"], "translation_notes": ["对白口语化"],
        "junk_field": "should be dropped",
    }, ensure_ascii=False)
    ctx = gc.parse_game_context(raw)
    assert ctx["game_name"] == "Test"
    assert ctx["setting"] == "未知"
    assert "Alice：教师" in ctx["characters"]
    assert "Bob：士兵" in ctx["characters"]
    assert ctx["terms"] == ["Mana：游戏机制"]
    assert "junk_field" not in ctx


def test_parse_game_context_fence_and_bad_json():
    """代码块围栏剥离；坏 JSON 回落 {}（绝不抛异常）。"""
    raw = "```json\n{\"genre\": \"动作\"}\n```"
    assert gc.parse_game_context(raw)["genre"] == "动作"
    assert gc.parse_game_context("not json at all") == {}
    assert gc.parse_game_context("") == {}


def test_parse_game_context_limits():
    """characters≤12 / terms≤15 / translation_notes≤5；去重去空。"""
    raw = json.dumps({
        "characters": [f"c{i}" for i in range(20)],
        "terms": [f"t{i}" for i in range(20)],
        "translation_notes": [f"n{i}" for i in range(10)],
    })
    ctx = gc.parse_game_context(raw)
    assert len(ctx["characters"]) == 12
    assert len(ctx["terms"]) == 15
    assert len(ctx["translation_notes"]) == 5


def test_game_context_summary():
    """摘要：genre · setting；无信息 → 空串。"""
    assert gc.game_context_summary({"genre": "RPG", "setting": "奇幻"}) == "RPG · 奇幻"
    assert gc.game_context_summary({"genre": "未知"}) == ""
    assert gc.game_context_summary(None) == ""


# ── 持久化 + 三态判定（§23） ────────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = ProjectStore(tmp_path / "test.db")
    s.init_schema()
    return s


def test_save_load_clear(store):
    ctx = {"game_name": "魔法学院", "genre": "RPG",
           "characters": ["Lily：学姐"]}
    gc.save_game_context(store, ctx)
    loaded = gc.load_game_context(store)
    assert loaded["genre"] == "RPG"
    assert loaded["characters"] == ["Lily：学姐"]
    gc.clear_game_context(store)
    assert gc.load_game_context(store) == {}


def test_save_syncs_profile_context_fields(store):
    """save_game_context 同步 GameProfile.context_* 字段——翻译/审校 prompt
    注入的同一份数据（2026-08-31 语境生效回归：此前只落 KV，profile 恒空
    → build_game_context_block 注入零内容，识别「没有实际作用」）。"""
    ctx = {"game_name": "魔法学院", "genre": "RPG", "setting": "奇幻魔法世界",
           "summary": "冒险者学院里的日常与战斗", "characters": ["Lily：学姐"],
           "terms": ["Mana：魔法值"], "style": "轻松喜剧", "translation_notes": []}
    gc.save_game_context(store, ctx)
    profile = store.get_profile()
    assert profile.context_game_name == "魔法学院"
    assert profile.context_genre == "RPG"
    assert profile.context_setting == "奇幻魔法世界"
    assert profile.context_summary == "冒险者学院里的日常与战斗"
    assert profile.context_characters == ["Lily：学姐"]
    assert profile.context_terms == ["Mana：魔法值"]
    assert profile.context_style == "轻松喜剧"
    # 注入块实际携带内容（而非空串）——零效果根因回归
    from hanhua.core.prompts import build_game_context_block
    block = build_game_context_block(profile)
    assert "魔法学院" in block and "RPG" in block


def test_save_skips_meaningless_to_profile(store):
    """「未知」/空数组不写进 profile——空值污染档案语义且零注入价值。"""
    ctx = {"game_name": "未知", "genre": "", "setting": "未知",
           "characters": [], "terms": ["Mana：魔法值"], "translation_notes": []}
    gc.save_game_context(store, ctx)
    profile = store.get_profile()
    assert profile.context_game_name == ""
    assert profile.context_genre == ""
    assert profile.context_terms == ["Mana：魔法值"]


def test_context_needs_update_threshold(store):
    """§23：新增可翻译文本 ≥ 已有 25% → 需要更新。"""
    ctx = {"genre": "RPG"}
    gc.save_game_context(store, ctx)
    assert gc.context_needs_update(store, 100) is False  # 无基线 → 已建立
    ctx["_sampled_total"] = 100
    gc.save_game_context(store, ctx)
    assert gc.context_needs_update(store, 124) is False   # < 125
    assert gc.context_needs_update(store, 125) is True    # ≥ 1.25×
    assert gc.context_needs_update(store, 300) is True
    # 基线保留：加载后 _sampled_total 可见（不污染注入字段白名单）
    assert gc.load_game_context(store)["_sampled_total"] == 100


def test_baseline_not_shared_to_context_block():
    """_sampled_total 持久化但绝不进入注入块（与 prompts 白名单一致）。"""
    from hanhua.core.prompts import build_game_context_block
    from hanhua.core.models import GameProfile
    p = GameProfile(context_genre="RPG", context_setting="奇幻")
    block = build_game_context_block(p)
    assert "游戏背景" in block
    assert "_sampled_total" not in block


# ── 统一识别器（§18 本地/云端同一链路） ──────────────────────

class _FakeClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def chat(self, system, messages):
        assert system  # 识别 system prompt 非空
        assert messages and messages[0]["role"] == "user"
        # 识别参数经 ApiConfig 副本注入：断言覆盖为识别默认值
        assert self.cfg.max_tokens == 2048
        assert self.cfg.temperature == 0.2
        return json.dumps({"game_name": "魔法学院", "genre": "RPG",
                           "setting": "奇幻魔法世界"}, ensure_ascii=False)


def test_recognizer_uses_api_config_copy(monkeypatch):
    """识别器把 max_tokens/temperature/timeout 写入配置副本后再 create_client。"""
    calls = {}

    def fake_create_client(cfg):
        calls["cfg"] = cfg
        return _FakeClient(cfg)

    monkeypatch.setattr(gc, "create_client", fake_create_client)
    cfg = ApiConfig(mode="api", base_url="http://x/v1", api_key="k",
                    model="m", max_tokens=4096, temperature=0.7,
                    timeout=99.0)
    rec = gc.GameContextRecognizer(cfg)
    raw = rec.recognize([{"category": "ui", "text": "Start"}], source_lang="auto")
    assert "RPG" in raw
    # 副本覆盖为识别参数；原 cfg 不被污染
    assert calls["cfg"].max_tokens == 2048
    assert calls["cfg"].temperature == 0.2
    assert calls["cfg"].timeout == 180.0
    assert cfg.max_tokens == 4096 and cfg.temperature == 0.7
