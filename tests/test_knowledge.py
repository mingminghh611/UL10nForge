"""知识库：多形态知识（文本/文件/抽象规则）分库存储 + 匹配 + 学习 + prompt 注入。"""
from pathlib import Path

import pytest

from hanhua.core.knowledge import (BUILTIN_RULES, KnowledgeBase,
                                   KnowledgeStore, _is_multilingual_source,
                                   _is_spaced_action, _is_uppercase_action,
                                   aggregate_spaced_letters,
                                   spaced_action_lexicon,
                                   translate_uppercase_action)
from hanhua.core.models import TextEntry


class _Entry:
    """TextEntry 轻量替身（只含 learn() 用到的字段）。"""

    def __init__(self, original, status="translated", translation=None,
                 meta=None):
        self.original = original
        self.status = status
        self.translation = translation if translation is not None else original
        self.meta = {"quality_passed": True, **(meta or {})}


# ── 内置形态识别 ──

class TestUppercaseAction:
    def test_action_phrase_detected(self):
        assert _is_uppercase_action("TOSS TRASH")
        assert _is_uppercase_action("PRESS START")
        assert _is_uppercase_action("PICK UP THE AXE")
        assert _is_uppercase_action("THROW THE BALL NOW")

    def test_proper_name_not_detected(self):
        # 真专名无动作动词 → 不误命中（专名仍走 proper_name_echo 豁免）
        assert not _is_uppercase_action("MEGA CORP")
        assert not _is_uppercase_action("STAR WARS")
        assert not _is_uppercase_action("GAME OVER")  # 无动作动词
        assert not _is_uppercase_action("NEW GAME")   # 无动作动词

    def test_edge_cases(self):
        assert not _is_uppercase_action("")
        assert not _is_uppercase_action("123")
        assert not _is_uppercase_action("just a sentence")  # 非全大写
        assert not _is_uppercase_action("A")                # 单词太短
        assert not _is_uppercase_action("LONG " + "WORD " * 6 + " HERE")  # 超 5 词


class TestTranslateUppercaseAction:
    def test_mechanical_translation(self):
        assert translate_uppercase_action("TOSS TRASH") == "丢垃圾"
        assert translate_uppercase_action("PRESS START") == "按开始"
        assert translate_uppercase_action("OPEN THE DOOR") == "打开门"
        assert translate_uppercase_action("PICK UP THE AXE") == "捡起斧头"

    def test_unknown_word_no_fallback(self):
        assert translate_uppercase_action("TOSS THE ZARBUL") is None
        assert translate_uppercase_action("MEGA CORP") is None
        assert translate_uppercase_action("") is None


class TestSpacedAction:
    def test_spaced_words_detected(self):
        assert _is_spaced_action("* Y A W N *")
        assert _is_spaced_action("G A S P")
        assert _is_spaced_action("* S C O F F *")
        # F8-A：对话动画标签前缀/后缀不阻碍判定（{punch}/{w=3}/{x} 是
        # 动画参数不是词——a-catfiends 实证原判定失效 → 回显恒败）
        assert _is_spaced_action("{punch=3,2}* Y A W N *{w=3}{x}")
        assert _is_spaced_action("* S I G H *{w=3}{x}")

    def test_non_spaced_not_detected(self):
        assert not _is_spaced_action("* TOSS TRASH *")
        assert not _is_spaced_action("HELLO")
        assert not _is_spaced_action("")
        # 完整句子不被误判（I am/am 是词不是单字母）
        assert not _is_spaced_action(
            "I am {punch=3,2}NOT who I used to be.{w=3}{x}")

    def test_aggregate_spaced_letters(self):
        assert aggregate_spaced_letters("* Y A W N *") == "* YAWN *"
        assert aggregate_spaced_letters(
            "{punch=3,2}* Y A W N *{w=3}{x}") == (
            "{punch=3,2}* YAWN *{w=3}{x}")
        # 无间隔词 → 原样返回
        assert aggregate_spaced_letters("TOSS TRASH") == "TOSS TRASH"

    def test_spaced_action_lexicon_covers_core_actions(self):
        """F10-A：间隔动作词封闭词典（1.8B 对聚合形态仍稳定回显——
        动作旁白词确定性直填）。"""
        for word, zh in (("YAWN", "打哈欠"), ("SCOFF", "嗤笑"),
                         ("SIGH", "叹气"), ("GASP", "倒吸一口气"),
                         ("VOMITS", "呕吐"), ("GROAN", "呻吟")):
            assert spaced_action_lexicon(word) == zh, word
        # 未收录（开放文本）→ None 交模型
        assert spaced_action_lexicon("FLOWCHART") is None
        assert spaced_action_lexicon("") is None
        # 大小写归一
        assert spaced_action_lexicon("yawn") == "打哈欠"


# ── 持久库：多形态分库 ──

class TestKnowledgeStore:
    def test_upsert_idempotent_hits_increment(self, tmp_path):
        store = KnowledgeStore(tmp_path / "knowledge.db")
        store.init_schema()
        assert store.upsert("text", "spaced_action", "G A S P",
                            action="translate") is True
        assert store.upsert("text", "spaced_action", "G A S P",
                            action="translate") is False
        rows = store.list_by_domain("text")
        assert len(rows) == 1
        assert rows[0]["hits"] == 2
        store.close()

    def test_multiple_domains_separate_libraries(self, tmp_path):
        store = KnowledgeStore(tmp_path / "knowledge.db")
        store.init_schema()
        store.upsert("text", "spaced_action", "Y A W N", action="translate")
        store.upsert("file", "us_record", "#US 固定码元", action="capacity_fixed")
        store.upsert("rule", "placeholder_restore", "{n} 补末尾", action="restore_to_end")
        assert len(store.list_by_domain("text")) == 1
        assert len(store.list_by_domain("file")) == 1
        assert len(store.list_by_domain("rule")) == 1
        store.close()

    def test_delete(self, tmp_path):
        store = KnowledgeStore(tmp_path / "knowledge.db")
        store.init_schema()
        store.upsert("text", "uppercase_action", "TOSS TRASH", action="translate")
        store.delete("text", "uppercase_action", "TOSS TRASH")
        assert store.list_by_domain("text") == []
        store.close()


# ── KnowledgeBase：匹配 / prompt 注入 / 学习 ──

class TestKnowledgeBase:
    def test_match_text_builtin_detection(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        assert kb.requires_translation("TOSS TRASH")
        assert kb.requires_translation("* Y A W N *")
        assert not kb.requires_translation("MEGA CORP")
        kb.close()

    def test_persisted_exact_phrase_injected(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("text", "uppercase_action", "TOSS TRASH",
                        action="translate", map_to="丢垃圾")
        prompt = kb.format_for_prompt()
        assert "TOSS TRASH" in prompt
        assert "丢垃圾" in prompt
        # 无 map_to 的条目（纯学习标记）不注入，避免 prompt 膨胀
        kb.store.upsert("text", "uppercase_action", "TOSS COINS",
                        action="translate", map_to="")
        assert "TOSS COINS" not in kb.format_for_prompt()
        kb.close()

    def test_builtin_seed_rules_described(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        described = kb.describe()
        domains = {item["domain"] for item in described}
        # 六库蓝图 + 保留域 file/rule（跨场景处置策略，2026-08-11 注释）；
        # fail_case 是运行时沉淀域（record_case），无种子
        assert domains == ((set(KnowledgeBase.SIX_LIBRARIES)
                            | {"file", "rule"}) - {"fail_case"})
        # 种子齐备：结构登记 / 文件知识 / 抽象规则 / 组件兼容
        assert any(k["kind"] == "unity_version"
                   for k in described if k["domain"] == "unity_structure")
        assert any(k["kind"] == "us_record"
                   for k in described if k["domain"] == "file")
        assert any(k["kind"] == "placeholder_restore"
                   for k in described if k["domain"] == "rule")
        assert any(k["kind"] == "textmeshpro"
                   for k in described if k["domain"] == "component_compat")
        assert len(described) >= len(BUILTIN_RULES)
        kb.close()

    def test_learn_from_echo_entries(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        entries = [
            _Entry("TOSS TRASH"),                 # 回显 + 大写动作 → 学习
            _Entry("* Y A W N *"),                # 回显 + 间隔动作词 → 学习
            _Entry("MEGA CORP"),                  # 纯专名回显 → 不学习
            _Entry("Princess Peach"),             # 小写词 + 不在专名单 → 不学习
            _Entry("Press START", translation="按开始"),  # 已翻译 → 不学习
            # 质量门拒绝的回显条目（重试仍回显的模型惯性）→ 学习
            _Entry("TOSS COINS", status="failed",
                   meta={"quality_reasons": ["untranslated_text"]}),
            # 失败但已翻译（translation != original）→ 不学习
            _Entry("TAKE CARE", status="failed",
                   translation="保重",
                   meta={"quality_reasons": ["untranslated_text"]}),
            # 半翻译残留（action_word_residue 拒绝，译文≠原文）→ 学习
            _Entry("TOSS RUBBISH", status="failed",
                   translation="TOSS 垃圾",
                   meta={"quality_reasons": ["action_word_residue"]}),
            # 非知识库形态失败（换行等）→ 不学习
            _Entry("LONG TEXT", status="failed",
                   translation="长文",
                   meta={"quality_reasons": ["newline_mismatch"]}),
        ]
        learned, hits = kb.learn(entries, "test-game", names={"Princess Peach"})
        assert learned == 4
        assert hits == 4
        rows = kb.store.list_by_domain("text")
        assert {r["pattern"] for r in rows} == {
            "TOSS TRASH", "* Y A W N *", "TOSS COINS", "TOSS RUBBISH"}
        kb.close()

    def test_learn_idempotent_accumulates_hits(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.learn([_Entry("G A S P")], "game-a")
        learned, hits = kb.learn([_Entry("G A S P")], "game-b")
        assert learned == 0
        assert hits == 1
        row = kb.store.list_by_domain("text")[0]
        assert row["hits"] == 2
        assert row["game"] == "game-b"   # 最新学习来源游戏（note 为固定模式描述）
        kb.close()

    def test_learn_generates_map_to_for_reference_pairs(self, tmp_path):
        """learn 给大写动作指令生成机械直译建议——native 降级重试靠它
        注入译例（Hy-MT2 无 system prompt，只能走 references 的 terms）。"""
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.learn([_Entry("TOSS TRASH", status="failed",
                         meta={"quality_reasons": ["untranslated_text"]})],
                 "game-x")
        pairs = kb.format_reference_pairs()
        assert ("TOSS TRASH", "丢垃圾") in pairs
        kb.close()

    def test_learn_single_lexicon_word(self, tmp_path):
        """单设置词回显（JUMP/Vsync 全大写/TitleCase 键名，1.8B 稳定
        回显）→ 词表命中沉淀 single_lexicon_word 译例（0.26 地毯式
        实证：force-reboot 设置页 16 条 JUMP/VSYNC/Vsync 恒败，单词
        形态不命中 _is_uppercase_action 2-5 词短语 → 无译例死循环）。"""
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        entries = [
            _Entry("JUMP", status="failed",
                   meta={"quality_reasons": ["untranslated_text"]}),
            _Entry("Vsync", status="failed",
                   meta={"quality_reasons": ["untranslated_text"]}),
            _Entry("VSYNC", status="failed",
                   meta={"quality_reasons": ["untranslated_text"]}),
            # 词表外单词 → 不学习（无机械直译来源，防污染）
            _Entry("ZARBUL", status="failed",
                   meta={"quality_reasons": ["untranslated_text"]}),
            # 短语形态仍走 uppercase_action（不重复沉淀）
            _Entry("PRESS START", status="failed",
                   meta={"quality_reasons": ["untranslated_text"]}),
        ]
        learned, hits = kb.learn(entries, "force-reboot")
        assert learned == 4   # JUMP/Vsync/VSYNC 去重 2 + PRESS START = 3? 见下
        pairs = kb.format_reference_pairs()
        assert ("JUMP", "跳跃") in pairs
        assert ("VSYNC", "垂直同步") in pairs
        assert ("Vsync", "垂直同步") in pairs
        assert ("PRESS START", "按开始") in pairs
        assert ("ZARBUL", "") not in pairs
        rows = {r["pattern"]: r for r in kb.store.list_by_domain("text")}
        assert rows["JUMP"]["kind"] == "single_lexicon_word"
        assert rows["JUMP"]["map_to"] == "跳跃"
        kb.close()

    def test_learn_without_store_is_noop(self, tmp_path):
        kb = KnowledgeBase()  # 无持久库 → learn 空操作
        assert kb.learn([_Entry("TOSS TRASH")], "game") == (0, 0)
        kb.close()

    def test_learn_multilingual_echo(self, tmp_path):
        """多语言源回显条目（法语 Clé en Fer 等模型不认识的语言）→ 沉淀
        text/multilingual_source 形态规则（alisa-demo 实证 1 条法语回显）。"""
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        entries = [
            _Entry("Clé en Fer", status="failed",
                   meta={"quality_reasons": ["untranslated_text"]}),
            # 日语回显（模型输出英语时 translation != original → 不学；
            # 双跳修复在 batch_translator 层，learn 只管纯回显）
            _Entry("右手の鍵", status="translated",
                   translation="Right-hand key",
                   meta={"quality_reasons": ["target_script_mismatch"],
                         "quality_passed": False}),
        ]
        learned, hits = kb.learn(entries, "alisa-demo")
        assert learned == 1
        rows = kb.store.list_by_domain("text")
        assert {r["pattern"] for r in rows} == {"Clé en Fer"}
        assert rows[0]["kind"] == "multilingual_source"
        assert kb.match_text("Clé en Fer")[0]["kind"] == "multilingual_source"
        kb.close()


class TestMultilingualSource:
    """多语言源文本形态：含日文假名或带重音拉丁字母 → 其他语言（非英语）
    源文本。模型对其倾向输出英语译文（alisa-demo 实证 26 条），须译中文。"""

    def test_japanese_kana_detected(self):
        assert _is_multilingual_source("右手の鍵")
        assert _is_multilingual_source("この鍵も 役に立つかも")
        assert _is_multilingual_source("ラベルには　こう書かれている")

    def test_accented_latin_detected(self):
        assert _is_multilingual_source("Clé en Fer")      # 法语 é
        assert _is_multilingual_source("Perchè hai transformato")  # 意语 è
        assert _is_multilingual_source("J'ai emprunté")   # 法语 é

    def test_ascii_romance_function_words_detected(self):
        # 意语/法语与英语共用拉丁字母，无重音字符时靠功能词识别
        assert _is_multilingual_source("Chiave di Ferro")   # di
        assert _is_multilingual_source("Canna da Pesca")    # da
        assert _is_multilingual_source("Il cibo su questo tavolo")  # il
        assert _is_multilingual_source("Clé en Fer")

    def test_plain_scripts_not_detected(self):
        assert not _is_multilingual_source("Iron Key")
        assert not _is_multilingual_source("TOSS TRASH")
        assert not _is_multilingual_source("Hello, world!")
        assert not _is_multilingual_source("中文文本")
        assert not _is_multilingual_source("")

    def test_rule_in_builtin_and_match(self):
        assert any(r["kind"] == "multilingual_source"
                   for r in BUILTIN_RULES)


def test_builtin_rules_shape():
    for rule in BUILTIN_RULES:
        assert rule["domain"] in (set(KnowledgeBase.SIX_LIBRARIES)
                                  | {"file", "rule"})
        assert rule["kind"]
        assert rule["pattern"]
        assert rule["action"]


def test_prompt_not_injected_via_build_system_prompt(tmp_path):
    """2026-08-14 用户要求「检索命中才加入」：知识不再全量拼 system_prompt
    （25 条对照 ≈ 884 tokens 膨胀上下文）——由 BatchTranslator 按原文
    match_text 命中注入（knowledge_hits，见 test_ctx_budget）。"""
    from hanhua.core.models import GameProfile
    from hanhua.core.prompts import build_system_prompt
    kb = KnowledgeBase(tmp_path / "knowledge.db")
    kb.store.upsert("text", "uppercase_action", "TOSS TRASH",
                    action="translate", map_to="丢垃圾")
    system = build_system_prompt(GameProfile(), "", known_names=None,
                                 knowledge_lines=kb.format_for_prompt())
    assert "【特殊情况规则" not in system
    assert "TOSS TRASH" not in system
    kb.close()


def test_writeback_case_rules_all_implemented():
    """知识库案例转规则（2026-08-11）：writeback_case 5 条理论案例必须
    全部映射到已实现规则（规则清单可查询，写回链路启用报告用）。"""
    from hanhua.core.knowledge import WRITEBACK_CASE_RULES, writeback_case_rules
    assert len(WRITEBACK_CASE_RULES) == 5
    rules = writeback_case_rules()
    assert [r["rule"] for r in rules] == [
        "fit_bytes_nul_padding", "placeholder_preserve",
        "textasset_encoding_preserve", "unityevent_binding_preserve",
        "logic_key_compare",
    ]
    for rule in rules:
        assert rule["case"] and rule["impl"], rule["rule"]
    # 规则实现真实性抽查：unityevent 规则对应 extractor 信号常量
    from hanhua.core.unity.extractor import _UNITYEVENT_SIGNALS
    assert "m_PersistentCalls" in _UNITYEVENT_SIGNALS
    # logic_key_compare 规则对应比较词表
    from hanhua.core.unity.logic_audit import LOGIC_COMPARE_WORDS
    assert "continue" in LOGIC_COMPARE_WORDS


class TestLanguageOptionFill:
    """语言选项标签确定性直填（F12-A，doog 实证 'Language: ENGLISH'
    模型 4 次重试稳定乱译 → 封闭集合词典直填不走模型）。"""

    def test_language_label_fill(self):
        from hanhua.core.knowledge import language_option_translation
        assert language_option_translation("Language: ENGLISH") == "语言：英语"
        assert language_option_translation("language: japanese") == "语言：日语"
        assert language_option_translation("Language：Spanish") == "语言：西班牙语"
        assert language_option_translation("言语：日本語") == "语言：日语"
        assert language_option_translation("Idioma: Español") == "语言：西班牙语"

    def test_pure_language_name_not_fill(self):
        # 纯语言名保留原名是业界惯例（_is_language_name 豁免），直填只覆盖
        # 「标签 + 语言名」组合形态
        from hanhua.core.knowledge import language_option_translation
        assert language_option_translation("ENGLISH") is None
        assert language_option_translation("Español") is None
        assert language_option_translation("日本語") is None

    def test_label_with_unknown_language_not_fill(self):
        from hanhua.core.knowledge import language_option_translation
        # 表外语言名交模型（不硬编码所有语种）
        assert language_option_translation("Language: Klingon") is None

    def test_non_language_text_not_fill(self):
        from hanhua.core.knowledge import language_option_translation
        assert language_option_translation("Press START to begin") is None
        assert language_option_translation("Volume: High") is None


# ── C16 知识库审计（2026-09-07）：反例召回 + 错误知识清理 + game 回填 ──

class TestReviewCounterexamples:
    """fail_case/审核 域此前只写不读（1674 条反例零召回）——
    review_counterexamples 按原文拆词召回历史误译，注入审核提示。"""

    def _note(self, *, original="Make all readers gullible",
              wrong="让读者都容易上当", correct="", reason="漏译祈使语气",
              suggestion=""):
        import json
        return json.dumps({
            "schema": "review_failure_v1", "game": "Fake it",
            "original": original, "wrong_translation": wrong,
            "correct_translation": correct, "review_reason": reason,
            "suggestion": suggestion, "converged": True,
            "final_outcome": "APPROVED"}, ensure_ascii=False)

    def test_recall_by_original(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("fail_case", "审核", "Fake it_Data/f:12",
                        action="apply_fix", note=self._note(),
                        game="Fake it")
        hits = kb.review_counterexamples(
            "Make all readers gullible.", game="Fake it")
        assert len(hits) == 1
        assert hits[0]["wrong_translation"] == "让读者都容易上当"
        assert hits[0]["review_reason"] == "漏译祈使语气"
        assert hits[0]["game"] == "Fake it"
        # mark_used 留痕
        row = [r for r in kb.store.list_by_domain("fail_case")
               if r["pattern"] == "Fake it_Data/f:12"][0]
        assert row["usage_count"] == 1
        kb.close()

    def test_self_contradictory_not_recalled(self, tmp_path):
        # wrong==correct（首审误判沉淀）不召回
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("fail_case", "审核", "g_Data/f:1",
                        action="apply_fix",
                        note=self._note(wrong="让读者上当",
                                        correct="让读者上当"),
                        game="Fake it")
        assert kb.review_counterexamples(
            "Make all readers gullible.") == []
        kb.close()

    def test_deprecated_not_recalled(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("fail_case", "审核", "g_Data/f:2",
                        action="apply_fix", note=self._note(),
                        game="Fake it")
        kb.store.set_status("fail_case", "审核", "g_Data/f:2", "deprecated")
        assert kb.review_counterexamples(
            "Make all readers gullible.") == []
        kb.close()

    def test_unrelated_original_no_recall(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("fail_case", "审核", "g_Data/f:3",
                        action="apply_fix", note=self._note(),
                        game="Fake it")
        assert kb.review_counterexamples("PRESS START") == []
        kb.close()

    def test_same_game_weighted_first(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        # 同分命中（整串 +3）时本作 +2 加权优先于他作
        kb.store.upsert(
            "fail_case", "审核", "g_Data/f:1", action="apply_fix",
            note=self._note(wrong="外作误译"), game="Other")
        kb.store.upsert(
            "fail_case", "审核", "g_Data/f:2", action="apply_fix",
            note=self._note(wrong="本作误译"), game="Fake it")
        hits = kb.review_counterexamples(
            "Make all readers gullible", game="Fake it", limit=1)
        assert len(hits) == 1
        assert hits[0]["wrong_translation"] == "本作误译"
        kb.close()


class TestPruneInvalidPatterns:
    """四类坏知识清理：invalid_regex / should_skip_leak /
    self_contradictory / false_language_claim → deprecated 退役。"""

    def test_invalid_regex_deprecated(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("text", "spaced_action", "* unclosed[",
                        action="translate")
        assert kb.prune_invalid_patterns()["invalid_regex"] == 1
        row = kb.store.list_by_domain("text")[0]
        assert row["status"] == "deprecated"
        # 幂等：再跑零清理
        assert kb.prune_invalid_patterns()["invalid_regex"] == 0
        kb.close()

    def test_should_skip_leak_deprecated(self, tmp_path):
        from hanhua.core.placeholders import should_skip
        structural = "line:de8670da"  # C16 实证泄漏样本
        assert should_skip(structural)
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("text", "multilingual_source", structural,
                        action="translate")
        assert kb.prune_invalid_patterns()["should_skip_leak"] == 1
        assert kb.store.list_by_domain("text")[0]["status"] == "deprecated"
        kb.close()

    def test_self_contradictory_deprecated(self, tmp_path):
        import json
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        note = json.dumps({
            "original": "Hello", "wrong_translation": "你好",
            "correct_translation": "你好", "review_reason": "x"},
            ensure_ascii=False)
        kb.store.upsert("fail_case", "审核", "g_Data/f:1",
                        action="apply_fix", note=note, game="g")
        assert kb.prune_invalid_patterns()["self_contradictory"] == 1
        kb.close()

    def test_false_kana_claim_deprecated(self, tmp_path):
        # 'noshuio' 纯 ASCII 但 reason 声称「原文为日文片假名」→ 与事实矛盾
        import json
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        note = json.dumps({
            "original": "noshuio", "wrong_translation": "诺休",
            "correct_translation": "", "review_reason":
            "原文为日文片假名，译文未翻译且拼写错误"}, ensure_ascii=False)
        kb.store.upsert("fail_case", "审核", "g_Data/f:1",
                        action="apply_fix", note=note, game="g")
        counts = kb.prune_invalid_patterns()
        assert counts["false_language_claim"] == 1
        assert counts["self_contradictory"] == 0
        kb.close()

    def test_good_knowledge_untouched(self, tmp_path):
        import json
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("text", "uppercase_action", "TOSS TRASH",
                        action="translate", map_to="丢垃圾")
        note = json.dumps({
            "original": "Hello", "wrong_translation": "喂喂喂",
            "correct_translation": "你好", "review_reason": "语气不对"},
            ensure_ascii=False)
        kb.store.upsert("fail_case", "审核", "g_Data/f:1",
                        action="apply_fix", note=note, game="g")
        assert all(v == 0 for v in kb.prune_invalid_patterns().values())
        assert kb.store.list_by_domain("text")[0]["status"] != "deprecated"
        kb.close()


class TestBackfillMissingGame:
    """game 空缺反例从 pattern 首段 '<Game>_Data' 提取游戏名回填。"""

    def test_backfill_from_data_dir(self, tmp_path):
        import json
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        note = json.dumps({
            "original": "Hello", "wrong_translation": "喂",
            "review_reason": "x", "game": ""}, ensure_ascii=False)
        kb.store.upsert("fail_case", "审核", "Minato_Data/files:3",
                        action="apply_fix", note=note, game="")
        assert kb.backfill_missing_game() == 1
        row = kb.store.list_by_domain("fail_case")[0]
        assert row["game"] == "Minato"
        assert json.loads(row["note"])["game"] == "Minato"
        # 幂等
        assert kb.backfill_missing_game() == 0
        kb.close()

    def test_no_data_segment_untouched(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("fail_case", "审核", "plain_locator:1",
                        action="apply_fix", note="{}", game="")
        assert kb.backfill_missing_game() == 0
        assert kb.store.list_by_domain("fail_case")[0]["game"] == ""
        kb.close()

    def test_existing_game_untouched(self, tmp_path):
        kb = KnowledgeBase(tmp_path / "knowledge.db")
        kb.store.upsert("fail_case", "审核", "Fake it_Data/f:1",
                        action="apply_fix", note="{}", game="Other")
        assert kb.backfill_missing_game() == 0
        kb.close()
