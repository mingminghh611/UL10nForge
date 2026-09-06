"""fix-29 词表/字典对象判定（happy-cat-tavern 实证 2026-08-12）。

打字游戏单词库对象（level1#1311 1700 条 100% 单 token 单词）中白名单
常见词（play/time/gold…）被 direct_code_signal/ui_control_signal 误放行
进池翻译，写回后玩家无法按英文打字（打字玩法破坏）。修复：对象级判定
——字符串几乎全部是单 token 单词且数量大 → 整对象跳过（word_table_object）。

证据分层：大型全单词数组是确定性词表结构证据，优先于形态性猜测；
正常 UI 对象含句式/描述文本且条目数少，不触发。
"""
import pytest

from hanhua.core.unity.extractor import _raw_string_entries

from tests.test_v2 import _scriptable_object_raw, _with_len


# 真实词表样本（happy-cat-tavern level1#1311 中被误译的单词）
_WORD_SAMPLE = ["play", "time", "gold", "walk", "read", "shop", "open",
                "money", "music", "window", "friend", "stamina", "victory",
                "keyboard", "size", "Batou", "placeholder", "Normal",
                "Hard", "Mild", "Winkle", "Regular", "Mirrored", "Smiley"]


def _word_table_raw(n: int) -> bytes:
    texts = [_WORD_SAMPLE[i % len(_WORD_SAMPLE)] for i in range(n)]
    return _scriptable_object_raw(*texts)


def _find(entries, text: str):
    hit = [e for e in entries if e.original == text]
    assert hit, f"{text!r} 未产生条目：{[e.original for e in entries]}"
    return hit[0]


def test_large_word_table_skipped_entirely():
    """≥50 条且 100% 单词的词表对象：全部跳过（含白名单词 play/time）——
    白名单显示词证据只在真实 UI 组件对象生效，词表词翻译破坏打字玩法。"""
    entries = _raw_string_entries("f1", 5, _word_table_raw(100), {},
                                  "sharedassets0.assets")
    assert len(entries) == 100
    for e in entries:
        assert e.status == "skipped", f"{e.original} 未跳过"
        assert e.meta["reason"] in ("word_table_object",
                                    "unity_control_state"), e.original
    # 白名单词同样跳过（词表对象里白名单不生效）
    assert _find(entries, "play").meta["reason"] == "word_table_object"
    assert _find(entries, "gold").meta["reason"] == "word_table_object"
    # 控件状态名（Normal 等）由 unity_control_state 硬拦截（语义一致：
    # 视觉状态串不进池）——词表对象标签不覆盖它
    assert _find(entries, "Normal").meta["reason"] == "unity_control_state"


def test_small_word_list_not_triggered():
    """小词表（<50 条）：不触发对象级判定，白名单词仍走原判定链
    （防误伤正常小 UI 对象/小配置）。"""
    entries = _raw_string_entries("f1", 5, _word_table_raw(20), {},
                                  "sharedassets0.assets")
    assert len(entries) == 20
    reasons = {e.meta["reason"] for e in entries}
    assert "word_table_object" not in reasons


def test_large_object_with_sentences_not_triggered():
    """大对象但含句式文本（单词占比 <95%）：对话/UI 描述对象不触发
    （单词占比被句式拉低，词表判定防过宽）。"""
    texts = [_WORD_SAMPLE[i % len(_WORD_SAMPLE)] for i in range(60)]
    texts += ["Word length starts at FOUR with normal bar speed",
              "Practice your typing with no pressure!",
              "Hard mode but all words are mirrored",
              "LIST OF COMMANDS"] * 3  # 60 单词 + 12 句式 ≈ 83%
    entries = _raw_string_entries("f1", 5, _scriptable_object_raw(*texts),
                                  {}, "sharedassets0.assets")
    reasons = {e.meta["reason"] for e in entries}
    assert "word_table_object" not in reasons


def test_normal_ui_object_unaffected():
    """正常 UI 对象（少量标签+句式）：词表判定不干预，显示文本照常进池。"""
    raw = (_with_len("Main Menu")
           + _with_len("Play")
           + _with_len("Word length starts at FOUR with normal bar speed"))
    entries = _raw_string_entries("f1", 5, raw, {}, "sharedassets0.assets")
    assert len(entries) == 3
    assert _find(entries, "Main Menu").status == "pending"


# ── F38（adapt-prologue 实证 2026-08-16）──
# 对象会被释放（非代码/输入/配置/键列表对象）时，其 prefilter_high_
# frequency 样本是游戏 UI 词而非引擎高频：生物卡片对象（'Clam 01'
# pending 同侪）里 'Health'/'Food'/'Resource' 全被高频跳过（哑信号）。
# 组件配置对象（code_heavy，'Play' 音效事件名 294 对象实证）保持跳过。


def _ui_card_raw() -> bytes:
    """生物卡片对象：'Clam 01'（句子形态，pending）+ Health/Food/
    Resource（全游戏高频 UI 词）。"""
    return _scriptable_object_raw(
        "Clam 01", "Health", "Food", "Resource", "Survival/Armor")


def test_f38_ui_card_high_freq_released():
    """UI 卡片对象（含 pending 同侪）：高频 UI 词升级为显示文本。

    B22 优先级 4 后路径变化：Health/Food/Resource 不再进高频预过滤
    （DISPLAY_WORDS/_WORD_CASE 显式证据优先于频次猜测），走常规链由
    F39 word_list_object 释放——终态不变（pending/display）。"""
    # freq：Health/Food/Resource 达到高频阈值（总量 30 条 → 阈值 ~7）
    freq = {"Health": 20, "Food": 20, "Resource": 20}
    entries = _raw_string_entries("f1", 9, _ui_card_raw(), freq,
                                  "sharedassets0.assets")
    by = {e.original: e for e in entries}
    assert by["Health"].status == "pending"
    # F38（prefilter 高频释放）或 F39（词表对象释放）任一路径均合法
    # ——两条都是「UI 词进池」的确定性释放闸门
    assert by["Health"].meta["reason"] in ("single_visible_string",
                                           "word_list_object")
    assert by["Food"].status == "pending"
    assert by["Resource"].status == "pending"
    assert by["Clam 01"].status == "pending"
    # Survival/Armor（路径形态'/'分隔）保持跳过——类别路径标签
    # 观察项（宁漏勿坏），不属本修复范围


def test_f38_component_object_high_freq_stays_skipped():
    """组件配置对象（code_heavy：方法名/类型引用 ≥2）：
    'Play' 高频词保持跳过（FMOD 事件名，翻译断音效引用）。

    B22 优先级 4 后：Play ∈ DISPLAY_WORDS 不进高频预过滤，改走
    code_heavy 链的 code_heavy_identifier 跳过（同侪 Populate 同因）——
    语义不变：code_heavy 对象里的白名单词保持跳过。"""
    raw = _scriptable_object_raw(
        "Play", "Populate", "SetState",
        "FMODUnity.StudioEventEmitter, FMODUnity",
        "UnityEngine.Object, UnityEngine")
    freq = {"Play": 300}
    entries = _raw_string_entries("f1", 7, raw, freq,
                                  "sharedassets0.assets")
    by = {e.original: e for e in entries}
    assert by["Play"].status == "skipped"
    assert by["Play"].meta["reason"] in ("prefilter_high_frequency",
                                         "code_heavy_identifier")


def test_f38_ugui_state_names_stay_skipped():
    """UGUI 状态名（Normal/Highlighted/Pressed/Disabled/Selected）即使
    在 UI 对象里也不升级（控件状态串，翻译写回状态错乱）。"""
    raw = _scriptable_object_raw(
        "Clam 01", "Health", "Normal", "Pressed", "Disabled")
    freq = {"Health": 20, "Normal": 20, "Pressed": 20, "Disabled": 20}
    entries = _raw_string_entries("f1", 9, raw, freq,
                                  "sharedassets0.assets")
    by = {e.original: e for e in entries}
    assert by["Health"].status == "pending"          # UI 词升级
    assert by["Normal"].status == "skipped"          # 状态名不升级
    assert by["Normal"].meta["reason"] in ("prefilter_high_frequency",
                                           "unity_control_state")
    assert by["Pressed"].status == "skipped"
    assert by["Disabled"].status == "skipped"


# ── F39（attack-on-wendigo 实证 2026-08-16）──
# 命名列表对象：TitleCase 单词式词 ≥3 且无代码信号 → 武器/物品/地点
# 目录（商店/掉落/库存 UI 文本），单词式写法是显示文本形态。

def test_f39_word_list_object_released():
    """武器列表对象（Pistol/Magnum/Rifle/Shotgun）整批恢复显示文本。"""
    raw = _scriptable_object_raw(
        "Drugs", "Pistol", "Magnum", "Rifle", "Shotgun")
    entries = _raw_string_entries("f1", 9, raw, {}, "sharedassets0.assets")
    by = {e.original: e for e in entries}
    for w in ("Drugs", "Pistol", "Magnum", "Rifle", "Shotgun"):
        assert by[w].status == "pending", w
        assert by[w].meta["reason"] == "word_list_object", w


def test_f39_particle_names_not_released():
    """粒子名（驼峰 SnowParticle）+ 类型引用混入 → code_heavy 信号 →
    整对象不释放（'Play' 音效事件名同保护）。"""
    raw = _scriptable_object_raw(
        "Pistol", "Magnum", "Rifle", "SnowParticle",
        "UnityEngine.Object, UnityEngine",
        "FMODUnity.StudioEventEmitter, FMODUnity")
    entries = _raw_string_entries("f1", 7, raw, {}, "sharedassets0.assets")
    by = {e.original: e for e in entries}
    assert by["Pistol"].status == "skipped"   # code_heavy 不释放
    assert by["SnowParticle"].status == "skipped"


def test_f39_two_words_not_triggered():
    """<3 个 TitleCase 词不触发（小配置/命名对对象保持原判定）。"""
    raw = _scriptable_object_raw("Pistol", "Magnum")
    entries = _raw_string_entries("f1", 7, raw, {}, "sharedassets0.assets")
    by = {e.original: e for e in entries}
    assert by["Pistol"].status == "skipped"
    assert by["Pistol"].meta["reason"] != "word_list_object"


# ── F44（dinofurie 实证 2026-08-16）──
# 按钮对象（控制状态 ≥3 + 类型引用 → code_heavy）里的单词式按钮
# 文本：'Jugar'（西语"玩"）不在英语白名单被 code_heavy_identifier
# 跳过——UI 控件证据（状态）优先，单词式形态（_WORD_CASE）是显示
# 文本证据，非白名单也放行。

def test_f44_button_text_word_case_released_in_ui_evidence():
    """按钮对象（5 状态 + 类型引用）：'Jugar' 单词式按钮文本放行，
    UGUI 状态名保持跳过。"""
    raw = _scriptable_object_raw(
        "Normal", "Highlighted", "Pressed", "Selected", "Disabled",
        "Jugar", "botons, Assembly-CSharp",
        "UnityEngine.Object, UnityEngine")
    entries = _raw_string_entries("f1", 9, raw, {}, "sharedassets0.assets")
    by = {e.original: e for e in entries}
    assert by["Jugar"].status == "pending"
    assert by["Jugar"].meta["reason"] == "code_heavy_display_word"
    for state in ("Normal", "Highlighted", "Pressed", "Selected",
                  "Disabled"):
        assert by[state].status == "skipped", state


def test_f44_no_ui_evidence_word_case_stays_skipped():
    """无 UI 证据（无控制状态）的 code_heavy 对象：单词式词保持跳过
    （纯代码对象无按钮文本语境）。"""
    raw = _scriptable_object_raw(
        "Jugar", "Populate", "SetState",
        "UnityEngine.Object, UnityEngine",
        "FMODUnity.StudioEventEmitter, FMODUnity")
    entries = _raw_string_entries("f1", 9, raw, {}, "sharedassets0.assets")
    by = {e.original: e for e in entries}
    assert by["Jugar"].status == "skipped"
    assert by["Jugar"].meta["reason"] == "code_heavy_identifier"
