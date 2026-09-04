from pathlib import Path


def _plugin_source() -> str:
    return (Path(__file__).resolve().parents[1]
            / "font_plugin" / "Hanhua.FontFallback"
            / "HanhuaFontPlugin.cs").read_text(encoding="utf-8")


def _build_script() -> str:
    return (Path(__file__).resolve().parents[1]
            / "font_plugin" / "build.ps1").read_text(encoding="utf-8")


def _method_body(source: str, signature: str) -> str:
    start = source.index(signature)
    opening_brace = source.index("{", start)
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace:index + 1]
    raise AssertionError(f"unclosed method body: {signature}")


def test_runtime_uses_exact_then_normalized_then_unique_template_mapping():
    source = _plugin_source()
    apply_method = source[
        source.index("private int ApplyExactTranslation"):
        source.index("private static bool IsTargetUnavailable")
    ]

    assert apply_method.index("TryGetExactTranslation") < apply_method.index(
        "TryGetNormalizedTranslation")
    assert apply_method.index("TryGetNormalizedTranslation") < (
        apply_method.index("TryGetUniqueTemplateTranslation"))
    assert "NormalizeRuntimeText" in source
    assert "Trim()" in source
    assert 'Replace("\\r\\n", "\\n")' in source
    assert "matchingTemplates != 1" in source
    assert "sourceFragments" in source and "targetFragments" in source
    assert "totalExactTranslationApplications" in source
    assert "totalNormalizedTranslationApplications" in source
    assert "totalTemplateTranslationApplications" in source
    assert '\\"exact_translations\\":' in source
    assert '\\"normalized_translations\\":' in source
    assert '\\"template_translations\\":' in source
    assert '\\"translation_targets\\":' in source
    assert "translationApplicationStates" in apply_method


def test_phase3_protocol_v5_per_scalar_verification_mechanisms():
    source = _plugin_source()

    # 协议 v5：逐 scalar 证明 + 会话 + 消费者统计
    assert "private const int HealthProtocolVersion = 5;" in source
    assert 'PluginVersion = "1.5.0"' in source
    assert 'sessionNonce = Guid.NewGuid().ToString("N")' in source
    assert "LoadRequiredGlyphs" in source
    assert "required-glyphs.json" in source
    assert "ReadJsonUintArray" in source
    assert "VerifyRequiredGlyphs" in source
    assert "legacyCovered" in source and "tmpCovered" in source
    assert "missingCodepoints" in source
    assert "verifiedMissingTotal" in source
    assert "CollectConsumerEvidence" in source
    assert "consumersDiscovered" in source and "consumersChinese" in source
    assert "NoteConsumerFailure" in source
    assert "MaxDetailRecords" in source
    assert '\\"snapshot_hash\\":' in source
    assert '\\"glyph_verification\\":' in source
    assert '\\"consumers\\":' in source
    assert '\\"failures\\":' in source
    assert '\\"session_nonce\\":' in source
    assert '\\"last_seen\\":' in source
    assert '\\"scenes\\":' in source
    # 扫描异常必须写入 error（不能假证明）
    assert "glyphVerificationError" in source
    assert 'glyphVerificationError = ""' in source
    # CLR 2.0 兼容的 unix 时间（不用 DateTimeOffset.ToUnixTimeSeconds）
    assert "new DateTime(1970, 1, 1" in source
    # 诚实报告：非 BMP 不能逐字添加 → 不假证明
    assert "char.ConvertFromUtf32" in source


def test_phase3_apply_fonts_runs_verification_before_manifest():
    source = _plugin_source()
    apply_fonts = source[
        source.index("private void ApplyFonts(string reason)"):
        source.index("private static bool RequiresDeferredTmpGlyphValidation")
    ]
    assert apply_fonts.index("VerifyRequiredGlyphs()") < apply_fonts.index(
        'WriteHealthManifest(reason == "periodic")')
    assert apply_fonts.index("CollectConsumerEvidence()") < apply_fonts.index(
        'WriteHealthManifest(reason == "periodic")')


def test_phase3_scan_exception_fails_attestation():
    source = _plugin_source()
    safe_apply = source[
        source.index("private void SafeApplyFonts(string reason)"):
        source.index("private void ApplyFonts(string reason)")
    ]
    assert safe_apply.index('glyphVerificationError = ""') < (
        safe_apply.index("ApplyFonts(reason)"))
    assert "font-scan-failed: " in safe_apply


def test_phase3_failure_details_are_capped_at_256():
    source = _plugin_source()
    assert source.count("MaxDetailRecords") >= 3
    assert "consumerFailures.Count >= MaxDetailRecords" in source
    assert "index < MaxDetailRecords" in source


def test_tmp_bundle_loader_uses_fixed_file_and_typed_asset_query():
    source = _plugin_source()
    loader = source[
        source.index("private bool TryLoadBundledTmpFont"):
        source.index("private void InitializeTmpFont")
    ]

    assert 'Path.Combine(pluginDirectory, "font-tmp.bundle")' in loader
    assert "AssetBundle.LoadFromFile(bundlePath)" in loader
    assert "LoadAllAssets(tmpFontAssetType)" in loader


def test_tmp_initialization_prefers_bundle_before_dynamic_factories():
    source = _plugin_source()
    initialize = source[
        source.index("private void InitializeTmpFont"):
        source.index("private bool ActivateNextTmpFont")
    ]

    bundle_attempt = initialize.index("TryLoadBundledTmpFont()")
    dynamic_factory_discovery = initialize.index(
        "tmpFontAssetType.GetMethods")
    assert bundle_attempt < dynamic_factory_discovery
    assert "tryBundledFont && TryLoadBundledTmpFont()" in initialize
    assert initialize.index("return;", bundle_attempt) < dynamic_factory_discovery


def test_tmp_bundle_lifecycle_keeps_asset_alive_until_bundle_unload():
    source = _plugin_source()
    destroy = source[
        source.index("private void OnDestroy()"):
        source.index("private void LoadFontPayload()")
    ]
    discard = source[
        source.index("private void DiscardDynamicTmpFont()"):
        source.index("private bool TryAddTmpGlyph")
    ]
    activate = source[
        source.index("private bool ActivateNextTmpFont"):
        source.index("private static string DescribeTmpCandidate")
    ]

    assert 'tmpFontSource == "bundle"' in destroy
    assert "tmpFontBundle.Unload(false)" in destroy
    assert destroy.index("dynamicTmpFont = null") < destroy.index(
        "tmpFontBundle.Unload(false)")
    assert 'tmpFontSource == "bundle"' in discard
    assert "tmpFontBundle.Unload(false)" in discard
    assert 'tmpFontSource = "dynamic"' in activate


def test_tmp_bundle_loader_logs_success_and_clean_fallbacks():
    source = _plugin_source()
    loader = source[
        source.index("private bool TryLoadBundledTmpFont"):
        source.index("private void InitializeTmpFont")
    ]

    assert '"TMP_BUNDLE_READY path="' in loader
    assert loader.count('"TMP_BUNDLE_FALLBACK reason=') >= 2
    assert "if (!File.Exists(bundlePath))" in loader
    assert loader.index('"TMP_BUNDLE_FALLBACK reason=missing') < loader.index(
        "AssetBundle.LoadFromFile(bundlePath)")
    assert "loadedBundle.Unload(false)" in loader


def test_plugin_build_references_asset_bundle_module():
    script = _build_script()
    references = script[
        script.index("$referencePaths = @("):
        script.index("$netstandardPath =")
    ]

    assert "UnityEngine.AssetBundleModule.dll" in references


def test_static_harmony_postfixes_log_through_plugin_instance():
    source = _plugin_source()
    ui_postfix = source[
        source.index("public static void UiSetTextPostfix"):
        source.index("public static void TmpSetTextPostfix")
    ]
    tmp_postfix = source[
        source.index("public static void TmpSetTextPostfix"):
        source.index("public static void ChangeLanguagePostfix")
    ]

    assert "plugin.Logger.LogInfo(" in ui_postfix
    assert "plugin.Logger.LogInfo(" in tmp_postfix


def test_build_validates_supported_clr_families_without_locking_output():
    script = _build_script()
    validation = script[
        script.index("$probeOutputDeadline"):
        script.index("$outputDirectory")
    ]

    assert "[System.IO.File]::ReadAllBytes($temporaryOutput)" in validation
    assert "$probeOutputFile.Length -eq 0" in validation
    assert "$probeAssemblyBytes.Length -ne $probeOutputFile.Length" in validation
    assert "AddSeconds(5)" in validation
    assert "Start-Sleep -Milliseconds 50" in validation
    assert "ReflectionOnlyLoad(" in validation
    assert "ReflectionOnlyLoadFrom" not in validation
    assert "@('2', '4')" in validation


def test_rejected_bundle_lazily_falls_back_to_dynamic_factories():
    source = _plugin_source()
    validation = source[
        source.index("private void EnsureUsableTmpFont()"):
        source.index("private void DiscardDynamicTmpFont()")
    ]

    rejected = validation.index('tmpFontSource == "bundle"')
    discarded = validation.index("DiscardDynamicTmpFont()", rejected)
    dynamic_fallback = validation.index(
        "InitializeTmpFont(tmpFontFamily, false)", discarded)
    assert rejected < discarded < dynamic_fallback


def test_tmp_font_application_paths_share_original_fallback_boundary():
    source = _plugin_source()
    tmp_postfix = _method_body(
        source, "public static void TmpSetTextPostfix")
    language_postfix = _method_body(
        source, "public static void ChangeLanguagePostfix")
    patch_tmp_texts = _method_body(source, "private int PatchTmpTexts")

    assert "ApplyTmpFontToText(__instance)" in tmp_postfix
    assert "ApplyTmpFontToText(tmp)" in language_postfix
    assert "ApplyTmpFontToText(text)" in patch_tmp_texts


def test_apply_tmp_font_preserves_a_compatible_original_before_assignment():
    source = _plugin_source()
    apply_font = _method_body(
        source, "private static bool ApplyTmpFontToText")

    read_original = apply_font.index("fontProperty.GetValue(target, null)")
    attach_original = apply_font.index(
        "plugin.AttachOriginalTmpFallback(original)")
    assign_tool = apply_font.index(
        "fontProperty.SetValue(target, plugin.dynamicTmpFont, null)")

    assert read_original < attach_original < assign_tool
    assert "ReferenceEquals(original, plugin.dynamicTmpFont)" in apply_font
    assert "tmpFontAssetType.IsInstanceOfType(original)" in apply_font
    assert "return true;" in apply_font


def test_apply_tmp_font_switches_sdf_font_and_mesh_generation_forces_it():
    source = _plugin_source()
    apply_font = _method_body(
        source, "private static bool ApplyTmpFontToText")
    # Rendezvous 重构：ApplyTmpMaterialToText 已被 mesh 生成钩子取代——
    # ApplyTmpFontToText 直接替换 font 属性并保留原字体 fallback；材质
    # 切换与网格刷新由 TmpGenerateTextMeshPrefix（生成前强制字体）保证。
    assert "fontProperty.SetValue(target, plugin.dynamicTmpFont, null)" \
        in apply_font
    assert "plugin.AttachOriginalTmpFallback(original)" in apply_font
    assert "return true;" in apply_font
    # mesh 生成钩子：font 在 GenerateTextMesh 之前强制，避免 UV/条纹
    setup = _method_body(source, "private void SetupHarmonyPatches")
    assert "TmpGenerateTextMeshPrefix" in source
    assert '"GenerateTextMesh"' in setup
    prefix = _method_body(
        source, "public static void TmpGenerateTextMeshPrefix")
    assert "ApplyTmpFontToText" in prefix


def test_original_tmp_fallback_is_compatible_acyclic_and_idempotent():
    source = _plugin_source()
    attach = _method_body(
        source, "private void AttachOriginalTmpFallback")

    assert 'GetProperty(\n                "fallbackFontAssetTable"' in attach
    assert "fallbackProperty.CanRead" in attach
    assert "fallbackProperty.CanWrite" in attach
    assert "Activator.CreateInstance" in attach
    assert "tmpFontAssetType.IsInstanceOfType(original)" in attach
    assert "ReferenceEquals(original, dynamicTmpFont)" in attach
    assert "originalFallbacks.Contains(dynamicTmpFont)" in attach
    assert attach.index("fallbacks.Contains(original)") < attach.index(
        "fallbacks.Add(original)")


def test_tmp_asset_scan_does_not_add_reverse_edge_to_preserved_fallback():
    source = _plugin_source()
    apply_fonts = _method_body(source, "private void ApplyFonts")
    patch_assets = _method_body(source, "private int PatchLoadedTmpAssets")
    cycle_guard = _method_body(
        source, "private bool IsDynamicTmpFallback")

    assert apply_fonts.index("PatchTmpTexts") < apply_fonts.index(
        "PatchLoadedTmpAssets")
    assert patch_assets.index(
        "IsDynamicTmpFallback(fontAsset)") < patch_assets.index(
            "fallbacks.Add(dynamicTmpFont)")
    assert '"fallbackFontAssetTable"' in cycle_guard
    assert "fallbacks.Contains(candidate)" in cycle_guard


def test_bundle_mode_keeps_fallback_graph_one_way_and_checks_transitive_cycles():
    source = _plugin_source()
    patch_assets = _method_body(source, "private int PatchLoadedTmpAssets")
    assert 'tmpFontSource == "bundle"' in patch_assets
    assert "WouldCreateTmpFallbackCycle" in patch_assets
    assert "return 0;" in patch_assets

    cycle_guard = (
        _method_body(source, "private bool WouldCreateTmpFallbackCycle")
        + _method_body(source, "private bool CanReachTmpFallback"))
    assert "HashSet<object>" in cycle_guard
    assert "fallbackFontAssetTable" in cycle_guard
    assert "visited.Add" in cycle_guard


def test_tmp_main_font_assignment_survives_fallback_attach_failure():
    source = _plugin_source()
    apply_font = _method_body(source, "private static bool ApplyTmpFontToText")
    attach = apply_font.index("AttachOriginalTmpFallback(original)")
    catch = apply_font.index("catch (Exception", attach)
    assign = apply_font.index(
        "fontProperty.SetValue(target, plugin.dynamicTmpFont, null)")
    assert attach < catch < assign


def test_tmp_enable_hook_covers_texts_created_before_translation_setter():
    source = _plugin_source()
    setup = _method_body(source, "private void SetupHarmonyPatches")
    assert '_tmpOnEnableMethod' in setup
    assert '"OnEnable"' in setup
    assert 'nameof(TmpOnEnablePostfix)' in setup
    assert "TmpOnEnablePostfix" in source


def test_tmp_mesh_generation_hook_forces_font_before_render():
    source = _plugin_source()
    setup = _method_body(source, "private void SetupHarmonyPatches")
    assert '_tmpGenerateTextMeshMethod' in setup
    assert '"GenerateTextMesh"' in setup
    assert 'nameof(TmpGenerateTextMeshPrefix)' in setup
    assert "TmpGenerateTextMeshPrefix" in source


def test_tmp_postfix_logs_the_actual_previous_font_only_after_a_change():
    source = _plugin_source()
    tmp_postfix = _method_body(
        source, "public static void TmpSetTextPostfix")

    describe = tmp_postfix.index("DescribeCurrentTmpFont(__instance)")
    apply_font = tmp_postfix.index("ApplyTmpFontToText(__instance)")
    log_old_font = tmp_postfix.index('" oldFont=" + oldFont')
    assert describe < apply_font < log_old_font
