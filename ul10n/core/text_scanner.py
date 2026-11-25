"""Strict, read-only pure-static UI text scanner for Unity games.

Design principle — zero pollution:
- ONLY scans 100% player-facing static UI display components (`TextMeshProUGUI`,
  `TextMeshPro`, `TMP_Text`, `Text`, `TextMesh`) directly from Unity scene/asset files.
- Does NOT blindly scrape custom MonoBehaviours, ScriptableObjects, arbitrary TextAssets,
  Assembly-CSharp.dll, or IL2CPP global-metadata.dat. All dynamic, dialogue, scripted,
  and runtime-assembled strings are left to XUnity.AutoTranslator's mature runtime Hook.
- Never modifies any original file in `game_dir`.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ul10n.core.quality import is_translatable

# Strictly Unity native display text components — nothing else
_DISPLAY_SCRIPTS = frozenset({
    "TextMeshProUGUI",
    "TextMeshPro",
    "TMP_Text",
    "Text",
    "TextMesh",
})

_ASSET_PATTERNS = (
    "globalgamemanagers",
    "globalgamemanagers.assets",
    "level*",
    "sharedassets*.assets",
    "resources.assets",
    "data.unity3d",
)

_CODE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:@#$-]+$")
_CAMEL_IDENT_RE = re.compile(r"^[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*$|^[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]+$")
_ASSEMBLY_QUALIFIED_RE = re.compile(
    r",\s*(?:Assembly-CSharp|UnityEngine|Unity\.|mscorlib|System\b|netstandard|Newtonsoft)",
    re.IGNORECASE,
)
_UNITY_INTERNAL_RE = re.compile(
    r"^(?:"
    r"UnityEngine\.|UnityEditor\.|Unity\.|System\.|TMPro\.|"
    r"Assets/|Resources/|Prefabs/|Sounds/|Audio/|Sprites/|Textures/|Shaders/|Materials/|Animations?/|Scenes?/|Fonts\s*&\s*Materials/|"
    r"Hidden/|Standard\b|Universal Render Pipeline|Sprites/Default|"
    r"\[[A-Za-z0-9_]+\.[A-Za-z0-9_]+\(\)\]|"
    r"\[(?:DEBUG|INFO|WARN|ERROR|TRACE|StyleSelectorPart)[:\s\]]|"
    r"-\[(?:stringId|debug)\b|"
    r"<map\s+value=|\^\(|yyyy['\-]MM|"
    r"[A-Za-z0-9_]+_[A-Za-z0-9_]+(?:[:\s]|$)|"
    r"Mouse [XY]|Mouse ScrollWheel|Fire[123]|Horizontal|Vertical|Submit|Cancel|"
    r"Example\s*<sprite=|The\s*<(?:#|color=)[^>]+>count is:|"
    r"Lorem ipsum\b|Some random text\b|"
    r"http://|https://|www\."
    r")",
    re.IGNORECASE,
)
_DEBUG_LOG_RE = re.compile(
    r"(?:"
    r"NullReferenceException|ArgumentException|InvalidOperationException|StackTrace|"
    r"LiberationSans SDF|\(Instance\)$|\(Clone\)$|"
    r"\.(?:cs|dll|exe|png|jpg|jpeg|wav|ogg|mp3|prefab|asset|mat|shader|fbx|ttf|otf|json|xml|txt)$"
    r")",
    re.IGNORECASE,
)
_PLACEHOLDER_DEFAULT_TEXTS = frozenset({
    "New Text",
    "Text",
    "Button",
    "Label",
    "Toggle",
    "Placeholder",
    "Enter text...",
    "Option A",
    "Option B",
    "Option C",
    "Dropdown",
    "Sample Text",
    "Lorem ipsum",
    "Highlighted",
    "Pressed",
    "Selected",
    "Disabled",
    "ITEM NAME",
    "A) New Label",
    "B) New Label",
    "C) New Label",
    "D) New Label",
})
_HTML_TAG_STRIP_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_VOWELS = frozenset("aeiouyAEIOUY")


@dataclass
class StaticScanReport:
    """Breakdown of read-only pure-static UI scan results."""

    texts: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.texts)


def _has_binary_or_surrogate(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF or code == 0xFFFD:
            return True
        if code < 32 and ch not in ("\n", "\r", "\t"):
            return True
    return False


_COMMON_EN_WORDS = frozenset({
    "the", "to", "of", "and", "a", "in", "is", "it", "you", "that", "for", "on",
    "are", "with", "as", "be", "at", "this", "have", "from", "or", "by", "not",
    "but", "your", "all", "can", "my", "we", "do", "will", "no", "if", "so", "up",
})


def _looks_like_keyboard_gibberish(plain: str) -> bool:
    tokens = re.findall(r"[A-Za-z]+", plain)
    if len(tokens) < 6:
        return False
    no_vowel = sum(1 for w in tokens if len(w) >= 3 and not any(c in _VOWELS for c in w))
    if no_vowel / len(tokens) >= 0.35:
        return True
    weird_clusters = sum(1 for w in tokens if re.search(r"[bcdfghjklmnpqrstvwxz]{4,}", w, re.I))
    if weird_clusters / len(tokens) >= 0.35:
        return True
    # Lowercase multi-word key-mash with zero common English words & high awkward cluster ratio
    if len(tokens) >= 8 and sum(1 for w in tokens if w.islower()) / len(tokens) >= 0.85:
        if not any(w in _COMMON_EN_WORDS for w in tokens):
            odd = sum(
                1 for w in tokens
                if (len(w) == 1 and w not in ("a", "i"))
                or re.search(r"[bcdfghjklmnpqrstvwxz]{3,}|[jqxz][bcdfghjklmnpqrstvwxz]|[bcdfghjklmnpqrstvwxz][jqxz]", w)
            )
            if odd / len(tokens) >= 0.30:
                return True
    return False


def _scan_length_prefixed_utf8(raw: bytes) -> list[str]:
    """Extract 4-byte aligned length-prefixed UTF-8 strings from a UI component body."""
    out: list[str] = []
    n = len(raw)
    i = 28  # skip m_GameObject(12) + m_Enabled(4) + m_Script(12)
    while i + 4 <= n:
        ln = struct.unpack_from("<I", raw, i)[0]
        if 2 <= ln <= 1500 and i + 4 + ln <= n:
            chunk = raw[i + 4 : i + 4 + ln]
            try:
                text = chunk.decode("utf-8", errors="strict")
                if not _has_binary_or_surrogate(text):
                    stripped = text.strip()
                    if stripped and any(c.isalpha() for c in stripped):
                        out.append(stripped)
                    i += 4 + ((ln + 3) & ~3)
                    continue
            except UnicodeDecodeError:
                pass
        i += 4
    return out


def _looks_like_noise_display(text: str) -> bool:
    """Filter non-display identifiers, placeholders, gibberish, and engine noise."""
    s = text.strip()
    if len(s) < 2 or len(s) > 1500 or _has_binary_or_surrogate(s) or not is_translatable(s):
        return True
    if _ASSEMBLY_QUALIFIED_RE.search(s) or _UNITY_INTERNAL_RE.search(s) or _DEBUG_LOG_RE.search(s):
        return True
    plain = _HTML_TAG_STRIP_RE.sub(" ", s).strip()
    if not plain or _looks_like_keyboard_gibberish(plain):
        return True
    if len(plain) >= 5 and len(set(plain.casefold())) <= 2:
        return True
    # Reject single code identifiers (e.g. "UI_Button_01", "SetQuality")
    if " " not in s and "\n" not in s and _CODE_TOKEN_RE.match(s):
        if "_" in s or "/" in s or "." in s or ":" in s or "\\" in s:
            return True
        if _CAMEL_IDENT_RE.match(s):
            return True
        if len(s) >= 16 and not s.isalpha():
            return True
    if s in _PLACEHOLDER_DEFAULT_TEXTS or plain in _PLACEHOLDER_DEFAULT_TEXTS:
        return True
    return False


def _resolve_script_class(
    obj,
    env_by_name: dict[str, object],
    cache: dict[tuple[str, int, int], str],
) -> str:
    """Return `m_ClassName` for a MonoBehaviour via its `m_Script` PPtr."""
    raw = obj.get_raw_data()
    if not raw or len(raw) < 28:
        return ""
    fid, pid = struct.unpack_from("<iq", raw, 16)
    sf = obj.assets_file
    sf_name = Path(getattr(sf, "name", "") or "").name.casefold()
    key = (sf_name, fid, pid)
    if key in cache:
        return cache[key]

    cls_name = ""
    try:
        target_sf = None
        if fid == 0:
            target_sf = sf
        elif 0 < fid <= len(sf.externals):
            ext_name = Path(sf.externals[fid - 1].path).name.casefold()
            target_sf = env_by_name.get(ext_name)
        if target_sf is not None:
            ms = target_sf.objects.get(pid)
            if ms is not None and ms.type.name == "MonoScript":
                cls_name = str(getattr(ms.read(), "m_ClassName", "") or "")
    except Exception:
        cls_name = ""
    cache[key] = cls_name
    return cls_name


def scan_unity_static_texts_detailed(
    game_dir: str | Path,
    *,
    max_entries: int = 2000,
) -> StaticScanReport:
    """Strictly read-only scan of 100%-safe pure static Unity UI text components.

    Only extracts display strings from `TextMeshProUGUI`, `TextMeshPro`, `TMP_Text`,
    `Text`, and `TextMesh`. Everything dynamic/scripted is delegated to XUAT's Hook.
    """
    report = StaticScanReport(by_source={"ui": 0})
    seen: set[str] = set()

    def _add(text: str) -> bool:
        if len(report.texts) >= max_entries:
            return False
        s = text.strip()
        if not s or s in seen or _looks_like_noise_display(s):
            return True
        seen.add(s)
        report.texts.append(s)
        report.by_source["ui"] += 1
        return True

    root = Path(game_dir).resolve()
    data_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.endswith("_Data")),
        key=lambda p: p.name.casefold(),
    )
    if not data_dirs:
        return report
    data_dir = data_dirs[0]

    try:
        import UnityPy  # noqa: PLC0415
    except ImportError:
        return report

    files: list[Path] = []
    for pat in _ASSET_PATTERNS:
        files.extend(
            p for p in data_dir.glob(pat)
            if p.is_file() and not p.name.endswith((".resS", ".resource"))
        )
    if not files:
        return report

    try:
        env = UnityPy.load(*[str(f) for f in sorted(set(files))])
    except Exception:
        return report

    env_by_name = {Path(k).name.casefold(): af for k, af in env.files.items()}
    script_cache: dict[tuple[str, int, int], str] = {}

    for obj in env.objects:
        if len(report.texts) >= max_entries:
            break
        otype = obj.type.name

        # Native Unity TextMesh component (non-MonoBehaviour)
        if otype == "TextMesh":
            try:
                tree = obj.read_typetree()
                val = tree.get("m_Text") or ""
                if isinstance(val, str):
                    _add(val)
            except Exception:
                pass
            continue

        if otype != "MonoBehaviour":
            continue

        # Path 1: Embedded typetree with explicit UI m_text / m_Text field
        st = getattr(obj, "serialized_type", None)
        node = getattr(st, "node", None) if st else None
        if node is not None:
            child_names = {c.m_Name for c in getattr(node, "m_Children", []) or []}
            if "m_text" in child_names or "m_Text" in child_names:
                try:
                    tree = obj.read_typetree()
                    val = tree.get("m_text") or tree.get("m_Text") or ""
                    if isinstance(val, str):
                        _add(val)
                except Exception:
                    pass
                continue

        # Path 2: Stripped build — ONLY known Unity UI display components
        cls_name = _resolve_script_class(obj, env_by_name, script_cache)
        if cls_name not in _DISPLAY_SCRIPTS:
            continue
        raw = obj.get_raw_data()
        if not raw:
            continue
        for s in _scan_length_prefixed_utf8(raw):
            if not _add(s):
                break

    return report


def scan_unity_display_texts(game_dir: str | Path, *, max_entries: int = 2000) -> list[str]:
    """Read-only pure-static UI scan returning deduplicated display strings."""
    return scan_unity_static_texts_detailed(game_dir, max_entries=max_entries).texts
