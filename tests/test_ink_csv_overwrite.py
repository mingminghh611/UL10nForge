"""Rendezvous 实证修复回归测试（2026-08-17）：

1. ink 对话 JSON 特判提取：控制词（done/end）、divert 目标、标签
   元数据不得进池（翻译会破坏对话流程）；对话行（^ 前缀）保留。
2. CSV 覆盖源列模式：游戏语言设置只有英文时，翻译写回源列（ENG），
   目标语言列（CHN）官方中文搬运（official_zh meta）。
3. CSV 目标语言列（CHN）别名识别（表头 'CHN' 大小写不敏感）。
"""
from __future__ import annotations

import json

from hanhua.core.formats.csv_format import (
    apply_csv, extract_csv_text, pick_target_col)
from hanhua.core.models import TextEntry
from hanhua.core.unity import extractor as ex

INK_SAMPLE = json.dumps({
    "inkVersion": 19,
    "root": [
        [["done", {"#f": 5, "#n": "g-0"}], None],
        "done",
        {"Setyo_WakeUp": [
            "^Setyo: *Sigh*... Hungover again.",
            {"#f": 5, "#n": "g-0"},
            "out",
            "^Setyo: And that goddamn dream...",
        ]},
    ],
}, ensure_ascii=False)


class TestInkExtraction:
    def test_control_words_filtered(self):
        es = ex._ink_entries("t", 1, INK_SAMPLE.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        originals = [e.original for e in es]
        assert originals == [
            "^Setyo: *Sigh*... Hungover again.",
            "^Setyo: And that goddamn dream...",
        ]
        # done/end/out 是 ink 流程控制词（翻译破坏对话流程）
        assert "done" not in originals
        assert "end" not in originals
        assert "out" not in originals

    def test_json_path_key(self):
        es = ex._ink_entries("t", 1, INK_SAMPLE.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        assert es[0].key_path == "root/2/Setyo_WakeUp/0"
        assert es[0].meta["ink_block"] == "Setyo_WakeUp"
        assert es[0].meta["ink_seq"] == 0
        assert es[1].meta["ink_seq"] == 1

    def test_ink_base_from_name(self):
        es = ex._ink_entries("t", 1, INK_SAMPLE.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        assert es[0].meta["ink_base"] == "Chapter1"

    def test_non_en_version_skipped(self):
        # 非英文语言版（CHN/ITA/GER...，m_Name 后缀判定）→ 整文件跳过
        # （游戏语言设置只有英文时只有 EN 版被读取——Rendezvous 实证）
        zh = json.dumps({
            "inkVersion": 19,
            "root": [["done", None], {"Setyo_WakeUp": [
                "^塞托: 又宿醉了。", "^塞托: 还有那个梦..."]}],
        }, ensure_ascii=False)
        skipped: dict = {}
        es = ex._ink_entries("t", 1, zh.encode("utf-8-sig"),
                             "test.assets", skipped, "Chapter1_CHN")
        assert es == []
        assert skipped.get("ink_non_en_version", 0) == 1
        # 拉丁字母语言版（ITA）同样跳过（内容级 CJK 检测不可分辨）
        it = json.dumps({
            "inkVersion": 19,
            "root": [["done", None], {"Block": [
                "^Setyo: Di nuovo i postumi..."]}],
        }, ensure_ascii=False)
        es2 = ex._ink_entries("t", 1, it.encode("utf-8-sig"),
                              "test.assets", {}, "Chapter1_ITA")
        assert es2 == []

    def test_flow_structure_skipped(self):
        # divert 目标（"->" 键值）与标签（#n 键值）不提取
        data = json.dumps({
            "inkVersion": 19,
            "root": [[["done"], None], {"Block": ["^Hi", {"->": "Other"},
                                                 {"#f": 1, "#n": "tag"}]}],
        }, ensure_ascii=False)
        es = ex._ink_entries("t", 1, data.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        assert [e.original for e in es] == ["^Hi"]

    def test_bare_flow_tokens_filtered(self):
        # ink 编译产物裸流程 token（无 ^ 前缀）：字节码操作/变量指令/自定义
        # handler 名。project-arrhythmia 实证——被当对话行译成「出去」等。
        data = json.dumps({
            "inkVersion": 19,
            "root": [[["done"], None],
                    {"Block": ["^Real line.",
                               "ev", "out", "/ev", "GetVar", "pop", "nop",
                               "_id", "spawnActor", "setEmotion", "du",
                               "StartShop", "MoveToPlace"]}],
        }, ensure_ascii=False)
        es = ex._ink_entries("t", 1, data.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        assert [e.original for e in es] == ["^Real line."]

    def test_bare_unknown_single_token_kept(self):
        # 未知单 token 裸串（不在流程全集）→ fail-open 保留（宁漏勿坏）
        data = json.dumps({
            "inkVersion": 19,
            "root": [[["done"], None],
                    {"Block": ["^Real line.", "MysteryToken"]}],
        }, ensure_ascii=False)
        es = ex._ink_entries("t", 1, data.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        assert [e.original for e in es] == ["^Real line.", "MysteryToken"]

    def test_dot_and_dollar_refs_skipped(self):
        # 点连无空格 = divert/choice 目标引用；$ 前缀 = 寄存器引用。非对话。
        data = json.dumps({
            "inkVersion": 19,
            "root": [[["done"], None],
                    {"Block": ["^Real line.", ".^.c-0", "Start.0.g-0.2.$r1",
                               "$r", "$r1"]}],
        }, ensure_ascii=False)
        es = ex._ink_entries("t", 1, data.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        assert [e.original for e in es] == ["^Real line."]

    def test_str_tag_block_identifiers_filtered(self):
        # str.../str 块内 ^ = 运行时标识符（^hal 动画/^angry 情绪）
        # #.../# 块内 ^ = 标签元数据/命令模板（^actor:PM_25.01/^Spawn ->）
        data = json.dumps({
            "inkVersion": 19,
            "root": [[["done"], None],
                    {"Block": ["ev", "str", "^hal", "/str", "/ev",
                               "#", "^actor:PM_25.01", "/#",
                               "^Real line.",
                               "ev", "str", "^rt.tonn.02.A$ What are you doing here?", "/str", "/ev"]}],
        }, ensure_ascii=False)
        es = ex._ink_entries("t", 1, data.encode("utf-8-sig"),
                             "test.assets", {}, "Chapter1_EN")
        # 块内标识符全部跳过；顶层对话与 choice 文本（$ 引用后显示文本）保留
        assert [e.original for e in es] == [
            "^Real line.",
            "^rt.tonn.02.A$ What are you doing here?",
        ]


class TestCsvOverwriteSource:
    CSV = ("ID,IND,ENG,FRE,CHN\n"
           "IName_Medkit,,Med-Kit,Kit médical,医疗包\n"
           "BTN_Load,,Load Save,Charger,,\n")

    def test_pick_target_col_chn(self):
        assert pick_target_col(
            ["ID", "IND", "ENG", "FRE", "CHN"], "zh-CN") == 4

    def test_overwrite_mode_meta(self):
        entries, _ = extract_csv_text(
            self.CSV, overwrite_source=True)
        pend = [e for e in entries if e.status != "skipped"]
        assert len(pend) == 2
        assert pend[0].meta["target_col"] == 2  # 写回源列 ENG
        assert pend[0].meta["overwrite_source"] is True
        assert pend[0].meta["official_zh"] == "医疗包"  # 官方中文搬运
        assert "official_zh" not in pend[1].meta  # CHN 空行无搬运

    def test_normal_mode_skips_filled_target(self):
        entries, tc = extract_csv_text(self.CSV)
        assert tc == 4
        statuses = [(e.status, e.meta.get("reason")) for e in entries]
        assert ("skipped", "target_col_already_filled") in statuses
        pend = [e for e in entries if e.status != "skipped"]
        assert len(pend) == 1

    def test_apply_csv_overwrite_source(self):
        entries, _ = extract_csv_text(self.CSV, overwrite_source=True)
        for e in entries:
            e.translation = e.meta.get("official_zh") or "加载存档"
        out = apply_csv(entries, self.CSV, target_col=2)
        rows = {r.split(",")[0]: r.split(",") for r in out.splitlines()}
        # 官方中文写入 ENG 列（index 2）
        assert rows["IName_Medkit"][2] == "医疗包"
        assert rows["BTN_Load"][2] == "加载存档"
        # CHN 列官方内容保留
        assert rows["IName_Medkit"][4] == "医疗包"
