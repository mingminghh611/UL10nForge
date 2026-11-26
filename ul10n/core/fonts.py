"""Font catalog discovery & TMP AssetBundle selection for UL10nForge."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

DEFAULT_FONT_ID = "notoserif"

_BUILTIN_META: dict[str, dict[str, str]] = {
    "notoserif": {
        "name": "思源宋体",
        "tag": "经典衬线",
        "desc": "端庄沉稳，适合暗黑奇幻、悬疑恐怖与复古剧情",
        "preview_ttf": "NotoSerifCJKsc-Medium.ttf",
        "bundle_prefix": "notoserif_sdf",
        "ugui_font": "Microsoft YaHei",
    },
    "lxgwwenkai": {
        "name": "霞鹜文楷",
        "tag": "温润手写",
        "desc": "柔和自然、长文阅读舒适，适合视觉小说与剧情 RPG",
        "preview_ttf": "LXGWWenKai-Regular.ttf",
        "bundle_prefix": "lxgwwenkai_sdf",
        "ugui_font": "KaiTi",
    },
    "fusionpixel": {
        "name": "缝合像素",
        "tag": "复古点阵",
        "desc": "锐利 10px 点阵风格，适合像素独立与复古横版游戏",
        "preview_ttf": "fusion-pixel-10px-monospaced-zh_hans.ttf",
        "bundle_prefix": "fusionpixel_sdf",
        "ugui_font": "Microsoft YaHei",
    },
}


@dataclass(slots=True)
class FontOption:
    id: str
    name: str
    tag: str
    desc: str
    bundle_prefix: str
    ugui_font: str
    preview_ttf_path: Path | None
    versions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tag": self.tag,
            "desc": self.desc,
            "bundle_prefix": self.bundle_prefix,
            "ugui_font": self.ugui_font,
            "has_preview": self.preview_ttf_path is not None and self.preview_ttf_path.is_file(),
            "preview_url": f"/api/fonts/{self.id}/file" if self.preview_ttf_path else "",
            "versions": list(self.versions),
        }


def _load_manifest(app_root: Path) -> dict[str, Any]:
    mf = app_root / "fonts" / "TMP_Font_AssetBundles" / "manifest.json"
    if not mf.is_file():
        return {}
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_fonts(app_root: Path) -> list[FontOption]:
    """Return available fonts in display order (manifest + auto-discovered bundles)."""
    bundles_dir = app_root / "fonts" / "TMP_Font_AssetBundles"
    ttf_dir = app_root / "fonts" / "SimplifiedChinese"
    manifest = _load_manifest(app_root)
    mf_fonts: dict[str, Any] = manifest.get("fonts") or {}

    # Preserve manifest order, then append any auto-discovered *_sdf_u* prefix
    ordered_ids: list[str] = list(mf_fonts.keys()) or list(_BUILTIN_META.keys())
    if bundles_dir.is_dir():
        for p in sorted(bundles_dir.iterdir()):
            if p.is_file() and "_sdf_u" in p.name:
                fid = p.name.split("_sdf_u", 1)[0]
                if fid and fid not in ordered_ids:
                    ordered_ids.append(fid)

    options: list[FontOption] = []
    for fid in ordered_ids:
        meta = dict(_BUILTIN_META.get(fid) or {})
        meta.update({k: v for k, v in (mf_fonts.get(fid) or {}).items() if isinstance(v, str)})
        prefix = meta.get("bundle_prefix") or f"{fid}_sdf"
        versions: list[str] = []
        if bundles_dir.is_dir():
            for ver in ("u2019", "u2021", "u2022", "u6000"):
                if (bundles_dir / f"{prefix}_{ver}").is_file():
                    versions.append(ver)

        ttf_name = meta.get("preview_ttf") or ""
        ttf_path = (ttf_dir / ttf_name) if ttf_name and (ttf_dir / ttf_name).is_file() else None

        # Always keep built-in default even in test environments without local binary payloads
        if not versions and ttf_path is None and fid != DEFAULT_FONT_ID:
            continue

        options.append(
            FontOption(
                id=fid,
                name=meta.get("name") or fid,
                tag=meta.get("tag") or "自定义字体",
                desc=meta.get("desc") or "",
                bundle_prefix=prefix,
                ugui_font=meta.get("ugui_font") or "Microsoft YaHei",
                preview_ttf_path=ttf_path,
                versions=versions,
            )
        )

    if not options:
        m = _BUILTIN_META[DEFAULT_FONT_ID]
        options.append(
            FontOption(
                id=DEFAULT_FONT_ID,
                name=m["name"],
                tag=m["tag"],
                desc=m["desc"],
                bundle_prefix=m["bundle_prefix"],
                ugui_font=m["ugui_font"],
                preview_ttf_path=None,
                versions=[],
            )
        )
    return options


def get_font_option(app_root: Path, font_id: str | None) -> FontOption:
    opts = list_fonts(app_root)
    fid = (font_id or "").strip().lower()
    for opt in opts:
        if opt.id == fid:
            return opt
    return opts[0]


def resolve_bundle_file(app_root: Path, unity_version: str, font_id: str | None = None) -> tuple[Path | None, FontOption]:
    """Select the best matching Unity-version TMP SDF bundle for the chosen font."""
    from ul10n.core.game_detector import select_tmp_bundle_name

    opt = get_font_option(app_root, font_id)
    base_name = select_tmp_bundle_name(unity_version, font_prefix=opt.bundle_prefix)
    if not base_name:
        return None, opt

    bundles_dir = app_root / "fonts" / "TMP_Font_AssetBundles"
    exact = bundles_dir / base_name
    if exact.is_file():
        return exact, opt

    # Fallback gracefully across available versions for this font (prefer closest older/compatible)
    for ver in ("u2022", "u2021", "u2019", "u6000"):
        cand = bundles_dir / f"{opt.bundle_prefix}_{ver}"
        if cand.is_file():
            return cand, opt

    # Final fallback to default font bundle if selected font has no bundle files on disk
    if opt.id != DEFAULT_FONT_ID:
        fallback_name = select_tmp_bundle_name(unity_version, font_prefix=f"{DEFAULT_FONT_ID}_sdf")
        if fallback_name and (bundles_dir / fallback_name).is_file():
            return bundles_dir / fallback_name, opt
    return None, opt


def resolve_sdf_source_bundle(app_root: Path, font_id: str | None = None) -> tuple[Path | None, FontOption]:
    """Locate any on-disk SDF bundle for `font_id` (preferring u2019) to extract raw atlas/glyph data."""
    opt = get_font_option(app_root, font_id)
    bundles_dir = app_root / "fonts" / "TMP_Font_AssetBundles"
    for prefix in (opt.bundle_prefix, f"{DEFAULT_FONT_ID}_sdf"):
        for ver in ("u2019", "u2021", "u2022", "u6000"):
            cand = bundles_dir / f"{prefix}_{ver}"
            if cand.is_file():
                return cand, opt
    return None, opt
