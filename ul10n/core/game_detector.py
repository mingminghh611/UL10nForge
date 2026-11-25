"""Pure-Python Unity game detector (zero UnityPy dependency).

Inspects a game directory read-only to identify:
- Main executable & *_Data folder
- Architecture (x64 vs x86) from PE COFF header
- Scripting runtime (IL2CPP vs Mono)
- Unity version (e.g. 2022.3.15f1, 6000.0.23f1) from globalgamemanagers / data.unity3d / UnityPlayer.dll
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

from ul10n.core.models import GameInfo

_UNITY_VER_RE = re.compile(rb"\b((?:20\d{2}|6000|[45])\.\d+\.\d+[abfp]\d+)\b")
_IGNORE_EXES = frozenset({
    "unitycrashhandler64.exe",
    "unitycrashhandler32.exe",
    "unins000.exe",
    "uninstall.exe",
    "crashreportclient.exe",
})


def detect_pe_arch(exe_path: Path) -> str:
    """Read Windows PE COFF machine header to distinguish x64 from x86."""
    try:
        with exe_path.open("rb") as f:
            dos = f.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return "x64"
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            f.seek(pe_offset)
            sig_and_machine = f.read(6)
            if len(sig_and_machine) < 6 or sig_and_machine[:4] != b"PE\x00\x00":
                return "x64"
            machine = struct.unpack_from("<H", sig_and_machine, 4)[0]
            if machine == 0x014C:
                return "x86"
            if machine == 0x8664:
                return "x64"
    except OSError:
        pass
    return "x64"


def _probe_unity_version(game_dir: Path, data_dir: Path | None) -> str:
    candidates: list[Path] = []
    if data_dir:
        candidates.extend([
            data_dir / "globalgamemanagers",
            data_dir / "mainData",
            data_dir / "data.unity3d",
            data_dir / "level0",
        ])
    candidates.append(game_dir / "UnityPlayer.dll")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as f:
                chunk = f.read(256 * 1024)
            m = _UNITY_VER_RE.search(chunk)
            if m:
                return m.group(1).decode("ascii", errors="ignore")
        except OSError:
            continue
    return ""


def _find_unity_layout(game_dir: Path) -> tuple[Path | None, Path | None]:
    """Locate (executable, *_Data) inside game_dir."""
    data_dirs = sorted(
        (p for p in game_dir.iterdir() if p.is_dir() and p.name.endswith("_Data")),
        key=lambda p: p.name.casefold(),
    )
    if data_dirs:
        data_dir = data_dirs[0]
        stem = data_dir.name[:-5]
        exe = game_dir / f"{stem}.exe"
        if exe.is_file():
            return exe, data_dir
        # Fallback: pick any non-crash-handler .exe in game_dir
        for cand in sorted(game_dir.glob("*.exe")):
            if cand.name.casefold() not in _IGNORE_EXES:
                return cand, data_dir
        return None, data_dir

    # Not a standard Unity layout — still locate main exe if present
    for cand in sorted(game_dir.glob("*.exe")):
        if cand.name.casefold() not in _IGNORE_EXES:
            return cand, None
    return None, None


_SKIP_SEARCH_DIRS = frozenset({
    "bepinex", "melonloader", "dotnet", "mods", "userlibs",
    "monobleedingedge", "mono", "__macosx", ".git",
})


def _has_unity_markers(folder: Path) -> bool:
    if (folder / "UnityPlayer.dll").is_file():
        return True
    try:
        return any(p.is_dir() and p.name.endswith("_Data") for p in folder.iterdir())
    except OSError:
        return False


def _resolve_player_root(root: Path, max_depth: int = 3) -> Path:
    """Resolve nested wrapper directories (e.g. `Flabby Pizza/Flabby Pizza/Flabby Pizza.exe`)."""
    if _has_unity_markers(root):
        return root
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        cur, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = sorted(
                (p for p in cur.iterdir() if p.is_dir() and p.name.casefold() not in _SKIP_SEARCH_DIRS and not p.name.endswith("_Data")),
                key=lambda p: p.name.casefold(),
            )
        except OSError:
            continue
        for child in children:
            if _has_unity_markers(child):
                return child
            queue.append((child, depth + 1))
    return root


_PROTECTION_PATTERNS = (
    ("easyanticheat", "EasyAntiCheat"),
    ("battleye", "BattlEye"),
    ("beclient", "BattlEye"),
    ("gameguard", "nProtect GameGuard"),
    ("nprotect", "nProtect"),
    ("ace-base", "ACE 反作弊"),
    ("mhyprot", "反作弊保护"),
    ("vgk", "Vanguard"),
    ("denuvo", "Denuvo"),
)


def detect_protection_markers(root: Path) -> str:
    """Detect known anti-cheat or anti-tamper modules in a game folder (pure read-only warning)."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return ""
    for p in entries:
        low = p.name.casefold()
        for pat, label in _PROTECTION_PATTERNS:
            if pat in low:
                return f"检测到保护/反作弊模块 {p.name}（{label}），游戏可能禁止加载第三方汉化插件"
        if p.is_file() and low.endswith("protect.dll"):
            return f"检测到保护模块 {p.name}，游戏可能禁止加载第三方汉化插件"
    return ""


def detect_game(game_dir: str | Path) -> GameInfo:
    """Inspect a game folder (auto-descending nested wrappers) and return Unity metadata."""
    selected = Path(game_dir).resolve()
    if not selected.is_dir():
        return GameInfo(game_dir=str(selected), name=selected.name, is_unity=False)

    root = _resolve_player_root(selected)
    exe, data_dir = _find_unity_layout(root)
    is_unity = data_dir is not None or (root / "UnityPlayer.dll").is_file()

    # Determine runtime (IL2CPP vs Mono)
    runtime_type = "unknown"
    if (root / "GameAssembly.dll").is_file() or (
        data_dir is not None and (data_dir / "il2cpp_data").is_dir()
    ):
        runtime_type = "il2cpp"
    elif (
        (root / "MonoBleedingEdge").is_dir()
        or (root / "Mono").is_dir()
        or (data_dir is not None and (data_dir / "Managed").is_dir())
    ):
        runtime_type = "mono"

    arch = detect_pe_arch(exe) if exe and exe.is_file() else "x64"
    unity_version = _probe_unity_version(root, data_dir) if is_unity else ""
    name = exe.stem if exe else root.name
    protection_warning = detect_protection_markers(root) if is_unity else ""

    return GameInfo(
        game_dir=str(root),
        name=name,
        executable=str(exe) if exe else "",
        data_dir=str(data_dir) if data_dir else "",
        runtime_type=runtime_type,
        arch=arch,
        unity_version=unity_version,
        is_unity=is_unity,
        recommended_loader="bepinex",
        protection_warning=protection_warning,
    )


# Exact Unity build versions of the shipped TMP SDF AssetBundles (sorted newest -> oldest).
# Unity AssetBundles are forward-compatible (game_version >= bundle_version), never backward-compatible.
_TMP_BUNDLES_BY_MIN_VERSION: tuple[tuple[tuple[int, int, int], str], ...] = (
    ((6000, 3, 0), "notoserif_sdf_u6000"),
    ((2022, 3, 61), "notoserif_sdf_u2022"),
    ((2021, 3, 33), "notoserif_sdf_u2021"),
    ((2019, 1, 0), "notoserif_sdf_u2019"),
)


def _parse_unity_tuple(unity_version: str) -> tuple[int, int, int] | None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", (unity_version or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def select_tmp_bundle_name(unity_version: str) -> str:
    """Pick the newest pre-built TMP SDF AssetBundle whose build version <= game's Unity version.

    Returns empty string for Unity < 2019.1.0 (e.g. Unity 5.x/2017/2018) so we never feed
    an incompatible Unity 2019+ AssetBundle into an older engine.
    """
    ver = _parse_unity_tuple(unity_version)
    if ver is None:
        return "notoserif_sdf_u2019"
    if ver < (2019, 1, 0):
        return ""
    for min_ver, name in _TMP_BUNDLES_BY_MIN_VERSION:
        if ver >= min_ver:
            return name
    return ""
