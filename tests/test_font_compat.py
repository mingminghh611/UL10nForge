"""Regression tests for anti-tofu (missing glyph) font compatibility & UL10nForge.FontGuard."""
from __future__ import annotations

import gzip
import io
from pathlib import Path
import shutil
import struct
import zipfile

from ul10n.core.font_compat import (
    bundle_has_legacy_glyphs,
    ensure_dual_schema_bundle,
    export_legacy_sdf_pack,
    inspect_game_font_profile,
    resolve_available_ugui_font,
    sanitize_for_legacy_static_atlas,
)
from ul10n.core.hook_deployer import (
    _should_prefer_unity6_os_fallback,
    build_standalone_patch_zip,
    deploy_hook,
    refresh_hook_config,
    uninstall_hook,
)
from ul10n.core.models import Entry, GameInfo, STATUS_TRANSLATED
from ul10n.core.xuat_codec import render_xuat_dict_text

APP_ROOT = Path(__file__).resolve().parents[1]


def test_game_font_profile_detection_mono_and_il2cpp(tmp_path: Path):
    # 1. Legacy TMP 1.3.0 Mono (like Slay Bells)
    g1 = tmp_path / "LegacyTmpGame"
    m1 = g1 / "LegacyTmpGame_Data" / "Managed"
    m1.mkdir(parents=True)
    (m1 / "Unity.TextMeshPro.dll").write_bytes(b"TMP_FontAsset\x00m_glyphInfoList\x00m_fontInfo\x00fallbackFontAssets\x00")
    p1 = inspect_game_font_profile(g1, "mono", "2019.3.0a2")
    assert p1.has_tmp is True
    assert p1.tmp_schema == "legacy_1x"
    assert p1.tmp_assembly == "Unity.TextMeshPro.dll"
    assert p1.supports_dynamic_tmp is False

    # 2. Custom Asset Store TMP DLL (Unity 2017/2018.1)
    g2 = tmp_path / "OldTmpGame"
    m2 = g2 / "OldTmpGame_Data" / "Managed"
    m2.mkdir(parents=True)
    (m2 / "TextMeshPro-2017.3-Runtime.dll").write_bytes(b"TMP_FontAsset\x00m_glyphInfoList\x00m_fontInfo\x00")
    p2 = inspect_game_font_profile(g2, "mono", "2018.1.9f2")
    assert p2.tmp_schema == "legacy_1x"
    assert p2.tmp_assembly == "TextMeshPro-2017.3-Runtime.dll"

    # 3. Modern Unity 6000.0 IL2CPP with TMP 4.0 CreateFontAsset
    g3 = tmp_path / "Unity6Game"
    meta3 = g3 / "Unity6Game_Data" / "il2cpp_data" / "Metadata"
    meta3.mkdir(parents=True)
    (meta3 / "global-metadata.dat").write_bytes(
        b"TMP_FontAsset\x00m_CharacterTable\x00m_GlyphTable\x00m_AtlasPopulationMode\x00CreateFontAsset\x00InternalDynamicOS\x00"
    )
    p3 = inspect_game_font_profile(g3, "il2cpp", "6000.0.23f1")
    assert p3.has_tmp is True
    assert p3.tmp_schema == "modern_110"
    assert p3.supports_dynamic_tmp is True
    assert p3.supports_os_create_font is True
    assert _should_prefer_unity6_os_fallback(p3) is True

    # Unity 6000.3+ uses shipped u6000 bundle rather than skipping it
    p4 = inspect_game_font_profile(g3, "il2cpp", "6000.3.0f1")
    assert _should_prefer_unity6_os_fallback(p4) is False


def test_resolve_ugui_font_and_legacy_symbol_sanitization():
    # Invalid font name gracefully falls back to an installed Windows CJK font
    resolved = resolve_available_ugui_font("DefinitelyMissingFont_XYZ_999")
    assert resolved != "DefinitelyMissingFont_XYZ_999"
    assert len(resolved) > 0

    # Legacy symbol sanitization maps static-atlas-missing symbols cleanly
    raw = "「勇者」获得 20°C 火焰剑 ×2 → 攻击力提升 ★①"
    clean = sanitize_for_legacy_static_atlas(raw)
    assert "「" not in clean and "」" not in clean
    assert "×" not in clean and "→" not in clean and "★" not in clean and "①" not in clean
    assert "“勇者”" in clean and "20℃" in clean and "x2 ->" in clean and "*(1)" in clean

    # render_xuat_dict_text only sanitizes when sanitize_legacy_symbols=True
    e = Entry(1, "Hero sword", raw, status=STATUS_TRANSLATED)
    modern_txt, _, _, _ = render_xuat_dict_text([e], sanitize_legacy_symbols=False)
    legacy_txt, _, _, _ = render_xuat_dict_text([e], sanitize_legacy_symbols=True)
    assert "「勇者」" in modern_txt
    assert "“勇者”" in legacy_txt and "「" not in legacy_txt


def test_dual_schema_bundle_upgrade_and_legacy_sdf_export(tmp_path: Path):
    src = APP_ROOT / "fonts" / "TMP_Font_AssetBundles" / "notoserif_sdf_u2019"
    if not src.is_file():
        return

    # 1. Local shipped u2019 bundle already has dual-schema legacy fields
    assert bundle_has_legacy_glyphs(src) is True

    # 2. Simulate an older v1.0.0 single-schema bundle (strip m_glyphInfoList & atlas) and auto-upgrade
    import UnityPy

    old_bundle = tmp_path / "old_notoserif_u2019"
    env = UnityPy.load(src.read_bytes())
    for obj in env.objects:
        if obj.type.name == "MonoBehaviour":
            tr = obj.read_typetree()
            if "m_glyphInfoList" in tr:
                tr["m_glyphInfoList"] = []
                tr["atlas"] = {"m_FileID": 0, "m_PathID": 0}
                obj.save_typetree(tr)
    env.save(pack="lz4", out_path=str(tmp_path))
    saved = [p for p in tmp_path.iterdir() if p.is_file() and p.name != old_bundle.name][0]
    saved.replace(old_bundle)
    assert bundle_has_legacy_glyphs(old_bundle) is False

    upgraded = tmp_path / "upgraded_u2019"
    patched = ensure_dual_schema_bundle(old_bundle, upgraded, target_assembly="TextMeshPro-2017.3-Runtime.dll")
    assert patched is True
    assert bundle_has_legacy_glyphs(upgraded) is True

    # Verify both modern & legacy fields + top-down Y + patched MonoScript assembly name
    env2 = UnityPy.load(upgraded.read_bytes())
    found_mb = False
    found_asm = ""
    for obj in env2.objects:
        if obj.type.name == "MonoBehaviour":
            tr = obj.read_typetree()
            if "m_CharacterTable" in tr:
                found_mb = True
                assert tr["m_Version"] == "1.1.0"
                assert len(tr["m_CharacterTable"]) == 8361
                assert len(tr["m_glyphInfoList"]) == 8361
                assert tr["atlas"]["m_PathID"] != 0
                assert tr["m_fontInfo"]["AtlasWidth"] == 4096.0
                # Spot-check top-down Y conversion: y = 4096 - m_Y - m_Height
                g_map = {int(g["m_Index"]): g for g in tr["m_GlyphTable"]}
                c_sample = next(c for c in tr["m_CharacterTable"] if c["m_Unicode"] == ord("的"))
                g_sample = g_map[int(c_sample["m_GlyphIndex"])]
                leg_sample = next(lg for lg in tr["m_glyphInfoList"] if lg["id"] == ord("的"))
                expected_y = 4096.0 - float(g_sample["m_GlyphRect"]["m_Y"]) - float(g_sample["m_GlyphRect"]["m_Height"])
                assert abs(leg_sample["y"] - expected_y) < 0.01
        elif obj.type.name == "MonoScript":
            tr = obj.read_typetree()
            if tr.get("m_ClassName") == "TMP_FontAsset":
                found_asm = tr.get("m_AssemblyName", "")
    assert found_mb is True
    assert found_asm == "TextMeshPro-2017.3-Runtime.dll"

    # 3. Verify legacy SDF gzip pack export
    pack = tmp_path / "ul10n_legacy_sdf.dat"
    assert export_legacy_sdf_pack(upgraded, pack) is True
    assert pack.is_file() and pack.stat().st_size > 100_000
    with gzip.open(pack, "rb") as gz:
        assert gz.read(8) == b"UL10NSDF"
        ver, aw, ah, gscale = struct.unpack("<Iiif", gz.read(16))
        assert (ver, aw, ah) == (1, 4096, 4096)
        assert gscale > 0


def test_fontguard_deploy_refresh_patch_and_clean_uninstall(tmp_path: Path):
    # Test Unity 2018.2 Mono game (previously had empty FallbackFontTextMeshPro & zero TMP support!)
    game_dir = tmp_path / "Unity2018TmpGame"
    managed = game_dir / "Unity2018TmpGame_Data" / "Managed"
    managed.mkdir(parents=True)
    (managed / "Unity.TextMeshPro.dll").write_bytes(b"TMP_FontAsset\x00m_glyphInfoList\x00m_fontInfo\x00")
    info = GameInfo(
        game_dir=str(game_dir),
        name="Unity2018TmpGame",
        executable=str(game_dir / "Unity2018TmpGame.exe"),
        data_dir=str(game_dir / "Unity2018TmpGame_Data"),
        runtime_type="mono",
        arch="x64",
        unity_version="2018.2.18f1",
    )

    res = deploy_hook(info, app_root=APP_ROOT, project_id="p_u2018", server_port=18765)
    fg_dll = game_dir / "BepInEx" / "plugins" / "UL10nForge.FontGuard" / "UL10nForge.FontGuard.Mono.dll"
    fg_ini = game_dir / "BepInEx" / "config" / "ul10n_fontguard.ini"
    legacy_dat = game_dir / "BepInEx" / "Translation" / "zh" / "Fonts" / "ul10n_legacy_sdf.dat"
    assert fg_dll.is_file()
    assert fg_ini.is_file()
    assert legacy_dat.is_file()
    ini_txt = fg_ini.read_text(encoding="utf-8")
    assert "LegacySdf=BepInEx\\Translation\\zh\\Fonts\\ul10n_legacy_sdf.dat" in ini_txt
    assert "EnableLegacySdf=True" in ini_txt

    # Redeploy does NOT misidentify UL10nForge.FontGuard as a third-party player mod
    res2 = deploy_hook(info, app_root=APP_ROOT, project_id="p_u2018", server_port=18765)
    assert res2.manifest["preexisting_loader"] is False

    # Refresh keeps FontGuard + updates TTF when switching font
    refresh_hook_config(info, app_root=APP_ROOT, project_id="p_u2018", server_port=18765, font_id="lxgwwenkai")
    ini_txt2 = fg_ini.read_text(encoding="utf-8")
    assert "LXGWWenKai-Regular.ttf" in ini_txt2

    # Standalone offline patch zip also includes FontGuard + legacy SDF pack
    zbytes = build_standalone_patch_zip(info, app_root=APP_ROOT, curated_dict_text="Play=开始\n")
    with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zf:
        names = set(zf.namelist())
        assert "BepInEx/plugins/UL10nForge.FontGuard/UL10nForge.FontGuard.Mono.dll" in names
        assert "BepInEx/config/ul10n_fontguard.ini" in names
        assert "BepInEx/Translation/zh/Fonts/ul10n_legacy_sdf.dat" in names

    # Uninstall cleanly removes ALL FontGuard & hook files leaving original game dir spotless
    uninstall_hook(game_dir, res2.manifest)
    assert not fg_dll.exists()
    assert not fg_ini.exists()
    assert not (game_dir / "BepInEx").exists()
