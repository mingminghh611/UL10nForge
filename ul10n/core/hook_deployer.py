"""One-click safe deployment and clean uninstallation of BepInEx / MelonLoader + XUAT + Fonts.

Never modifies any original game binary or asset file. Tracks every deployed file
in a manifest so uninstall restores the game directory to 100% original state.
"""
from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ul10n.core.font_compat import (
    GameFontProfile,
    ensure_dual_schema_bundle,
    export_legacy_sdf_pack,
    font_stamp_signature,
    inspect_game_font_profile,
    resolve_available_ugui_font,
)
from ul10n.core.fonts import DEFAULT_FONT_ID, resolve_bundle_file, resolve_sdf_source_bundle
from ul10n.core.models import GameInfo


@dataclass(frozen=True)
class DeployResult:
    loader: str
    runtime_type: str
    arch: str
    config_path: str
    translation_dir: str
    font_bundle: str
    files_written: int
    manifest: dict[str, Any]


def _find_kit(kits_dir: Path, prefix: str) -> Path:
    matches = sorted(kits_dir.glob(f"{prefix}*.zip"))
    if not matches:
        raise FileNotFoundError(f"缺少 Hook 套件压缩包：{prefix}*.zip（位于 {kits_dir}）")
    return matches[-1]


def _select_kit_zips(kits_dir: Path, runtime_type: str, arch: str, loader: str) -> tuple[Path, Path]:
    """Return `(loader_zip, xuat_zip)` matching runtime, arch, and loader choice."""
    is_il2cpp = runtime_type == "il2cpp"
    arch_tag = "x86" if arch == "x86" else "x64"

    if loader == "melonloader":
        if arch_tag != "x64":
            raise ValueError("MelonLoader 备用方案当前仅内置 64 位版本，请改用 BepInEx。")
        if not is_il2cpp:
            raise ValueError("Mono 游戏请使用 BepInEx 5（兼容性最佳）。")
        return (
            _find_kit(kits_dir, "MelonLoader.x64"),
            _find_kit(kits_dir, "XUnity.AutoTranslator-MelonMod-IL2CPP"),
        )

    # Default: BepInEx 5 (Mono) or BepInEx 6 Bleeding Edge (IL2CPP)
    if is_il2cpp:
        return (
            _find_kit(kits_dir, f"BepInEx-Unity.IL2CPP-win-{arch_tag}"),
            _find_kit(kits_dir, "XUnity.AutoTranslator-BepInEx-IL2CPP"),
        )
    return (
        _find_kit(kits_dir, f"BepInEx_win_{arch_tag}_5"),
        _find_kit(kits_dir, "XUnity.AutoTranslator-BepInEx-5"),
    )


def _safe_extract_zip(
    zip_path: Path, dest_root: Path, *, skip_existing: bool = False
) -> tuple[list[str], list[str]]:
    """Safely extract zip into dest_root (zip-slip protected).

    Returns `(written_files, newly_created_files)` so uninstall ONLY deletes files
    created by UL10nForge and never clobbers pre-existing player mods/loaders.
    """
    root = dest_root.resolve()
    written: list[str] = []
    created: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename.replace("\\", "/").lstrip("/")
            if not rel or ".." in Path(rel).parts:
                raise ValueError(f"压缩包包含不安全路径：{info.filename}")
            target = (root / rel).resolve()
            if not str(target).startswith(str(root)):
                raise ValueError(f"压缩包路径逃逸：{info.filename}")
            existed = target.exists()
            if skip_existing and existed:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(rel)
            if not existed:
                created.append(rel)
    return written, created


def _normalize_from_lang(source_lang: str | None) -> str:
    """Normalize source language for XUAT `FromLanguage=` (supports auto/en/ja/ko/ru/fr/de/es)."""
    s = (source_lang or "auto").strip().lower()
    if s in ("ja", "jp", "ja-jp"):
        return "ja"
    if s in ("ko", "kr", "ko-kr"):
        return "ko"
    if s in ("ru", "ru-ru"):
        return "ru"
    if s in ("en", "en-us", "en-gb"):
        return "en"
    if s in ("fr", "de", "es", "pt", "it"):
        return s
    return "auto"


def _build_xuat_config(
    *,
    endpoint_url: str,
    fallback_tmp_font_rel: str,
    override_font_name: str = "Microsoft YaHei",
    target_lang: str = "zh",
    source_lang: str = "auto",
    offline_only: bool = False,
    is_il2cpp: bool = False,
) -> str:
    """Build AutoTranslatorConfig.ini tuned for maximum stability on Mono and IL2CPP."""
    lang = "zh" if target_lang.lower().startswith("zh") else target_lang
    from_lang = _normalize_from_lang(source_lang)
    endpoint_line = "Endpoint=" if offline_only else "Endpoint=CustomTranslate"
    custom_block = (
        ""
        if offline_only
        else (
            "\n[Custom]\n"
            f"Url={endpoint_url}\n"
            "EnableShortDelay=True\n"
            "DisableSpamChecks=True\n"
        )
    )
    # On stripped IL2CPP games, probing stripped UI modules (TextMesh/UIElements/NGUI/FairyGUI)
    # triggers Il2CppInterop resolution errors; keep only the rock-solid UGUI + TextMeshPro core.
    extra_fw = "False" if is_il2cpp else "True"
    return (
        "[Service]\n"
        f"{endpoint_line}\n"
        "FallbackEndpoint=\n"
        "\n"
        "[General]\n"
        f"Language={lang}\n"
        f"FromLanguage={from_lang}\n"
        "\n"
        "[Files]\n"
        "Directory=Translation\\{Lang}\\Text\n"
        "OutputFile=Translation\\{Lang}\\Text\\_AutoGeneratedTranslations.txt\n"
        "\n"
        "[TextFrameworks]\n"
        "EnableUGUI=True\n"
        f"EnableUIElements={extra_fw}\n"
        f"EnableNGUI={extra_fw}\n"
        "EnableTextMeshPro=True\n"
        f"EnableTextMesh={extra_fw}\n"
        f"EnableFairyGUI={extra_fw}\n"
        "EnableIMGUI=False\n"
        "\n"
        "[Behaviour]\n"
        "MaxCharactersPerTranslation=500\n"
        "IgnoreWhitespaceInDialogue=True\n"
        "IgnoreWhitespaceInNGUI=True\n"
        "MinDialogueChars=20\n"
        "ForceSplitTextAfterCharacters=0\n"
        "CopyToClipboard=False\n"
        "EnableUIResizing=True\n"
        "EnableBatching=True\n"
        "UseStaticTranslations=True\n"
        f"OverrideFont={override_font_name}\n"
        "OverrideFontTextMeshPro=\n"
        f"FallbackFontTextMeshPro={fallback_tmp_font_rel}\n"
        "ResizeUILineSpacingScale=0.85\n"
        "ForceUIResizing=False\n"
        "HandleRichText=True\n"
        "EnableTextAssetRedirector=False\n"
        "EnableSafeMode=True\n"
        "EnableSilentMode=True\n"
        "EnableTranslationScoping=False\n"
        "DisableTextMeshProScrollInEffects=True\n"
        "TemplateAllNumberAway=True\n"
        "ReloadTranslationsOnFileChange=True\n"
        "\n"
        "[Texture]\n"
        "EnableTextureTranslation=False\n"
        "EnableSpriteRendererHooking=False\n"
        "EnableSpriteHooking=False\n"
        "\n"
        "[ResourceRedirector]\n"
        "EnableTextAssetRedirector=False\n"
        "CacheMetadataForAllFiles=False\n"
        f"{custom_block}"
    )


def _safe_bepinex_cfg_text() -> str:
    """Minimal safe BepInEx.cfg avoiding console clutter and Unity-log hook conflicts."""
    return (
        "[Logging]\n"
        "UnityLogListening = false\n"
        "\n"
        "[Logging.Console]\n"
        "Enabled = false\n"
        "\n"
        "[Logging.Disk]\n"
        "Enabled = true\n"
        "LogLevels = Fatal, Error, Warning, Message, Info\n"
    )


def _resolve_fontguard_dll(app_root: Path, loader: str, runtime_type: str) -> tuple[Path | None, str]:
    """Return `(src_dll_path, dest_relative_posix_path)` for the matching UL10nForge.FontGuard DLL."""
    fg_dir = app_root / "ul10n" / "resources" / "font_guard"
    if loader == "melonloader":
        src = fg_dir / "UL10nForge.FontGuard.Melon.dll"
        return (src if src.is_file() else None), "Mods/UL10nForge.FontGuard.Melon.dll"
    if runtime_type == "il2cpp":
        src = fg_dir / "UL10nForge.FontGuard.IL2CPP.dll"
        return (
            (src if src.is_file() else None),
            "BepInEx/plugins/UL10nForge.FontGuard/UL10nForge.FontGuard.IL2CPP.dll",
        )
    src = fg_dir / "UL10nForge.FontGuard.Mono.dll"
    return (
        (src if src.is_file() else None),
        "BepInEx/plugins/UL10nForge.FontGuard/UL10nForge.FontGuard.Mono.dll",
    )


def _fontguard_ini_rel(loader: str) -> str:
    if loader == "melonloader":
        return "AutoTranslator/ul10n_fontguard.ini"
    return "BepInEx/config/ul10n_fontguard.ini"


def _build_fontguard_ini(
    *,
    font_ttf_rel: str,
    os_font_name: str,
    legacy_sdf_rel: str,
    is_il2cpp: bool = False,
) -> str:
    return (
        "[FontGuard]\n"
        f"FontTtf={font_ttf_rel}\n"
        f"OsFont={os_font_name}\n"
        f"LegacySdf={legacy_sdf_rel}\n"
        "EnableDynamicTmp=True\n"
        "EnableLegacySdf=True\n"
        f"EnableUiToolkit={'False' if is_il2cpp else 'True'}\n"
        "EnableLegacyTextFix=True\n"
    )


def _should_prefer_unity6_os_fallback(profile: GameFontProfile) -> bool:
    """Unity 6000.0–6000.2 lacks a 6000.0 bundle (u6000 is 6000.3); prefer native TMP 3.2+ OS/TTF dynamic fallback."""
    ver = profile.unity_tuple
    if ver is None:
        return False
    return (6000, 0, 0) <= ver < (6000, 3, 0) and profile.supports_os_create_font


def _sync_fonts_and_guard(
    game_dir: Path,
    game_info: GameInfo,
    *,
    app_root: Path,
    chosen_loader: str,
    runtime_type: str,
    font_id: str,
    paths: dict[str, Path],
) -> tuple[str, str, str, list[str], list[str]]:
    """Deploy/upgrade fonts, legacy SDF pack, and UL10nForge.FontGuard into `game_dir`.

    Returns `(fallback_tmp_rel, override_ugui_font, bundle_name, written_files, created_files)`.
    """
    written: list[str] = []
    created: list[str] = []

    profile = inspect_game_font_profile(game_dir, runtime_type, game_info.unity_version)
    bundle_src, font_opt = resolve_bundle_file(app_root, game_info.unity_version, font_id)
    override_ugui_font = resolve_available_ugui_font(font_opt.ugui_font)

    paths["font_dir"].mkdir(parents=True, exist_ok=True)
    stamp_path = paths["font_dir"] / ".ul10n_font_stamp"
    prev_stamp = ""
    if stamp_path.is_file():
        try:
            prev_stamp = stamp_path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            prev_stamp = ""

    bundle_name = ""
    fallback_tmp_rel = ""
    use_u6_os = _should_prefer_unity6_os_fallback(profile)
    src_mtime = bundle_src.stat().st_mtime if (bundle_src is not None and bundle_src.is_file()) else 0.0
    cur_stamp = font_stamp_signature(
        font_id=font_opt.id,
        bundle_name=bundle_src.name if (bundle_src is not None and not use_u6_os) else "",
        tmp_schema=profile.tmp_schema,
        tmp_assembly=profile.tmp_assembly,
        src_mtime=src_mtime,
    )

    if use_u6_os:
        # Native TMP 4.0 / Unity 6 dynamic OS fallback for XUAT; FontGuard prepends chosen .ttf at index 0
        fallback_tmp_rel = override_ugui_font
    elif bundle_src is not None and bundle_src.is_file():
        bundle_name = bundle_src.name
        bundle_dst = paths["font_dir"] / bundle_name
        existed = bundle_dst.exists()
        if (not existed) or (prev_stamp != cur_stamp):
            ensure_dual_schema_bundle(bundle_src, bundle_dst, target_assembly=profile.tmp_assembly)
        rel = bundle_dst.relative_to(game_dir).as_posix()
        written.append(rel)
        if not existed:
            created.append(rel)
        fallback_tmp_rel = rel.replace("/", "\\")

    # Always ensure user's chosen .ttf is present in game's Fonts dir (used by FontGuard dynamic TMP!)
    ttf_rel = ""
    ttf_src = font_opt.preview_ttf_path
    if ttf_src is not None and ttf_src.is_file():
        ttf_dst = paths["font_dir"] / ttf_src.name
        existed = ttf_dst.exists()
        if (not existed) or (ttf_dst.stat().st_size != ttf_src.stat().st_size):
            shutil.copy2(ttf_src, ttf_dst)
        rel_ttf = ttf_dst.relative_to(game_dir).as_posix()
        written.append(rel_ttf)
        if not existed:
            created.append(rel_ttf)
        ttf_rel = rel_ttf.replace("/", "\\")

    # Export engine-version-agnostic legacy SDF pack for Unity < 2019.1 or TMP 1.0–1.3 games
    legacy_sdf_rel = ""
    need_legacy_pack = (
        profile.tmp_schema == "legacy_1x"
        or (profile.unity_tuple is not None and profile.unity_tuple < (2019, 1, 0))
        or (not fallback_tmp_rel)
    )
    if need_legacy_pack:
        sdf_src, _ = resolve_sdf_source_bundle(app_root, font_opt.id)
        if sdf_src is not None and sdf_src.is_file():
            legacy_dst = paths["font_dir"] / "ul10n_legacy_sdf.dat"
            existed = legacy_dst.exists()
            if (not existed) or (prev_stamp != cur_stamp):
                if export_legacy_sdf_pack(sdf_src, legacy_dst):
                    rel_sdf = legacy_dst.relative_to(game_dir).as_posix()
                    written.append(rel_sdf)
                    if not existed:
                        created.append(rel_sdf)
                    legacy_sdf_rel = rel_sdf.replace("/", "\\")
            elif legacy_dst.is_file():
                rel_sdf = legacy_dst.relative_to(game_dir).as_posix()
                written.append(rel_sdf)
                legacy_sdf_rel = rel_sdf.replace("/", "\\")

    # Write stamp file so repeated refresh_hook_config calls stay <2ms
    stamp_existed = stamp_path.exists()
    try:
        stamp_path.write_text(cur_stamp, encoding="utf-8", newline="\n")
        rel_stamp = stamp_path.relative_to(game_dir).as_posix()
        written.append(rel_stamp)
        if not stamp_existed:
            created.append(rel_stamp)
    except OSError:
        pass

    # Deploy UL10nForge.FontGuard runtime plugin + config
    fg_src, fg_rel = _resolve_fontguard_dll(app_root, chosen_loader, runtime_type)
    if fg_src is not None and fg_src.is_file():
        fg_dst = game_dir / fg_rel
        fg_existed = fg_dst.exists()
        fg_dst.parent.mkdir(parents=True, exist_ok=True)
        if (not fg_existed) or (fg_dst.stat().st_size != fg_src.stat().st_size) or (fg_dst.read_bytes() != fg_src.read_bytes()):
            shutil.copy2(fg_src, fg_dst)
        written.append(fg_rel)
        if not fg_existed:
            created.append(fg_rel)

        fg_ini_rel = _fontguard_ini_rel(chosen_loader)
        fg_ini_dst = game_dir / fg_ini_rel
        fg_ini_existed = fg_ini_dst.exists()
        fg_ini_dst.parent.mkdir(parents=True, exist_ok=True)
        fg_ini_dst.write_text(
            _build_fontguard_ini(
                font_ttf_rel=ttf_rel,
                os_font_name=override_ugui_font,
                legacy_sdf_rel=legacy_sdf_rel,
                is_il2cpp=(runtime_type == "il2cpp"),
            ),
            encoding="utf-8",
            newline="\n",
        )
        written.append(fg_ini_rel)
        if not fg_ini_existed:
            created.append(fg_ini_rel)

    return fallback_tmp_rel, override_ugui_font, bundle_name, written, created


def get_xuat_paths(game_dir: str | Path, loader: str = "bepinex") -> dict[str, Path]:
    """Return canonical XUAT config, translation dir, auto-generated file, and curated export file."""
    root = Path(game_dir).resolve()
    if loader == "melonloader":
        base = root / "AutoTranslator"
        cfg = base / "Config.ini"
        tr_dir = base / "Translation" / "zh" / "Text"
        font_dir = base / "Translation" / "zh" / "Fonts"
    else:
        base = root / "BepInEx"
        cfg = base / "config" / "AutoTranslatorConfig.ini"
        tr_dir = base / "Translation" / "zh" / "Text"
        font_dir = base / "Translation" / "zh" / "Fonts"
    return {
        "base": base,
        "config": cfg,
        "translation_dir": tr_dir,
        "auto_file": tr_dir / "_AutoGeneratedTranslations.txt",
        "curated_file": tr_dir / "00_UL10nForge.txt",
        "font_dir": font_dir,
    }


def deploy_hook(
    game_info: GameInfo,
    *,
    app_root: Path,
    project_id: str,
    server_port: int,
    loader: str | None = None,
    target_lang: str = "zh-CN",
    source_lang: str = "auto",
    font_id: str = DEFAULT_FONT_ID,
) -> DeployResult:
    """Deploy BepInEx/MelonLoader + XUAT + Chinese Font fallback into a Unity game directory."""
    game_dir = Path(game_info.game_dir).resolve()
    if not game_dir.is_dir():
        raise FileNotFoundError(f"游戏目录不存在：{game_dir}")

    chosen_loader = (loader or game_info.recommended_loader or "bepinex").lower()
    runtime_type = game_info.runtime_type if game_info.runtime_type in ("mono", "il2cpp") else "mono"
    arch = game_info.arch if game_info.arch in ("x64", "x86") else "x64"

    kits_dir = app_root / "resources" / "hook_kits"
    loader_zip, xuat_zip = _select_kit_zips(kits_dir, runtime_type, arch, chosen_loader)

    # Detect if player already had third-party mods installed (ignore our own XUnity/UL10nForge files on redeploy)
    if chosen_loader == "bepinex":
        plugins_dir = game_dir / "BepInEx" / "plugins"
        other_plugins = [
            p for p in (plugins_dir.iterdir() if plugins_dir.is_dir() else [])
            if not p.name.lower().startswith(("xunity.", "ul10nforge.", "readme (autotranslator)"))
        ]
        preexisting_loader = (game_dir / "BepInEx" / "core").is_dir() and bool(other_plugins)
    else:
        mods_dir = game_dir / "Mods"
        other_mods = [
            p for p in (mods_dir.iterdir() if mods_dir.is_dir() else [])
            if not p.name.lower().startswith(("xunity.", "ul10nforge."))
        ]
        preexisting_loader = (game_dir / "MelonLoader").is_dir() and bool(other_mods)

    written_files: list[str] = []
    created_files: list[str] = []
    w_loader, c_loader = _safe_extract_zip(loader_zip, game_dir, skip_existing=preexisting_loader)
    written_files.extend(w_loader)
    created_files.extend(c_loader)
    w_xuat, c_xuat = _safe_extract_zip(xuat_zip, game_dir, skip_existing=False)
    written_files.extend(w_xuat)
    created_files.extend(c_xuat)

    paths = get_xuat_paths(game_dir, chosen_loader)
    paths["translation_dir"].mkdir(parents=True, exist_ok=True)
    paths["font_dir"].mkdir(parents=True, exist_ok=True)
    paths["config"].parent.mkdir(parents=True, exist_ok=True)

    fallback_tmp_rel, override_ugui_font, bundle_name, w_fonts, c_fonts = _sync_fonts_and_guard(
        game_dir,
        game_info,
        app_root=app_root,
        chosen_loader=chosen_loader,
        runtime_type=runtime_type,
        font_id=font_id,
        paths=paths,
    )
    written_files.extend(w_fonts)
    created_files.extend(c_fonts)

    if not paths["auto_file"].exists():
        paths["auto_file"].write_text("", encoding="utf-8", newline="\n")
        rel_auto = paths["auto_file"].relative_to(game_dir).as_posix()
        written_files.append(rel_auto)
        created_files.append(rel_auto)

    # Optional offline Cpp2IL pre-placement for MelonLoader (avoids GitHub download failure)
    if chosen_loader == "melonloader":
        kits_dir = app_root / "resources" / "hook_kits"
        cpp2il_cands = sorted(kits_dir.glob("Cpp2IL*.exe")) if kits_dir.is_dir() else []
        if cpp2il_cands:
            cpp2il_dst = (
                game_dir
                / "MelonLoader"
                / "Dependencies"
                / "Il2CppAssemblyGenerator"
                / "Cpp2IL"
                / "Cpp2IL.exe"
            )
            cpp2il_dst.parent.mkdir(parents=True, exist_ok=True)
            existed = cpp2il_dst.exists()
            shutil.copy2(cpp2il_cands[0], cpp2il_dst)
            rel_cpp = cpp2il_dst.relative_to(game_dir).as_posix()
            written_files.append(rel_cpp)
            if not existed:
                created_files.append(rel_cpp)

    # Write safe BepInEx.cfg on fresh BepInEx deploy (disables UnityLogListening to prevent IL2CPP hook conflicts)
    if chosen_loader == "bepinex" and not preexisting_loader:
        bep_cfg = game_dir / "BepInEx" / "config" / "BepInEx.cfg"
        if not bep_cfg.exists():
            bep_cfg.write_text(_safe_bepinex_cfg_text(), encoding="utf-8", newline="\n")
            rel_bep = bep_cfg.relative_to(game_dir).as_posix()
            written_files.append(rel_bep)
            created_files.append(rel_bep)

    endpoint_url = f"http://127.0.0.1:{int(server_port)}/xuat/translate/{project_id}"
    cfg_text = _build_xuat_config(
        endpoint_url=endpoint_url,
        fallback_tmp_font_rel=fallback_tmp_rel,
        override_font_name=override_ugui_font,
        target_lang=target_lang,
        source_lang=source_lang,
        is_il2cpp=(runtime_type == "il2cpp"),
    )
    cfg_existed = paths["config"].exists()
    paths["config"].write_text(cfg_text, encoding="utf-8", newline="\n")
    rel_cfg = paths["config"].relative_to(game_dir).as_posix()
    written_files.append(rel_cfg)
    if not cfg_existed:
        created_files.append(rel_cfg)
    created_files.append(paths["curated_file"].relative_to(game_dir).as_posix())

    unique_files = list(dict.fromkeys(written_files))
    unique_created = list(dict.fromkeys(created_files))
    manifest = {
        "loader": chosen_loader,
        "runtime_type": runtime_type,
        "arch": arch,
        "loader_zip": loader_zip.name,
        "xuat_zip": xuat_zip.name,
        "font_bundle": bundle_name,
        "preexisting_loader": preexisting_loader,
        "files": unique_files,
        "created_files": unique_created,
    }
    return DeployResult(
        loader=chosen_loader,
        runtime_type=runtime_type,
        arch=arch,
        config_path=str(paths["config"]),
        translation_dir=str(paths["translation_dir"]),
        font_bundle=bundle_name,
        files_written=len(unique_files),
        manifest=manifest,
    )


def refresh_hook_config(
    game_info: GameInfo,
    *,
    app_root: Path,
    project_id: str,
    server_port: int,
    loader: str | None = None,
    target_lang: str = "zh",
    source_lang: str = "auto",
    preexisting_loader: bool = False,
    font_id: str = DEFAULT_FONT_ID,
) -> None:
    """Fast (<2ms) self-healing config refresh called before launching an already-deployed game.

    Ensures AutoTranslatorConfig.ini reflects the latest safe IL2CPP/Unity-version rules and
    patches BepInEx.cfg (when managed by UL10nForge) to keep UnityLogListening=false.
    """
    game_dir = Path(game_info.game_dir)
    if not game_dir.is_dir():
        return
    chosen_loader = (loader or game_info.recommended_loader or "bepinex").lower()
    if chosen_loader == "melonloader" and (game_info.runtime_type != "il2cpp" or game_info.arch != "x64"):
        chosen_loader = "bepinex"

    paths = get_xuat_paths(game_dir, chosen_loader)
    paths["translation_dir"].mkdir(parents=True, exist_ok=True)
    paths["config"].parent.mkdir(parents=True, exist_ok=True)
    if not paths["auto_file"].exists():
        paths["auto_file"].write_text("", encoding="utf-8", newline="\n")

    runtime_type = game_info.runtime_type if game_info.runtime_type in ("mono", "il2cpp") else "mono"
    fallback_tmp_rel, override_ugui_font, _bundle_name, _w_f, _c_f = _sync_fonts_and_guard(
        game_dir,
        game_info,
        app_root=app_root,
        chosen_loader=chosen_loader,
        runtime_type=runtime_type,
        font_id=font_id,
        paths=paths,
    )

    if chosen_loader == "bepinex" and (not preexisting_loader or game_info.runtime_type == "il2cpp"):
        bep_cfg = game_dir / "BepInEx" / "config" / "BepInEx.cfg"
        if bep_cfg.is_file():
            try:
                raw_bep = bep_cfg.read_text(encoding="utf-8", errors="ignore")
                patched = raw_bep.replace("UnityLogListening = true", "UnityLogListening = false")
                # Also silence BepInEx popup console window (stay strictly inside [Logging.Console] section!)
                patched = re.sub(
                    r"(\[Logging\.Console\][^\[]*?\nEnabled\s*=\s*)true\b",
                    r"\1false",
                    patched,
                    count=1,
                    flags=re.IGNORECASE,
                )
                if patched != raw_bep:
                    bep_cfg.write_text(patched, encoding="utf-8", newline="\n")
            except OSError:
                pass
        else:
            bep_cfg.write_text(_safe_bepinex_cfg_text(), encoding="utf-8", newline="\n")

    endpoint_url = f"http://127.0.0.1:{int(server_port)}/xuat/translate/{project_id}"
    cfg_text = _build_xuat_config(
        endpoint_url=endpoint_url,
        fallback_tmp_font_rel=fallback_tmp_rel,
        override_font_name=override_ugui_font,
        target_lang=target_lang,
        source_lang=source_lang,
        is_il2cpp=(runtime_type == "il2cpp"),
    )
    paths["config"].write_text(cfg_text, encoding="utf-8", newline="\n")


def _prune_empty_parents(start: Path, stop_root: Path) -> None:
    """Remove empty directories upward until `stop_root`, never touching non-empty user mod dirs."""
    cur = start
    while cur != stop_root and str(cur).startswith(str(stop_root)):
        if cur.is_dir():
            try:
                if any(cur.iterdir()):
                    break
                cur.rmdir()
            except OSError:
                break
        cur = cur.parent


def uninstall_hook(game_dir: str | Path, manifest: dict[str, Any] | None) -> int:
    """Safely remove ONLY files installed by UL10nForge and prune empty dirs (preserves other mods)."""
    root = Path(game_dir).resolve()
    removed = 0
    touched_dirs: set[Path] = set()

    files_to_remove: list[str] = []
    if manifest:
        if manifest.get("preexisting_loader") and isinstance(manifest.get("created_files"), list):
            files_to_remove = list(manifest["created_files"])
        elif isinstance(manifest.get("files"), list):
            files_to_remove = list(manifest["files"]) + list(manifest.get("created_files") or [])

    for rel in dict.fromkeys(files_to_remove):
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)):
            continue
        if target.is_file():
            try:
                target.unlink()
                removed += 1
                touched_dirs.add(target.parent)
            except OSError:
                pass

    loader = (manifest or {}).get("loader", "bepinex")
    paths = get_xuat_paths(root, loader)
    if paths["curated_file"].is_file():
        try:
            paths["curated_file"].unlink()
            removed += 1
            touched_dirs.add(paths["curated_file"].parent)
        except OSError:
            pass

    # Always remove UL10nForge.FontGuard artifacts even if added later via refresh_hook_config
    for fg_cleanup in (
        root / "BepInEx" / "plugins" / "UL10nForge.FontGuard",
        root / "BepInEx" / "config" / "ul10n_fontguard.ini",
        root / "Mods" / "UL10nForge.FontGuard.Melon.dll",
        root / "AutoTranslator" / "ul10n_fontguard.ini",
        paths["font_dir"] / "ul10n_legacy_sdf.dat",
        paths["font_dir"] / ".ul10n_font_stamp",
    ):
        if fg_cleanup.is_file():
            try:
                fg_cleanup.unlink()
                removed += 1
                touched_dirs.add(fg_cleanup.parent)
            except OSError:
                pass
        elif fg_cleanup.is_dir():
            shutil.rmtree(fg_cleanup, ignore_errors=True)
            touched_dirs.add(fg_cleanup.parent)

    if not (manifest or {}).get("preexisting_loader"):
        bep_plugins = root / "BepInEx" / "plugins"
        other_plugins = []
        if bep_plugins.is_dir():
            for p in bep_plugins.rglob("*.dll"):
                low_p = str(p).casefold()
                if "xunity" not in low_p and "extinject" not in low_p and "ul10nforge" not in low_p:
                    other_plugins.append(p)
        if not other_plugins:
            for cache_sub in (
                root / "BepInEx" / "cache",
                root / "BepInEx" / "LogOutput.log",
                root / "BepInEx" / "config" / "BepInEx.cfg",
                root / "BepInEx" / "Translation",
            ):
                if cache_sub.is_file():
                    try:
                        cache_sub.unlink()
                        removed += 1
                    except OSError:
                        pass
                elif cache_sub.is_dir():
                    shutil.rmtree(cache_sub, ignore_errors=True)

    for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
        _prune_empty_parents(d, root)
    for top in ("BepInEx", "dotnet", "MelonLoader", "Mods", "UserLibs", "AutoTranslator"):
        top_dir = root / top
        if top_dir.is_dir():
            for sub in sorted(top_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if sub.is_dir():
                    _prune_empty_parents(sub, root)
            _prune_empty_parents(top_dir, root)
    return removed


def build_standalone_patch_zip(
    game_info: GameInfo,
    *,
    app_root: Path,
    curated_dict_text: str,
    loader: str | None = None,
    target_lang: str = "zh-CN",
    source_lang: str = "auto",
    font_id: str = DEFAULT_FONT_ID,
) -> bytes:
    """Build a standalone, zero-dependency offline Chinese patch `.zip` ready to share."""
    import io

    chosen_loader = (loader or game_info.recommended_loader or "bepinex").lower()
    runtime_type = game_info.runtime_type if game_info.runtime_type in ("mono", "il2cpp") else "mono"
    arch = game_info.arch if game_info.arch in ("x64", "x86") else "x64"

    kits_dir = app_root / "resources" / "hook_kits"
    loader_zip, xuat_zip = _select_kit_zips(kits_dir, runtime_type, arch, chosen_loader)
    profile = inspect_game_font_profile(game_info.game_dir, runtime_type, game_info.unity_version)
    bundle_src, font_opt = resolve_bundle_file(app_root, game_info.unity_version, font_id)
    override_ugui_font = resolve_available_ugui_font(font_opt.ugui_font)
    bundle_name = bundle_src.name if bundle_src is not None else ""
    ttf_src = font_opt.preview_ttf_path

    base_prefix = "AutoTranslator" if chosen_loader == "melonloader" else "BepInEx"
    cfg_rel = "AutoTranslator/Config.ini" if chosen_loader == "melonloader" else "BepInEx/config/AutoTranslatorConfig.ini"
    tr_prefix = f"{base_prefix}/Translation/zh/Text"
    font_prefix = f"{base_prefix}/Translation/zh/Fonts"

    import tempfile

    buf = io.BytesIO()
    added: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="ul10n_patch_") as stage_dir, zipfile.ZipFile(
        buf, "w", compression=zipfile.ZIP_DEFLATED
    ) as out_zf:
        stage = Path(stage_dir)
        for src_zip in (loader_zip, xuat_zip):
            with zipfile.ZipFile(src_zip, "r") as in_zf:
                for info in in_zf.infolist():
                    if info.is_dir():
                        continue
                    rel = info.filename.replace("\\", "/").lstrip("/")
                    if not rel or ".." in Path(rel).parts or rel in added:
                        continue
                    out_zf.writestr(rel, in_zf.read(info.filename))
                    added.add(rel)

        fallback_tmp_rel = ""
        use_u6_os = _should_prefer_unity6_os_fallback(profile)
        if use_u6_os:
            fallback_tmp_rel = override_ugui_font
        elif bundle_name and bundle_src is not None and bundle_src.is_file():
            staged_bundle = stage / bundle_name
            ensure_dual_schema_bundle(bundle_src, staged_bundle, target_assembly=profile.tmp_assembly)
            rel = f"{font_prefix}/{bundle_name}"
            out_zf.write(staged_bundle, arcname=rel)
            added.add(rel)
            fallback_tmp_rel = rel.replace("/", "\\")

        ttf_rel = ""
        if ttf_src is not None and ttf_src.is_file():
            rel = f"{font_prefix}/{ttf_src.name}"
            out_zf.write(ttf_src, arcname=rel)
            added.add(rel)
            ttf_rel = rel.replace("/", "\\")

        legacy_sdf_rel = ""
        need_legacy_pack = (
            profile.tmp_schema == "legacy_1x"
            or (profile.unity_tuple is not None and profile.unity_tuple < (2019, 1, 0))
            or (not fallback_tmp_rel)
        )
        if need_legacy_pack:
            sdf_src, _ = resolve_sdf_source_bundle(app_root, font_opt.id)
            if sdf_src is not None and sdf_src.is_file():
                staged_sdf = stage / "ul10n_legacy_sdf.dat"
                if export_legacy_sdf_pack(sdf_src, staged_sdf):
                    rel_sdf = f"{font_prefix}/ul10n_legacy_sdf.dat"
                    out_zf.write(staged_sdf, arcname=rel_sdf)
                    added.add(rel_sdf)
                    legacy_sdf_rel = rel_sdf.replace("/", "\\")

        fg_src, fg_rel = _resolve_fontguard_dll(app_root, chosen_loader, runtime_type)
        if fg_src is not None and fg_src.is_file():
            out_zf.write(fg_src, arcname=fg_rel)
            added.add(fg_rel)
            fg_ini_rel = _fontguard_ini_rel(chosen_loader)
            out_zf.writestr(
                fg_ini_rel,
                _build_fontguard_ini(
                    font_ttf_rel=ttf_rel,
                    os_font_name=override_ugui_font,
                    legacy_sdf_rel=legacy_sdf_rel,
                    is_il2cpp=(runtime_type == "il2cpp"),
                ).encode("utf-8"),
            )
            added.add(fg_ini_rel)

        if chosen_loader == "bepinex":
            out_zf.writestr("BepInEx/config/BepInEx.cfg", _safe_bepinex_cfg_text().encode("utf-8"))
        elif chosen_loader == "melonloader":
            cpp2il_cands = sorted(kits_dir.glob("Cpp2IL*.exe")) if kits_dir.is_dir() else []
            if cpp2il_cands:
                cpp_rel = "MelonLoader/Dependencies/Il2CppAssemblyGenerator/Cpp2IL/Cpp2IL.exe"
                if cpp_rel not in added:
                    out_zf.write(cpp2il_cands[0], arcname=cpp_rel)
                    added.add(cpp_rel)

        cfg_text = _build_xuat_config(
            endpoint_url="",
            fallback_tmp_font_rel=fallback_tmp_rel,
            override_font_name=override_ugui_font,
            target_lang=target_lang,
            source_lang=source_lang,
            offline_only=True,
            is_il2cpp=(runtime_type == "il2cpp"),
        )
        out_zf.writestr(cfg_rel, cfg_text.encode("utf-8"))
        out_zf.writestr(f"{tr_prefix}/00_UL10nForge.txt", curated_dict_text.encode("utf-8"))
        out_zf.writestr(f"{tr_prefix}/_AutoGeneratedTranslations.txt", b"")

        readme = (
            f"《{game_info.name}》中文离线汉化补丁\n"
            f"由 UL10nForge 自动生成（无损插件模式，不修改游戏原文件）\n\n"
            "【安装方法】\n"
            "1. 将压缩包内所有文件解压到游戏根目录（与游戏主程序 .exe 同级）。\n"
            "2. 直接启动游戏即可体验中文，无需联网，无需安装其他软件。\n\n"
            "【卸载方法】\n"
            f"删除 {base_prefix} 文件夹及 winhttp.dll / doorstop_config.ini（或 version.dll / dobby.dll）即可完全还原。\n"
        )
        out_zf.writestr("安装说明.txt", readme.encode("utf-8"))

    return buf.getvalue()
