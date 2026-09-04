# -*- coding: utf-8 -*-
"""FontCompatibilityPipeline 单元测试（Phase 4，计划 §7.4 接口）。

覆盖：plan 定格需求集、apply_static/verify_static 覆盖证明、
deploy_runtime 的 Mono 部署与 IL2CPP stub、evaluate_publish 决策表、
run() 一键编排（GUI/headless/批量共用同一闭环）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hanhua.core.font import COVERED
from hanhua.core.font.glyph_set import build_required_glyph_set
from hanhua.core.font.pipeline import (FontCompatibilityPipeline,
                                       FontPipelineInput)
from hanhua.core.font_support import (FontInstallResult,
                                      FontProviderCapability)
from hanhua.core.models import FontConfig


def _capability(*, provider_supported=True, runtime="mono",
                provider_id="mono_font_plugin") -> FontProviderCapability:
    return FontProviderCapability(
        provider_id=provider_id, runtime=runtime, architecture="x64",
        provider_supported=provider_supported, payload_available=True,
        static_writeback_allowed=True)


def _input(tmp_path: Path, *, enabled=True, runtime="mono",
           capability=None, required=None,
           unity_version="2022.3") -> FontPipelineInput:
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    config = FontConfig(enabled=enabled)
    return FontPipelineInput(
        game_dir=game, staging=staging, font_config=config,
        unity_version=unity_version, runtime=runtime, player_root=None,
        capability=capability or _capability(runtime=runtime),
        translations={"Settings": "设置"},
        required_set=required,
    )


def _required() -> object:
    from hanhua.core.models import TextEntry
    return build_required_glyph_set([
        TextEntry("f", "k1", "Settings", translation="设置",
                  status="translated")])


def test_plan_returns_frozen_required_set(tmp_path):
    required = _required()
    pipeline = FontCompatibilityPipeline(_input(tmp_path, required=required))
    assert pipeline.plan() is required


def test_apply_static_none_when_font_disabled(tmp_path, monkeypatch):
    pipeline = FontCompatibilityPipeline(_input(tmp_path, enabled=False))
    assert pipeline.apply_static(pipeline.plan()) is None


def test_apply_static_runs_install_static_fonts(tmp_path, monkeypatch):
    calls = {}
    from hanhua.core.font import pipeline as pipeline_module

    def fake_install_static(staging, config, *, unity_version, required,
                            typetree_generator=None, source_dir=None):
        calls["staging"] = staging
        calls["unity_version"] = unity_version
        calls["source_dir"] = source_dir
        from hanhua.core.unity.font_replace import FontReplaceResult
        return FontReplaceResult(replaced=1)

    monkeypatch.setattr(
        pipeline_module, "install_static_fonts", fake_install_static)
    pipeline = FontCompatibilityPipeline(_input(tmp_path, required=_required()))
    result = pipeline.apply_static(pipeline.plan())
    assert result is not None and result.replaced == 1
    assert calls["unity_version"] == "2022.3"
    assert calls["source_dir"] == Path(tmp_path) / "game"


def test_verify_static_extracts_coverage(tmp_path):
    from hanhua.core.font import FontConsumer, compute_coverage
    static = type("S", (), {"coverage": compute_coverage(
        [FontConsumer("c", "tmp_font", static_replaced=True,
                      font_scalars=frozenset(ord(c) for c in "设置"),
                      unity_version="2022.3")],
        _required())})()
    outcome = FontCompatibilityPipeline.verify_static(static)
    assert outcome is not None
    assert outcome.overall == COVERED


def test_deploy_runtime_il2cpp_without_provider_returns_stub(tmp_path):
    inputs = _input(tmp_path, runtime="il2cpp",
                    capability=_capability(runtime="il2cpp",
                                           provider_supported=False,
                                           provider_id="unsupported_il2cpp"))
    pipeline = FontCompatibilityPipeline(inputs)
    font = pipeline.deploy_runtime(pipeline.plan(), None)
    assert font.installed is False
    assert font.provider_supported is False
    assert font.provider_id == "unsupported_il2cpp"


def test_deploy_runtime_il2cpp_after_static_replace_installs(
        tmp_path, monkeypatch):
    """IL2CPP 的字体 provider 是静态 payload：静态替换成功后部署。"""
    from hanhua.core.font import pipeline as pipeline_module

    def fake_install(game_dir, staging, config, **kwargs):
        return FontInstallResult(
            installed=True, filename=config.filename,
            payload_deployed=True, provider_id="font_payload")

    monkeypatch.setattr(pipeline_module, "install_font_override", fake_install)
    inputs = _input(tmp_path, runtime="il2cpp")
    pipeline = FontCompatibilityPipeline(inputs)
    static = type("S", (), {"replaced": 2, "coverage": None})()
    font = pipeline.deploy_runtime(pipeline.plan(), static)
    assert font.installed is True


def test_deploy_runtime_selects_unity_2019_tmp_bundle(
        tmp_path, monkeypatch):
    from hanhua.core.font import pipeline as pipeline_module

    calls = {}

    def fake_install(game_dir, staging, config, **kwargs):
        calls["tmp_bundle"] = kwargs.get("tmp_bundle")
        return FontInstallResult(installed=True)

    monkeypatch.setattr(pipeline_module, "install_font_override", fake_install)
    pipeline = FontCompatibilityPipeline(_input(
        tmp_path, unity_version="2019.4.40f1"))
    static = type("S", (), {"replaced": 1, "coverage": None})()

    pipeline.deploy_runtime(pipeline.plan(), static)

    assert calls["tmp_bundle"] is not None
    assert calls["tmp_bundle"].name == "notoserif_sdf_u2019"


@pytest.mark.parametrize("unity_version", ["2018.4.36f1", None])
def test_deploy_runtime_passes_no_tmp_bundle_for_incompatible_version(
        tmp_path, monkeypatch, unity_version):
    from hanhua.core.font import pipeline as pipeline_module

    calls = {}

    def fake_install(game_dir, staging, config, **kwargs):
        calls["tmp_bundle"] = kwargs.get("tmp_bundle", "missing")
        return FontInstallResult(installed=True)

    monkeypatch.setattr(pipeline_module, "install_font_override", fake_install)
    pipeline = FontCompatibilityPipeline(_input(
        tmp_path, unity_version=unity_version))

    pipeline.deploy_runtime(pipeline.plan(), None)

    assert calls["tmp_bundle"] is None


def test_deploy_runtime_unsupported_il2cpp_calls_install(tmp_path, monkeypatch):
    """IL2CPP 无 provider：install_font_override 内部会拒绝并返回
    unsupported（工具侧不应手工绕过）。"""
    from hanhua.core.font import pipeline as pipeline_module

    def fake_install(game_dir, staging, config, **kwargs):
        return FontInstallResult(False, provider_supported=False)

    monkeypatch.setattr(pipeline_module, "install_font_override", fake_install)
    inputs = _input(tmp_path, runtime="il2cpp")
    pipeline = FontCompatibilityPipeline(inputs)
    font = pipeline.deploy_runtime(pipeline.plan(), None)
    assert font.installed is False


def test_evaluate_publish_blocks_candidate_only_without_confirm(tmp_path):
    from hanhua.core.font import (CANDIDATE_ONLY, FontConsumer,
                                  compute_coverage)
    outcome = compute_coverage(
        [FontConsumer("m", "tmp_font", static_replaced=True,
                      font_scalars=frozenset(ord(c) for c in "设"),
                      unity_version="2022.3")],
        _required())
    pipeline = FontCompatibilityPipeline(_input(tmp_path))
    gate = pipeline.evaluate_publish(None, FontInstallResult(True), outcome,
                                     allow_unverified_font_candidate=False)
    assert gate["status"] == "BLOCKED"
    assert outcome.overall == CANDIDATE_ONLY


def _dynamic_static(required, *, provider_ok=True) -> object:
    """动态 TMP 消费者（0 glyph）+ 静态覆盖消费者的 FontReplaceResult。"""
    from hanhua.core.font import FontConsumer, compute_coverage
    from hanhua.core.unity.font_replace import FontReplaceResult
    consumers = [
        FontConsumer("bundle#f1", "tmp_font", static_replaced=True,
                     font_scalars=frozenset(ord(c) for c in "设置"),
                     unity_version="2022.3"),
        FontConsumer("bundle#dyn", "dynamic_tmp",
                     runtime_provider_available=False,
                     ref="dynamic 0 glyph——静态无法证明覆盖"),
    ]
    outcome = compute_coverage(consumers, required)
    return FontReplaceResult(
        replaced=1, consumers=consumers, coverage=outcome,
        overall=outcome.overall.name, incomplete=outcome.blocks_publish())


def test_run_dynamic_consumer_flipped_when_plugin_deployed(
        tmp_path, monkeypatch):
    """hickory 实证回归：Mono 插件实际部署（provider_supported）→
    dynamic_tmp 消费者 runtime_provider_available 翻转为真并重算覆盖——
    终态 PENDING_RUNTIME_ATTESTATION（未实机 attest），不再
    RUNTIME_PROVIDER_UNAVAILABLE 永久 BLOCKED；候选确认降级 WARN。"""
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.font import PENDING_RUNTIME_ATTESTATION
    from hanhua.core.unity.font_replace import FontReplaceResult

    def fake_static(staging, config, *, unity_version, required,
                            typetree_generator=None, source_dir=None):
        return _dynamic_static(required)

    monkeypatch.setattr(pipeline_module, "install_static_fonts", fake_static)
    monkeypatch.setattr(
        pipeline_module, "install_font_override",
        lambda *a, **k: FontInstallResult(
            installed=True, filename="f.otf", payload_deployed=True,
            provider_supported=True, provider_id="bepinex5_mono_x64"))

    pipeline = FontCompatibilityPipeline(
        _input(tmp_path, required=_required()))
    outcome = pipeline.run(allow_unverified_font_candidate=False)
    dyn = next(c for c in outcome.static.consumers
               if c.kind == "dynamic_tmp")
    assert dyn.runtime_provider_available is True
    assert outcome.coverage.overall == PENDING_RUNTIME_ATTESTATION
    assert outcome.gate["status"] == "BLOCKED"      # 未确认仍阻断
    outcome2 = pipeline.run(allow_unverified_font_candidate=True)
    assert outcome2.gate["status"] == "WARN"        # 候选确认降级 WARN


def test_run_dynamic_consumer_stays_blocked_without_plugin(tmp_path,
                                                           monkeypatch):
    """插件未部署（provider_supported=False / 安装异常）→ dynamic_tmp
    保持 RUNTIME_PROVIDER_UNAVAILABLE BLOCKED——候选确认不可绕过。"""
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.font import BLOCKED

    def fake_static(staging, config, *, unity_version, required,
                            typetree_generator=None, source_dir=None):
        return _dynamic_static(required)

    monkeypatch.setattr(pipeline_module, "install_static_fonts", fake_static)
    monkeypatch.setattr(
        pipeline_module, "install_font_override",
        lambda *a, **k: FontInstallResult(
            installed=False, provider_supported=False))

    pipeline = FontCompatibilityPipeline(
        _input(tmp_path, required=_required()))
    outcome = pipeline.run(allow_unverified_font_candidate=True)
    dyn = next(c for c in outcome.static.consumers
               if c.kind == "dynamic_tmp")
    assert dyn.runtime_provider_available is False
    assert outcome.coverage.overall == BLOCKED
    assert outcome.gate["status"] == "BLOCKED"


def test_run_returns_unified_outcome(tmp_path, monkeypatch):
    from hanhua.core.font import FontConsumer, compute_coverage
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.unity.font_replace import FontReplaceResult

    def fake_static(staging, config, *, unity_version, required,
                            typetree_generator=None, source_dir=None):
        consumers = [FontConsumer(
            "covered", "tmp_font", static_replaced=True,
            font_scalars=frozenset(ord(c) for c in "设置"),
            unity_version=unity_version)]
        outcome = compute_coverage(consumers, required)
        return FontReplaceResult(
            replaced=1, consumers=consumers, coverage=outcome,
            overall="COVERED", incomplete=False)

    monkeypatch.setattr(pipeline_module, "install_static_fonts", fake_static)
    monkeypatch.setattr(
        pipeline_module, "install_font_override",
        lambda *a, **k: FontInstallResult(
            installed=True, filename="NotoSerifCJKsc-Medium.ttf",
            payload_deployed=True, provider_id="mono_font_plugin"))

    pipeline = FontCompatibilityPipeline(
        _input(tmp_path, required=_required()))
    outcome = pipeline.run(allow_unverified_font_candidate=False)
    assert outcome.plan.scalars
    assert outcome.static is not None and outcome.static.replaced == 1
    assert outcome.font.installed is True
    assert outcome.coverage is not None
    assert outcome.gate["status"] == "PASS"


def test_deploy_runtime_skips_plugin_when_static_covered(tmp_path,
                                                         monkeypatch):
    """卡顿根治回归：静态覆盖 COVERED 且无待认证 dynamic_tmp 消费者 →
    不再部署 BepInEx 插件（插件常驻每秒全对象扫描拖垮帧率），返回
    static_font_replace 结果 + 警告；publish 门仍 PASS（宁漏勿坏不破坏）。"""
    from hanhua.core.font import FontConsumer, compute_coverage
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.unity.font_replace import FontReplaceResult

    calls: list = []

    def fake_install(*a, **k):
        calls.append(a)
        return FontInstallResult(installed=True, provider_supported=True)

    monkeypatch.setattr(pipeline_module, "install_font_override", fake_install)
    consumers = [FontConsumer(
        "covered", "tmp_font", static_replaced=True,
        font_scalars=frozenset(ord(c) for c in "设置"),
        unity_version="2022.3")]
    outcome = compute_coverage(consumers, _required())
    static = FontReplaceResult(
        replaced=1, consumers=consumers, coverage=outcome,
        overall="COVERED", incomplete=False)

    pipeline = FontCompatibilityPipeline(
        _input(tmp_path, required=_required()))
    font = pipeline.deploy_runtime(pipeline.plan(), static)

    assert calls == []                       # 插件安装零调用
    assert font.installed is True
    assert font.provider_id == "static_font_replace"
    assert font.runtime_verified is True
    assert any("静态覆盖已完整证明" in w for w in pipeline._warnings)
    gate = pipeline.evaluate_publish(static, font, outcome,
                                     allow_unverified_font_candidate=False)
    assert gate["status"] == "PASS"


def test_deploy_runtime_installs_plugin_when_dynamic_tmp_pending(tmp_path,
                                                                 monkeypatch):
    """存在未认证 dynamic_tmp 消费者 → 插件兜底仍部署（不能因跳过
    使动态字体回退 BLOCKED——run() 的 provider 翻转依赖插件在场）。"""
    from hanhua.core.font import pipeline as pipeline_module

    calls: list = []

    def fake_install(*a, **k):
        calls.append(a)
        return FontInstallResult(
            installed=True, filename="f.ttf", payload_deployed=True,
            provider_supported=True, provider_id="bepinex5_mono_x64")

    monkeypatch.setattr(pipeline_module, "install_font_override", fake_install)
    pipeline = FontCompatibilityPipeline(
        _input(tmp_path, required=_required()))
    static = _dynamic_static(pipeline.plan())

    font = pipeline.deploy_runtime(pipeline.plan(), static)

    assert len(calls) == 1                   # 动态消费者在场必须装插件
    assert font.provider_supported is True


# ── Phase 5：位图字体注入（apply_bitmap） ───────────────────────

def _bitmap_input(tmp_path, *, providers=(), executor=None,
                  required=None):
    inputs = _input(tmp_path, required=required or _required())
    inputs.bitmap_providers = tuple(providers)
    inputs.bmfont_executor = executor
    return inputs


def test_apply_bitmap_none_without_providers(tmp_path):
    pipeline = FontCompatibilityPipeline(
        _bitmap_input(tmp_path))
    assert pipeline.apply_bitmap(pipeline.plan(), None) is None


def test_apply_bitmap_audit_covered_backfeeds_consumers(tmp_path, monkeypatch):
    """既有 .fnt 已覆盖需求集 → 无注入但反哺 ngui_bitmap 消费者。"""
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.font.providers import BitmapProvider
    from hanhua.core.unity.font_replace import FontReplaceResult
    from hanhua.core.font import FontConsumer

    fnt = tmp_path / "game" / "Fonts" / "ui.fnt"
    fnt.parent.mkdir(parents=True)
    fnt.write_text("covered", encoding="utf-8")

    def fake_audit(fnt_path, required):
        from hanhua.core.font.providers import BitmapAudit
        return BitmapAudit(fnt_path, True, frozenset(), "已覆盖")

    monkeypatch.setattr(pipeline_module, "audit_bitmap_font", fake_audit)
    static = FontReplaceResult(replaced=0, consumers=[
        FontConsumer("bundle#f1", "ngui_bitmap",
                     ref="NGUI BMFont")])
    pipeline = FontCompatibilityPipeline(_bitmap_input(
        tmp_path, providers=[BitmapProvider("bmfont", "bmfont", fnt)]))
    outcome = pipeline.apply_bitmap(pipeline.plan(), static)
    assert outcome is not None
    assert outcome.injected == 0
    assert outcome.blocks_publish() is False
    consumer = static.consumers[0]
    assert consumer.static_replaced is True
    assert consumer.font_scalars == static.consumers[0].font_scalars


def test_apply_bitmap_injects_when_missing(tmp_path, monkeypatch):
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.font.providers import BitmapProvider
    from hanhua.core.unity.font_replace import FontReplaceResult
    from hanhua.core.font import FontConsumer

    fnt = tmp_path / "game" / "Fonts" / "ui.fnt"
    fnt.parent.mkdir(parents=True)
    fnt.write_text("old", encoding="utf-8")
    calls = {}

    def fake_audit(fnt_path, required):
        from hanhua.core.font.providers import BitmapAudit
        return BitmapAudit(fnt_path, True, frozenset(required.scalars), "缺字")

    def fake_inject(provider, staging_fnt, plan):
        calls["staging"] = str(staging_fnt)
        from hanhua.core.tooling.bmfont import BmFontArtifact
        return BmFontArtifact(
            staging_fnt, (staging_fnt.parent / "ui_0.png",),
            frozenset(plan.scalars), 16, 16)

    monkeypatch.setattr(pipeline_module, "audit_bitmap_font", fake_audit)
    monkeypatch.setattr(pipeline_module, "inject_bitmap_font", fake_inject)
    static = FontReplaceResult(replaced=0, consumers=[
        FontConsumer("bundle#f1", "ngui_bitmap",
                     ref="NGUI BMFont")])
    pipeline = FontCompatibilityPipeline(_bitmap_input(
        tmp_path, providers=[BitmapProvider("bmfont", "bmfont", fnt)],
        executor=fake_inject))
    outcome = pipeline.apply_bitmap(pipeline.plan(), static)
    assert outcome.injected == 1
    assert outcome.blocks_publish() is False
    assert calls["staging"].replace("\\", "/").endswith("Fonts/ui.fnt")
    assert static.consumers[0].static_replaced is True


def test_apply_bitmap_without_executor_keeps_candidate(tmp_path, monkeypatch):
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.font.providers import BitmapProvider
    from hanhua.core.unity.font_replace import FontReplaceResult
    from hanhua.core.font import FontConsumer

    fnt = tmp_path / "game" / "Fonts" / "ui.fnt"
    fnt.parent.mkdir(parents=True)
    fnt.write_text("old", encoding="utf-8")

    def fake_audit(fnt_path, required):
        from hanhua.core.font.providers import BitmapAudit
        return BitmapAudit(fnt_path, True, frozenset(required.scalars), "缺字")

    monkeypatch.setattr(pipeline_module, "audit_bitmap_font", fake_audit)
    static = FontReplaceResult(replaced=0, consumers=[
        FontConsumer("bundle#f1", "ngui_bitmap",
                     ref="NGUI BMFont")])
    pipeline = FontCompatibilityPipeline(_bitmap_input(
        tmp_path, providers=[BitmapProvider("bmfont", "bmfont", fnt)]))
    outcome = pipeline.apply_bitmap(pipeline.plan(), static)
    assert outcome.blocks_publish() is True
    assert static.consumers[0].static_replaced is False
    assert pipeline._warnings and "BMFont 工具链不可用" in pipeline._warnings[0]


def test_run_integration_bitmap_injection_recomputes_coverage(
        tmp_path, monkeypatch):
    """run() 编排：位图注入反哺后 coverage 重算——ngui_bitmap 消费者
    CANDIDATE_ONLY → COVERED，发布门 PASS。"""
    from hanhua.core.font import pipeline as pipeline_module
    from hanhua.core.font.providers import BitmapProvider
    from hanhua.core.unity.font_replace import FontReplaceResult
    from hanhua.core.font import FontConsumer

    fnt = tmp_path / "game" / "Fonts" / "ui.fnt"
    fnt.parent.mkdir(parents=True)
    fnt.write_text("old", encoding="utf-8")

    def fake_static(staging, config, *, unity_version, required,
                            typetree_generator=None, source_dir=None):
        return FontReplaceResult(
            replaced=1, consumers=[
                FontConsumer("c1", "tmp_font", static_replaced=True,
                             font_scalars=frozenset(ord(c) for c in "设置"),
                             unity_version="2022.3"),
                FontConsumer("c2", "ngui_bitmap",
                             ref="NGUI BMFont")])

    def fake_audit(fnt_path, required):
        from hanhua.core.font.providers import BitmapAudit
        return BitmapAudit(fnt_path, True, frozenset(required.scalars), "缺字")

    def fake_inject(provider, staging_fnt, plan):
        from hanhua.core.tooling.bmfont import BmFontArtifact
        return BmFontArtifact(
            staging_fnt, (staging_fnt.parent / "ui_0.png",),
            frozenset(plan.scalars), 16, 16)

    monkeypatch.setattr(pipeline_module, "install_static_fonts", fake_static)
    monkeypatch.setattr(pipeline_module, "audit_bitmap_font", fake_audit)
    monkeypatch.setattr(pipeline_module, "inject_bitmap_font", fake_inject)
    monkeypatch.setattr(
        pipeline_module, "install_font_override",
        lambda *a, **k: FontInstallResult(
            installed=True, filename="f.otf", payload_deployed=True,
            provider_id="mono_font_plugin"))
    inputs = _input(tmp_path, required=_required())
    inputs.bitmap_providers = (BitmapProvider("bmfont", "bmfont", fnt),)
    inputs.bmfont_executor = fake_inject
    outcome = FontCompatibilityPipeline(inputs).run(
        allow_unverified_font_candidate=False)
    assert outcome.bitmap is not None and outcome.bitmap.injected == 1
    assert outcome.coverage is not None
    assert outcome.coverage.overall == COVERED
    assert outcome.gate["status"] == "PASS"
