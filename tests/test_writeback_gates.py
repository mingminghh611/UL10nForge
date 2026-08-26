"""写回安全闸门测试（指南 §14 P0-1/P0-2/P0-3 + P0-4 不可变字段）。

覆盖：四态闸门评估、rejected/truncated 阻断默认发布与 allow_partial
放行、source/target manifest 持久化、不可变字段集合收集与重开校验。
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hanhua.core.memory import ProjectStore
from hanhua.core.models import FontConfig, WriteRejection
from hanhua.core.project import Project
from hanhua.core.unity.writer import (
    WriteResult,
    _collect_immutable_values,
    _verify_saved_bundle,
)
from tests.test_scanner import _make_tree
from tests.test_project import _install_fake_raw_asset_environment


def _fake_font(installed=False, payload_deployed=False, runtime_verified=False,
               provider_supported=True, unsupported_reason=""):
    return SimpleNamespace(
        installed=installed, payload_deployed=payload_deployed,
        runtime_verified=runtime_verified,
        provider_supported=provider_supported,
        unsupported_reason=unsupported_reason)


def _fake_v2(files=0, entries=0):
    return SimpleNamespace(files=files, entries=entries)


def _gates(project: Project, *, v2=None, font=None, rejected=(), truncated=0,
           text_files=1, text_verified=1, allow_partial=False, ready_text=1,
           font_enabled=False, written_total=0,
           logic_mismatch_count=0, logic_reverted=0,
           font_candidate_confirm=None, font_coverage=None):
    return project._evaluate_writeback_gates(
        text_files=text_files, v2=v2 if v2 is not None else _fake_v2(files=1, entries=1),
        text_verified=text_verified,
        font=font if font is not None else _fake_font(),
        font_level="disabled", active_font_config=FontConfig(enabled=font_enabled),
        rejected=list(rejected), truncated=truncated,
        allow_partial=allow_partial, ready_text_translations=ready_text,
        written_total=written_total, logic_mismatch_count=logic_mismatch_count,
        logic_reverted=logic_reverted, font_candidate_confirm=font_candidate_confirm,
        font_coverage=font_coverage)


# ── P0-1：四态闸门评估 ──

def test_runtime_gate_follows_font_candidate_confirm(tmp_path):
    """hickory 实证回归（2026-08-13）：runner 传
    allow_unverified_font_candidate=True + allow_partial=False 时，四态
    闸门的 runtime 门必须跟随候选确认（WARN）而不是用 allow_partial
    重估（BLOCKED）——pipeline 内门与四态闸门终态必须一致。"""
    from hanhua.core.font import FontConsumer, compute_coverage
    from hanhua.core.font.glyph_set import build_required_glyph_set
    from hanhua.core.project import _GlyphEntry
    required = build_required_glyph_set([_GlyphEntry("f", "k", "设置", "设置")])
    consumers = [
        FontConsumer("bundle#f1", "tmp_font", static_replaced=True,
                     font_scalars=frozenset(ord(c) for c in "设置"),
                     unity_version="2022.3"),
        FontConsumer("bundle#dyn", "dynamic_tmp",
                     runtime_provider_available=True,
                     ref="dynamic 0 glyph——静态无法证明覆盖"),
    ]
    coverage = compute_coverage(consumers, required)
    assert coverage.overall.name == "PENDING_RUNTIME_ATTESTATION"
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    font = _fake_font(payload_deployed=True)  # runtime_verified=False

    # runner 场景：候选确认 True + allow_partial False → runtime=WARN
    gates = _gates(proj, font=font, font_enabled=True,
                   allow_partial=False, font_candidate_confirm=True,
                   font_coverage=coverage)
    assert gates["runtime"]["status"] == "WARN"
    # 未确认：即使 allow_partial=False 也是 BLOCKED（默认严格）
    gates2 = _gates(proj, font=font, font_enabled=True,
                    allow_partial=False, font_coverage=coverage)
    assert gates2["runtime"]["status"] == "BLOCKED"
    # 跟随 allow_partial（None 语义）
    gates3 = _gates(proj, font=font, font_enabled=True,
                    allow_partial=True, font_coverage=coverage)
    assert gates3["runtime"]["status"] == "WARN"


def test_gates_all_pass_when_everything_clean(tmp_path):
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj)
    assert gates["overall"]["status"] == "PASS"
    assert gates["file"]["status"] == "PASS"
    assert gates["container"]["status"] == "PASS"
    assert gates["object"]["status"] == "PASS"
    assert gates["runtime"]["status"] == "N/A"          # 未启用字体


def test_gates_object_blocked_without_allow_partial(tmp_path):
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, rejected=[WriteRejection("a:b", "reason")])
    assert gates["object"]["status"] == "BLOCKED"
    assert gates["overall"]["status"] == "BLOCKED"


def test_gates_object_warn_with_allow_partial(tmp_path):
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(
        proj, rejected=[WriteRejection("a:b", "reason")], allow_partial=True)
    assert gates["object"]["status"] == "WARN"
    assert gates["overall"]["status"] == "WARN"


def test_gates_truncated_warns_but_does_not_block_default_publish(tmp_path):
    """截断 = 容量内部分翻译（主体+省略号已写入），进报告 WARN 不阻断——
    1 条超长译文不应拖垮整场写回（taxes 'I did ' 实证）。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, truncated=3)
    assert gates["object"]["status"] == "WARN"
    assert gates["overall"]["status"] == "WARN"


# ── 写回 C6b：批量截断闸门联动 ──

def test_gates_bulk_truncated_blocks_default_publish(tmp_path):
    """C6b：批量截断（≥5 条且占待写 ≥10%）是语义残缺成片信号——
    默认 BLOCKED，不再无条件 WARN 照写。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, truncated=6, written_total=30)
    assert gates["object"]["status"] == "BLOCKED"
    assert gates["overall"]["status"] == "BLOCKED"
    assert "占待写" in gates["object"]["detail"]


def test_gates_bulk_truncated_warns_with_allow_partial(tmp_path):
    """C6b：批量截断经 allow_partial 确认后放行为 WARN（与 rejected 同级）。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, truncated=6, written_total=30, allow_partial=True)
    assert gates["object"]["status"] == "WARN"
    assert gates["overall"]["status"] == "WARN"


def test_gates_sparse_truncated_below_threshold_still_warns(tmp_path):
    """C6b：少量截断（<5 条）保持 WARN 不阻断（taxes 单条超长译文实证）。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, truncated=4, written_total=100)
    assert gates["object"]["status"] == "WARN"


def test_gates_low_ratio_truncated_not_bulk(tmp_path):
    """C6b：截断条数够多但占比 <10%（如整表容量小范围超限）不升级 BLOCKED。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, truncated=6, written_total=200)
    assert gates["object"]["status"] == "WARN"


# ── 写回 C6c：逻辑审计计数闸门联动 ──

def test_gates_logic_mismatch_blocks_even_without_rejected(tmp_path):
    """C6c：重开逻辑验证失败（字符串边界不一致）是写坏信号——即使异常
    路径被外层吞掉，闸门兜底 BLOCKED，绝不发布。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, logic_mismatch_count=1, allow_partial=True)
    assert gates["object"]["status"] == "BLOCKED"
    assert "逻辑验证失败" in gates["object"]["detail"]


def test_gates_bulk_logic_reverted_warns(tmp_path):
    """C6c：逻辑审计自动回退 ≥30% 待写条目 → WARN 提示输入绑定区域
    疑似大范围受损（回退本身安全，但该翻的键翻不了需人工关注）。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, logic_reverted=10, written_total=20)
    assert gates["object"]["status"] == "WARN"
    assert "自动回退" in gates["object"]["detail"]


def test_gates_small_logic_reverted_passes(tmp_path):
    """C6c：少量自动回退（<30%）是正常防护动作，不升级闸门。"""
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(proj, logic_reverted=2, written_total=20)
    assert gates["object"]["status"] == "PASS"


def test_gates_runtime_warn_when_unverified_payload(tmp_path):
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(
        proj,
        font=_fake_font(installed=True, payload_deployed=True,
                        runtime_verified=False),
        font_enabled=True)
    assert gates["runtime"]["status"] == "WARN"


def test_gates_overall_prefers_blocked_over_warn(tmp_path):
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    gates = _gates(
        proj, rejected=[WriteRejection("a:b", "reason")], allow_partial=True,
        font=_fake_font(), font_enabled=True)
    assert gates["object"]["status"] == "WARN"      # allow_partial 放行
    assert gates["runtime"]["status"] == "BLOCKED"  # 字体回退层不可验证
    assert gates["overall"]["status"] == "BLOCKED"  # BLOCKED 优先于 WARN


# ── P0-3：source/target manifest ──

def test_manifest_lists_all_files_with_hashes(tmp_path):
    proj = Project.open_game_dir(_make_tree(), tmp_path / "app")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    fingerprint = SimpleNamespace(unity_version="2022.3.34", runtime="mono")
    source_hashes = {
        "a.json": "src-hash-a",
        "b.txt": "src-hash-b",
    }
    output_hashes = {
        "a.json": "target-hash-a-changed",
        "b.txt": "src-hash-b",          # 未修改文件也列出
    }
    gates = _gates(proj)

    name = proj._write_publish_manifest(
        out_dir, source_hashes, output_hashes, fingerprint, gates, False)

    assert name == ".hanhua-manifest.json"
    manifest = json.loads((out_dir / name).read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    assert manifest["game"] == {"unity_version": "2022.3.34", "runtime": "mono"}
    assert manifest["changed_files"] == 1
    assert manifest["file_count"] == 2
    by_path = {item["path"]: item for item in manifest["files"]}
    assert by_path["a.json"]["changed"] is True
    assert by_path["a.json"]["target_sha256"] == "target-hash-a-changed"
    # 未修改文件：双 hash 一致且显式列出
    assert by_path["b.txt"]["changed"] is False
    assert by_path["b.txt"]["source_sha256"] == "src-hash-b"
    assert manifest["gates"]["overall"]["status"] == "PASS"


# ── P0-2：write_all 集成（默认阻断 / allow_partial 放行） ──

def _make_write_ready_project(tmp_path, monkeypatch):
    d = _make_tree()
    app_dir = tmp_path / "app"
    proj = Project.open_game_dir(d, app_dir)
    proj.scan()
    entry = next(row for row in proj.store.get_entries()
                 if row["status"] == "pending")
    proj.store.set_manual(entry["file_id"], entry["key_path"], "已翻译")
    return proj


def test_write_all_publishes_with_manifest_when_clean(tmp_path, monkeypatch):
    _install_fake_raw_asset_environment(monkeypatch)
    proj = _make_write_ready_project(tmp_path, monkeypatch)

    result = proj.write_all()

    assert result["verification"]["overall"] == "PASS"
    assert result["verification"]["gates"]["object"]["status"] == "PASS"
    assert result["verification"]["manifest"] == ".hanhua-manifest.json"
    manifest_path = proj.out_dir / ".hanhua-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["changed_files"] >= 1
    unchanged = [item for item in manifest["files"] if not item["changed"]]
    assert unchanged, "未修改文件也必须列入 manifest"


def test_write_all_blocks_default_publish_on_rejected(
        tmp_path, monkeypatch):
    _install_fake_raw_asset_environment(monkeypatch)
    proj = _make_write_ready_project(tmp_path, monkeypatch)
    fake_outcome = WriteResult(
        files=1, entries=1, attempted=2,
        rejected=[WriteRejection("fake:key", "test_reject")])

    def capture_v2(store, game_dir, staging, typetree_generator=None):
        return fake_outcome

    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_v2)

    with pytest.raises(RuntimeError, match="阻断默认发布"):
        proj.write_all()
    assert not proj.out_dir.exists(), "被阻断时不得发布副本"


def test_write_all_publishes_with_warn_on_truncated_entries(tmp_path, monkeypatch):
    """截断不再阻断发布：部分翻译已写入（容量内收尾+省略号），
    发布成功并带 WARN 闸门与截断报告。"""
    _install_fake_raw_asset_environment(monkeypatch)
    proj = _make_write_ready_project(tmp_path, monkeypatch)
    fake_outcome = WriteResult(
        files=1, entries=2, attempted=2, truncated=2,
        truncated_items=["「长文本」→「长文本…」"])

    def capture_v2(store, game_dir, staging, typetree_generator=None):
        return fake_outcome

    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_v2)

    result = proj.write_all()

    assert result["verification"]["overall"] == "WARN"
    assert result["verification"]["gates"]["object"]["status"] == "WARN"
    assert proj.out_dir.is_dir()
    assert len(result["verification"]["truncated_entries"]) == 1
    assert result["verification"]["writer_outcome"]["truncated"] == 2
    assert any("截断" in line for line in result["verification"]["warnings"])


def test_write_all_persists_reverted_locators_to_store(
        tmp_path, monkeypatch):
    """写回 C10：逻辑审计回退条目发布成功后持久化到 store（GUI 状态
    与游戏实际状态同步，不再显示 translated 与实际脱节）。"""
    _install_fake_raw_asset_environment(monkeypatch)
    proj = _make_write_ready_project(tmp_path, monkeypatch)
    entry = next(row for row in proj.store.get_entries()
                 if row["status"] == "translated")
    locator = f"{entry['file_id']}:{entry['key_path']}"
    fake_outcome = WriteResult(
        files=1, entries=1, attempted=1,
        reverted_locators={locator})

    def capture_v2(store, game_dir, staging, typetree_generator=None):
        return fake_outcome

    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_v2)

    result = proj.write_all()

    assert result["verification"]["reverted_persisted"] == 1
    row = proj.store.get_entries()
    updated = next(r for r in row if r["file_id"] == entry["file_id"]
                   and r["key_path"] == entry["key_path"])
    assert updated["status"] == "skipped"
    # 发布失败（闸门 BLOCKED）时不得持久化——游戏实际状态未变
    proj2 = _make_write_ready_project(tmp_path, monkeypatch)
    entry2 = next(r for r in proj2.store.get_entries()
                  if r["status"] == "translated")
    blocked_outcome = WriteResult(
        files=1, entries=1, attempted=2,
        rejected=[WriteRejection("fake:key", "test_reject")],
        reverted_locators={f"{entry2['file_id']}:{entry2['key_path']}"})

    def capture_blocked(store, game_dir, staging, typetree_generator=None):
        return blocked_outcome

    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_blocked)
    with pytest.raises(RuntimeError, match="阻断默认发布"):
        proj2.write_all()
    row2 = proj2.store.get_entries()
    still = next(r for r in row2 if r["file_id"] == entry2["file_id"]
                 and r["key_path"] == entry2["key_path"])
    assert still["status"] == "translated"


def test_write_all_purges_reverted_originals_from_translation_memory(
        tmp_path, monkeypatch):
    """写回 C10 补漏：语义回退（对象名/按钮名宁漏勿坏）原文须从翻译记忆
    撤销——审后 settle_translation_memory 已把它 promote 进 memory
    （pending=0），不回撤则 get_memory_hits 在后续游戏直接命中同一坏译文，
    跨游戏重复引入按键失灵/断链。"""
    _install_fake_raw_asset_environment(monkeypatch)
    proj = _make_write_ready_project(tmp_path, monkeypatch)
    # 预置一条已提交记忆（模拟审后 promote 的按钮名坏译文）
    bad_original = "Start"
    proj.store.add_memory(bad_original, "开始", "test-model", "→zh-CN")
    assert proj.store.get_memory_hits(
        [bad_original], "test-model", "→zh-CN")[bad_original] == "开始"
    # 该原文进逻辑回退源
    fake_outcome = WriteResult(
        files=1, entries=1, attempted=2,
        logic_reverted_sources={bad_original},
        logic_reverted=1)

    def capture_v2(store, game_dir, staging, typetree_generator=None):
        return fake_outcome

    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_v2)

    result = proj.write_all()

    assert result["verification"]["reverted_memory_purged"] == 1
    # 记忆已撤——后续翻译不再命中
    hits = proj.store.get_memory_hits(
        [bad_original], "test-model", "→zh-CN")
    assert bad_original not in hits


def test_normalize_store_font_punctuation_persists(tmp_path, monkeypatch):
    """字体标点兼容归一化（hickory 实证回归）：译文里的 U+2013 – → U+2014 —
    持久化到 store，保留 status 与 meta，幂等且不误改无缺字条目。"""
    _install_fake_raw_asset_environment(monkeypatch)
    proj = _make_write_ready_project(tmp_path, monkeypatch)
    entry = next(row for row in proj.store.get_entries()
                 if row["status"] == "pending")
    proj.store.set_manual(entry["file_id"], entry["key_path"],
                          "I. – clk – 了解。")
    other = next(row for row in proj.store.get_entries()
                 if row["key_path"] != entry["key_path"])
    untouched_text = other["translation"] or other["original"]

    from hanhua.core.project import _normalize_store_font_punctuation
    updated = _normalize_store_font_punctuation(proj.store)

    assert updated == 1
    rows = proj.store.get_entries()
    fixed = next(r for r in rows if r["key_path"] == entry["key_path"])
    assert fixed["translation"] == "I. — clk — 了解。"
    assert fixed["status"] == "translated"
    intact = next(r for r in rows if r["key_path"] == other["key_path"])
    assert (intact["translation"] or intact["original"]) == untouched_text
    # 幂等：再次归一化无变化
    assert _normalize_store_font_punctuation(proj.store) == 0


def test_normalize_store_font_punctuation_normalizes_skipped_original(
        tmp_path, monkeypatch):
    """未翻译条目（skipped/blocked，译文空）回退原文含缺字标点 → 译文置为
    归一化原文并保留 status（hickory 实证：需求集因此消除 U+2013，
    静态消费者不再 MISSING_CODEPOINT）。"""
    _install_fake_raw_asset_environment(monkeypatch)
    d = _make_tree()
    with open(d / "strings.txt", "a", encoding="utf-8") as fh:
        fh.write("b=Level 1 – 2\n")   # EN DASH：用户 SDF 字符表缺失
    proj = Project.open_game_dir(d, tmp_path / "app")
    proj.scan()
    entry = next(r for r in proj.store.get_entries() if "–" in r["original"])
    assert entry["status"] != "translated"
    assert not entry["translation"]

    from hanhua.core.project import _normalize_store_font_punctuation
    assert _normalize_store_font_punctuation(proj.store) == 1
    fixed = next(r for r in proj.store.get_entries()
                 if r["key_path"] == entry["key_path"])
    assert fixed["translation"] == "Level 1 — 2"
    assert fixed["status"] == entry["status"]          # status 原样保留
    assert _normalize_store_font_punctuation(proj.store) == 0  # 幂等


def test_render_fallback_punctuation_gated_by_font_flag(tmp_path, monkeypatch):
    """writer 回退原文归一化受开关控制：字体启用 → 未翻译条目原文里的
    – 写为 —（与新 bundle 渲染字节一致）；字体未启用 → 原样写回。"""
    from hanhua.core.writer import _render
    d = _make_tree()
    (d / "strings.txt").write_text("a=Level 1 – 2\n", encoding="utf-8")
    proj = Project.open_game_dir(d, tmp_path / "app")
    proj.scan()
    f = next(x for x in proj.store.get_files()
             if x["rel_path"].endswith("strings.txt"))
    entries = [e for e in proj.store.get_entries() if e["file_id"] == f["id"]]
    assert entries and all(not e.get("translation") for e in entries)

    body_off = _render(d / f["rel_path"], f, entries, "zh-CN", False)
    assert "–" in body_off          # 字体未启用：原文原样
    body_on = _render(d / f["rel_path"], f, entries, "zh-CN", True)
    assert "—" in body_on
    assert "–" not in body_on       # 字体启用：回退原文归一化


def test_write_all_normalizes_punctuation_when_font_enabled(
        tmp_path, monkeypatch):
    """write_all 在字体启用时执行归一化（单一接缝），禁用时不改译文。"""
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.font_support import FontInstallResult
    _install_fake_raw_asset_environment(monkeypatch)
    # fake 树结构不完整：桩掉真实插件安装，只验证归一化接缝
    monkeypatch.setattr(
        pipeline_module, "install_font_override",
        lambda *a, **k: FontInstallResult(
            installed=True, filename="f.otf", payload_deployed=True,
            provider_supported=True, provider_id="bepinex5_mono_x64"))
    proj = _make_write_ready_project(tmp_path, monkeypatch)
    entry = next(row for row in proj.store.get_entries()
                 if row["status"] == "pending")
    proj.store.set_manual(entry["file_id"], entry["key_path"],
                          "– 测试")

    # 字体禁用：不归一化（默认 FontConfig(enabled=False)）
    proj.write_all()
    rows = proj.store.get_entries()
    kept = next(r for r in rows if r["key_path"] == entry["key_path"])
    assert kept["translation"] == "– 测试"

    # 字体启用：写回入口归一化
    proj2 = _make_write_ready_project(tmp_path, monkeypatch)
    entry2 = next(row for row in proj2.store.get_entries()
                  if row["status"] == "pending")
    proj2.store.set_manual(entry2["file_id"], entry2["key_path"],
                           "– 测试")
    proj2.write_all(font_config=FontConfig(enabled=True))
    rows2 = proj2.store.get_entries()
    fixed = next(r for r in rows2 if r["key_path"] == entry2["key_path"])
    assert fixed["translation"] == "— 测试"


def test_write_all_publishes_with_warn_when_allow_partial(
        tmp_path, monkeypatch):
    _install_fake_raw_asset_environment(monkeypatch)
    proj = _make_write_ready_project(tmp_path, monkeypatch)
    fake_outcome = WriteResult(
        files=1, entries=1, attempted=2,
        rejected=[WriteRejection("fake:key", "test_reject")],
        truncated=1, truncated_items=["「a」→「a…」"])

    def capture_v2(store, game_dir, staging, typetree_generator=None):
        return fake_outcome

    monkeypatch.setattr("hanhua.core.project.write_back_v2", capture_v2)

    result = proj.write_all(allow_partial=True)

    assert result["verification"]["overall"] == "WARN"
    assert result["verification"]["gates"]["object"]["status"] == "WARN"
    assert result["verification"]["allow_partial"] is True
    assert proj.out_dir.is_dir()
    assert len(result["verification"]["rejected_entries"]) == 1
    assert len(result["verification"]["truncated_entries"]) == 1
    blocked = result["verification"]["blocked_entries"]
    assert len(blocked) == 2, "rejected + truncated 必须全量进入报告"


# ── P0-4：不可变字段集合 ──

def test_collect_immutable_values_recursive():
    tree = {
        "m_Name": "Menu",
        "m_TableData": [
            {"m_Id": 1, "m_Localized": "Play"},
            {"m_Id": 2, "m_Localized": "Quit"},
        ],
        "m_Script": {"m_FileID": 100, "m_PathID": 0},
        "m_Address": "bundles/menu",
        "plain_field": "这是显示文本",
    }
    collected = _collect_immutable_values(tree)
    paths = [path for path, _ in collected]
    assert ["m_Name"] in paths
    assert ["m_TableData", 0, "m_Id"] in paths
    assert ["m_Script", "m_FileID"] in paths
    assert ["m_Address"] in paths
    # 显示文本字段不收集
    assert not any(path == ["plain_field"] for path in paths)


class _FakeTypetreeObject:
    def __init__(self, tree):
        self._tree = tree
        self.assets_file = type("AssetFile", (), {"name": "x.assets"})()
        self.path_id = 7
        self.type = type("ObjectType", (), {"name": "MonoBehaviour"})()

    def read_typetree(self):
        return self._tree

    def get_raw_data(self):
        return b"raw"


class _FakeVerifierEnvironment:
    """验证器 Environment 替身：重开后返回篡改后的 typetree。"""

    def __init__(self, objects):
        self.objects = objects
        self.files = {}

    def load(self, paths):
        pass


def test_verify_saved_bundle_detects_immutable_field_drift(monkeypatch):
    import UnityPy

    baseline_tree = {
        "m_Name": "Menu",
        "m_TableData": [{"m_Id": 1, "m_Localized": "Play"}],
    }
    drifted_tree = {
        "m_Name": "Menu改",           # 不可变字段被意外改动
        "m_TableData": [{"m_Id": 1, "m_Localized": "开始游戏"}],
    }
    env = _FakeVerifierEnvironment([_FakeTypetreeObject(drifted_tree)])

    # 写回前收集：m_Name=Menu、m_Id=1 必须保持不变（重开后已漂移）
    immutable = _collect_immutable_values(baseline_tree)
    monkeypatch.setattr(UnityPy, "Environment", lambda: env)

    with pytest.raises(ValueError, match="验证失败"):
        _verify_saved_bundle(
            Path("unused"),
            expected_raw_by_path_id={},
            expected_immutable_values={("x.assets", 7): immutable})


def test_verify_saved_bundle_passes_when_immutable_intact(monkeypatch):
    import UnityPy

    tree = {
        "m_Name": "Menu",
        "m_TableData": [{"m_Id": 1, "m_Localized": "开始游戏"}],
    }
    env = _FakeVerifierEnvironment([_FakeTypetreeObject(tree)])
    immutable = _collect_immutable_values(tree)
    monkeypatch.setattr(UnityPy, "Environment", lambda: env)

    # m_Localized 变化不影响不可变校验
    _verify_saved_bundle(
        Path("unused"),
        expected_raw_by_path_id={},
        expected_immutable_values={("x.assets", 7): immutable})


# ── Phase 0（审计 §9）：字体 coverage 不完整必须阻断发布 ─────────
# 发布门决策表 §8.2 锁定：正式发布仅「所有消费者静态完整覆盖」或
# 「runtime attestation 完整」允许；已知缺字/未覆盖/IL2CPP 无 provider
# 一律禁止（测试候选除外）。Phase 4 将 coverage_blocks_publish 接入
# _evaluate_writeback_gates——语义在此先行锁定。

def _coverage_outcome(*consumers):
    from hanhua.core.font import compute_coverage
    from hanhua.core.font.glyph_set import build_required_glyph_set
    from hanhua.core.models import TextEntry
    entry = TextEntry("f", "k1", "Continue", translation="继续游戏",
                      status="translated")
    return compute_coverage(
        list(consumers), build_required_glyph_set([entry]))


def _static_covered() -> SimpleNamespace:
    from hanhua.core.font import FontConsumer
    return FontConsumer("covered", "tmp_font", static_replaced=True,
                        font_scalars=frozenset(ord(c) for c in "继续游戏"),
                        unity_version="2021.3")


def test_font_coverage_incomplete_blocks_publish(tmp_path):
    """已知缺字/未覆盖消费者 → 阻断正式发布。"""
    from hanhua.core.font import (CANDIDATE_ONLY, FontConsumer,
                                   coverage_blocks_publish)
    missing = FontConsumer(
        "missing", "tmp_font", static_replaced=True,
        font_scalars=frozenset(ord(c) for c in "继续"),   # 缺「游戏」
        unity_version="2021.3")
    outcome = _coverage_outcome(missing)
    assert outcome.overall == CANDIDATE_ONLY
    assert coverage_blocks_publish(outcome) is True


def test_font_coverage_il2cpp_no_provider_blocks(tmp_path):
    from hanhua.core.font import (BLOCKED, FontConsumer,
                                   coverage_blocks_publish)
    outcome = _coverage_outcome(FontConsumer(
        "il2cpp", "dynamic_tmp", runtime_provider_available=False))
    assert outcome.overall == BLOCKED
    assert coverage_blocks_publish(outcome) is True


def test_font_coverage_complete_allows_publish(tmp_path):
    from hanhua.core.font import COVERED, coverage_blocks_publish
    outcome = _coverage_outcome(_static_covered())
    assert outcome.overall == COVERED
    assert coverage_blocks_publish(outcome) is False


def test_font_coverage_pending_allows_candidate_not_formal(tmp_path):
    """runtime 已部署未验证：测试候选允许，禁止称正式完成（§8.2）。"""
    from hanhua.core.font import (PENDING_RUNTIME_ATTESTATION,
                                   FontConsumer, coverage_blocks_publish)
    outcome = _coverage_outcome(FontConsumer(
        "mono_pending", "dynamic_tmp", runtime_provider_available=True))
    assert outcome.overall == PENDING_RUNTIME_ATTESTATION
    assert coverage_blocks_publish(outcome) is False   # 不阻断候选
    assert outcome.pending_runtime() is True           # 但未正式完成
