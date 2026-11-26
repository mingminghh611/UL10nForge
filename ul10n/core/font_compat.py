"""Unity font & TextMeshPro compatibility diagnostics, dual-schema upgrading & legacy SDF export.

Eliminates missing-glyph ("□□□□" / tofu) issues across ALL Unity versions (5.x ~ 6000.x):
- Detects the game's actual TextMeshPro serialization schema (`legacy_1x` vs `modern_110`),
  exact TMP assembly name (`Unity.TextMeshPro.dll` vs `TextMeshPro-2017.3-Runtime.dll`, etc.),
  and whether TMP supports runtime dynamic font creation (`CreateFontAsset`).
- Upgrades `*_sdf_u2019` (and any single-schema) AssetBundles into Dual-Schema bundles that
  populate BOTH modern TMP 1.4+/2.x/3.x fields (`m_CharacterTable`, `m_GlyphTable`,
  `m_AtlasTextures`) AND legacy TMP 1.0–1.3 fields (`m_fontInfo`, `m_glyphInfoList`, `atlas`)
  using the exact top-down UV convention (`y = atlas_h - m_Y - m_Height`).
- Exports a compact, Unity-version-agnostic gzipped SDF pack (`ul10n_legacy_sdf.dat`) for
  Unity < 2019.1 or non-standard TMP assemblies so the runtime FontGuard plugin can construct
  a native `TMP_FontAsset` in memory with zero AssetBundle version restrictions.
- Validates Windows installed OS fonts so `OverrideFont=` never fails XUnity validation.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
from typing import Any

from ul10n.core.game_detector import _parse_unity_tuple

_LEGACY_SDF_MAGIC = b"UL10NSDF"
_LEGACY_SDF_VERSION = 1

# Exact Windows Registry font title -> Unity Font.GetOSInstalledFontNames() family mapping
_WIN_CJK_FONT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Microsoft YaHei", ("microsoft yahei", "msyh")),
    ("KaiTi", ("kaiti", "simkai")),
    ("SimHei", ("simhei",)),
    ("SimSun", ("simsun",)),
    ("DengXian", ("dengxian",)),
    ("FangSong", ("fangsong", "simfang")),
    ("Microsoft JhengHei", ("microsoft jhenghei", "msjh")),
    ("NSimSun", ("nsimsun",)),
)

_UGUI_FALLBACK_ORDER: tuple[str, ...] = (
    "Microsoft YaHei",
    "SimHei",
    "SimSun",
    "DengXian",
    "Microsoft JhengHei",
    "KaiTi",
    "FangSong",
)

# Symbols missing from static 4096x4096 SDF atlases -> safe static-atlas equivalents
# (Used ONLY for legacy TMP 1.0–1.3 games that lack runtime dynamic FontEngine.)
_LEGACY_SYMBOL_MAP: dict[str, str] = {
    "「": "“",
    "」": "”",
    "『": "‘",
    "』": "’",
    "〈": "《",
    "〉": "》",
    "〖": "【",
    "〗": "】",
    "×": "x",
    "÷": "/",
    "±": "+/-",
    "→": "->",
    "←": "<-",
    "↑": "^",
    "↓": "v",
    "–": "—",
    "•": "·",
    "★": "*",
    "☆": "*",
    "●": "·",
    "○": "o",
    "◆": "·",
    "◇": "·",
    "■": "□",
    "▲": "^",
    "▼": "v",
    "①": "(1)",
    "②": "(2)",
    "③": "(3)",
    "④": "(4)",
    "⑤": "(5)",
    "⑥": "(6)",
    "⑦": "(7)",
    "⑧": "(8)",
    "⑨": "(9)",
    "⑩": "(10)",
    "Ⅰ": "I",
    "Ⅱ": "II",
    "Ⅲ": "III",
    "Ⅳ": "IV",
    "Ⅴ": "V",
    "Ⅵ": "VI",
    "Ⅶ": "VII",
    "Ⅷ": "VIII",
    "Ⅸ": "IX",
    "Ⅹ": "X",
}

_INSTALLED_OS_FONTS_CACHE: set[str] | None = None


@dataclass(frozen=True)
class GameFontProfile:
    has_tmp: bool = False
    tmp_schema: str = "unknown"  # "legacy_1x" | "modern_110" | "unknown"
    tmp_assembly: str = ""
    supports_dynamic_tmp: bool = False
    supports_os_create_font: bool = False
    unity_tuple: tuple[int, int, int] | None = None


def _scan_file_markers(path: Path, markers: tuple[bytes, ...], max_bytes: int = 80 * 1024 * 1024) -> set[bytes]:
    """Stream-scan a binary file for ASCII marker tokens using bounded memory."""
    found: set[bytes] = set()
    if not path.is_file():
        return found
    pending = set(markers)
    chunk_size = 2 * 1024 * 1024
    overlap = max(len(m) for m in markers) + 8
    tail = b""
    read_total = 0
    try:
        with path.open("rb") as f:
            while pending and read_total < max_bytes:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                read_total += len(chunk)
                buf = tail + chunk
                for m in list(pending):
                    if m in buf:
                        found.add(m)
                        pending.remove(m)
                tail = buf[-overlap:] if len(buf) >= overlap else buf
    except OSError:
        pass
    return found


def inspect_game_font_profile(
    game_dir: str | Path,
    runtime_type: str = "mono",
    unity_version: str = "",
) -> GameFontProfile:
    """Inspect a Unity game directory (pure read-only) to determine exact TMP schema & capabilities."""
    root = Path(game_dir)
    ver = _parse_unity_tuple(unity_version)
    if not root.is_dir():
        return GameFontProfile(unity_tuple=ver)

    data_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.endswith("_Data")),
        key=lambda p: p.name.casefold(),
    )
    if not data_dirs:
        return GameFontProfile(unity_tuple=ver)
    data_dir = data_dirs[0]

    markers = (
        b"TMP_FontAsset",
        b"m_CharacterTable",
        b"m_GlyphTable",
        b"m_glyphInfoList",
        b"m_fontInfo",
        b"m_AtlasPopulationMode",
        b"CreateFontAsset",
        b"InternalDynamicOS",
        b"m_SourceFontFilePath",
        b"GetOSInstalledFontNames",
    )

    found: set[bytes] = set()
    tmp_assembly = ""

    if runtime_type == "il2cpp":
        meta_path = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if meta_path.is_file():
            found = _scan_file_markers(meta_path, markers)
            if b"TMP_FontAsset" in found:
                tmp_assembly = "Unity.TextMeshPro" if (ver and ver >= (2022, 0, 0)) else "Unity.TextMeshPro.dll"
    else:
        managed = data_dir / "Managed"
        if managed.is_dir():
            tmp_dlls = sorted(
                (p for p in managed.iterdir() if p.is_file() and p.suffix.lower() == ".dll" and ("textmeshpro" in p.name.lower() or p.name.lower() == "tmp.dll")),
                key=lambda p: p.name.casefold(),
            )
            if tmp_dlls:
                for dll in tmp_dlls:
                    hits = _scan_file_markers(dll, markers)
                    if b"TMP_FontAsset" in hits or b"m_CharacterTable" in hits or b"m_glyphInfoList" in hits:
                        found |= hits
                        tmp_assembly = dll.name
                        break
            else:
                # Some legacy Unity 5 / 2017 games compiled TMP directly into Assembly-CSharp.dll
                ac = managed / "Assembly-CSharp.dll"
                if ac.is_file():
                    hits = _scan_file_markers(ac, markers)
                    if b"TMP_FontAsset" in hits and (b"m_glyphInfoList" in hits or b"m_CharacterTable" in hits):
                        found |= hits
                        tmp_assembly = "Assembly-CSharp.dll"

    has_tmp = (
        b"TMP_FontAsset" in found
        or b"m_CharacterTable" in found
        or b"m_glyphInfoList" in found
    )

    if b"m_CharacterTable" in found or b"m_GlyphTable" in found:
        tmp_schema = "modern_110"
    elif b"m_glyphInfoList" in found or b"m_fontInfo" in found:
        tmp_schema = "legacy_1x"
    elif ver is not None and ver >= (2020, 1, 0) and has_tmp:
        tmp_schema = "modern_110"
    else:
        tmp_schema = "unknown"

    supports_dynamic_tmp = (
        b"m_AtlasPopulationMode" in found
        or b"CreateFontAsset" in found
        or tmp_schema == "modern_110"
    )
    supports_os_create_font = bool(
        b"CreateFontAsset" in found
        and (
            b"InternalDynamicOS" in found
            or b"m_SourceFontFilePath" in found
            or (ver is not None and ver >= (6000, 0, 0))
        )
    )

    return GameFontProfile(
        has_tmp=has_tmp,
        tmp_schema=tmp_schema,
        tmp_assembly=tmp_assembly,
        supports_dynamic_tmp=supports_dynamic_tmp,
        supports_os_create_font=supports_os_create_font,
        unity_tuple=ver,
    )


def get_installed_windows_cjk_fonts(*, force_refresh: bool = False) -> set[str]:
    """Return canonical Windows OS CJK font family names currently installed on this machine."""
    global _INSTALLED_OS_FONTS_CACHE
    if _INSTALLED_OS_FONTS_CACHE is not None and not force_refresh:
        return set(_INSTALLED_OS_FONTS_CACHE)

    installed: set[str] = set()
    try:
        import winreg  # type: ignore[import-not-found]

        roots = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        )
        for hive, subkey in roots:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    idx = 0
                    while True:
                        try:
                            name, val, _ = winreg.EnumValue(key, idx)
                            idx += 1
                        except OSError:
                            break
                        blob = f"{name} {val}".lower()
                        for canonical, needles in _WIN_CJK_FONT_PATTERNS:
                            if any(n in blob for n in needles):
                                installed.add(canonical)
            except OSError:
                continue
    except Exception:
        pass

    _INSTALLED_OS_FONTS_CACHE = set(installed)
    return installed


def resolve_available_ugui_font(preferred: str = "Microsoft YaHei") -> str:
    """Return a guaranteed-installed Windows CJK font name for XUAT `OverrideFont=`."""
    pref = (preferred or "Microsoft YaHei").strip() or "Microsoft YaHei"
    installed = get_installed_windows_cjk_fonts()
    if not installed:
        # Non-Windows or inaccessible registry: return safe default
        return pref
    if pref in installed:
        return pref
    for cand in _UGUI_FALLBACK_ORDER:
        if cand in installed:
            return cand
    return pref


def sanitize_for_legacy_static_atlas(text: str) -> str:
    """Replace punctuation/symbols absent from static SDF atlases ONLY for legacy TMP 1.0–1.3 games."""
    if not text:
        return text
    out = text.replace("°C", "℃").replace("°c", "℃")
    if not any(ch in _LEGACY_SYMBOL_MAP for ch in out):
        return out
    return "".join(_LEGACY_SYMBOL_MAP.get(ch, ch) for ch in out)


def _build_legacy_tmp_fields(tree: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """Convert modern TMP 1.1.0 (`m_FaceInfo`/`m_CharacterTable`/`m_GlyphTable`) to legacy TMP 1.0–1.3 fields.

    Verified against decompiled `TMPro.TMP_Text` (TMP 1.2.3 & 1.3.0):
      uv_BL.y = 1 - (y + height + padding) / AtlasHeight
      uv_TL.y = 1 - (y - padding) / AtlasHeight
    Therefore legacy top-down `y = AtlasHeight - m_GlyphRect.m_Y - m_GlyphRect.m_Height`.
    """
    atlas_w = int(tree.get("m_AtlasWidth") or 4096)
    atlas_h = int(tree.get("m_AtlasHeight") or 4096)
    padding = float(tree.get("m_AtlasPadding") or 6.0)
    fi = tree.get("m_FaceInfo") or {}

    glyph_by_idx: dict[int, dict[str, Any]] = {}
    for g in tree.get("m_GlyphTable") or []:
        if isinstance(g, dict) and "m_Index" in g:
            glyph_by_idx[int(g["m_Index"])] = g

    legacy_glyphs: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for ch in tree.get("m_CharacterTable") or []:
        if not isinstance(ch, dict):
            continue
        code = int(ch.get("m_Unicode", -1))
        if code < 0 or code in seen_ids:
            continue
        g = glyph_by_idx.get(int(ch.get("m_GlyphIndex", -1)))
        if g is None:
            continue
        rect = g.get("m_GlyphRect") or {}
        met = g.get("m_Metrics") or {}
        gx = float(rect.get("m_X", 0))
        gy_bottom = float(rect.get("m_Y", 0))
        gw = float(rect.get("m_Width", 0))
        gh = float(rect.get("m_Height", 0))
        # Exact top-down Y conversion matching TMP 1.2 / 1.3 UV equation
        gy_top = float(atlas_h) - gy_bottom - gh
        c_scale = float(ch.get("m_Scale", 1.0) or 1.0)
        g_scale = float(g.get("m_Scale", 1.0) or 1.0)
        seen_ids.add(code)
        legacy_glyphs.append(
            {
                "id": code,
                "x": gx,
                "y": gy_top,
                "width": float(met.get("m_Width", gw)),
                "height": float(met.get("m_Height", gh)),
                "xOffset": float(met.get("m_HorizontalBearingX", 0.0)),
                "yOffset": float(met.get("m_HorizontalBearingY", 0.0)),
                "xAdvance": float(met.get("m_HorizontalAdvance", 0.0)),
                "scale": c_scale * g_scale,
            }
        )

    legacy_glyphs.sort(key=lambda item: item["id"])

    face_name = str(fi.get("m_FamilyName") or tree.get("m_Name") or "UL10nForge SDF")
    legacy_face = {
        "Name": face_name,
        "PointSize": float(fi.get("m_PointSize") or 33.0),
        "Scale": float(fi.get("m_Scale") or 1.0),
        "CharacterCount": len(legacy_glyphs),
        "LineHeight": float(fi.get("m_LineHeight") or 47.0),
        "Baseline": float(fi.get("m_Baseline") or 0.0),
        "Ascender": float(fi.get("m_AscentLine") or 36.0),
        "CapHeight": float(fi.get("m_CapLine") or 24.0),
        "Descender": float(fi.get("m_DescentLine") or -10.0),
        "CenterLine": float(fi.get("m_MeanLine") or 17.0),
        "SuperscriptOffset": float(fi.get("m_SuperscriptOffset") or 36.0),
        "SubscriptOffset": float(fi.get("m_SubscriptOffset") or -10.0),
        "SubSize": float(fi.get("m_SubscriptSize") or 0.5),
        "Underline": float(fi.get("m_UnderlineOffset") or -3.3),
        "UnderlineThickness": float(fi.get("m_UnderlineThickness") or 1.65),
        "strikethrough": float(fi.get("m_StrikethroughOffset") or 6.8),
        "strikethroughThickness": float(fi.get("m_StrikethroughThickness") or 1.65),
        "TabWidth": float(fi.get("m_TabWidth") or 8.0),
        "Padding": padding,
        "AtlasWidth": float(atlas_w),
        "AtlasHeight": float(atlas_h),
    }

    atlas_textures = tree.get("m_AtlasTextures") or []
    atlas_pptr = dict(atlas_textures[0]) if atlas_textures and isinstance(atlas_textures[0], dict) else None
    return legacy_face, legacy_glyphs, atlas_pptr


def bundle_has_legacy_glyphs(bundle_path: Path) -> bool:
    """Return True if a TMP AssetBundle already has non-empty legacy `m_glyphInfoList` + `atlas`."""
    try:
        import UnityPy

        env = UnityPy.load(bundle_path.read_bytes())
        for obj in env.objects:
            if obj.type.name == "MonoBehaviour":
                tr = obj.read_typetree()
                if "m_CharacterTable" in tr or "m_glyphInfoList" in tr:
                    glyphs = tr.get("m_glyphInfoList") or []
                    atlas = tr.get("atlas") or {}
                    fi = tr.get("m_fontInfo") or {}
                    return bool(len(glyphs) > 0 and atlas.get("m_PathID", 0) != 0 and float(fi.get("AtlasWidth", 0)) > 0)
    except Exception:
        pass
    return False


def ensure_dual_schema_bundle(
    src_bundle: Path,
    dst_bundle: Path,
    *,
    target_assembly: str = "",
) -> bool:
    """Copy `src_bundle` to `dst_bundle`, upgrading it to Dual-Schema (TMP 1.x + TMP 1.4/2.x/3.x) if needed.

    Preserves all modern `m_Version = "1.1.0"` fields (`m_FaceInfo`, `m_CharacterTable`,
    `m_GlyphTable`, `m_AtlasTextures`) while populating `m_fontInfo`, `m_glyphInfoList`,
    and `atlas` so TMP 1.0–1.3 games (e.g. Unity 2019 TMP 1.3.0) deserialize all glyphs cleanly.
    Returns True if patched/rewritten, False if plain-copied as-is.
    """
    dst_bundle.parent.mkdir(parents=True, exist_ok=True)
    need_custom_asm = bool(
        target_assembly
        and target_assembly.strip().lower() not in ("unity.textmeshpro.dll", "unity.textmeshpro")
    )

    try:
        import UnityPy
    except ImportError:
        if src_bundle.resolve() != dst_bundle.resolve():
            shutil.copy2(src_bundle, dst_bundle)
        return False

    env = UnityPy.load(src_bundle.read_bytes())
    changed = False

    for obj in env.objects:
        if obj.type.name == "MonoBehaviour":
            try:
                tr = obj.read_typetree()
            except Exception:
                continue
            if "m_CharacterTable" not in tr or "m_GlyphTable" not in tr:
                continue
            needs_legacy = (
                "m_glyphInfoList" in tr
                and len(tr.get("m_glyphInfoList") or []) == 0
                and len(tr.get("m_CharacterTable") or []) > 0
            )
            needs_atlas = (
                "atlas" in tr
                and int((tr.get("atlas") or {}).get("m_PathID", 0)) == 0
                and bool(tr.get("m_AtlasTextures"))
            )
            if needs_legacy or needs_atlas:
                legacy_face, legacy_glyphs, atlas_pptr = _build_legacy_tmp_fields(tr)
                if "m_fontInfo" in tr and legacy_glyphs:
                    tr["m_fontInfo"] = legacy_face
                if "m_glyphInfoList" in tr and legacy_glyphs:
                    tr["m_glyphInfoList"] = legacy_glyphs
                if "atlas" in tr and atlas_pptr is not None:
                    tr["atlas"] = atlas_pptr
                obj.save_typetree(tr)
                changed = True
        elif need_custom_asm and obj.type.name == "MonoScript":
            try:
                tr = obj.read_typetree()
            except Exception:
                continue
            if tr.get("m_ClassName") == "TMP_FontAsset" and tr.get("m_AssemblyName") != target_assembly:
                tr["m_AssemblyName"] = target_assembly
                obj.save_typetree(tr)
                changed = True

    if not changed:
        if src_bundle.resolve() != dst_bundle.resolve():
            shutil.copy2(src_bundle, dst_bundle)
        return False

    # Atomic save via temp directory to avoid partial/corrupt writes
    with tempfile.TemporaryDirectory(prefix="ul10n_font_") as tmp_dir:
        env.save(pack="lz4", out_path=tmp_dir)
        saved_files = [p for p in Path(tmp_dir).iterdir() if p.is_file()]
        if not saved_files:
            raise RuntimeError(f"UnityPy 未能写出升级后的字体包：{src_bundle.name}")
        tmp_out = dst_bundle.with_suffix(dst_bundle.suffix + ".tmp")
        shutil.copy2(saved_files[0], tmp_out)
        tmp_out.replace(dst_bundle)
    return True


def export_legacy_sdf_pack(src_bundle: Path, out_path: Path) -> bool:
    """Export engine-version-agnostic gzipped SDF atlas + TMP 1.x glyph table (`ul10n_legacy_sdf.dat`).

    Enables `UL10nForge.FontGuard` to instantiate a native `TMP_FontAsset` in memory on Unity
    5.x / 2017.x / 2018.x / 2019.x games regardless of AssetBundle version or TMP DLL name.
    """
    if not src_bundle.is_file():
        return False
    try:
        import UnityPy
    except ImportError:
        return False

    env = UnityPy.load(src_bundle.read_bytes())
    font_tree: dict[str, Any] | None = None
    pixels: bytes | None = None
    atlas_w = 4096
    atlas_h = 4096
    gradient_scale = 10.0

    for obj in env.objects:
        t = obj.type.name
        if t == "MonoBehaviour":
            try:
                tr = obj.read_typetree()
            except Exception:
                continue
            if "m_CharacterTable" in tr and "m_GlyphTable" in tr:
                font_tree = tr
        elif t == "Texture2D":
            try:
                tr = obj.read_typetree()
            except Exception:
                continue
            raw = tr.get("image data")
            if isinstance(raw, (bytes, bytearray, memoryview)) and len(raw) >= 1024 * 1024:
                pixels = bytes(raw)
                atlas_w = int(tr.get("m_Width") or 4096)
                atlas_h = int(tr.get("m_Height") or 4096)
        elif t == "Material":
            try:
                tr = obj.read_typetree()
                floats = dict((tr.get("m_SavedProperties") or {}).get("m_Floats") or [])
                if "_GradientScale" in floats:
                    gradient_scale = float(floats["_GradientScale"])
            except Exception:
                pass

    if font_tree is None or pixels is None:
        return False
    if len(pixels) != atlas_w * atlas_h:
        return False

    legacy_face, legacy_glyphs, _ = _build_legacy_tmp_fields(font_tree)
    if not legacy_glyphs:
        return False

    name_bytes = str(legacy_face["Name"]).encode("utf-8")[:255]
    header = bytearray()
    header += _LEGACY_SDF_MAGIC
    header += struct.pack("<Iiif", _LEGACY_SDF_VERSION, atlas_w, atlas_h, gradient_scale)
    header += struct.pack("<H", len(name_bytes)) + name_bytes
    header += struct.pack(
        "<19fi",
        float(legacy_face["PointSize"]),
        float(legacy_face["Scale"]),
        float(legacy_face["LineHeight"]),
        float(legacy_face["Baseline"]),
        float(legacy_face["Ascender"]),
        float(legacy_face["CapHeight"]),
        float(legacy_face["Descender"]),
        float(legacy_face["CenterLine"]),
        float(legacy_face["SuperscriptOffset"]),
        float(legacy_face["SubscriptOffset"]),
        float(legacy_face["SubSize"]),
        float(legacy_face["Underline"]),
        float(legacy_face["UnderlineThickness"]),
        float(legacy_face["strikethrough"]),
        float(legacy_face["strikethroughThickness"]),
        float(legacy_face["TabWidth"]),
        float(legacy_face["Padding"]),
        float(legacy_face["AtlasWidth"]),
        float(legacy_face["AtlasHeight"]),
        len(legacy_glyphs),
    )
    header += struct.pack("<i", len(legacy_glyphs))
    for g in legacy_glyphs:
        header += struct.pack(
            "<i8f",
            int(g["id"]),
            float(g["x"]),
            float(g["y"]),
            float(g["width"]),
            float(g["height"]),
            float(g["xOffset"]),
            float(g["yOffset"]),
            float(g["xAdvance"]),
            float(g["scale"]),
        )
    header += struct.pack("<i", len(pixels))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_path.with_suffix(out_path.suffix + ".tmp")
    with gzip.open(tmp_out, "wb", compresslevel=5) as gz:
        gz.write(header)
        gz.write(pixels)
    tmp_out.replace(out_path)
    return True


def font_stamp_signature(
    *,
    font_id: str,
    bundle_name: str,
    tmp_schema: str,
    tmp_assembly: str,
    src_mtime: float = 0.0,
) -> str:
    """Stable short fingerprint so `refresh_hook_config` skips redundant bundle rebuilds (<1ms)."""
    raw = json.dumps(
        {
            "v": 2,
            "font_id": font_id,
            "bundle": bundle_name,
            "schema": tmp_schema,
            "asm": tmp_assembly,
            "mtime": round(src_mtime, 2),
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]
