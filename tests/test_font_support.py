from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

import hanhua.core.font_support as font_support
from hanhua.core.font_support import (
    FontInstallError,
    FontInstallResult,
    FontRuntimeAssets,
    _default_assets,
    _detect_pe_architecture,
    _load_payload_manifest,
    install_font_override,
)
from hanhua.core.models import FontConfig


def test_default_font_runtime_assets_are_production_payloads() -> None:
    assets = _default_assets()
    license_path = assets.runtime_zip.parent / "LICENSE-BepInEx.txt"
    manifest_path = assets.runtime_zip.parent / "BepInEx_payloads.json"
    expected_runtime_sha256 = (
        "82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4"
    )

    assert assets.fonts_dir.is_dir()
    assert assets.runtime_zip.name == "BepInEx_win_x64_5.4.23.5.zip"
    assert {path.name for path in assets.runtime_zip.parent.iterdir() if path.is_file()} == {
        "BepInEx_win_x64_5.4.23.5.zip",
        "BepInEx_win_x86_5.4.23.5.zip",
        "BepInEx_payloads.json",
        "Hanhua.FontFallback.dll",
        "LICENSE-BepInEx.txt",
    }
    assert assets.runtime_zip.is_file()
    assert assets.runtime_zip.stat().st_size == 639118
    assert assets.expected_runtime_size == 639118
    assert assets.plugin_dll.is_file()
    assert license_path.is_file()
    assert assets.expected_runtime_sha256 == expected_runtime_sha256
    assert assets.runtime_x86_zip is not None
    assert assets.runtime_x86_zip.name == "BepInEx_win_x86_5.4.23.5.zip"
    assert assets.runtime_x86_zip.stat().st_size == 638544
    assert assets.expected_x86_size == 638544
    assert assets.expected_x86_sha256 == (
        "37651c79e40d6f909572a4f461ac25350bb3ef8fe7fbd29f1aa8791a33b84c82")
    assert hashlib.sha256(assets.runtime_x86_zip.read_bytes()).hexdigest() == (
        assets.expected_x86_sha256)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_release"] == (
        "https://github.com/BepInEx/BepInEx/releases/tag/v5.4.23.5")
    assert manifest["license_file"] == "LICENSE-BepInEx.txt"
    assert {item["architecture"] for item in manifest["payloads"]} == {
        "x86", "x64"}
    with zipfile.ZipFile(assets.runtime_x86_zip) as archive:
        x86_members = {
            name.replace("\\", "/").casefold()
            for name in archive.namelist()}
        assert archive.testzip() is None
    assert set(manifest["required_members"]) <= {
        "winhttp.dll", "doorstop_config.ini", "BepInEx/core/BepInEx.dll"}
    assert {
        "winhttp.dll", "doorstop_config.ini", "bepinex/core/bepinex.dll"
    } <= x86_members
    assert (
        hashlib.sha256(assets.runtime_zip.read_bytes()).hexdigest()
        == expected_runtime_sha256
    )

    with zipfile.ZipFile(assets.runtime_zip) as archive:
        members = {name.replace("\\", "/").casefold() for name in archive.namelist()}
    assert {
        "winhttp.dll",
        "doorstop_config.ini",
        "bepinex/core/bepinex.dll",
    } <= members

    plugin_payload = assets.plugin_dll.read_bytes()
    # W3 排除表支持（translations-exclude.json + IsExcludedTranslation）
    # 重新编译后的确定性产物哈希——Phase 6 用 tiiny-ragdoll（CLR 2.0
    # Managed）真实构建替换（InvalidDataException→FormatException、
    # HasDefaultValue→IsOptional 两处 CLR 2.0 兼容修复后重编译）。
    assert hashlib.sha256(plugin_payload).hexdigest() == (
        "b806a8f87077d4d4736821214eb1fce552a2b32adfc21fc76e5792f4bfb32b52"
    )
    assert len(plugin_payload) > 0x40
    assert plugin_payload[:2] == b"MZ"
    pe_offset = struct.unpack_from("<I", plugin_payload, 0x3C)[0]
    assert plugin_payload[pe_offset : pe_offset + 4] == b"PE\0\0"


def test_build_script_pins_unity6_compatible_runtime_independently() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = (project_root / "font_plugin" / "build.ps1").read_text(
        encoding="utf-8"
    )

    assert "BepInEx_win_x64_5.4.23.5.zip" in script
    assert (
        "82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4"
        in script
    )
    assert "$expectedBepInExSize = 639118" in script
    assert "$expectedSdkVersion = [System.Version]'10.0.301'" in script
    assert "'/deterministic+'" in script


def test_plugin_source_uses_cross_version_reflection_and_isolated_adapters() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "font_plugin" / "Hanhua.FontFallback"
              / "HanhuaFontPlugin.cs").read_text(encoding="utf-8")

    assert "UnityEngine.TextCoreFontEngineModule" not in source
    assert "using TMPro;" not in source
    assert "using UnityEngine.UI;" not in source
    assert "typeof(TMP_" not in source
    assert "FindObjectsOfTypeAll<TMP_" not in source
    assert "FindObjectsOfTypeAll<Text>" not in source
    assert "UnityEngine.TextCore.LowLevel" not in source
    assert "GlyphRenderMode." not in source
    assert "TMP_FontAsset.CreateFontAsset(" not in source
    assert "GetMethods" in source and '"CreateFontAsset"' in source
    assert "InitializeLegacyFont" in source
    assert "InitializeTmpFont" in source
    assert "InitializeUiToolkitAdapter" in source
    assert "PatchUiToolkitTexts" in source
    assert "FindOptionalType" in source
    assert '"UnityEngine.UIElements.TextElement"' in source
    assert "RunFontScan" in source
    assert "font-health.json" in source
    assert "translations.json" in source


def test_optional_text_discovery_and_ui_toolkit_tree_are_font_independent() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "font_plugin" / "Hanhua.FontFallback"
              / "HanhuaFontPlugin.cs").read_text(encoding="utf-8")
    awake = source[source.index("private void Awake()"):
                   source.index("private void OnDestroy()")]

    assert awake.index("DiscoverOptionalTextTypes();") < awake.index(
        "LoadExactTranslations();")
    assert awake.index("DiscoverOptionalTextTypes();") < awake.index(
        "LoadFontPayload();")
    assert "uiToolkitDocumentType" in source
    assert '"UnityEngine.UIElements.UIDocument"' in source
    assert '"rootVisualElement"' in source
    assert '"hierarchy"' in source
    assert '"childCount"' in source
    assert '"ElementAt"' in source
    assert "EnumerateUiToolkitTextElements" in source
    assert "ApplyExactTranslationsToUiToolkit" in source
    assert "ApplyExactTranslations" in source
    assert "TryAddTmpGlyph" in source
    assert "DescribeFactoryFailure" in source
    assert "BuildTmpFontCandidates" in source
    assert "new Font(fontPath)" in source
    assert "WriteHealthManifest(false);" in source
    assert 'WriteHealthManifest(reason == "periodic");' in source
    factory_arguments = source[
        source.index("private object[] BuildTmpFactoryArguments"):
        source.index("private static void SetOptionalProperty")
    ]
    assert factory_arguments.index('"padding"') < factory_arguments.index('"atlas"')
    assert '"population"' in factory_arguments
    assert 'Enum.Parse(type, "Dynamic", true)' in factory_arguments
    assert factory_arguments.index('"population"') < factory_arguments.index(
        'Enum.Parse(type, "SDFAA", true)')
    assert "#pragma warning disable 0618" in source
    assert "#pragma warning restore 0618" in source
    assert "TMP_FACTORY_READY" in source
    font_candidates = source[
        source.index("private List<Font> BuildTmpFontCandidates"):
        source.index("private void CleanupTmpFontCandidates")
    ]
    assert font_candidates.index("candidates.Add(dynamicFont)") < (
        font_candidates.index("new Font(family)"))
    assert font_candidates.index("new Font(family)") < font_candidates.index(
        "new Font(fontPath)")


def test_plugin_health_manifest_has_versioned_fields_and_refreshes_after_scan() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "font_plugin" / "Hanhua.FontFallback"
              / "HanhuaFontPlugin.cs").read_text(encoding="utf-8")
    health_method = source[
        source.index("private void WriteHealthManifest"):
        source.index("private char RepresentativeGlyph")
    ]
    assert ("private const int HealthProtocolVersion = "
            + str(font_support._FONT_HEALTH_PROTOCOL_VERSION) + ";") in source
    required_json_fields = {
        '\\"protocol_version\\":',
        '\\"plugin_version\\":\\"',
        '\\"session_nonce\\":\\"',
        '\\"last_seen\\":',
        '\\"scenes\\":',
        '\\"glyph_verification\\":{',
        '\\"snapshot_hash\\":\\"',
        '\\"missing_codepoints\\":',
        '\\"consumers\\":{',
        '\\"failures\\":',
        '\\"adapters\\":{',
        '\\"legacy\\":{',
        '\\"tmp\\":{',
        '\\"uitoolkit\\":{',
        '\\"status\\":\\"',
        '\\"error\\":\\"',
        '\\"glyph\\":',
        '\\"glyph_probe\\":\\"',
        '\\"applications\\":{',
        '\\"tmp\\":',
        '\\"ui\\":',
        '\\"uitoolkit\\":',
        '\\"textmesh\\":',
        '\\"translations\\":',
        '\\"exact_translations\\":',
        '\\"normalized_translations\\":',
        '\\"template_translations\\":',
        '\\"translation_targets\\":{',
    }
    missing_fields = required_json_fields - {
        field for field in required_json_fields if field in health_method}
    assert not missing_fields

    apply_method = source[
        source.index("private void ApplyFonts"):
        source.index("private int ApplyExactTranslations")
    ]
    assert "totalExactTranslationApplications++" in source
    assert "totalNormalizedTranslationApplications++" in source
    assert "totalTemplateTranslationApplications++" in source
    assert apply_method.rindex(
        'WriteHealthManifest(reason == "periodic");') > apply_method.rindex(
        'RunFontScan(\n                "ExactTranslations"')
    assert "private string lastHealthPayload;" in source
    assert "string.Equals(payload, lastHealthPayload" in health_method
    assert health_method.index("return;") < health_method.index(
        "File.WriteAllText(temporary")
    assert health_method.rindex("lastHealthPayload = payload;") > max(
        health_method.index("File.Replace(temporary"),
        health_method.index("File.Move(temporary"),
    )


def test_plugin_exact_translation_state_blocks_chains_and_handles_id_reuse() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "font_plugin" / "Hanhua.FontFallback"
              / "HanhuaFontPlugin.cs").read_text(encoding="utf-8")

    assert "sealed class TranslationApplicationState" in source
    assert "Dictionary<int, TranslationApplicationState>" in source
    assert "PruneTranslationApplicationStates();" in source
    assert "unityTarget.GetInstanceID()" in source
    assert "RuntimeHelpers.GetHashCode(target)" in source
    assert "ReferenceEquals(state.Target, target)" in source
    assert "current, state.LastTarget, StringComparison.Ordinal" in source
    assert "state.LastSource" in source
    assert source.count("ApplyExactTranslationsForType(") >= 3
    assert "ApplyExactTranslationsToUiToolkit()" in source
    assert "textProperty.SetValue(target, translated, null);" in source


def test_plugin_json_escape_covers_every_control_character() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "font_plugin" / "Hanhua.FontFallback"
              / "HanhuaFontPlugin.cs").read_text(encoding="utf-8")
    escape_method = source[
        source.index("private static string EscapeJson"):
        source.index("private void EnsureGlobalTmpFallback")
    ]

    assert "foreach (char current in value ?? \"\")" in escape_method
    for literal in ("case '\\b':", "case '\\f':", "case '\\n':",
                    "case '\\r':", "case '\\t':"):
        assert literal in escape_method
    assert "if (current < ' ')" in escape_method
    assert 'ToString("x4")' in escape_method

    encoded = 'prefix\\u0000\\u0001\\b\\t\\fsuffix'
    assert json.loads(f'"{encoded}"') == "prefix\x00\x01\b\t\fsuffix"


def test_tmp_reflection_tries_every_path_then_font_factory_candidate() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "font_plugin" / "Hanhua.FontFallback"
              / "HanhuaFontPlugin.cs").read_text(encoding="utf-8")
    initialize = source[
        source.index("private void InitializeTmpFont"):
        source.index("private object[] BuildTmpFactoryArguments")
    ]

    assert "List<MethodInfo> fileFactories" in initialize
    assert "List<MethodInfo> fontFactories" in initialize
    assert "SortTmpFactoriesBySpecificity" in initialize
    assert "factories.AddRange(fileFactories);" in initialize
    assert "factories.AddRange(fontFactories);" in initialize
    assert "foreach (MethodInfo selected in factories)" in initialize
    assert "List<string> factoryFailures" in initialize
    assert "DescribeFactoryFailure" in initialize
    assert "foreach (object firstArgument in firstArguments)" in initialize
    assert "continue;" in initialize
    assert "fileFactory ?? fontFactory" not in initialize


def test_tmp_candidate_is_verified_and_failed_candidate_falls_back_periodically() -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "font_plugin" / "Hanhua.FontFallback" /
              "HanhuaFontPlugin.cs").read_text(encoding="utf-8")

    initialize = source[
        source.index("private void InitializeTmpFont"):
        source.index("private object[] BuildTmpFactoryArguments")
    ]
    apply_fonts = source[
        source.index("private void ApplyFonts"):
        source.index("private int ApplyExactTranslations")
    ]

    assert "List<TmpFactoryAttempt> tmpFactoryAttempts" in source
    assert "object activeTmpFactoryArgument" in source
    assert "ActivateNextTmpFont" in initialize
    assert "CleanupTmpFontCandidates(fontCandidates, tmpSourceFont)" not in initialize
    assert 'reason == "periodic"' in apply_fonts
    assert 'reason.StartsWith("scene:", StringComparison.Ordinal)' in apply_fonts
    assert 'reason == "awake" && !RequiresDeferredTmpGlyphValidation()' in (
        apply_fonts)
    assert "EnsureUsableTmpFont" in apply_fonts
    assert apply_fonts.index("EnsureUsableTmpFont") < apply_fonts.index(
        'RunFontScan("TMP", PatchLoadedTmpAssets)')
    assert "TMP_FACTORY_REJECTED" in source
    assert "DescribeTmpCandidate(activeTmpFactoryArgument)" in source
    assert "FONT_SCAN_LOOP_STARTED" in source
    assert "TMP_GLYPH_VALIDATION_STARTED" in source
    assert "RemoveDynamicTmpFallbacks();" in source
    assert "Destroy(rejectedFont);" in source
    discard = source[
        source.index("private void DiscardDynamicTmpFont"):
        source.index("private bool TryAddTmpGlyph")
    ]
    assert "finally" in discard
    assert "TMP fallback detach failed during candidate failover" in discard
    assert discard.index("dynamicTmpFont = null;") < discard.index(
        "Destroy(rejectedFont);")


def test_build_script_does_not_require_optional_textcore_assembly() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = (project_root / "font_plugin" / "build.ps1").read_text(
        encoding="utf-8")

    assert "UnityEngine.TextCoreFontEngineModule.dll" not in script


def test_plugin_binary_has_no_static_tmp_factory_or_textcore_reference() -> None:
    import dnfile

    project_root = Path(__file__).resolve().parents[1]
    plugin = project_root / "resources" / "font_override" / "Hanhua.FontFallback.dll"
    pe = dnfile.dnPE(str(plugin))
    try:
        factory_refs = [
            str(row.Name)
            for row in pe.net.mdtables.MemberRef
            if str(row.Name) == "CreateFontAsset"
        ]
        assembly_refs = {
            str(row.Name) for row in pe.net.mdtables.AssemblyRef}
    finally:
        pe.close()

    assert factory_refs == []
    assert "UnityEngine.TextCoreFontEngineModule" not in assembly_refs
    assert "Unity.TextMeshPro" not in assembly_refs
    assert "UnityEngine.UI" not in assembly_refs


def test_build_script_rejects_wrong_size_archive(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    archive = tmp_path / "BepInEx_win_x64_5.4.23.5.zip"
    archive.write_bytes(b"wrong-size")

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project_root / "font_plugin" / "build.ps1"),
            "-GameDir",
            str(game_dir),
            "-BepInExZip",
            str(archive),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode != 0
    assert b"size mismatch" in result.stdout


def test_build_script_is_deterministic_with_real_unity_assemblies() -> None:
    game_dir_value = os.environ.get("HANHUA_SEWER_CALL_DIR")
    if not game_dir_value:
        pytest.skip("set HANHUA_SEWER_CALL_DIR for the real deterministic build gate")

    project_root = Path(__file__).resolve().parents[1]
    plugin = project_root / "resources" / "font_override" / "Hanhua.FontFallback.dll"
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(project_root / "font_plugin" / "build.ps1"),
        "-GameDir",
        str(Path(game_dir_value).resolve()),
        "-BepInExZip",
        str(
            project_root
            / "resources"
            / "font_override"
            / "BepInEx_win_x64_5.4.23.5.zip"
        ),
    ]

    digests = []
    for _ in range(2):
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            check=True,
        )
        digests.append(hashlib.sha256(plugin.read_bytes()).hexdigest())

    assert digests == [
        "f056d927895024905520c58b920d0c944bf08b86765117dc287140fe0879e7d5",
        "f056d927895024905520c58b920d0c944bf08b86765117dc287140fe0879e7d5",
    ]


def _write_fake_pe(
    path: Path,
    machine: int = 0x8664,
    *,
    optional_size: int = 0xF0,
    optional_magic: int = 0x020B,
    characteristics: int = 0x0022,
) -> None:
    pe_offset = 0x80
    optional_offset = pe_offset + 4 + 20
    data = bytearray(optional_offset + optional_size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset:pe_offset + 4] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH",
        data,
        pe_offset + 4,
        machine,
        1,
        0,
        0,
        0,
        optional_size,
        characteristics,
    )
    struct.pack_into("<H", data, optional_offset, optional_magic)
    path.write_bytes(data)


def _make_mono_game(root: Path, machine: int = 0x8664) -> Path:
    root.mkdir()
    optional_magic = 0x010B if machine == 0x014C else 0x020B
    _write_fake_pe(
        root / "TraceGame.exe", machine, optional_magic=optional_magic)
    (root / "TraceGame_Data" / "Managed").mkdir(parents=True)
    (root / "TraceGame_Data" / "Managed" / "UnityEngine.CoreModule.dll").write_bytes(
        b"core"
    )
    (root / "MonoBleedingEdge").mkdir()
    return root


def _make_assets(root: Path) -> FontRuntimeAssets:
    fonts_dir = root / "fonts"
    sc_dir = fonts_dir / "SimplifiedChinese"
    sc_dir.mkdir(parents=True)
    # 2026-09-04 D1 根治：白名单唯一字体改真 TrueType .ttf
    # （magic 00010000）。payload 用合法 TTF 头而非随意字节，
    # 保证 D1 部署闸门（拒绝非 TrueType）不被假数据误伤。
    (sc_dir / "NotoSerifCJKsc-Medium.ttf").write_bytes(
        b"\x00\x01\x00\x00" + b"font-payload")
    runtime_zip = root / "runtime.zip"
    with zipfile.ZipFile(runtime_zip, "w") as archive:
        archive.writestr("winhttp.dll", b"doorstop")
        archive.writestr("doorstop_config.ini", b"enabled=true")
        archive.writestr("BepInEx/core/BepInEx.dll", b"bepinex")
        archive.writestr("changelog.txt", b"BepInEx release notes")
    plugin_dll = root / "Hanhua.FontFallback.dll"
    plugin_dll.write_bytes(b"plugin")
    return FontRuntimeAssets(fonts_dir, runtime_zip, plugin_dll)


def _make_dual_arch_assets(root: Path) -> FontRuntimeAssets:
    assets = _make_assets(root)
    x86_zip = root / "runtime-x86.zip"
    with zipfile.ZipFile(x86_zip, "w") as archive:
        archive.writestr("winhttp.dll", b"doorstop-x86")
        archive.writestr("doorstop_config.ini", b"enabled=true")
        archive.writestr("BepInEx/core/BepInEx.dll", b"bepinex-x86")
    return FontRuntimeAssets(
        assets.fonts_dir, assets.runtime_zip, assets.plugin_dll,
        runtime_x86_zip=x86_zip,
    )


def test_flat_layout_mono_install_without_data_dir(tmp_path: Path) -> None:
    """扁平布局（老 Unity standalone/WebGL 导出：Data 内容散根目录、
    无 *_Data 宿主——hotel-paradise 实证「HotelParadise v1.1 WIN.exe」
    + 根目录 Managed/）同样识别为 Mono 游戏并可安装字体载荷。"""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    _write_fake_pe(
        game_dir / "HotelParadise v1.1 WIN.exe", 0x8664,
        optional_magic=0x020B)
    (game_dir / "Managed").mkdir()
    (game_dir / "Managed" / "UnityEngine.CoreModule.dll").write_bytes(b"core")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")

    result = install_font_override(
        game_dir,
        out_dir,
        FontConfig(
            enabled=True, filename="SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"),
        assets=assets,
    )

    assert result.installed is True
    assert result.payload_deployed is True
    assert result.architecture == "x64"
    assert result.provider_id == "bepinex5_mono_x64"
    assert (out_dir / "winhttp.dll").read_bytes() == b"doorstop"
    assert (out_dir / "BepInEx" / "core" / "BepInEx.dll").read_bytes() == b"bepinex"


def test_installs_font_runtime_into_mono_x64_copy(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")

    result = install_font_override(
        game_dir,
        out_dir,
        FontConfig(
            enabled=True, filename="SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"),
        assets=assets,
    )

    plugin_dir = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    assert result.installed is True
    assert result.payload_deployed is True
    assert result.runtime_verified is False
    assert result.architecture == "x64"
    assert result.provider_id == "bepinex5_mono_x64"
    assert result.payload_available is True
    assert result.filename == "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"
    assert result.family == "Noto Serif CJK SC"
    assert (out_dir / "winhttp.dll").read_bytes() == b"doorstop"
    assert (out_dir / "doorstop_config.ini").read_bytes() == b"enabled=true"
    assert (out_dir / "BepInEx" / "core" / "BepInEx.dll").read_bytes() == b"bepinex"
    assert (plugin_dir / "Hanhua.FontFallback.dll").read_bytes() == b"plugin"
    assert (plugin_dir / "font.ttf").read_bytes() == b"\x00\x01\x00\x00font-payload"
    assert (plugin_dir / "font-family.txt").read_bytes() == b"Noto Serif CJK SC\n"
    assert (plugin_dir / "translations.json").read_bytes() == b"{}\n"
    # BepInEx 发行包自带的根级文档不部署：覆盖游戏同名文件（Windows
    # 大小写不敏感）会破坏原文件（containment-breach 实测根因）。
    assert not (out_dir / "changelog.txt").exists()


def test_installer_atomically_deploys_exact_tmp_bundle_payload(
        tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")
    bundle = tmp_path / "notoserif_sdf_u2019"
    bundle_payload = b"exact-tmp-bundle-payload\x00\xff"
    bundle.write_bytes(bundle_payload)

    install_font_override(
        game_dir, out_dir, FontConfig(), assets=assets,
        tmp_bundle=bundle,
    )

    plugin_dir = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    assert (plugin_dir / "font-tmp.bundle").read_bytes() == bundle_payload
    assert {path.name for path in plugin_dir.iterdir()} == {
        "Hanhua.FontFallback.dll", "font-family.txt", "font.ttf",
        "font-tmp.bundle", "translations.json", "runtime-templates.json",
        "required-glyphs.json",
    }


def test_installer_without_tmp_bundle_deploys_no_bundle_file(
        tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"

    install_font_override(
        game_dir, out_dir, FontConfig(),
        assets=_make_assets(tmp_path / "assets"),
        tmp_bundle=None,
    )

    plugin_dir = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    assert not (plugin_dir / "font-tmp.bundle").exists()


def test_installer_rejects_tmp_bundle_symlink_without_deploying_owned_tree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    bundle_target = tmp_path / "bundle-target"
    bundle_target.write_bytes(b"bundle")
    bundle = tmp_path / "bundle-link"
    try:
        bundle.symlink_to(bundle_target)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        bundle.write_bytes(bundle_target.read_bytes())
        from hanhua.core import font_support as font_support_module
        original_is_link = font_support_module._path_is_link
        monkeypatch.setattr(
            font_support_module,
            "_path_is_link",
            lambda path: Path(path) == bundle or original_is_link(Path(path)),
        )

    with pytest.raises(FontInstallError, match="TMP|bundle|普通文件|符号链接"):
        install_font_override(
            game_dir, out_dir, FontConfig(),
            assets=_make_assets(tmp_path / "assets"),
            tmp_bundle=bundle,
        )

    assert not (out_dir / "BepInEx" / "plugins" / "HanhuaFont").exists()


def test_installer_tmp_bundle_read_error_preserves_existing_owned_tree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    owned = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    owned.mkdir(parents=True)
    (owned / "old.dll").write_bytes(b"keep-existing-owned-tree")
    bundle = tmp_path / "bundle"
    bundle.write_bytes(b"unreadable")
    original_read_bytes = Path.read_bytes

    def fail_bundle_read(path: Path) -> bytes:
        if Path(path) == bundle:
            raise PermissionError("synthetic bundle read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_bundle_read)

    with pytest.raises(FontInstallError, match="TMP bundle"):
        install_font_override(
            game_dir, out_dir, FontConfig(),
            assets=_make_assets(tmp_path / "assets"),
            tmp_bundle=bundle,
        )

    assert {path.name for path in owned.iterdir()} == {"old.dll"}
    assert (owned / "old.dll").read_bytes() == b"keep-existing-owned-tree"


def test_installer_writes_exact_translation_mapping_without_ascii_escaping(
        tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")

    install_font_override(
        game_dir, out_dir, FontConfig(enabled=True), assets=assets,
        translations={"Settings": "设置", "Quit": "退出"},
    )

    payload = (out_dir / "BepInEx" / "plugins" / "HanhuaFont"
               / "translations.json").read_bytes()
    assert payload.endswith(b"\n")
    assert json.loads(payload.decode("utf-8")) == {
        "Quit": "退出", "Settings": "设置"}
    assert b"\\u" not in payload


def test_installer_writes_required_glyphs_snapshot_matching_translations(
        tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")
    translations = {
        "Settings": "设置",
        "Quit": "退出",
        "Health: {0}": "生命值：{0}",
    }

    install_font_override(
        game_dir, out_dir, FontConfig(enabled=True), assets=assets,
        translations=translations,
    )

    required_path = (out_dir / "BepInEx" / "plugins" / "HanhuaFont"
                     / "required-glyphs.json")
    payload = json.loads(required_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    scalars = payload["scalars"]
    expected = sorted({
        ord(character)
        for text in translations.values()
        for character in text
        if not character.isspace()
    })
    assert scalars == expected
    snapshot = hashlib.sha256(
        ",".join(f"U+{scalar:04X}" for scalar in scalars).encode("ascii")
    ).hexdigest()
    assert payload["snapshot_hash"] == snapshot
    # 部署的 required-glyphs.json 与插件健康文件同目录 → 运行时可比对
    assert (required_path.parent / "required-glyphs.json").exists()


def test_installer_writes_versioned_runtime_template_payload(
        tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")
    translations = {
        "Settings\r\nApply": "设置\n应用",
        "Health: {0}": "生命值：{0}",
        "Qu{0}": "退{0}",
        "Level{0}": "第{0}关",
        "Quit{0}": "退出{0}",
        "<b>Settings</b>": "<b>设置</b>",
        "Ammo: {0}/{1}": "弹药：{1}/{0}",
        "Name: {player}": "名称：",
    }

    install_font_override(
        game_dir, out_dir, FontConfig(enabled=True), assets=assets,
        translations=translations,
    )

    plugin_dir = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    assert json.loads((plugin_dir / "translations.json").read_text(
        encoding="utf-8")) == translations
    payload = json.loads((plugin_dir / "runtime-templates.json").read_text(
        encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "templates": [{
            "slots": ["{0}"],
            "source_fragments": ["Health: ", ""],
            "target_fragments": ["生命值：", ""],
        }],
    }


def test_upgrade_replaces_only_owned_plugin_directory(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    owned = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    owned.mkdir(parents=True)
    (owned / "stale.dll").write_bytes(b"stale-owned")
    user_plugin = out_dir / "BepInEx" / "plugins" / "UserPlugin.dll"
    user_plugin.write_bytes(b"user-plugin")
    user_runtime = out_dir / "BepInEx" / "config" / "user.cfg"
    user_runtime.parent.mkdir(parents=True)
    user_runtime.write_bytes(b"user-runtime")
    bundle = tmp_path / "notoserif_sdf_u2019"
    bundle_payload = b"upgrade-tmp-bundle"
    bundle.write_bytes(bundle_payload)

    install_font_override(
        game_dir, out_dir, FontConfig(),
        assets=_make_assets(tmp_path / "assets"),
        tmp_bundle=bundle,
    )

    assert {path.name for path in owned.iterdir()} == {
        "Hanhua.FontFallback.dll", "font-family.txt", "font.ttf",
        "font-tmp.bundle", "translations.json", "runtime-templates.json",
        "required-glyphs.json",
    }
    assert (owned / "font-tmp.bundle").read_bytes() == bundle_payload
    assert user_plugin.read_bytes() == b"user-plugin"
    assert user_runtime.read_bytes() == b"user-runtime"


def test_owned_plugin_swap_failure_rolls_back_runtime_and_owned_tree(
        tmp_path: Path, monkeypatch) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    owned = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    owned.mkdir(parents=True)
    (owned / "old.dll").write_bytes(b"old-owned")
    user_plugin = out_dir / "BepInEx" / "plugins" / "UserPlugin.dll"
    user_plugin.write_bytes(b"user-plugin")
    original_replace = os.replace

    def fail_new_owned_swap(path: Path, target: Path):
        path = Path(path)
        if path.name == "HanhuaFont" and "install" in path.parts:
            raise OSError("synthetic owned swap failure")
        return original_replace(path, target)

    monkeypatch.setattr(os, "replace", fail_new_owned_swap)

    with pytest.raises(FontInstallError, match="回滚|rollback|swap"):
        install_font_override(
            game_dir, out_dir, FontConfig(),
            assets=_make_assets(tmp_path / "assets"),
        )

    assert {path.name for path in owned.iterdir()} == {"old.dll"}
    assert (owned / "old.dll").read_bytes() == b"old-owned"
    assert user_plugin.read_bytes() == b"user-plugin"
    assert not (out_dir / "winhttp.dll").exists()
    assert not (out_dir / "doorstop_config.ini").exists()


def test_runtime_replace_lock_never_truncates_existing_target(
        tmp_path: Path, monkeypatch) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    target = out_dir / "doorstop_config.ini"
    target.write_bytes(b"original-not-truncated")
    original_replace = os.replace

    def lock_target(source, destination):
        if Path(destination) == target:
            raise PermissionError("synthetic Windows lock")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", lock_target)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(FontInstallError, match="回滚|lock"):
        install_font_override(
            game_dir, out_dir, FontConfig(),
            assets=_make_assets(tmp_path / "assets"))

    assert target.read_bytes() == b"original-not-truncated"
    assert not list(out_dir.glob(".*.hanhua-*.tmp"))


def test_owned_double_failure_preserves_stable_recovery_backup(
        tmp_path: Path, monkeypatch) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    owned = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    owned.mkdir(parents=True)
    (owned / "old.dll").write_bytes(b"recover-me")
    original_replace = os.replace

    def fail_new_and_restore(source, destination):
        source = Path(source)
        if ((source.name == "HanhuaFont" and "install" in source.parts)
                or ".hanhua-backup-" in source.name):
            raise PermissionError("synthetic double swap failure")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_new_and_restore)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(FontInstallError, match=r"可恢复.*backup: [A-Z]?:?[\\/]"):
        install_font_override(
            game_dir, out_dir, FontConfig(),
            assets=_make_assets(tmp_path / "assets"))

    backups = list(owned.parent.glob(".HanhuaFont.hanhua-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.dll").read_bytes() == b"recover-me"


def test_owned_backup_cleanup_failure_keeps_committed_plugin_and_reports_pending(
        tmp_path: Path, monkeypatch) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    owned = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    owned.mkdir(parents=True)
    (owned / "old.dll").write_bytes(b"obsolete")
    original_rmtree = shutil.rmtree

    def partially_fail_tombstone(path, *args, **kwargs):
        path = Path(path)
        if ".hanhua-cleanup-" in path.name:
            (path / "old.dll").unlink(missing_ok=True)
            raise OSError("synthetic partial cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", partially_fail_tombstone)

    result = install_font_override(
        game_dir, out_dir, FontConfig(),
        assets=_make_assets(tmp_path / "assets"))

    assert result.installed is True
    assert result.cleanup_pending
    pending = Path(result.cleanup_pending)
    assert pending.is_absolute() and pending.exists()
    assert {path.name for path in owned.iterdir()} == {
        "Hanhua.FontFallback.dll", "font-family.txt", "font.ttf",
        "translations.json", "runtime-templates.json",
        "required-glyphs.json",
    }
    assert not (owned / "old.dll").exists()


def test_payload_manifest_damage_is_explicit(tmp_path: Path) -> None:
    resource_dir = tmp_path / "font_override"
    resource_dir.mkdir()
    (resource_dir / "BepInEx_payloads.json").write_text(
        '{"schema_version":1}', encoding="utf-8")

    with pytest.raises(FontInstallError, match="payload manifest.*无效"):
        _load_payload_manifest(resource_dir)


def test_pe_detection_does_not_read_entire_sparse_executable(
        tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "Sparse.exe"
    _write_fake_pe(executable)
    with executable.open("r+b") as stream:
        stream.seek(1024 * 1024 * 1024)
        stream.write(b"\0")
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("PE detection must use bounded reads")))

    assert _detect_pe_architecture(executable) == "x64"


def test_disabled_font_override_skips_all_validation(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assets = FontRuntimeAssets(missing, missing / "runtime.zip", missing / "plugin.dll")

    result = install_font_override(
        missing / "game",
        missing / "output",
        FontConfig(enabled=False, filename="not-in-the-whitelist.ttf"),
        assets=assets,
    )

    assert result.installed is False
    assert result.filename == ""
    assert result.family == ""


def test_legacy_installed_result_maps_to_payload_deployed() -> None:
    result = FontInstallResult(True, "font.ttf", "Test Font")

    assert result.installed is True
    assert result.payload_deployed is True
    assert result.runtime_verified is False
    assert result.architecture == ""


def test_rejects_font_outside_whitelist(tmp_path: Path) -> None:
    assets = _make_assets(tmp_path / "assets")

    with pytest.raises(FontInstallError, match="白名单"):
        install_font_override(
            tmp_path / "game",
            tmp_path / "output",
            FontConfig(enabled=True, filename="../evil.ttf"),
            assets=assets,
        )


def test_rejects_missing_whitelisted_font_before_installing(tmp_path: Path) -> None:
    assets = _make_assets(tmp_path / "assets")
    (assets.fonts_dir / "SimplifiedChinese" / "NotoSerifCJKsc-Medium.ttf").unlink()
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="字体文件不存在"):
        install_font_override(
            _make_mono_game(tmp_path / "game"),
            out_dir,
            FontConfig(),
            assets=assets,
        )

    assert not out_dir.exists()


def test_rejects_zip_slip_before_extracting_any_member(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    with zipfile.ZipFile(assets.runtime_zip, "a") as archive:
        archive.writestr("../escaped.txt", b"escape")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="不安全.*路径"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not (tmp_path / "escaped.txt").exists()
    assert not out_dir.exists()


def test_rejects_symlink_member_before_extracting_any_member(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    link = zipfile.ZipInfo("BepInEx/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(assets.runtime_zip, "a") as archive:
        archive.writestr(link, "../../escaped.txt")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="符号链接"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_reports_il2cpp_font_provider_as_unsupported(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    (game_dir / "GameAssembly.dll").write_bytes(b"il2cpp")
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    result = install_font_override(
        game_dir, out_dir, FontConfig(), assets=assets)

    assert result.installed is False
    assert result.payload_deployed is False
    assert result.provider_supported is False
    assert result.provider_id == "bepinex6_il2cpp_x64"
    assert result.architecture == "x64"
    assert result.payload_available is False
    assert result.runtime_verified is False
    assert "IL2CPP" in result.unsupported_reason
    assert not out_dir.exists()


def test_installs_x86_payload_for_pe32_mono_game(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game", machine=0x014C)
    assets = _make_dual_arch_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    result = install_font_override(
        game_dir, out_dir, FontConfig(), assets=assets)

    assert result.installed is True
    assert result.payload_deployed is True
    assert result.runtime_verified is False
    assert result.architecture == "x86"
    assert (out_dir / "winhttp.dll").read_bytes() == b"doorstop-x86"


@pytest.mark.parametrize(
    ("machine", "architecture", "provider_id"),
    [
        (0x014C, "x86", "bepinex5_mono_x86"),
        (0x8664, "x64", "bepinex5_mono_x64"),
    ],
)
def test_font_provider_capability_is_explicit_for_mono_architectures(
        tmp_path: Path, machine: int, architecture: str, provider_id: str) -> None:
    game_dir = _make_mono_game(tmp_path / architecture, machine=machine)
    assets = (
        _make_dual_arch_assets(tmp_path / f"assets-{architecture}")
        if architecture == "x86"
        else _make_assets(tmp_path / f"assets-{architecture}")
    )
    capability = font_support.resolve_font_provider(
        game_dir, "mono", assets=assets)

    assert capability.provider_id == provider_id
    assert capability.runtime == "mono"
    assert capability.architecture == architecture
    assert capability.provider_supported is True
    assert capability.payload_available is True
    assert capability.payload_deployed is False
    assert capability.runtime_verified is False
    assert capability.static_writeback_allowed is False
    assert capability.reason == ""


def test_nested_selected_player_font_provider_and_install_are_root_scoped(
        tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    selected = _make_mono_game(source / "PlayerA")
    sibling = _make_mono_game(source / "PlayerB", machine=0x014C)
    sibling_marker = sibling / "BepInEx" / "plugins" / "keep.txt"
    sibling_marker.parent.mkdir(parents=True, exist_ok=True)
    sibling_marker.write_bytes(b"keep")
    assets = _make_assets(tmp_path / "assets")
    capability = font_support.resolve_font_provider(
        source, "mono", assets=assets, player_root=selected)
    assert capability.provider_id == "bepinex5_mono_x64"
    assert capability.payload_deployed is False
    out_dir = tmp_path / "output"
    staged_sibling_marker = out_dir / "PlayerB" / "BepInEx" / "keep.txt"
    staged_sibling_marker.parent.mkdir(parents=True)
    staged_sibling_marker.write_bytes(b"staged-keep")
    result = install_font_override(
        source, out_dir, FontConfig(), assets=assets, player_root=selected)
    assert result.installed is True
    assert (out_dir / "PlayerA" / "winhttp.dll").read_bytes() == b"doorstop"
    assert (out_dir / "PlayerA" / "BepInEx" / "plugins" / "HanhuaFont"
            / "font.ttf").read_bytes() == b"\x00\x01\x00\x00font-payload"
    assert not (out_dir / "winhttp.dll").exists()
    assert sibling_marker.read_bytes() == b"keep"
    assert staged_sibling_marker.read_bytes() == b"staged-keep"


def test_font_provider_capability_reports_il2cpp_and_unknown_without_payload(
        tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    il2cpp = font_support.resolve_font_provider(game_dir, "il2cpp")
    unknown = font_support.resolve_font_provider(game_dir, "unknown")

    assert (
        il2cpp.provider_id,
        il2cpp.architecture,
        il2cpp.provider_supported,
        il2cpp.payload_available,
        il2cpp.payload_deployed,
        il2cpp.runtime_verified,
        il2cpp.reason,
    ) == (
        "bepinex6_il2cpp_x64",
        "x64",
        False,
        False,
        False,
        False,
        "IL2CPP x64 字体 provider 未提供经验证的 BepInEx 6/Il2CppInterop 载荷",
    )
    assert unknown.provider_id == "unsupported_unknown_runtime"
    assert unknown.provider_supported is False
    assert unknown.payload_available is False
    assert unknown.runtime_verified is False
    assert il2cpp.static_writeback_allowed is True
    assert unknown.static_writeback_allowed is False
    assert "unknown" in unknown.reason


def test_unknown_mono_font_capability_requires_installer_validation(
        tmp_path: Path) -> None:
    game_dir = tmp_path / "incomplete-mono"
    game_dir.mkdir()

    capability = font_support.resolve_font_provider(game_dir, "mono")

    assert capability.provider_id == "unsupported_mono_unknown"
    assert capability.provider_supported is False
    assert capability.static_writeback_allowed is False


_V5_SNAPSHOT_HASH = "snapshot-0001"


def _valid_font_health() -> dict:
    return {
        "protocol_version": 5,
        "plugin_version": "1.5.0",
        "session_nonce": "sess-0001",
        "last_seen": int(time.time()),
        "scenes": ["Main"],
        "adapters": {
            "legacy": {"status": "ready", "error": "", "glyph": True},
            "tmp": {"status": "ready", "error": "", "glyph": True},
            "uitoolkit": {
                "status": "unsupported",
                "error": "TextElement unavailable",
                "glyph": False,
            },
        },
        "glyph_probe": "汉",
        "glyph_verification": {
            "snapshot_hash": _V5_SNAPSHOT_HASH,
            "legacy_total": 1,
            "legacy_covered": 1,
            "legacy_missing": 0,
            "tmp_total": 1,
            "tmp_covered": 1,
            "tmp_missing": 0,
            "missing_codepoints": [],
            "error": "",
        },
        "consumers": {
            "discovered": 2,
            "chinese": 1,
            "covered": 1,
            "missing": 0,
            "failed": 0,
        },
        "failures": [],
        "applications": {
            "tmp": 1,
            "ui": 0,
            "uitoolkit": 0,
            "textmesh": 0,
            "translations": 0,
            "exact_translations": 0,
            "normalized_translations": 0,
            "template_translations": 0,
        },
        "translation_targets": {
            name: {"exact": 0, "normalized": 0, "template": 0}
            for name in ("tmp", "ui", "uitoolkit", "textmesh")
        },
    }


def _write_font_health(tmp_path: Path, payload: dict | None = None) -> Path:
    """写 font-health.json + 配套 required-glyphs.json（snapshot 一致）。"""
    payload = _valid_font_health() if payload is None else payload
    required = {
        "schema_version": 1,
        "snapshot_hash": _V5_SNAPSHOT_HASH,
        "scalars": [0x6C49],
    }
    (tmp_path / "required-glyphs.json").write_text(
        json.dumps(required, ensure_ascii=False), encoding="utf-8")
    health_path = tmp_path / "font-health.json"
    health_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return health_path


def test_font_health_requires_strict_versioned_runtime_evidence(
        tmp_path: Path) -> None:
    health_path = _write_font_health(tmp_path)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is True
    assert health.reason == ""


@pytest.mark.parametrize(
    ("mutate", "reason_fragment"),
    [
        (lambda value: value.update(protocol_version=2), "protocol_version"),
        (lambda value: value.update(plugin_version="0.9.0"), "plugin_version"),
        (
            lambda value: [adapter.update(status="failed", error="boom")
                           for adapter in value["adapters"].values()],
            "adapter",
        ),
        (
            lambda value: [adapter.update(glyph=False)
                           for adapter in value["adapters"].values()],
            "glyph",
        ),
        (lambda value: value.update(glyph_probe=""), "glyph_probe"),
        (
            lambda value: value.update(applications={
                "tmp": 0, "ui": 0, "uitoolkit": 0,
                "textmesh": 0, "translations": 0,
                "exact_translations": 0,
                "normalized_translations": 0,
                "template_translations": 0,
            }),
            "application",
        ),
    ],
)
def test_font_health_rejects_stale_or_incomplete_evidence(
        tmp_path: Path, mutate, reason_fragment: str) -> None:
    payload = _valid_font_health()
    mutate(payload)
    health_path = _write_font_health(tmp_path, payload)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is False
    assert reason_fragment in health.reason


def test_font_health_accepts_one_ready_adapter_with_application_evidence(
        tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["adapters"]["tmp"].update(
        status="failed", error="TMP unavailable", glyph=False)
    payload["applications"].update(tmp=0, ui=1)
    health_path = _write_font_health(tmp_path, payload)

    assert font_support.read_font_health(health_path).runtime_verified is True


def test_font_health_rejects_applied_tmp_without_tmp_glyph(
        tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["adapters"]["tmp"].update(glyph=False)
    payload["applications"].update(tmp=2, ui=4)
    health_path = _write_font_health(tmp_path, payload)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is False
    assert "tmp" in health.reason.lower()


def test_font_health_rejects_tmp_translation_masked_by_legacy_font_success(
        tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["adapters"]["tmp"].update(
        status="failed", error="TMP font failed", glyph=False)
    payload["applications"].update(
        tmp=0, ui=2, translations=1, exact_translations=1)
    payload["translation_targets"]["tmp"]["exact"] = 1
    health_path = _write_font_health(tmp_path, payload)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is False
    assert "tmp translation" in health.reason.lower()


def test_font_health_requires_ui_toolkit_adapter_for_ui_toolkit_applications(
        tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["adapters"]["uitoolkit"].update(
        status="ready", error="", glyph=True)
    payload["applications"].update(tmp=0, uitoolkit=2)
    health_path = _write_font_health(tmp_path, payload)

    assert font_support.read_font_health(health_path).runtime_verified is True

    payload["adapters"]["uitoolkit"].update(glyph=False)
    health_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    health = font_support.read_font_health(health_path)
    assert health.runtime_verified is False
    assert "uitoolkit" in health.reason.lower()


def test_font_health_rejects_translation_only_application_evidence(
        tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["applications"].update(
        tmp=0, ui=0, textmesh=0, translations=3,
        exact_translations=3)
    payload["translation_targets"]["ui"]["exact"] = 3
    health_path = _write_font_health(tmp_path, payload)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is False
    assert "font application" in health.reason.lower()


def test_font_provider_separates_deployed_payload_from_runtime_verification(
        tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    plugin_dir = game_dir / "BepInEx" / "plugins" / "HanhuaFont"
    plugin_dir.mkdir(parents=True)
    (game_dir / "winhttp.dll").write_bytes(b"doorstop")
    (game_dir / "doorstop_config.ini").write_bytes(b"enabled=true")
    core = game_dir / "BepInEx" / "core" / "BepInEx.dll"
    core.parent.mkdir(parents=True)
    core.write_bytes(b"bepinex")
    (plugin_dir / "Hanhua.FontFallback.dll").write_bytes(b"plugin")
    (plugin_dir / "font.ttf").write_bytes(b"font")

    deployed = font_support.resolve_font_provider(
        game_dir, "mono", assets=assets)
    assert deployed.payload_deployed is True
    assert deployed.runtime_verified is False

    (plugin_dir / "required-glyphs.json").write_text(
        json.dumps(
            {"schema_version": 1, "snapshot_hash": _V5_SNAPSHOT_HASH,
             "scalars": [0x6C49]},
            ensure_ascii=False),
        encoding="utf-8")
    (plugin_dir / "font-health.json").write_text(
        json.dumps(_valid_font_health(), ensure_ascii=False), encoding="utf-8")
    verified = font_support.resolve_font_provider(
        game_dir, "mono", assets=assets)
    assert verified.payload_deployed is True
    assert verified.runtime_verified is True


# ── Phase 3 协议 v5 特定拒绝路径 ──


@pytest.mark.parametrize(
    ("mutate", "reason_fragment"),
    [
        (
            lambda value: value.update(last_seen=int(time.time()) - 13 * 3600),
            "stale",
        ),
        (lambda value: value.update(session_nonce=""), "session_nonce"),
        (lambda value: value.update(scenes="Main"), "scenes"),
        (
            lambda value: value["glyph_verification"].update(
                legacy_covered=0, legacy_missing=0),
            "glyph counts",
        ),
        (
            lambda value: value["glyph_verification"].update(
                missing_codepoints=[0x6C49]),
            "missing codepoints",
        ),
        (
            lambda value: value["glyph_verification"].update(error="boom"),
            "reported errors",
        ),
        (
            lambda value: value["glyph_verification"].update(snapshot_hash="nope"),
            "does not match deployment",
        ),
        (
            lambda value: value["consumers"].update(
                covered=0, missing=0, failed=0),
            "inconsistent",
        ),
        (
            lambda value: value["consumers"].update(chinese=0, covered=0),
            "no chinese",
        ),
        (
            lambda value: value["consumers"].update(discovered=0),
            "discovered",
        ),
        (
            lambda value: value.update(failures=[{
                "stable_identity": "o", "kind": "ui", "font_asset": "f",
                "missing": ["0x6C49"],
            }]),
            "failure record",
        ),
        (
            lambda value: value.update(failures=list(range(257))),
            "failures are invalid",
        ),
    ],
)
def test_font_health_v5_rejects_attestation_violations(
        tmp_path: Path, mutate, reason_fragment: str) -> None:
    payload = _valid_font_health()
    mutate(payload)
    health_path = _write_font_health(tmp_path, payload)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is False
    assert reason_fragment in health.reason


def test_font_health_v5_rejects_failed_consumers_without_failures(
        tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["consumers"].update(
        chinese=2, discovered=2, covered=0, missing=1, failed=1)
    health_path = _write_font_health(tmp_path, payload)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is False
    assert "failures are missing" in health.reason


def test_font_health_v5_rejects_missing_required_glyphs_deployment(
        tmp_path: Path) -> None:
    (tmp_path / "font-health.json").write_text(
        json.dumps(_valid_font_health(), ensure_ascii=False), encoding="utf-8")

    health = font_support.read_font_health(tmp_path / "font-health.json")

    assert health.runtime_verified is False
    assert "required-glyphs.json" in health.reason


def test_font_health_v5_rejects_stale_last_seen_type(tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["last_seen"] = "recent"
    health_path = _write_font_health(tmp_path, payload)

    health = font_support.read_font_health(health_path)

    assert health.runtime_verified is False
    assert "last_seen" in health.reason


def test_font_health_v5_accepts_failure_details_within_limit(
        tmp_path: Path) -> None:
    payload = _valid_font_health()
    payload["consumers"].update(chinese=2, covered=0, missing=2)
    payload["glyph_verification"].update(
        legacy_covered=1, legacy_missing=0)
    payload["failures"] = [
        {"stable_identity": f"obj-{index}", "kind": "ui",
         "font_asset": "Arial", "missing": [0x6C49, 0x4E00]}
        for index in range(200)
    ]
    health_path = _write_font_health(tmp_path, payload)

    assert font_support.read_font_health(health_path).runtime_verified is True


@pytest.mark.parametrize(
    ("machine", "optional_magic"),
    ((0x014C, 0x020B), (0x8664, 0x010B)),
)
def test_rejects_crossed_pe_machine_and_optional_magic(
        tmp_path: Path, machine: int, optional_magic: int) -> None:
    game_dir = _make_mono_game(tmp_path / "game", machine=machine)
    _write_fake_pe(
        game_dir / "TraceGame.exe", machine,
        optional_magic=optional_magic,
    )
    assets = _make_dual_arch_assets(tmp_path / "assets")

    with pytest.raises(FontInstallError, match="machine|PE32|架构"):
        install_font_override(
            game_dir, tmp_path / "output", FontConfig(), assets=assets)


def test_rejects_incomplete_mono_game(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    (
        game_dir / "TraceGame_Data" / "Managed" / "UnityEngine.CoreModule.dll"
    ).unlink()
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="不完整"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_accepts_legacy_unity4_mono_layout(tmp_path: Path) -> None:
    """Unity 4.x 老结构：Managed 无 UnityEngine.CoreModule.dll，仅整体
    UnityEngine.dll（222am 实证，UnityScript/Boo.Lang 特征），不应拒绝。"""
    game_dir = _make_mono_game(tmp_path / "game")
    managed = game_dir / "TraceGame_Data" / "Managed"
    (managed / "UnityEngine.CoreModule.dll").unlink()
    (managed / "UnityEngine.dll").write_bytes(b"core-legacy")
    (game_dir / "MonoBleedingEdge").rmdir()
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    result = install_font_override(
        game_dir, out_dir, FontConfig(), assets=assets)

    assert result.architecture == "x64"
    assert out_dir.exists()


def test_accepts_legacy_mono_layout_without_monobleedingedge(
        tmp_path: Path) -> None:
    """Unity 5.x 老游戏无 MonoBleedingEdge（Mono 内嵌 UnityPlayer.dll），
    BepInEx 5.x 同样支持，不应拒绝（foxhunt/tiiny-ragdoll 实测）。"""
    game_dir = _make_mono_game(tmp_path / "game")
    (game_dir / "MonoBleedingEdge").rmdir()
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    result = install_font_override(
        game_dir, out_dir, FontConfig(), assets=assets)

    assert result.installed is True
    assert result.payload_deployed is True


def test_rejects_unknown_existing_winhttp_without_overwriting(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    existing = out_dir / "winhttp.dll"
    existing.write_bytes(b"someone-elses-proxy")

    with pytest.raises(FontInstallError, match="winhttp.dll.*冲突"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert existing.read_bytes() == b"someone-elses-proxy"
    assert not (out_dir / "doorstop_config.ini").exists()


def test_allows_matching_existing_winhttp(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    existing = out_dir / "winhttp.dll"
    existing.write_bytes(b"doorstop")

    result = install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert result.installed is True
    assert existing.read_bytes() == b"doorstop"


def test_rejects_runtime_hash_mismatch_before_extraction(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    original = _make_assets(tmp_path / "assets")
    assets = FontRuntimeAssets(
        original.fonts_dir,
        original.runtime_zip,
        original.plugin_dll,
        expected_runtime_sha256="0" * 64,
    )
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="SHA-256"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_runtime_size_mismatch_before_extraction(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    original = _make_assets(tmp_path / "assets")
    assets = FontRuntimeAssets(
        original.fonts_dir,
        original.runtime_zip,
        original.plugin_dll,
        expected_runtime_size=1,
    )
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="大小"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


@pytest.mark.parametrize(
    "missing",
    ["winhttp.dll", "doorstop_config.ini", "BepInEx/core/BepInEx.dll"],
)
def test_rejects_runtime_missing_required_member(tmp_path: Path, missing: str) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    payloads = {
        "winhttp.dll": b"doorstop",
        "doorstop_config.ini": b"enabled=true",
        "BepInEx/core/BepInEx.dll": b"bepinex",
    }
    with zipfile.ZipFile(assets.runtime_zip, "w") as archive:
        for name, payload in payloads.items():
            if name != missing:
                archive.writestr(name, payload)
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="缺少必要文件"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_case_insensitive_duplicate_member_without_writing(
    tmp_path: Path,
) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    with zipfile.ZipFile(assets.runtime_zip, "a") as archive:
        archive.writestr("WINHTTP.DLL", b"alias-overwrite")
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    existing = out_dir / "winhttp.dll"
    existing.write_bytes(b"doorstop")

    with pytest.raises(FontInstallError, match="重复|冲突"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert existing.read_bytes() == b"doorstop"
    assert not (out_dir / "doorstop_config.ini").exists()


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "BepInEx/core/payload.dll:stream",
        "BepInEx/core/trailing.",
        "BepInEx/core/trailing ",
        "BepInEx/core/CON.dll",
        "BepInEx//core/payload.dll",
    ],
)
def test_rejects_unsafe_windows_member_name_without_writing(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    with zipfile.ZipFile(assets.runtime_zip, "a") as archive:
        archive.writestr(unsafe_name, b"unsafe")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="不安全|Windows"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_file_child_topology_conflict_without_writing(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    with zipfile.ZipFile(assets.runtime_zip, "a") as archive:
        archive.writestr("BepInEx", b"blocks-child")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="拓扑|冲突"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_missing_plugin_before_writing_output(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    assets.plugin_dll.unlink()
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="插件.*不存在"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_unreadable_runtime_zip_without_writing_output(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    assets.runtime_zip.write_bytes(b"not-a-zip")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="压缩包.*损坏|无法读取"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_bad_member_crc_before_writing_output(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    corrupted = bytearray(assets.runtime_zip.read_bytes())
    payload_offset = corrupted.find(b"bepinex")
    assert payload_offset >= 0
    corrupted[payload_offset] ^= 0x01
    assets.runtime_zip.write_bytes(corrupted)
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="压缩包.*损坏|CRC|无法读取"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_output_topology_failure_preserves_existing_tree(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    blocker = out_dir / "BepInEx"
    blocker.write_bytes(b"existing-file")

    with pytest.raises(FontInstallError, match="输出|安装"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert blocker.read_bytes() == b"existing-file"
    assert sorted(path.name for path in out_dir.iterdir()) == ["BepInEx"]


def test_rejects_whitelisted_font_symlink_escaping_font_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    assets = _make_assets(tmp_path / "assets")
    outside_font = tmp_path / "outside.ttf"
    outside_font.write_bytes(b"outside-font")
    selected_font = assets.fonts_dir / "SimplifiedChinese" / "NotoSerifCJKsc-Medium.ttf"
    selected_font.unlink()
    try:
        selected_font.symlink_to(outside_font)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        selected_font.write_bytes(outside_font.read_bytes())
        original_resolve = Path.resolve

        def resolve_with_simulated_link(path: Path, *args, **kwargs) -> Path:
            if path == selected_font:
                return outside_font.resolve()
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_with_simulated_link)
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="字体根目录|符号链接|范围"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_truncated_pe_coff_header(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    executable = game_dir / "TraceGame.exe"
    executable.write_bytes(executable.read_bytes()[:0x86])
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="PE|截断"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_pe32_plus_with_only_magic_in_optional_header(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    _write_fake_pe(game_dir / "TraceGame.exe", optional_size=2)
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="PE|optional"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_pe_without_pe32_plus_magic(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    _write_fake_pe(game_dir / "TraceGame.exe", optional_magic=0x010B)
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match=r"machine|magic|架构"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_rejects_pe_without_executable_characteristic(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    _write_fake_pe(game_dir / "TraceGame.exe", characteristics=0x0020)
    assets = _make_assets(tmp_path / "assets")
    out_dir = tmp_path / "output"

    with pytest.raises(FontInstallError, match="可执行"):
        install_font_override(game_dir, out_dir, FontConfig(), assets=assets)

    assert not out_dir.exists()


def test_uses_valid_pe_when_another_matching_candidate_is_invalid(tmp_path: Path) -> None:
    game_dir = _make_mono_game(tmp_path / "game")
    _write_fake_pe(game_dir / "Broken.exe", optional_magic=0x010B)
    (game_dir / "Broken_Data" / "Managed").mkdir(parents=True)
    (
        game_dir / "Broken_Data" / "Managed" / "UnityEngine.CoreModule.dll"
    ).write_bytes(b"core")
    assets = _make_assets(tmp_path / "assets")

    result = install_font_override(
        game_dir,
        tmp_path / "output",
        FontConfig(),
        assets=assets,
    )

    assert result.installed is True


def test_installer_excludes_reverted_logic_keys_from_runtime_translations(
        tmp_path: Path) -> None:
    """W3：静态写回被回退（保留原文防断链）的逻辑键原文，必须从插件
    翻译表剔除并落排除表文件——插件运行时再翻译 → 按名比较断链。"""
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")

    install_font_override(
        game_dir, out_dir, FontConfig(enabled=True), assets=assets,
        translations={
            "Continue": "继续",      # 显示侧译文
            "Quit": "退出",
        },
        exclude={"Continue", "moveForward"},   # 写回侧回退的逻辑键
    )

    plugin_dir = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    payload = json.loads(
        (plugin_dir / "translations.json").read_text(encoding="utf-8"))
    assert payload == {"Quit": "退出"}          # Continue 被剔除
    exclude_payload = json.loads(
        (plugin_dir / "translations-exclude.json").read_text(encoding="utf-8"))
    assert exclude_payload == ["Continue", "moveForward"]


def test_installer_no_exclude_writes_no_exclude_file(tmp_path: Path) -> None:
    """无回退逻辑键时不产生排除表文件（零额外载荷）。"""
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")

    install_font_override(
        game_dir, out_dir, FontConfig(enabled=True), assets=assets,
        translations={"Quit": "退出"},
    )

    plugin_dir = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    assert not (plugin_dir / "translations-exclude.json").exists()
    assert json.loads(
        (plugin_dir / "translations.json").read_text(encoding="utf-8")) == {
        "Quit": "退出"}


def test_legacy_font_filename_maps_to_new(tmp_path: Path) -> None:
    """旧库兼容（Rendezvous 实证 2026-08-17）：store 配置的弃用字体
    路径自动映射到当前唯一 TrueType 字体——只建新字体文件、不建旧
    文件，安装必须成功且装载新字体。2026-09-04 D1 根治：旧默认 OTF
    路径也映射到 .ttf（CFF 不得作插件部署源）。"""
    from hanhua.core.font_support import _normalize_font_filename
    # 当前白名单路径是自身映射
    assert _normalize_font_filename(
        "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf") == \
        "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"
    # 旧默认 OTF 与弃用思源黑体系列全部映射到 .ttf
    for legacy in (
        "SimplifiedChinese/NotoSerifCJKsc-Medium.otf",
        "SimplifiedChinese/SourceHanSansSC-Regular.otf",
        "SimplifiedChinese/SourceHanSansSC-Medium.otf",
        "SimplifiedChinese/SourceHanSansSC-Medium.ttf",
    ):
        assert _normalize_font_filename(legacy) == \
            "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf", legacy
    assert _normalize_font_filename("other/path.ttf") == "other/path.ttf"

    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")
    # 只存在新字体文件——旧路径文件不存在（磁盘已弃用）
    (assets.fonts_dir / "SimplifiedChinese"
     / "NotoSerifCJKsc-Medium.ttf").write_bytes(b"\x00\x01\x00\x00new-font")

    result = install_font_override(
        game_dir, out_dir,
        # 显式用旧 OTF 配置，覆盖「旧库 store 记录的 CFF 路径」的映射线路
        FontConfig(enabled=True,
                   filename="SimplifiedChinese/NotoSerifCJKsc-Medium.otf"),
        assets=assets,
    )
    assert result.installed is True
    assert result.filename == "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf"
    assert result.family == "Noto Serif CJK SC"
    plugin_dir = out_dir / "BepInEx" / "plugins" / "HanhuaFont"
    assert (plugin_dir / "font.ttf").read_bytes() == b"\x00\x01\x00\x00new-font"
    assert (plugin_dir / "font-family.txt").read_text(
        encoding="utf-8").strip() == "Noto Serif CJK SC"


def test_rejects_cff_otf_payload_for_plugin_deployment(tmp_path: Path) -> None:
    """D1 部署闸门（问题集 D1 复发根治 2026-09-04）：即使白名单/别名
    将来被配错回 OTF，install_font_override 也必须在部署前拒绝 CFF
    （OTTO magic）载荷——CFF 被 Unity `new Font(fontPath)` 按 TTF 解析
    → 缺字口口口。宁漏勿坏：部署失败可见，好过静默部署坏字体。"""
    game_dir = _make_mono_game(tmp_path / "game")
    out_dir = tmp_path / "output"
    assets = _make_assets(tmp_path / "assets")
    # 白名单 TTF 文件位置写入 OTTO 内容（模拟配错/损坏源）
    (assets.fonts_dir / "SimplifiedChinese"
     / "NotoSerifCJKsc-Medium.ttf").write_bytes(b"OTTO" + b"\x00" * 64)

    with pytest.raises(FontInstallError, match="真 TrueType"):
        install_font_override(game_dir, out_dir, FontConfig(enabled=True),
                              assets=assets)
    # 部署被拒：不产生任何输出
    assert not out_dir.exists()
