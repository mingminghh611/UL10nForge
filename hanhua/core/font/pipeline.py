# -*- coding: utf-8 -*-
"""FontCompatibilityPipeline：GUI、headless 和批量写回共用的字体闭环
（Phase 4，计划 §7.4/§9 Phase 4）。

接口（计划 §7.4 Pipeline Interface 落地）：
  plan()            定格不可变译文快照 → RequiredGlyphSet（本次发布实际
                    渲染字形需求集；快照在静态替换前定格，后续写回不得
                    影响字形验证基准）
  apply_static()    静态字体替换（install_static_fonts → FontReplaceResult，
                    含逐消费者记录与覆盖计算）
  verify_static()   覆盖证明（apply 结果提取 coverage——不重扫；静态
                    replaced > 0 不再代表全局成功，P0-4 缺陷锁）
  deploy_runtime()  运行时回退部署（Mono 插件 install_font_override；
                    IL2CPP 无 provider → unsupported stub）
  evaluate_publish()发布门（evaluate_font_gate，§8.2 决策表：
                    CANDIDATE_ONLY/BLOCKED 阻断正式发布，
                    allow_candidate 只降级 PENDING/CANDIDATE_ONLY）

project.write_all 只编排 Pipeline（+ Addressables catalog 同步与 route
状态），不再手工推导 runtime_verified/字体终态；all_record_runner 与
mass_writeback_all 消费同一 outcome。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hanhua.core.font.coverable import (FontCoverageOutcome,
                                        CoverageState)
from hanhua.core.font.glyph_set import RequiredGlyphSet
from hanhua.core.font.providers import (BitmapInjectionResult,
                                        BitmapProvider,
                                        audit_bitmap_font,
                                        inject_bitmap_font)
from hanhua.core.font.publish_gate import evaluate_font_gate
from hanhua.core.font_support import (FontInstallResult,
                                      FontProviderCapability,
                                      install_font_override)
from hanhua.core.models import FontConfig
from hanhua.core.unity.font_replace import (FontReplaceResult,
                                            install_static_fonts,
                                            select_tmp_bundle)


@dataclass
class FontPipelineInput:
    """一次字体闭环的输入（project.write_all 从项目状态装配）。"""

    game_dir: Path
    staging: Path
    font_config: FontConfig
    unity_version: str | None
    runtime: str
    player_root: Path | None
    capability: FontProviderCapability
    translations: dict[str, str] = field(default_factory=dict)
    exclude: frozenset[str] = frozenset()
    required_set: RequiredGlyphSet | None = None
    # Phase 5：位图字体 provider（原游戏 .fnt 清单；空 = 无位图栈）。
    # bmfont_executor 由调用方注入（真实 BMFont 工具链），保持本模块纯净。
    bitmap_providers: tuple[BitmapProvider, ...] = ()
    bmfont_executor: Callable | None = None
    # 无 typetree 资产（DisableWriteTypeTree）的 Mono 游戏：MonoBehaviour
    # 读取需要 TypeTreeGenerator（Managed DLL）；None = 不需要。
    typetree_generator: Any | None = None


@dataclass
class FontPipelineOutcome:
    """统一终态：GUI/runner/批量对同一游戏得到相同字体终态与 reason。"""

    plan: RequiredGlyphSet
    static: FontReplaceResult | None
    font: FontInstallResult | None
    warnings: list[str] = field(default_factory=list)
    gate: dict = field(default_factory=dict)
    coverage: FontCoverageOutcome | None = None
    bitmap: BitmapInjectionResult | None = None   # Phase 5 位图注入结果


class FontCompatibilityPipeline:
    """字体闭环编排器（单一入口 run()，各步骤可独立调用供测试）。"""

    def __init__(self, inputs: FontPipelineInput) -> None:
        self.inputs = inputs
        self._warnings: list[str] = []

    # ── 计划 §7.4 步骤 ──

    def plan(self) -> RequiredGlyphSet:
        """定格需求集：调用方未预计算时为空集（project 用 store 快照
        计算后传入——见 _font_required_glyph_set）。"""
        if self.inputs.required_set is not None:
            return self.inputs.required_set
        from hanhua.core.font.glyph_set import build_required_glyph_set
        return build_required_glyph_set([])

    def apply_static(self, plan: RequiredGlyphSet) -> FontReplaceResult | None:
        """静态字体替换（legacy Font 内嵌 TTF + TMP bundle）。未启用
        字体配置 → None（旧行为：直接走运行时路径）。"""
        if not self.inputs.font_config.enabled:
            return None
        return install_static_fonts(
            self.inputs.staging, self.inputs.font_config,
            unity_version=self.inputs.unity_version,
            required=plan,
            typetree_generator=self.inputs.typetree_generator,
            source_dir=self.inputs.game_dir)

    @staticmethod
    def verify_static(static: FontReplaceResult | None) \
            -> FontCoverageOutcome | None:
        """覆盖证明：apply 结果自带 coverage（install 内计算，不重扫）。
        静态替换成功 ≠ 全局覆盖证明——coverage 终态由发布门消费。"""
        return static.coverage if static is not None else None

    def apply_bitmap(self, plan: RequiredGlyphSet,
                     static: FontReplaceResult | None,
                     ) -> BitmapInjectionResult | None:
        """位图字体注入（Phase 5）：审计原游戏 .fnt 缺字 → 生成注入 staging
        同相对路径 → 重开验证；全部 provider 注入/已覆盖后反哺 ngui_bitmap
        消费者为静态覆盖（static_replaced=True + 需求码点集）。

        未注入的 provider 记 warning（明确资产 + 原因 + 手工处置建议），
        消费者保持 CANDIDATE_ONLY——发布门继续阻断，不假装覆盖。
        """
        inputs = self.inputs
        if not inputs.bitmap_providers or static is None:
            return None
        outcome = BitmapInjectionResult(
            providers=list(inputs.bitmap_providers))
        pending: list[str] = []
        for provider in inputs.bitmap_providers:
            audit = audit_bitmap_font(provider.fnt, plan)
            outcome.audited += 1
            if not audit.valid:
                pending.append(
                    f"{provider.fnt.name}: 描述器无效（{audit.detail}）——"
                    "请手工处置（导出原字体重新生成中文字库）")
                continue
            if not audit.missing:
                continue  # 原 .fnt 已覆盖需求集，无需注入
            if inputs.bmfont_executor is None:
                pending.append(
                    f"{provider.fnt.name}: 缺字 "
                    + "、".join(f"U+{s:04X}" for s in sorted(audit.missing)[:16])
                    + "——BMFont 工具链不可用，请手工处置")
                continue
            relative = provider.fnt.relative_to(inputs.game_dir)
            staging_fnt = inputs.staging / relative
            try:
                artifact = inputs.bmfont_executor(provider, staging_fnt, plan)
            except Exception as exc:  # noqa: BLE001 注入失败不阻断写回
                self._warnings.append(
                    f"位图字体注入失败 {provider.fnt.name}：{exc}")
                pending.append(
                    f"{provider.fnt.name}: 注入失败（{exc}）——"
                    "请手工处置（生成中文字库后替换同名文件）")
                continue
            outcome.injected += 1
        outcome.pending = len(pending)
        if pending:
            self._warnings.append(
                "位图字体未完全注入："
                + "；".join(pending[:5])
                + "（消费者保持未覆盖，发布门阻断）")
        # 反哺：所有 provider 注入成功或既有覆盖已证明 → ngui_bitmap
        # 消费者升级为静态覆盖（重算 coverage 后整体可能 COVERED）。
        # 任一 pending → 不反哺，消费者保持 CANDIDATE_ONLY（诚实口径）。
        if not pending:
            from dataclasses import replace as dc_replace
            static.consumers = [
                dc_replace(c, static_replaced=True,
                           font_scalars=frozenset(plan.scalars))
                if c.kind == "ngui_bitmap" else c
                for c in static.consumers]
        return outcome

    def deploy_runtime(self, plan: RequiredGlyphSet,
                       static: FontReplaceResult | None,
                       reverted_sources: frozenset[str] = frozenset(),
                       ) -> FontInstallResult:
        """运行时回退部署：Mono 安装 BepInEx 插件；IL2CPP 无 provider →
        unsupported stub（发布门按 coverage/flag 决策，这里不阻断）。"""
        inputs = self.inputs
        tmp_bundle = select_tmp_bundle(inputs.unity_version)
        if static is not None and static.replaced:
            # 静态替换成功后部署运行时插件兜底（覆盖动态加载字体）——
            # 插件失败不阻断（静态已生效），记 warning 由调用方附加
            if inputs.capability.provider_supported:
                # 静态覆盖已完整证明且无动态 TMP 消费者待运行时认证时，
                # 插件兜底非必需——直接跳过部署（卡顿根治 P0：插件常驻
                # 每秒全对象扫描拖垮帧率；overall==COVERED 已隐含所有
                # dynamic_tmp 消费者已认证，跳过不会使任何消费者回退，
                # 此处 any 判断是防御性显式守卫）。
                skip_plugin = (
                    static.coverage is not None
                    and static.coverage.overall == CoverageState.COVERED
                    and not any(
                        c.kind == "dynamic_tmp"
                        and not c.runtime_provider_available
                        for c in static.consumers))
                if skip_plugin:
                    self._warnings.append(
                        "运行时字体插件未部署（静态覆盖已完整证明，"
                        "插件兜底非必需）")
                    return self._static_font_result(plan, static)
                try:
                    return install_font_override(
                        inputs.game_dir, inputs.staging, inputs.font_config,
                        translations=inputs.translations,
                        exclude=set(reverted_sources),
                        player_root=inputs.player_root,
                        tmp_bundle=tmp_bundle,
                    )
                except Exception as exc:  # noqa: BLE001
                    # 静态覆盖已完整证明时插件兜底非必需（hickory 实证：
                    # 用户 SDF 方案无 TTF 数据源，插件部署必然失败但 4 个
                    # 消费者全静态 COVERED）——提示不阻断；覆盖有缺口时
                    # 插件是唯一兜底，才按失败提示。
                    static_ok = (
                        static.coverage is not None
                        and static.coverage.overall == CoverageState.COVERED)
                    if static_ok:
                        self._warnings.append(
                            "运行时字体插件未部署（静态覆盖已完整证明，"
                            "插件兜底非必需）")
                    else:
                        self._warnings.append(
                            f"运行时字体插件部署失败（静态替换已生效，"
                            f"动态字体仍缺兜底）: {exc}")
            return self._static_font_result(plan, static)
        # 静态未替换（无字体配置/未找到可换对象）：退回运行时路径
        if not inputs.font_config.enabled or inputs.runtime == "mono":
            return install_font_override(
                inputs.game_dir, inputs.staging, inputs.font_config,
                translations=inputs.translations,
                exclude=set(reverted_sources),
                player_root=inputs.player_root,
                tmp_bundle=tmp_bundle,
            )
        return FontInstallResult(
            installed=False,
            filename=inputs.font_config.filename,
            payload_deployed=False,
            runtime_verified=False,
            architecture=inputs.capability.architecture,
            provider_supported=False,
            unsupported_reason=inputs.capability.reason,
            provider_id=inputs.capability.provider_id,
            payload_available=inputs.capability.payload_available,
        )

    def _static_font_result(self, plan: RequiredGlyphSet,
                            static: FontReplaceResult) -> FontInstallResult:
        """静态替换分支的统一结果（Phase 4：runtime_verified 只由覆盖
        终态决定——静态 replaced > 0 不再代表全局成功，P0-4 缺陷锁）。"""
        return FontInstallResult(
            installed=True,
            filename=self.inputs.font_config.filename,
            payload_deployed=True,
            runtime_verified=(
                static.coverage is not None
                and static.coverage.overall == CoverageState.COVERED),
            architecture=self.inputs.capability.architecture,
            provider_supported=False,
            unsupported_reason=self.inputs.capability.reason,
            provider_id="static_font_replace",
            payload_available=True,
            required_glyphs=plan.scalars,
        )

    def evaluate_publish(self,
                         static: FontReplaceResult | None,
                         font: FontInstallResult,
                         coverage: FontCoverageOutcome | None,
                         allow_unverified_font_candidate: bool) -> dict:
        """发布门：coverage 终态优先，CANDIDATE_ONLY/BLOCKED 阻断正式
        发布；allow_unverified_font_candidate 只降级候选（§8.3）。"""
        return evaluate_font_gate(
            coverage=coverage,
            runtime_verified=font.runtime_verified,
            payload_deployed=font.payload_deployed,
            provider_supported=font.provider_supported,
            font_enabled=self.inputs.font_config.enabled,
            allow_unverified_font_candidate=allow_unverified_font_candidate,
        )

    # ── 一键编排 ──

    def run(self, *, reverted_sources: frozenset[str] = frozenset(),
            allow_unverified_font_candidate: bool = False,
            ) -> FontPipelineOutcome:
        """plan → apply_static → apply_bitmap → verify_static →
        deploy_runtime → evaluate_publish 一次编排
        （project.write_all 主调用入口）。"""
        plan = self.plan()
        static = self.apply_static(plan)
        bitmap = self.apply_bitmap(plan, static)
        if bitmap is not None and static is not None and not bitmap.blocks_publish():
            # 位图反哺后重算覆盖：ngui_bitmap 消费者已静态证明
            from hanhua.core.font.coverable import compute_coverage
            static.coverage = compute_coverage(static.consumers, plan)
            static.overall = static.coverage.overall.name
            static.incomplete = static.coverage.blocks_publish()
        coverage = self.verify_static(static)
        font = self.deploy_runtime(plan, static, reverted_sources)
        if font.provider_supported and static is not None and any(
                c.kind == "dynamic_tmp" and not c.runtime_provider_available
                for c in static.consumers):
            # 运行时 provider 已实际部署（Mono BepInEx 插件装入副本）→
            # 动态 TMP 字体（0 glyph，字形由运行时生成）消费者标记
            # runtime_provider_available=True 并重算覆盖（hickory 实证：
            # 插件已装而消费者仍 RUNTIME_PROVIDER_UNAVAILABLE → BLOCKED，
            # 闸门永远无法通过）。语义与 coverable.py 字段注释一致
            # （"Mono 插件已部署？"）；静态无法证明覆盖是诚实的——
            # 终态降为 PENDING_RUNTIME_ATTESTATION（未实机 attest），
            # 而非可绕过候选确认的 BLOCKED。插件未部署（provider_supported
            # False / 安装异常）→ 保持 BLOCKED 诚实阻断。
            from dataclasses import replace as dc_replace
            static.consumers = [
                dc_replace(c, runtime_provider_available=True)
                if c.kind == "dynamic_tmp" and not c.runtime_provider_available
                else c
                for c in static.consumers]
            # verify_static 只返回已有 coverage 不重算——必须显式重算
            # （与位图反哺同款：compute_coverage 后回写三字段）
            from hanhua.core.font.coverable import compute_coverage
            coverage = compute_coverage(static.consumers, plan)
            static.coverage = coverage
            static.overall = coverage.overall.name
            static.incomplete = coverage.blocks_publish()
        gate = self.evaluate_publish(
            static, font, coverage, allow_unverified_font_candidate)
        return FontPipelineOutcome(
            plan=plan, static=static, font=font,
            coverage=coverage, gate=gate,
            warnings=list(self._warnings),
            bitmap=bitmap)
