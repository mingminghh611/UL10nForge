from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import struct
import subprocess

import pytest

from hanhua.core.font_support import FontProviderCapability
import hanhua.core.tooling.fingerprint as fingerprint_module
from hanhua.core.tooling.fingerprint import FingerprintError, fingerprint_game
import hanhua.core.tooling.player_layout as player_layout_module
from hanhua.core.tooling.player_layout import (
    PlayerLayoutError,
    discover_application_assemblies,
    discover_player_candidates,
    is_pe_image,
)
from hanhua.core.tooling.planner import (
    plan_backends,
    plan_is_completable,
    plan_is_unblocked,
)


def _mono_game(tmp_path):
    game = tmp_path / "Mono Game"
    data = game / "Mono Game_Data"
    managed = data / "Managed"
    managed.mkdir(parents=True)
    _write_pe(game / "Mono Game.exe")
    (game / "UnityCrashHandler64.exe").write_bytes(b"MZ")
    _write_pe(managed / "Assembly-CSharp.dll", cli=True)
    (managed / "Unity.TextMeshPro.dll").write_bytes(b"fixture")
    (managed / "NGUI.dll").write_bytes(b"fixture")
    (data / "level0").write_bytes(b"serialized fixture")
    bundles = data / "StreamingAssets"
    bundles.mkdir()
    (bundles / "0123456789abcdef").write_bytes(b"UnityFS\0fixture")
    (data / "globalgamemanagers").write_bytes(b"prefix 2021.3.5f1 suffix")
    return game


def _il2cpp_game(tmp_path, version=29, bitmap=False):
    game = tmp_path / "IL2CPP Game"
    data = game / "IL2CPP Game_Data"
    metadata = data / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    metadata.parent.mkdir(parents=True)
    _write_pe(game / "IL2CPP Game.exe")
    _write_pe(game / "GameAssembly.dll")
    metadata.write_bytes(struct.pack("<II", 0xFAB11BAF, version) + b"\0" * 64)
    (data / "globalgamemanagers").write_bytes(b"2022.3.10f1")
    if bitmap:
        font = data / "StreamingAssets" / "font"
        font.mkdir(parents=True)
        (font / "dialog.fnt").write_text("info face=fixture", encoding="utf-8")
    return game


def _write_pe(path: Path, *, cli: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytearray(0x400)
    blob[:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, 0x80)
    blob[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", blob, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0x22)
    struct.pack_into("<H", blob, 0x98, 0x20B)
    struct.pack_into("<I", blob, 0x98 + 108, 16)
    section = 0x80 + 4 + 20 + 0xF0
    blob[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", blob, section + 8, 0x200, 0x2000, 0x200, 0x200)
    if cli:
        struct.pack_into("<II", blob, 0x98 + 112 + 14 * 8, 0x2000, 0x48)
        struct.pack_into("<IHHII", blob, 0x200, 0x48, 2, 5, 0x2080, 0x20)
        struct.pack_into("<I", blob, 0x210, 1)
        blob[0x280:0x284] = b"BSJB"
    path.write_bytes(blob)


def _flat_mono(root: Path, assembly: str = "Assembly-CSharp.dll") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_pe(root / "Game.exe")
    (root / "globalgamemanagers").write_bytes(b"Unity fixture")
    _write_pe(root / "Managed" / assembly, cli=True)
    return root


def _flat_il2cpp(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_pe(root / "Game.exe")
    _write_pe(root / "GameAssembly.dll")
    (root / "mainData").write_bytes(b"Unity fixture")
    metadata = root / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(struct.pack("<II", 0xFAB11BAF, 29))
    return root


def _standard_mono(root: Path, stem: str = "Game") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_pe(root / f"{stem}.exe")
    data = root / f"{stem}_Data"
    (data / "globalgamemanagers").parent.mkdir(parents=True)
    (data / "globalgamemanagers").write_bytes(b"Unity fixture")
    _write_pe(data / "Managed" / "Assembly-CSharp.dll", cli=True)
    return root


def _same_root_mono_players(root: Path) -> Path:
    _standard_mono(root, "A")
    _standard_mono(root, "B")
    return root


def test_player_layout_resolves_flat_mono_and_unityscript_fallback(tmp_path):
    mono = discover_player_candidates(_flat_mono(tmp_path / "mono"))
    unityscript = discover_player_candidates(
        _flat_mono(tmp_path / "unityscript", "Assembly-UnityScript.dll"))

    assert len(mono) == len(unityscript) == 1
    assert mono[0].layout_kind == "flat"
    assert mono[0].data_dir == mono[0].player_root
    assert [path.name for path in mono[0].application_assemblies] == [
        "Assembly-CSharp.dll"]
    assert [path.name for path in unityscript[0].application_assemblies] == [
        "Assembly-UnityScript.dll"]


def test_player_layout_resolves_flat_il2cpp(tmp_path):
    layout, = discover_player_candidates(_flat_il2cpp(tmp_path))

    assert layout.layout_kind == "flat"
    assert layout.game_assembly.name == "GameAssembly.dll"
    assert layout.metadata.name == "global-metadata.dat"
    assert layout.application_assemblies == ()


def test_fingerprint_selects_unique_flat_mono_player(tmp_path):
    source = _flat_mono(tmp_path / "flat-mono")

    result = fingerprint_game(source)

    assert result.game_dir == source.resolve()
    assert result.player_root == source.resolve()
    assert result.layout_kind == "flat"
    assert result.runtime == "mono"
    assert result.application_assemblies == (
        (source / "Managed" / "Assembly-CSharp.dll").resolve(),
    )


def test_fingerprint_selects_unique_flat_il2cpp_player(tmp_path):
    source = _flat_il2cpp(tmp_path / "flat-il2cpp")

    result = fingerprint_game(source)

    assert result.player_root == source.resolve()
    assert result.layout_kind == "flat"
    assert result.runtime == "il2cpp"
    assert result.game_assembly == (source / "GameAssembly.dll").resolve()
    assert result.metadata == (
        source / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    ).resolve()


def test_fingerprint_uses_manifest_application_assemblies(tmp_path):
    source = _standard_mono(tmp_path / "custom", "Spolous")
    data = source / "Spolous_Data"
    (data / "Managed" / "Assembly-CSharp.dll").unlink()
    names = ["StgAssembly_1.dll", "StgAssembly_2.dll"]
    for name in names:
        _write_pe(data / "Managed" / name, cli=True)
    (data / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": names,
        "types": [16, 16],
    }), encoding="utf-8")

    result = fingerprint_game(source)

    assert result.runtime == "mono"
    assert [path.name for path in result.application_assemblies] == names


def test_fingerprint_reports_ambiguous_nested_players_stably(tmp_path):
    source = tmp_path / "collection"
    _standard_mono(source / "B", "Second")
    _standard_mono(source / "A", "First")

    result = fingerprint_game(source)

    assert result.game_dir == source.resolve()
    assert result.player_root is None
    assert result.layout_kind == "ambiguous"
    assert result.runtime == "unknown"
    assert result.application_assemblies == ()
    assert result.capabilities == ()
    assert result.evidence == (
        "ambiguous_player_layout",
        "player_candidate:A:A/First.exe",
        "player_candidate:B:B/Second.exe",
    )
    route = plan_backends(result, {})
    text_scan = next(step for step in route if step.step_id == "text_scan")
    assert text_scan.status == "blocked"
    assert not any(step.step_id == "text_scan" and step.status == "pending"
                   for step in route)


def test_fingerprint_same_root_requires_executable_selector(tmp_path):
    source = _same_root_mono_players(tmp_path / "shared")

    with pytest.raises(FingerprintError, match="player_executable"):
        fingerprint_game(source, player_root=source)


def test_fingerprint_same_root_selects_by_root_and_executable(tmp_path):
    source = _same_root_mono_players(tmp_path / "shared")

    result = fingerprint_game(
        source,
        player_root=source,
        player_executable=source / "B.exe",
    )

    assert result.player_root == source.resolve()
    assert result.executable == (source / "B.exe").resolve()
    assert result.data_dir == (source / "B_Data").resolve()
    assert [path.name for path in result.application_assemblies] == [
        "Assembly-CSharp.dll"]


def test_fingerprint_same_root_selects_by_relative_executable_only(tmp_path):
    source = _same_root_mono_players(tmp_path / "shared")

    result = fingerprint_game(source, player_executable=Path("B.exe"))

    assert result.player_root == source.resolve()
    assert result.executable == (source / "B.exe").resolve()
    assert result.data_dir == (source / "B_Data").resolve()


def test_fingerprint_rejects_conflicting_root_and_executable_selectors(tmp_path):
    source = tmp_path / "collection"
    first = _standard_mono(source / "A", "First")
    second = _standard_mono(source / "B", "Second")

    with pytest.raises(FingerprintError, match="player_executable"):
        fingerprint_game(
            source,
            player_root=first,
            player_executable=second / "Second.exe",
        )


@pytest.mark.parametrize("selector_kind", ("missing", "outside", "noncandidate"))
def test_fingerprint_rejects_invalid_executable_selector(
        tmp_path, selector_kind):
    source = _same_root_mono_players(tmp_path / "shared")
    if selector_kind == "missing":
        selector = Path("missing.exe")
    elif selector_kind == "outside":
        selector = tmp_path / "outside.exe"
        _write_pe(selector)
    else:
        selector = source / "helper.exe"
        _write_pe(selector)

    with pytest.raises(FingerprintError, match="player_executable"):
        fingerprint_game(source, player_executable=selector)


def test_fingerprint_rejects_reparse_executable_selector(tmp_path, monkeypatch):
    source = _same_root_mono_players(tmp_path / "shared")
    selector = source / "B.exe"
    original = fingerprint_module._is_reparse_point
    monkeypatch.setattr(
        fingerprint_module,
        "_is_reparse_point",
        lambda path: path == selector or original(path),
    )

    with pytest.raises(FingerprintError, match="player_executable"):
        fingerprint_game(source, player_executable=selector)


def test_fingerprint_explicitly_selects_nested_player(tmp_path):
    source = tmp_path / "collection"
    _standard_mono(source / "A", "First")
    selected = _standard_mono(source / "B", "Second")

    result = fingerprint_game(source, player_root=selected)

    assert result.game_dir == source.resolve()
    assert result.player_root == selected.resolve()
    assert result.layout_kind == "nested_standard"
    assert result.runtime == "mono"
    assert result.executable == (selected / "Second.exe").resolve()


def test_fingerprint_rejects_invalid_explicit_player_selector(tmp_path):
    source = tmp_path / "collection"
    _standard_mono(source / "A", "First")

    with pytest.raises(FingerprintError, match="player_root"):
        fingerprint_game(source, player_root=source / "missing")


def test_fingerprint_dual_backend_is_runtime_ambiguous(tmp_path):
    source = _flat_mono(tmp_path / "dual")
    _write_pe(source / "GameAssembly.dll")
    metadata = source / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(struct.pack("<II", 0xFAB11BAF, 29))

    result = fingerprint_game(source)

    assert result.runtime == "unknown"
    assert result.application_assemblies
    assert result.game_assembly == (source / "GameAssembly.dll").resolve()
    assert result.metadata == metadata.resolve()
    assert "ambiguous_runtime_backend" in result.evidence
    assert not any("literal" in capability or "font_fallback" in capability
                   for capability in result.capabilities)
    assert all("writeback" not in capability
               for capability in result.capabilities)


def test_fingerprint_standard_player_keeps_source_relative_paths(tmp_path):
    source = _standard_mono(tmp_path / "standard", "Existing")

    result = fingerprint_game(source)

    assert result.player_root == source.resolve()
    assert result.layout_kind == "standard"
    assert result.executable.relative_to(result.game_dir).as_posix() == (
        "Existing.exe")
    assert result.data_dir.relative_to(result.game_dir).as_posix() == (
        "Existing_Data")


def test_fingerprint_preserves_standard_unknown_player_evidence(tmp_path):
    source = _standard_mono(tmp_path / "unknown", "Incomplete")
    data = source / "Incomplete_Data"
    (data / "Managed" / "Assembly-CSharp.dll").unlink()
    (data / "globalgamemanagers").write_bytes(b"2021.3.5f1")
    (data / "content.bundle").write_bytes(b"UnityFS\0fixture")

    result = fingerprint_game(source)

    assert result.runtime == "unknown"
    assert result.player_root is None
    assert result.layout_kind == "unknown"
    assert result.executable == (source / "Incomplete.exe").resolve()
    assert result.data_dir == data.resolve()
    assert result.unity_version == "2021.3.5f1"
    assert {"player_pair", "asset_bundle"} <= set(result.evidence)
    assert {"native_text_extract", "native_asset_extract"} <= set(
        result.capabilities)
    assert all("writeback" not in capability
               for capability in result.capabilities)
    assert not any("literal" in capability or "font_fallback" in capability
                   for capability in result.capabilities)


def test_fingerprint_malformed_manifest_fallback_is_read_only(tmp_path):
    source = _standard_mono(tmp_path / "malformed", "Game")
    data = source / "Game_Data"
    (data / "ScriptingAssemblies.json").write_text(
        '{"names": [', encoding="utf-8")

    result = fingerprint_game(source)

    assert result.runtime == "unknown"
    assert result.executable == (source / "Game.exe").resolve()
    assert all("writeback" not in capability
               for capability in result.capabilities)


def test_fingerprint_missing_manifest_assembly_fallback_is_read_only(tmp_path):
    source = _standard_mono(tmp_path / "missing", "Game")
    data = source / "Game_Data"
    (data / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": ["Missing.Game.dll"],
        "types": [16],
    }), encoding="utf-8")

    result = fingerprint_game(source)

    assert result.runtime == "unknown"
    assert result.data_dir == data.resolve()
    assert all("writeback" not in capability
               for capability in result.capabilities)


def test_fingerprint_invalid_cli_fallback_is_read_only(tmp_path):
    source = _standard_mono(tmp_path / "invalid-cli", "Game")
    (source / "Game_Data" / "Managed" / "Assembly-CSharp.dll").write_bytes(
        b"not a CLI image")

    result = fingerprint_game(source)

    assert result.runtime == "unknown"
    assert result.executable == (source / "Game.exe").resolve()
    assert all("writeback" not in capability
               for capability in result.capabilities)


def test_fingerprint_resource_limit_fallback_is_read_only(tmp_path, monkeypatch):
    source = _standard_mono(tmp_path / "bounded", "Game")
    monkeypatch.setattr(player_layout_module, "_MAX_DISCOVERY_DIRECTORIES", 0)

    result = fingerprint_game(source)

    assert result.runtime == "unknown"
    assert result.data_dir == (source / "Game_Data").resolve()
    assert all("writeback" not in capability
               for capability in result.capabilities)


def test_manifest_discovers_custom_standard_mono_assemblies(tmp_path):
    root = tmp_path / "custom"
    root.mkdir()
    _write_pe(root / "Custom.exe")
    data = root / "Custom_Data"
    (data / "globalgamemanagers").parent.mkdir(parents=True)
    (data / "globalgamemanagers").write_bytes(b"Unity fixture")
    for name in ("StgAssembly_1.dll", "StgAssembly_2.dll"):
        _write_pe(data / "Managed" / name, cli=True)
    (data / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": ["StgAssembly_1.dll", "StgAssembly_2.dll", "UnityEngine.dll"],
        "types": [16, 16, 1],
    }), encoding="utf-8")

    layout, = discover_player_candidates(root)

    assert layout.layout_kind == "standard"
    assert [path.name for path in layout.application_assemblies] == [
        "StgAssembly_1.dll", "StgAssembly_2.dll"]


def test_manifest_ignores_missing_render_pipeline_package_assemblies(tmp_path):
    source = _standard_mono(tmp_path / "render-pipeline", "Game")
    data = source / "Game_Data"
    (data / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": [
            "Assembly-CSharp.dll",
            "Unity.RenderPipelines.Core.ShaderLibrary.dll",
            "Unity.RenderPipelines.ShaderGraph.ShaderGraphLibrary.dll",
        ],
        "types": [16, 16, 16],
    }), encoding="utf-8")

    result = fingerprint_game(source)

    assert result.runtime == "mono"
    assert [path.name for path in result.application_assemblies] == [
        "Assembly-CSharp.dll"]


def test_manifest_type16_filters_framework_and_package_assemblies(tmp_path):
    root = tmp_path / "custom"
    root.mkdir()
    _write_pe(root / "Custom.exe")
    data = root / "Custom_Data"
    (data / "globalgamemanagers").parent.mkdir(parents=True)
    (data / "globalgamemanagers").write_bytes(b"Unity fixture")
    custom = [f"StgAssembly_{index}.dll" for index in range(1, 7)]
    framework = [
        "Unity.Postprocessing.Runtime.dll", "Unity.VisualScripting.Core.dll",
        "UnityEngine.UI.dll", "Unity.Timeline.dll", "Unity.TextMeshPro.dll",
        "System.Core.dll", "Microsoft.CSharp.dll", "netstandard.dll",
    ]
    for name in custom + framework:
        _write_pe(data / "Managed" / name, cli=True)
    (data / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": custom + framework,
        "types": [16] * (len(custom) + len(framework)),
    }), encoding="utf-8")

    assert [path.name for path in discover_application_assemblies(root, data)] == custom
    layout, = discover_player_candidates(root)
    assert [path.name for path in layout.application_assemblies] == custom


def test_manifest_framework_packages_alone_do_not_establish_mono(tmp_path):
    root = _flat_mono(tmp_path)
    assembly = root / "Managed" / "Assembly-CSharp.dll"
    assembly.unlink()
    packages = [
        "Unity.TextMeshPro.dll", "UnityEngine.UI.dll", "UnityEditor.Core.dll",
        "System.Core.dll", "Microsoft.CSharp.dll", "Mono.Security.dll",
        "mscorlib.dll", "netstandard.dll",
    ]
    for name in packages:
        _write_pe(root / "Managed" / name, cli=True)
    (root / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": packages, "types": [16] * len(packages),
    }), encoding="utf-8")

    assert discover_application_assemblies(root, root) == ()
    assert discover_player_candidates(root) == ()


def test_manifest_keeps_vendor_named_game_dlls_but_filters_known_packages(
        tmp_path):
    root = _flat_mono(tmp_path)
    (root / "Managed" / "Assembly-CSharp.dll").unlink()
    game_dlls = ["Unity.MyGame.dll", "Microsoft.MyStudio.Game.dll"]
    packages = [
        "Newtonsoft.Json.dll", "Unity.Postprocessing.Runtime.dll",
        "Unity.VisualScripting.Core.dll", "Unity.VisualScripting.Flow.dll",
        "Unity.VisualScripting.State.dll", "Unity.Timeline.dll",
        "Unity.TextMeshPro.dll", "Unity.InputSystem.dll",
        "Unity.Localization.dll", "DOTween.dll", "DOTween.Modules.dll",
        "DOTweenPro.dll",
    ]
    for name in game_dlls + packages:
        _write_pe(root / "Managed" / name, cli=True)
    (root / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": game_dlls + packages,
        "types": [16] * (len(game_dlls) + len(packages)),
    }), encoding="utf-8")

    assert [
        path.name for path in discover_application_assemblies(root, root)
    ] == ["Microsoft.MyStudio.Game.dll", "Unity.MyGame.dll"]


def test_manifest_fallback_matches_boo_assembly(tmp_path):
    # Boo 脚本语言编译程序集（Assembly-Boo.dll，老 Unity 脚本）此前不在
    # fallback 前缀列表，Boo 游戏整个不提取——与 UnityScript 同类遗漏。
    root = tmp_path / "boo"
    root.mkdir()
    _write_pe(root / "BooGame.exe")
    data = root / "BooGame_Data"
    (data / "globalgamemanagers").parent.mkdir(parents=True)
    (data / "globalgamemanagers").write_bytes(b"Unity fixture")
    for name in ("Assembly-CSharp.dll", "Assembly-Boo.dll",
                 "Assembly-UnityScript-firstpass.dll", "Boo.Lang.dll"):
        _write_pe(data / "Managed" / name, cli=True)

    found = [p.name for p in discover_application_assemblies(root, data)]

    assert found == ["Assembly-Boo.dll", "Assembly-CSharp.dll",
                     "Assembly-UnityScript-firstpass.dll"]
    assert "Boo.Lang.dll" not in found  # 语言运行时（非游戏文本）不提取


def test_real_spolous_manifest_selects_only_six_game_assemblies():
    configured = os.environ.get("HANHUA_CORPUS_DIR")
    if not configured:
        pytest.skip("set HANHUA_CORPUS_DIR for the real read-only corpus gate")
    corpus = Path(configured)
    matches = [path for path in corpus.iterdir()
               if path.is_dir() and path.name.casefold() == "spolous-ii"]
    assert len(matches) == 1

    layout, = discover_player_candidates(matches[0])

    assert [path.name for path in layout.application_assemblies] == [
        f"StgAssembly_{index}.dll" for index in range(1, 7)]


@pytest.mark.parametrize("payload", [
    "{broken",
    json.dumps({"names": ["Game.dll"], "types": []}),
    json.dumps({"names": ["Game.dll"], "types": [True]}),
    json.dumps({"names": ["Missing.dll"], "types": [16]}),
    json.dumps({"names": ["../Game.dll"], "types": [16]}),
])
def test_present_invalid_manifest_invalidates_mono_evidence(tmp_path, payload):
    root = _flat_mono(tmp_path)
    (root / "ScriptingAssemblies.json").write_text(payload, encoding="utf-8")

    assert discover_application_assemblies(root, root) == ()
    assert discover_player_candidates(root) == ()


@pytest.mark.parametrize("payload", [
    '{"names":[],"types":[],"nested":' + "[" * 5000 + "0" + "]" * 5000 + "}",
    '{"names":["Assembly-CSharp.dll"],"types":[' + "1" * 5000 + "]}",
])
def test_manifest_parser_resource_errors_are_safe_failures(tmp_path, payload):
    root = _flat_mono(tmp_path)
    (root / "ScriptingAssemblies.json").write_text(payload, encoding="utf-8")

    assert discover_application_assemblies(root, root) == ()
    assert discover_player_candidates(root) == ()


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, MemoryError])
def test_manifest_parser_does_not_swallow_process_level_errors(
        tmp_path, monkeypatch, error_type):
    root = _flat_mono(tmp_path)
    (root / "ScriptingAssemblies.json").write_text(
        '{"names":[],"types":[]}', encoding="utf-8")

    def fail_parse(_payload):
        raise error_type()

    monkeypatch.setattr(player_layout_module.json, "loads", fail_parse)
    with pytest.raises(error_type):
        discover_application_assemblies(root, root)


def test_present_non_file_manifest_does_not_enable_fallback(tmp_path):
    root = _flat_mono(tmp_path)
    (root / "ScriptingAssemblies.json").mkdir()

    assert discover_application_assemblies(root, root) == ()
    assert discover_player_candidates(root) == ()


@pytest.mark.parametrize("names", [
    ["Assembly-CSharp.dll", "Assembly-CSharp.dll"],
    ["Assembly-CSharp.dll", "assembly-csharp.DLL"],
])
def test_manifest_rejects_casefold_duplicate_names(tmp_path, names):
    root = _flat_mono(tmp_path)
    (root / "ScriptingAssemblies.json").write_text(json.dumps({
        "names": names, "types": [16, 16],
    }), encoding="utf-8")

    assert discover_application_assemblies(root, root) == ()
    assert discover_player_candidates(root) == ()


def test_pe_and_cli_validation_requires_mapped_complete_structures(tmp_path):
    native = tmp_path / "native.exe"
    managed = tmp_path / "managed.dll"
    invalid = tmp_path / "invalid.dll"
    unmapped = tmp_path / "unmapped.dll"
    truncated = tmp_path / "truncated.dll"
    _write_pe(native)
    _write_pe(managed, cli=True)
    invalid.write_bytes(b"MZ")
    _write_pe(unmapped, cli=True)
    blob = bytearray(unmapped.read_bytes())
    struct.pack_into("<II", blob, 0x98 + 112 + 14 * 8, 0x9000, 0x48)
    unmapped.write_bytes(blob)
    _write_pe(truncated, cli=True)
    truncated.write_bytes(truncated.read_bytes()[:0x220])

    assert is_pe_image(native)
    assert not is_pe_image(native, require_cli=True)
    assert is_pe_image(managed, require_cli=True)
    assert not is_pe_image(invalid)
    assert not is_pe_image(unmapped, require_cli=True)
    assert not is_pe_image(truncated, require_cli=True)


def test_flat_layout_requires_marker_and_non_installer_player_exe(tmp_path):
    no_marker = _flat_mono(tmp_path / "no-marker")
    (no_marker / "globalgamemanagers").unlink()
    installer = tmp_path / "installer"
    installer.mkdir()
    _write_pe(installer / "setup-installer.exe")
    (installer / "globalgamemanagers").write_bytes(b"Unity fixture")
    _write_pe(installer / "Managed" / "Assembly-CSharp.dll", cli=True)

    assert discover_player_candidates(no_marker) == ()
    assert discover_player_candidates(installer) == ()


def test_dual_backend_candidate_preserves_both_evidence_sets(tmp_path):
    root = _flat_mono(tmp_path)
    _write_pe(root / "GameAssembly.dll")
    metadata = root / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(struct.pack("<II", 0xFAB11BAF, 29))

    layout, = discover_player_candidates(root)
    assert [path.name for path in layout.application_assemblies] == [
        "Assembly-CSharp.dll"]
    assert layout.game_assembly == (root / "GameAssembly.dll").resolve()
    assert layout.metadata == metadata.resolve()


def test_unique_and_multiple_nested_players_have_stable_candidates(tmp_path):
    one = tmp_path / "one"
    _standard_mono(one / "wrapper" / "A", "A")
    candidate, = discover_player_candidates(one)
    assert candidate.player_root == (one / "wrapper" / "A").resolve()
    assert candidate.layout_kind == "nested_standard"

    two = tmp_path / "two"
    _standard_mono(two / "z-player", "Z")
    _standard_mono(two / "A-player", "A")
    assert [item.player_root.name for item in discover_player_candidates(two)] == [
        "A-player", "z-player"]


def test_two_standard_players_in_one_root_are_stable_candidates(tmp_path):
    _standard_mono(tmp_path, "B")
    _standard_mono(tmp_path, "A")

    layouts = discover_player_candidates(tmp_path)

    assert [layout.executable.name for layout in layouts] == ["A.exe", "B.exe"]
    assert [layout.data_dir.name for layout in layouts] == ["A_Data", "B_Data"]


def test_direct_standard_player_does_not_hide_nested_player(tmp_path):
    _standard_mono(tmp_path, "Direct")
    _standard_mono(tmp_path / "bonus" / "Nested", "Nested")

    layouts = discover_player_candidates(tmp_path)

    assert [layout.executable.name for layout in layouts] == [
        "Direct.exe", "Nested.exe"]
    assert [layout.layout_kind for layout in layouts] == [
        "standard", "nested_standard"]


def test_manifest_discovery_is_byte_bounded(tmp_path, monkeypatch):
    manifest_root = _flat_mono(tmp_path / "manifest")
    manifest = manifest_root / "ScriptingAssemblies.json"
    manifest.write_text(json.dumps({
        "names": ["Assembly-CSharp.dll"], "types": [16], "padding": "x" * 40,
    }), encoding="utf-8")
    monkeypatch.setattr(player_layout_module, "_MAX_MANIFEST_BYTES", 16)
    assert discover_application_assemblies(manifest_root, manifest_root) == ()


def test_single_directory_discovery_is_entry_bounded(tmp_path, monkeypatch):
    entry_root = _flat_mono(tmp_path / "entries")
    monkeypatch.setattr(player_layout_module, "_MAX_DIRECTORY_ENTRIES", 2)
    assert discover_player_candidates(entry_root) == ()


def test_recursive_discovery_is_directory_bounded(tmp_path, monkeypatch):
    nested_root = tmp_path / "nested"
    _standard_mono(nested_root / "wrapper" / "Game", "Game")
    monkeypatch.setattr(player_layout_module, "_MAX_DISCOVERY_DIRECTORIES", 1)
    assert discover_player_candidates(nested_root) == ()


def test_case_insensitive_duplicate_canonical_names_are_rejected(
        tmp_path, monkeypatch):
    root = _flat_mono(tmp_path)
    original_iterdir = type(root).iterdir

    def duplicate_managed(path):
        entries = list(original_iterdir(path))
        if path == root:
            entries.append(root / "managed")
        return iter(entries)

    monkeypatch.setattr(type(root), "iterdir", duplicate_managed)

    with pytest.raises(PlayerLayoutError, match="duplicate_canonical_entry"):
        discover_player_candidates(root)


def test_fingerprint_rejects_duplicate_canonical_source_entries(
        tmp_path, monkeypatch):
    root = _standard_mono(tmp_path / "source", "Game")
    original_iterdir = type(root).iterdir

    def duplicate_direct_entry(path):
        entries = list(original_iterdir(path))
        if path == root:
            entries.extend((root / "NOTES.bin", root / "notes.BIN"))
        return iter(entries)

    monkeypatch.setattr(type(root), "iterdir", duplicate_direct_entry)

    with pytest.raises(FingerprintError, match="duplicate|大小写|canonical"):
        fingerprint_game(root)


def test_fingerprint_rejects_duplicate_canonical_managed_entries(
        tmp_path, monkeypatch):
    root = _standard_mono(tmp_path / "source", "Game")
    managed = root / "Game_Data" / "Managed"
    original_iterdir = type(root).iterdir

    def duplicate_backend_entry(path):
        entries = list(original_iterdir(path))
        if path == managed:
            entries.append(managed / "assembly-csharp.DLL")
        return iter(entries)

    monkeypatch.setattr(type(root), "iterdir", duplicate_backend_entry)

    with pytest.raises(FingerprintError, match="duplicate|大小写|canonical"):
        fingerprint_game(root)


def test_fake_internal_reparse_boundary_is_never_traversed(tmp_path, monkeypatch):
    root = tmp_path / "source"
    nested = _standard_mono(root / "linked" / "Game", "Game")
    boundary = root / "linked"
    real = player_layout_module._is_reparse_point
    monkeypatch.setattr(
        player_layout_module, "_is_reparse_point",
        lambda path: Path(path) == boundary or real(Path(path)))

    assert discover_player_candidates(root) == ()
    assert nested.exists()


def test_real_internal_symlink_is_never_traversed_when_available(tmp_path):
    source = tmp_path / "source"
    outside = _standard_mono(tmp_path / "outside" / "Game", "Game")
    source.mkdir()
    link = source / "linked"
    try:
        link.symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")

    assert discover_player_candidates(source) == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_real_internal_windows_junction_is_never_traversed(tmp_path):
    source = tmp_path / "source"
    outside = _standard_mono(tmp_path / "outside" / "Game", "Game")
    source.mkdir()
    link = source / "linked"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside.parent)],
        capture_output=True, check=False)  # 中文 GBK 输出勿按 UTF-8 解码
    if created.returncode != 0:
        pytest.skip("junction creation unavailable")
    try:
        assert link.is_junction()
        assert discover_player_candidates(source) == ()
    finally:
        os.rmdir(link)


def test_fingerprint_detects_mono_pair_and_runtime_evidence(tmp_path):
    result = fingerprint_game(_mono_game(tmp_path))

    assert result.runtime == "mono"
    assert result.executable.name == "Mono Game.exe"
    assert result.data_dir.name == "Mono Game_Data"
    assert result.unity_version == "2021.3.5f1"
    assert result.metadata is None
    assert "tmp" in result.evidence
    assert {"asset_bundle", "serialized_file", "ngui"} <= set(result.evidence)
    assert {
        "native_asset_extract", "native_asset_writeback", "runtime_font_fallback",
    } <= set(result.capabilities)


def test_fingerprint_detects_il2cpp_metadata_and_bitmap_font(tmp_path):
    result = fingerprint_game(_il2cpp_game(tmp_path, bitmap=True))

    assert result.runtime == "il2cpp"
    assert result.metadata_version == 29
    assert result.game_assembly.name == "GameAssembly.dll"
    assert "bitmap_font" in result.evidence
    assert "native_il2cpp_literal_extract" in result.capabilities
    assert "native_il2cpp_literal_writeback" in result.capabilities
    assert "bitmap_artifact_generation" in result.capabilities
    assert "bitmap_injection_unverified" in result.capabilities
    assert "bitmap_injection_ready" not in result.capabilities


def test_planner_routes_v29_cross_check_and_blocks_unknown_writeback(tmp_path):
    v29 = fingerprint_game(_il2cpp_game(tmp_path / "v29", version=29))
    supported = plan_backends(v29, {"il2cpp_dumper": "verified", "bmfont": "verified"})
    by_id = {step.step_id: step for step in supported}
    assert by_id["tool_analysis"].backend == "il2cpp_dumper"
    assert by_id["tool_analysis"].status == "pending"
    assert by_id["writeback"].status == "pending"
    assert plan_is_unblocked(supported)
    assert not plan_is_completable(supported)
    completed = tuple(
        replace(step, status="succeeded") if step.required else step
        for step in supported
    )
    assert plan_is_completable(completed)
    required_skipped = tuple(
        replace(step, status="skipped") if step.step_id == "text_scan" else step
        for step in completed
    )
    assert not plan_is_completable(required_skipped)

    v30 = fingerprint_game(_il2cpp_game(tmp_path / "v30", version=30))
    unsupported = plan_backends(v30, {"il2cpp_dumper": "verified", "bmfont": "verified"})
    unsupported_by_id = {step.step_id: step for step in unsupported}
    assert unsupported_by_id["tool_analysis"].status == "pending"
    assert unsupported_by_id["writeback"].status == "blocked"
    assert unsupported_by_id["writeback"].required is True
    assert not plan_is_unblocked(unsupported)
    assert not plan_is_completable(unsupported)


def test_planner_exposes_optional_unsupported_il2cpp_font_capability(tmp_path):
    fingerprint = fingerprint_game(_il2cpp_game(tmp_path, version=29))
    reason = (
        "IL2CPP x64 字体 provider 未提供经验证的 "
        "BepInEx 6/Il2CppInterop 载荷"
    )
    capability = FontProviderCapability(
        "bepinex6_il2cpp_x64", "il2cpp", "x64", False, False,
        reason=reason, static_writeback_allowed=True,
    )

    route = plan_backends(
        fingerprint,
        {"il2cpp_dumper": "verified", "bmfont": "verified"},
        font_capability=capability,
    )
    font = {step.step_id: step for step in route}["font"]

    assert font.backend == "static_replace"
    assert font.status == "pending"
    assert font.required is True
    assert font.confidence == "high"
    assert "静态字体替换" in font.reason
    assert plan_is_unblocked(route) is True


def test_planner_keeps_unknown_mono_font_pending_for_installer(tmp_path):
    fingerprint = fingerprint_game(_mono_game(tmp_path))
    capability = FontProviderCapability(
        "unsupported_mono_unknown", "mono", "unknown", False, False,
        reason="无法确定 Mono player 架构",
    )

    route = plan_backends(
        fingerprint,
        {"il2cpp_dumper": "verified", "bmfont": "verified"},
        font_capability=capability,
    )
    font = {step.step_id: step for step in route}["font"]

    assert font.status == "pending"
    assert font.required is True
    assert plan_is_unblocked(route) is True


def test_planner_uses_bmfont_only_with_bitmap_evidence(tmp_path):
    bitmap = fingerprint_game(_il2cpp_game(tmp_path / "bitmap", bitmap=True))
    bitmap_plan = plan_backends(bitmap, {"il2cpp_dumper": "verified", "bmfont": "verified"})
    bitmap_by_id = {step.step_id: step for step in bitmap_plan}
    assert bitmap_by_id["font_artifact"].backend == "bmfont"
    assert bitmap_by_id["font_injection"].status == "blocked"
    assert not plan_is_completable(bitmap_plan)

    mono = fingerprint_game(_mono_game(tmp_path / "mono"))
    mono_plan = plan_backends(mono, {"il2cpp_dumper": "verified", "bmfont": "verified"})
    assert {step.step_id: step for step in mono_plan}["font"].backend == "bepinex_runtime"


def test_planner_bitmap_providers_promote_injection_to_pending(tmp_path):
    """Phase 5：发现可注入 .fnt 资产 + bmfont 工具 verified →
    font_injection pending（写回阶段自动审计缺字并注入）。"""
    bitmap = fingerprint_game(_il2cpp_game(tmp_path / "bitmap", bitmap=True))
    route = plan_backends(
        bitmap, {"il2cpp_dumper": "verified", "bmfont": "verified"},
        bitmap_provider_count=2)
    by_id = {step.step_id: step for step in route}
    injection = by_id["font_injection"]
    assert injection.backend == "bmfont_inject"
    assert injection.status == "pending"
    assert injection.confidence == "high"
    assert "2 个 BMFont 资产" in injection.reason
    assert plan_is_unblocked(route) is True


def test_planner_bitmap_providers_without_tool_blocks_injection(tmp_path):
    """Phase 5：发现资产但 bmfont 工具缺失 → font_injection blocked
    并给出工具状态原因（不假装可注入）。"""
    bitmap = fingerprint_game(_il2cpp_game(tmp_path / "bitmap", bitmap=True))
    route = plan_backends(
        bitmap, {"il2cpp_dumper": "verified", "bmfont": "missing"},
        bitmap_provider_count=1)
    injection = {step.step_id: step for step in route}["font_injection"]
    assert injection.status == "blocked"
    assert "工具状态" in injection.reason


def test_fingerprint_keeps_bundle_evidence_after_later_plain_extensionless_file(
        tmp_path):
    game = tmp_path / "Evidence Game"
    data = game / "Evidence Game_Data"
    streaming = data / "StreamingAssets"
    streaming.mkdir(parents=True)
    _write_pe(game / "Evidence Game.exe")
    _write_pe(data / "Managed" / "Assembly-CSharp.dll", cli=True)
    bundle = streaming / "00_bundle"
    plain = streaming / "99_plain"
    bundle.write_bytes(b"UnityFS\0fixture")
    plain.write_bytes(b"\x00\x01ordinary data")

    result = fingerprint_game(game)

    assert "asset_bundle" in result.evidence


def test_fingerprint_rejects_lexical_game_reparse_before_resolving(
        tmp_path, monkeypatch):
    game = _mono_game(tmp_path)
    monkeypatch.setattr(
        fingerprint_module, "_is_reparse_point", lambda path: path == game)

    with pytest.raises(FingerprintError, match="reparse|重解析"):
        fingerprint_game(game)


def test_planner_marks_unverified_stack_blocked_by_publish_gate(tmp_path):
    """Phase 4：未知渲染栈 → 静态替换降为 medium，reason 声明发布门
    BLOCKED 不可绕过（候选确认不放行 §8.3）。"""
    from dataclasses import replace

    fingerprint = fingerprint_game(_il2cpp_game(tmp_path, version=29))
    fingerprint = replace(fingerprint, font_stacks=("unverified_font_stack",))
    capability = FontProviderCapability(
        "bepinex6_il2cpp_x64", "il2cpp", "x64", False, False,
        reason="IL2CPP x64 无动态 provider",
        static_writeback_allowed=True,
    )
    route = plan_backends(
        fingerprint, {"il2cpp_dumper": "verified", "bmfont": "verified"},
        font_capability=capability)
    font = {step.step_id: step for step in route}["font"]

    assert font.backend == "static_replace"
    assert font.confidence == "high"
    assert "未知渲染栈" in font.reason
    assert "不可绕过" in font.reason
    assert plan_is_unblocked(route) is True


def test_planner_runtime_fallback_hints_dynamic_attestation(tmp_path):
    """Phase 4：Mono runtime_font_fallback 栈 → reason 注明动态消费者
    需运行时 attestation（发布门消费）。"""
    from dataclasses import replace

    fingerprint = fingerprint_game(_mono_game(tmp_path))
    fingerprint = replace(
        fingerprint, font_stacks=("runtime_font_fallback",))
    route = plan_backends(
        fingerprint, {"il2cpp_dumper": "verified", "bmfont": "verified"})
    font = {step.step_id: step for step in route}["font"]

    assert font.backend == "bepinex_runtime"
    assert "attestation" in font.reason
