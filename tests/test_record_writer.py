"""记录文档哨兵测试（审计 P2-9）：豁免放行统计哨兵 + 跳过分布聚合。

哨兵是根因 C（无反馈闭环）的收口：跳过/回显豁免是正常机制，但异常
比例是「大块形态未识别」的哑信号——阈值告警写进 summary.md，用户
第一眼可见，不再等实测发现问题。
"""
import json

from hanhua.core.batch_translator import _rules_version
from hanhua.core.memory import ProjectStore
from hanhua.core.record_writer import (
    _budget_exhausted_of, _exemption_sentinels, _morphology_stats,
    _skipped_by_reason, _export_text_records)

# 形态分类测试路径（classify_morphology 的 rel 语义）
_UNITYSCRIPT_DLL = "Game_Data/Managed/Assembly-UnityScript.dll"   # dense
_ASSET = "Game_Data/level1"                                       # mixed
_META = "Game_Data/il2cpp_data/Metadata/global-metadata.dat"      # mixed


def _store(tmp_path, rows: list[dict]) -> ProjectStore:
    """rows 元素：(original, meta?, translation?, status?)。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("f1", "file.txt", "txt", "utf-8", "")
    for i, row in enumerate(rows):
        entry = {"file_id": "f1", "key_path": f"k{i}", "original": row[0]}
        if len(row) > 1:
            entry["meta"] = row[1]
        if len(row) > 2:
            entry["translation"] = row[2]
        if len(row) > 3:
            entry["status"] = row[3]
        store.upsert_entries([entry])
    return store


def _normal(tmp_path, total: int) -> ProjectStore:
    """全 pending 基线（无跳过无豁免 → 无哨兵）。"""
    return _store(tmp_path, [("Hello player",) for _ in range(total)])


def test_no_warnings_on_balanced_store(tmp_path):
    """基线：无跳过/无豁免 → 哨兵不误报。"""
    rows = [("Translated text", {}, "译文", "translated") for _ in range(50)]
    rows += [("Pending text",) for _ in range(30)]
    assert _exemption_sentinels(_store(tmp_path, rows)) == []


def test_skip_rate_warning_fires_over_threshold(tmp_path):
    """跳过率 >70% 且 ≥30 条 → 显式告警（含跳过率与处置指引）。"""
    rows = [("Hello player",) for _ in range(10)]
    rows += [("skipped one",
              {"reason": "prefilter_engine_string", "skipped_count": 40},
              None, "skipped") for _ in range(3)]
    warnings = _exemption_sentinels(_store(tmp_path, rows))
    assert any("跳过率" in w and "异常高" in w for w in warnings)


def test_skip_rate_no_warning_below_minimum_sample(tmp_path):
    """小样本（<30 条跳过）不告警——防阈值误报（小游戏正常跳过少）。"""
    rows = [("Hello player",) for _ in range(2)]
    rows += [("skipped one",
              {"reason": "prefilter_engine_string", "skipped_count": 5},
              None, "skipped") for _ in range(1)]
    assert _exemption_sentinels(_store(tmp_path, rows)) == []


def test_echo_exempt_warning_fires_over_threshold(tmp_path):
    """回显豁免 >30% 且 ≥10 条 → 告警（模型大面积未翻译的信号）。"""
    rows = []
    for i in range(12):
        rows.append(("ProperName", {"echo_exempt": "proper_name"},
                     "ProperName", "translated"))
    for _ in range(18):
        rows.append(("Translated text", {}, "译文", "translated"))
    warnings = _exemption_sentinels(_store(tmp_path, rows))
    assert any("回显豁免" in w and "未翻译" in w for w in warnings)


def test_echo_exempt_no_warning_below_threshold(tmp_path):
    """回显豁免占比低（10%）→ 不告警。"""
    rows = [("ProperName", {"echo_exempt": "proper_name"}, "ProperName",
             "translated") for _ in range(2)]
    for _ in range(18):
        rows.append(("Translated text", {}, "译文", "translated"))
    assert _exemption_sentinels(_store(tmp_path, rows)) == []


def test_dominant_reason_warning_fires_on_concentration(tmp_path):
    """单一跳过原因 >90% 且 ≥30 条 → 提示复核该形态。"""
    rows = [("Hello player",) for _ in range(2)]
    rows += [("skipped one",
              {"reason": "prefilter_engine_string", "skipped_count": 30},
              None, "skipped") for _ in range(3)]
    warnings = _exemption_sentinels(_store(tmp_path, rows))
    assert any("集中于单一原因" in w and "prefilter_engine_string" in w
               for w in warnings)


def test_dominant_reason_no_warning_when_mixed(tmp_path):
    """跳过原因分散（无单一 >90%）→ 不提示；跳过率正常（≤70%）时不告警。"""
    rows = [("Hello player",) for _ in range(40)]
    rows += [("a", {"reason": "r1", "skipped_count": 20}, None, "skipped")
             for _ in range(1)]
    rows += [("b", {"reason": "r2", "skipped_count": 20}, None, "skipped")
             for _ in range(1)]
    assert _exemption_sentinels(_store(tmp_path, rows)) == []


def test_skipped_by_reason_aggregates_prefilter_samples_and_plain():
    """分布聚合：skipped_count 承载真实总数（样本留档，回写为单元最终
    计数），普通条目计 1。不同单元（file_id）各自聚合后求和；同单元
    多条样本（回写后值相同）只取一次（max 语义——累计计数 1..10 被
    求和的 55 失真修复）。"""
    rows = [
        {"file_id": "a", "status": "skipped",
         "meta": {"reason": "prefilter_engine_string", "skipped_count": 40}},
        {"file_id": "b", "status": "skipped",
         "meta": {"reason": "prefilter_engine_string", "skipped_count": 12}},
        {"file_id": "a", "status": "skipped",
         "meta": {"reason": "prefilter_engine_string", "skipped_count": 40}},
        {"status": "skipped", "meta": {"reason": "code_line"}},
        {"status": "skipped", "meta": {}},
    ]
    dist = _skipped_by_reason(rows)
    assert dist["prefilter_engine_string"] == 52
    assert dist["code_line"] == 1
    assert dist["unknown"] == 1


def test_sentinel_meta_handles_stringified_json(tmp_path):
    """meta 是字符串 JSON 的行（GUI 存储形态）也能正确统计豁免。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.add_file("f1", "file.txt", "txt", "utf-8", "")
    store.upsert_entries([{
        "file_id": "f1", "key_path": f"e{i}",
        "original": "ProperName",
        "translation": "ProperName",
        "meta": json.dumps({"echo_exempt": "proper_name"}),
        "status": "translated",
    } for i in range(12)] + [{
        "file_id": "f1", "key_path": f"t{i}",
        "original": "Text", "translation": "译文",
        "meta": json.dumps({"quality_passed": True}),
        "status": "translated",
    } for i in range(18)])
    warnings = _exemption_sentinels(store)
    assert any("回显豁免" in w for w in warnings)


# ── 翻译 C7：翻译质量哨兵（失败率/语言源保留率/预算耗尽率）────────

def test_fail_rate_warning_fires_over_threshold(tmp_path):
    """失败率 >40% 且 ≥15 条 → 告警（该翻未翻集中出现的可见性）。"""
    rows = [("Translated", {}, "译文", "translated") for _ in range(20)]
    rows += [("Failed", {"reason": "request_timeout"}, None, "failed")
             for _ in range(15)]
    warnings = _exemption_sentinels(_store(tmp_path, rows))
    assert any("失败率" in w and "异常高" in w for w in warnings)


def test_fail_rate_no_warning_below_minimum(tmp_path):
    """失败条数少（<15）→ 不告警（个别失败正常）。"""
    rows = [("Translated", {}, "译文", "translated") for _ in range(5)]
    rows += [("Failed", {"reason": "request_timeout"}, None, "failed")
             for _ in range(3)]
    assert _exemption_sentinels(_store(tmp_path, rows)) == []


def test_language_source_kept_warning_fires_over_threshold(tmp_path):
    """语言源保留 >30% 且 ≥10 条 → 提示多语言游戏（中文包可选用）。"""
    rows = [("中文原文", {"language_source_kept": True}, "中文原文",
             "translated") for _ in range(12)]
    for _ in range(18):
        rows.append(("Translated", {}, "译文", "translated"))
    warnings = _exemption_sentinels(_store(tmp_path, rows))
    assert any("语言源保留" in w and "多语言游戏" in w for w in warnings)


def test_language_source_kept_no_warning_below_threshold(tmp_path):
    """语言源保留占比低 → 不提示。"""
    rows = [("中文原文", {"language_source_kept": True}, "中文原文",
             "translated") for _ in range(2)]
    for _ in range(18):
        rows.append(("Translated", {}, "译文", "translated"))
    assert _exemption_sentinels(_store(tmp_path, rows)) == []


def test_budget_exhausted_warning_fires_over_threshold(tmp_path):
    """预算耗尽占失败 >60% 且 ≥20 条 → 告警（失败被大量放弃无痕）。"""
    v = _rules_version()
    rows = []
    for _ in range(20):
        rows.append(("Failed", {"_rules_version": v, "attempt_count": 3,
                                "failure_category": "request"},
                     None, "failed"))
    for _ in range(10):
        rows.append(("Failed", {"reason": "request_timeout"}, None, "failed"))
    warnings = _exemption_sentinels(_store(tmp_path, rows))
    assert any("预算耗尽" in w and "放弃" in w for w in warnings)


def test_budget_exhausted_no_warning_when_version_stale(tmp_path):
    """旧规则版本戳的 attempt 计数不算耗尽（C2：规则升级自动重置）——
    失败率低于阈值时预算哨兵不触发。"""
    rows = [("Translated", {}, "译文", "translated") for _ in range(60)]
    rows += [("Failed", {"_rules_version": -1, "attempt_count": 99,
                         "failure_category": "request"},
              None, "failed") for _ in range(25)]
    assert _exemption_sentinels(_store(tmp_path, rows)) == []


def test_budget_exhausted_of_matches_translator_semantics(tmp_path):
    """_budget_exhausted_of 与 batch_translator 同源：request 上限 3、
    model_behavior 上限 2、content_inherent 上限 1。"""
    v = _rules_version()
    base = {"_rules_version": v, "attempt_count": 3,
            "failure_category": "request"}
    assert _budget_exhausted_of({"meta": base}) is True
    base2 = dict(base, attempt_count=2)
    assert _budget_exhausted_of({"meta": base2}) is False
    mb = {"_rules_version": v, "attempt_count": 2,
          "failure_category": "model_behavior"}
    assert _budget_exhausted_of({"meta": mb}) is True
    inh = {"_rules_version": v, "attempt_count": 1,
           "failure_category": "content_inherent"}
    assert _budget_exhausted_of({"meta": inh}) is True


def test_summary_includes_sentinel_section(tmp_path, monkeypatch):
    """哨兵告警写入 summary.md（用户第一眼可见），不依赖具体数据场景。"""
    from hanhua.core.record_writer import export_records
    from tests.test_scanner import _make_tree
    from hanhua.core.project import Project

    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, tmp_path / "app")
    proj.scan()
    row = next(r for r in proj.store.get_entries()
               if r["status"] == "pending")
    proj.store.set_manual(row["file_id"], row["key_path"], "已翻译")

    monkeypatch.setattr(
        "hanhua.core.record_writer._exemption_sentinels",
        lambda store: ["跳过率 90% 异常高——测试告警"])
    out_root = tmp_path / "records"
    export_records(proj, out_root)
    rec_dir = out_root / proj.game_dir.name
    summary = (rec_dir / "summary.md").read_text(encoding="utf-8")
    assert "哨兵告警" in summary
    assert "跳过率 90% 异常高" in summary


def test_summary_memory_reject_rate_warning(tmp_path, monkeypatch):
    """记忆拒绝率 >50% 且应用 ≥10 次 → summary 记忆节显式告警
    （记忆毒化复发信号：应用被质量门拒绝比例大）。"""
    from hanhua.core.record_writer import export_records
    from tests.test_scanner import _make_tree
    from hanhua.core.project import Project

    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, tmp_path / "app")
    proj.scan()
    monkeypatch.setattr(
        "hanhua.core.record_writer._exemption_sentinels",
        lambda store: [])
    out_root = tmp_path / "records"
    export_records(proj, out_root, agent_report={
        "session": {"proposed": 5, "evidence_added": 2, "confirmed": 1,
                    "direct_applied": 10, "accepted": 3, "rejected": 7,
                    "retired": 0, "conflicts": 0},
        "library": {}, "game": "t", "top_memories": [], "conflicts": [],
    })
    summary = (out_root / proj.game_dir.name / "summary.md").read_text(
        encoding="utf-8")
    assert "记忆拒绝率" in summary and "异常高" in summary


# ── 识别 L2：形态×reason 矩阵哨兵 ─────────────────────────────

def _store_morph(tmp_path, rows: list[tuple[str, str, dict, str, str]]) -> ProjectStore:
    """rows：(file_id(rel 路径), original, meta, status, translation?)。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    seen: dict[str, str] = {}
    for i, (fid, original, meta, status, *rest) in enumerate(rows):
        if fid not in seen:
            store.add_file(fid, fid, "txt", "utf-8", "")
            seen[fid] = fid
        entry = {"file_id": fid, "key_path": f"k{i}", "original": original,
                 "meta": meta, "status": status}
        if rest:
            entry["translation"] = rest[0]
        store.upsert_entries([entry])
    return store


def test_morphology_stats_aggregates_samples_and_plain(tmp_path):
    """形态聚合：dense 形态样本 skipped_count 承载真实总数、普通行计 1，
    按 file_id 分类；未注册形态归 unknown。"""
    store = _store_morph(tmp_path, [
        (_UNITYSCRIPT_DLL, "对话一", {"reason": "mono_diagnostic",
                                      "skipped_count": 30}, "skipped"),
        (_UNITYSCRIPT_DLL, "对话二", {}, "pending"),
        (_ASSET, "Text", {}, "translated", "译文"),
        ("Game_Data/Managed/SomeNew.dll", "x", {}, "pending"),
    ])
    stats = _morphology_stats(store.get_entries())
    us = stats["mono_unityscript"]
    assert us["skipped"] == 30            # 样本聚合真实总数
    assert us["pending"] == 1
    assert stats["asset_unity"]["translated"] == 1
    assert stats["mono_other"]["pending"] == 1  # 非标准前缀 DLL 归 mono_other


def test_dense_sentinel_fires_over_threshold(tmp_path):
    """dense 形态（UnityScript 程序集）跳过过半且 ≥20 条 → 告警
    （整形态遗漏信号：lilys-day-off 825 条教训的自动化）。"""
    store = _store_morph(tmp_path, [
        (_UNITYSCRIPT_DLL, "对话", {"reason": "mono_diagnostic",
                                    "skipped_count": 40}, "skipped"),
    ] + [(_UNITYSCRIPT_DLL, f"对话{i}", {}, "pending") for i in range(10)])
    warnings = _exemption_sentinels(store)
    assert any("dense 形态 mono_unityscript" in w
               and "整形态遗漏" in w for w in warnings)


def test_dense_sentinel_no_warning_below_minimum(tmp_path):
    """dense 形态跳过但样本小（<20）→ 不告警。"""
    store = _store_morph(tmp_path, [
        (_UNITYSCRIPT_DLL, "对话", {"reason": "mono_diagnostic",
                                    "skipped_count": 5}, "skipped"),
    ] + [(_UNITYSCRIPT_DLL, f"对话{i}", {}, "pending") for i in range(3)])
    warnings = _exemption_sentinels(store)
    assert not any("dense 形态" in w for w in warnings)


def test_dense_sentinel_no_warning_for_mixed_prior(tmp_path):
    """mixed 形态（资产/IL2CPP）全跳过不触发 dense 哨兵——跳过是
    mixed 形态的正常主体，只有先验声明 dense 才可校验。"""
    store = _store_morph(tmp_path, [
        (_ASSET, f"v{i}", {"reason": "typetree_prefilter",
                           "skipped_count": 60}, "skipped")
        for i in range(3)] + [
        (_META, f"v{i}", {"reason": "engine_morph"}, "skipped")
        for i in range(25)])
    warnings = _exemption_sentinels(store)
    assert not any("dense 形态" in w for w in warnings)


def test_summary_includes_morphology_matrix(tmp_path, monkeypatch):
    """summary 含形态分布矩阵：每形态一行（先验/文件/条目/主导跳过
    原因），形态清单从审计清单变为可对照的运行分布。"""
    from hanhua.core.record_writer import export_records
    from tests.test_scanner import _make_tree
    from hanhua.core.project import Project

    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, tmp_path / "app")
    proj.scan()
    row = next(r for r in proj.store.get_entries()
               if r["status"] == "pending")
    proj.store.set_manual(row["file_id"], row["key_path"], "已翻译")

    monkeypatch.setattr(
        "hanhua.core.record_writer._exemption_sentinels",
        lambda store: [])
    out_root = tmp_path / "records"
    export_records(proj, out_root)
    summary = (out_root / proj.game_dir.name / "summary.md").read_text(
        encoding="utf-8")
    assert "形态分布（识别 L2）" in summary
    assert any(line.startswith("  - ") and "条目" in line
               for line in summary.splitlines())


def test_summary_memory_reject_rate_no_warning_when_healthy(
        tmp_path, monkeypatch):
    """拒绝占比低/样本小 → 不告警。"""
    from hanhua.core.record_writer import export_records
    from tests.test_scanner import _make_tree
    from hanhua.core.project import Project

    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, tmp_path / "app")
    proj.scan()
    monkeypatch.setattr(
        "hanhua.core.record_writer._exemption_sentinels",
        lambda store: [])
    out_root = tmp_path / "records"
    export_records(proj, out_root, agent_report={
        "session": {"proposed": 5, "evidence_added": 2, "confirmed": 1,
                    "direct_applied": 8, "accepted": 7, "rejected": 1,
                    "retired": 0, "conflicts": 0},
        "library": {}, "game": "t", "top_memories": [], "conflicts": [],
    })
    summary = (out_root / proj.game_dir.name / "summary.md").read_text(
        encoding="utf-8")
    assert "记忆拒绝率" not in summary


def test_font_gate_and_coverage_in_records(tmp_path, monkeypatch):
    """Phase 4：writeback.txt / summary.md 输出字体发布门 + 逐栈/逐码点
    覆盖摘要（计划 §11 统一口径——GUI/runner 共用 verification）。"""
    from hanhua.core.record_writer import export_records
    from tests.test_scanner import _make_tree
    from hanhua.core.project import Project

    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, tmp_path / "app")
    proj.scan()
    monkeypatch.setattr(
        "hanhua.core.record_writer._exemption_sentinels",
        lambda store: [])
    result = {
        "text_files": 2,
        "font": type("F", (), {
            "family": "NotoSerifCJKsc-Medium.otf",
            "level": "runtime_fallback",
            "installed": True})(),
        "verification": {
            "input_protected": True,
            "reopen_verified": True,
            "changed_files": 3,
            "written_translations": 1,
            "overall": "PASS",
            "font_level": "runtime_fallback",
            "font_gate": {"status": "BLOCKED",
                          "detail": "存在缺字/未覆盖消费者，默认阻断发布"},
            "font_coverage": {
                "overall": "CANDIDATE_ONLY",
                "stack_counts": {"tmp_font": 2, "legacy_font": 1},
                "state_counts": {"COVERED": 2, "CANDIDATE_ONLY": 1},
                "missing": [{"scalar": "设 (U+8BBE)",
                             "consumer": "bundle#font1",
                             "kind": "tmp_font",
                             "locators": ["en.json:title"]}],
            },
            "font_bitmap": {
                "providers": ["bmfont"],
                "injected": 1, "audited": 1, "pending": 0,
            },
        },
    }
    out_root = tmp_path / "records"
    export_records(proj, out_root, write_result=result)
    rec_dir = out_root / proj.game_dir.name
    writeback = (rec_dir / "writeback" / "writeback.txt").read_text(
        encoding="utf-8")
    assert "发布门：BLOCKED" in writeback
    assert "逐栈：legacy_font: 1 · tmp_font: 2" in writeback
    assert "缺字 Top-1" in writeback
    assert "设 (U+8BBE)" in writeback and "en.json:title" in writeback
    assert "位图注入：provider 1 个（bmfont）· 注入 1 · 审计 1 · 未注入 0" in writeback
    summary = (rec_dir / "summary.md").read_text(encoding="utf-8")
    assert "字体发布门：BLOCKED" in summary
    assert "字体覆盖：CANDIDATE_ONLY" in summary
    assert "tmp_font: 2" in summary
    assert "位图注入：1 个 provider · 注入 1 · 未注入 0" in summary


def test_export_text_records_review_meta_line(tmp_path):
    """#43 阶段 F：审校元数据透出到 translated.txt——
    人工修正（review_outcome=APPROVED/MANUAL）与批量审核终态
    （NEEDS_REVISION + 风险分）两种形态；无审校字段的条目不出现该行。"""
    from hanhua.core.record_writer import export_records
    from tests.test_scanner import _make_tree
    from hanhua.core.project import Project

    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, tmp_path / "app")
    proj.scan()
    rows = proj.store.get_entries()
    pending = [r for r in rows if r["status"] == "pending"]
    # 条目 A：人工修正（审校页直接编辑的落库形态）
    proj.store.apply_manual_correction(
        pending[0]["file_id"], pending[0]["key_path"], "人工译文A")
    # 条目 B：批量审核终态 + 风险分（review_entries 落库形态——
    # batch_update_translation_results 合并 meta 写终态）
    from hanhua.core.models import TextEntry
    p1 = pending[1]
    proj.store.batch_update_translation_results([TextEntry(
        file_id=p1["file_id"], key_path=p1["key_path"],
        original=p1["original"], translation="审核译文B",
        status="translated",
        meta={"review_outcome": "NEEDS_REVISION", "review_level": "MAJOR",
              "risk_score": 65, "risk_level": "HIGH",
              "quality_passed": True})])
    # 条目 C：无审校字段（旧记录形态，不补行）
    proj.store.update_translation(
        pending[2]["file_id"], pending[2]["key_path"], "普通译文C")
    out_root = tmp_path / "records"
    export_records(proj, out_root)
    text = (out_root / proj.game_dir.name / "text" / "translated.txt").read_text(
        encoding="utf-8")
    assert "审核：APPROVED（MANUAL）" in text
    assert "审核：NEEDS_REVISION（MAJOR） · 风险 65 HIGH" in text
    # 条目 C 没有「审核：」行
    assert "普通译文C" in text
    assert text.count("审核：") == 2


def test_export_records_review_summary_writes_report(tmp_path):
    """P4（2026-09-06 fromivan 实证「审核的内容没有记录，只在运行记录
    中临时记录了」）：export_records 传入 review_summary 后——
    ①生成 review/review-report.md（全量送审明细，含 PASS 条目）；
    ②summary.md 补 §3.5 语义审核节（审核条数/不合格/阻断/终态分布）；
    ③记录文件清单包含 review/review-report.md。不传时三者都不出现。"""
    from hanhua.core.record_writer import export_records
    from tests.test_scanner import _make_tree
    from hanhua.core.project import Project

    game_dir = _make_tree()
    proj = Project.open_game_dir(game_dir, tmp_path / "app")
    proj.scan()
    rows = proj.store.get_entries()
    pending = [r for r in rows if r["status"] == "pending"]
    proj.store.update_translation(
        pending[0]["file_id"], pending[0]["key_path"], "审核过的译文")
    proj.store.update_translation(
        pending[1]["file_id"], pending[1]["key_path"], "被阻断的译文")

    eid_a = f'{pending[0]["file_id"]}:{pending[0]["key_path"]}'
    eid_b = f'{pending[1]["file_id"]}:{pending[1]["key_path"]}'
    review_summary = {
        "used": True,
        "reviewed": 2, "sent": 2, "flagged": 1, "blocked": 1,
        "pairs_added": 1, "pairs_rejected": [],
        "outcomes": {"APPROVED": 1, "NEEDS_REVISION": 1},
        "levels": {"PASS": 1, "CRITICAL": 1},
        # GUI 形态：flagged/results 都以 entry_id 为键，locators 映射
        # 回 file_id:key_path
        "results": {
            "eid-A": type("RR", (), {
                "entry_id": "eid-A", "level": "PASS",
                "reason": "译文准确", "issues": [], "reviewed": True,
                "verdict": "PASS", "issue": "", "suggestion": "",
                "error": "", "overall_score": 92, "dimensions": {}})(),
        },
        "locators": {"eid-A": eid_a},
        "detail": [
            {"locator": eid_a, "text_type": "display",
             "original": "Hello", "translation": "审核过的译文",
             "final_translation": "审核过的译文", "level": "PASS",
             "reason": "译文准确", "suggestion": "", "issues": [],
             "overall_score": 92, "dimensions": {}, "quality_reasons": [],
             "outcome": "APPROVED", "review_round": 0},
            {"locator": eid_b, "text_type": "display",
             "original": "Bye", "translation": "被阻断的译文",
             "final_translation": "", "level": "CRITICAL",
             "reason": "语义相反，重译未收敛", "suggestion": "重新翻译",
             "issues": ["语义相反"], "overall_score": 20,
             "dimensions": {"准确性": 1}, "quality_reasons": ["语义相反"],
             "outcome": "BLOCKED", "review_round": 2},
        ],
    }
    out_root = tmp_path / "records"
    export_records(proj, out_root, review_summary=review_summary)
    rec_dir = out_root / proj.game_dir.name

    report = (rec_dir / "review" / "review-report.md").read_text(
        encoding="utf-8")
    assert "全量送审明细" in report
    assert "Hello" in report and "审核过的译文" in report
    assert "译文准确" in report
    assert "语义相反，重译未收敛" in report

    summary = (rec_dir / "summary.md").read_text(encoding="utf-8")
    assert "## 3.5 语义审核" in summary
    assert "审核条数：2" in summary
    assert "不合格：1" in summary
    assert "重译未收敛阻断：1" in summary
    assert "术语沉淀：1" in summary
    assert "APPROVED: 1" in summary
    assert "review/review-report.md" in summary

    # 基线：不传 review_summary 时不生成报告、无 §3.5
    out_root2 = tmp_path / "records2"
    export_records(proj, out_root2)
    rec_dir2 = out_root2 / proj.game_dir.name
    assert not (rec_dir2 / "review" / "review-report.md").exists()
    summary2 = (rec_dir2 / "summary.md").read_text(encoding="utf-8")
    assert "## 3.5 语义审核" not in summary2
