from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from hanhua.core.models import FontConfig
from hanhua.core.placeholders import extract_placeholders


# 运行时插件字体源（2026-08-18 单字体收敛，用户指令）：
# - 只保留唯一字体 Noto Serif CJK SC Medium（宋体中等字重，与
#   SDF_Font_Asset/NotoSerifCJKsc-Medium SDF.asset 同源同字重）；
# - 2026-09-04 D1 复发根治：字体源从 .otf（CFF 轮廓）切换为 .ttf
#   （真 TrueType glyf，scripts/convert_cff_to_ttf.py 确定性转换生成，
#   cmap/度量与 OTF 逐字段一致）——插件 `new Font(fontPath)` 按
#   TrueType 解析 CFF 会缺字（口口口），2026-08-18 收敛时把唯一字体
#   源换成 CFF OTF 导致 D1 复发，现在白名单只接受真 TrueType；
# - 历史 SourceHanSansSC（思源黑体）系列弃用，兼容别名映射到新字体。
FONT_OPTIONS = {
    "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf": "Noto Serif CJK SC",
}

# 旧项目库兼容：历史 store 配置的 filename 指向已弃用/已替换字体
# （Rendezvous 实证：默认值/旧项目一直部署过期字体）。安装前规范化到
# 新字体——旧库自动更正线路，不再使用过期字体。2026-09-04：旧 OTF
# 默认路径也映射到 TTF（同族同字重，CFF 不得再作插件部署源）。
_LEGACY_FONT_ALIASES = {
    "SimplifiedChinese/NotoSerifCJKsc-Medium.otf":
        "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf",
    "SimplifiedChinese/SourceHanSansSC-Regular.otf":
        "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf",
    "SimplifiedChinese/SourceHanSansSC-Medium.otf":
        "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf",
    "SimplifiedChinese/SourceHanSansSC-Medium.ttf":
        "SimplifiedChinese/NotoSerifCJKsc-Medium.ttf",
}


def _normalize_font_filename(filename: str) -> str:
    """旧字体路径 → 新字体路径（兼容映射）；未知路径原样返回（由
    FONT_OPTIONS 白名单在调用方拒绝）。"""
    return _LEGACY_FONT_ALIASES.get(filename, filename)

_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    *(f"lpt{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
}

_FONT_HEALTH_PROTOCOL_VERSION = 5
_FONT_HEALTH_PLUGIN_VERSION = "1.5.0"
_FONT_HEALTH_MAX_BYTES = 64 * 1024
# Phase 3：逐 scalar 证明。health 文件超过该时长未刷新 → 陈旧 attestation
# （STALE_RUNTIME_ATTESTATION 语义）：插件可能已退出/卡死/被删。
_FONT_HEALTH_MAX_AGE_SECONDS = 12 * 3600
# 明细上限：总数必须完整（恒等式），明细截断防膨胀
_FONT_HEALTH_MAX_DETAIL = 256
_FONT_HEALTH_FAILURE_KEYS = frozenset({
    "stable_identity", "kind", "font_asset", "missing",
})
_FONT_HEALTH_GLYPH_VERIFICATION_KEYS = frozenset({
    "snapshot_hash", "legacy_total", "legacy_covered", "legacy_missing",
    "tmp_total", "tmp_covered", "tmp_missing", "missing_codepoints", "error",
})
_FONT_HEALTH_CONSUMERS_KEYS = frozenset({
    "discovered", "chinese", "covered", "missing", "failed",
})

# BepInEx 发行包自带的根级文档文件：部署时跳过，绝不写入副本。
# 它们与游戏自带的同名文件（如 Changelog.txt）在 Windows 大小写不敏感
# 文件系统上冲突，覆盖会破坏原游戏文件（containment-breach 实测根因）。
_RUNTIME_ROOT_DOCS = frozenset({
    "changelog", "changelog.txt", "changelog.md", "changelog.rst",
    "readme", "readme.txt", "readme.md", "readme.rst",
    "license", "license.txt", "license.md", "licence", "licence.txt",
    "notice", "notice.txt", "authors", "authors.txt", "copying",
    "copying.txt", "copying.md",
})
_RUNTIME_TEMPLATE_SLOT = re.compile(
    r"(?:\{[a-zA-Z0-9_.\-]+(?:,-?\d+)?(?::[^{}\r\n]+)?\}"
    r"|%[-+0-9.l]*[a-zA-Z])"
)


class FontInstallError(RuntimeError):
    pass


class UnsupportedFontProvider(FontInstallError):
    pass


@dataclass(frozen=True)
class FontRuntimeAssets:
    fonts_dir: Path
    runtime_zip: Path
    plugin_dll: Path
    expected_runtime_sha256: str | None = None
    expected_runtime_size: int | None = None
    runtime_x86_zip: Path | None = None
    expected_x86_sha256: str | None = None
    expected_x86_size: int | None = None


@dataclass(frozen=True)
class FontProviderCapability:
    provider_id: str
    runtime: str
    architecture: str
    provider_supported: bool
    payload_available: bool
    payload_deployed: bool = False
    runtime_verified: bool = False
    reason: str = ""
    static_writeback_allowed: bool = False


@dataclass(frozen=True)
class FontHealthResult:
    runtime_verified: bool
    reason: str = ""


@dataclass(frozen=True)
class FontInstallResult:
    installed: bool
    filename: str = ""
    family: str = ""
    payload_deployed: bool | None = None
    runtime_verified: bool = False
    architecture: str = ""
    provider_supported: bool = True
    unsupported_reason: str = ""
    cleanup_pending: str = ""
    provider_id: str = ""
    payload_available: bool = False
    #: 本次发布实际渲染字形需求集（静态替换分支附带，Phase 1；
    #: 动态分支留空——字形由运行时插件承担，Phase 2 覆盖验证入口）
    required_glyphs: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if self.payload_deployed is None:
            object.__setattr__(self, "payload_deployed", self.installed)


def _placeholder_fragments(text: str, placeholders: list[str]) -> list[str] | None:
    fragments: list[str] = []
    cursor = 0
    for placeholder in placeholders:
        position = text.find(placeholder, cursor)
        if position < 0:
            return None
        fragments.append(text[cursor:position])
        cursor = position + len(placeholder)
    fragments.append(text[cursor:])
    return fragments


def _has_sufficient_runtime_anchor(fragments: list[str]) -> bool:
    """Reject templates whose capture can consume nearly the whole string."""
    alnum_count = sum(
        char.isalnum() for fragment in fragments for char in fragment)
    has_slot_boundary = any(
        (index < len(fragments) - 1 and fragment
         and not fragment[-1].isalnum())
        or (index > 0 and fragment and not fragment[0].isalnum())
        for index, fragment in enumerate(fragments)
    )
    return alnum_count >= 2 and has_slot_boundary


def _runtime_template_payload(translations: dict[str, str]) -> bytes:
    templates: list[dict[str, object]] = []
    signatures: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for source, target in sorted(translations.items()):
        source_slots = extract_placeholders(source)
        if (not source_slots
                or any(not _RUNTIME_TEMPLATE_SLOT.fullmatch(slot)
                       for slot in source_slots)
                or source_slots != extract_placeholders(target)):
            continue
        source_fragments = _placeholder_fragments(source, source_slots)
        target_fragments = _placeholder_fragments(target, source_slots)
        if source_fragments is None or target_fragments is None:
            continue
        if not _has_sufficient_runtime_anchor(source_fragments):
            continue
        if len(source_slots) > 1 and any(
                not fragment for fragment in source_fragments[1:-1]):
            continue
        signature = (tuple(source_fragments), tuple(source_slots))
        if signature in signatures:
            continue
        signatures.add(signature)
        templates.append({
            "slots": source_slots,
            "source_fragments": source_fragments,
            "target_fragments": target_fragments,
        })
    return (json.dumps(
        {"schema_version": 1, "templates": templates},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")


def _required_glyphs_payload(
        translations: dict[str, str]) -> tuple[bytes, str]:
    """从译文集合提取运行时字形需求集 → (required-glyphs.json 字节, hash)。

    Phase 3：插件逐 scalar 验证输入。码点 = 全部译文文本的非空白标量
    （静态已写回部分不需要运行时字形，但动态兜底按全集证明最保守——
    多余码点只是多验证，不缺）。snapshot_hash 是码点列表的稳定指纹
    （ASCII 表示，跨端一致），插件回传、工具比对。
    """
    scalars = sorted({
        ord(character)
        for text in translations.values()
        for character in text
        if not character.isspace()
    })
    snapshot_hash = hashlib.sha256(
        ",".join(f"U+{scalar:04X}" for scalar in scalars).encode("ascii")
    ).hexdigest() if scalars else ""
    payload = (
        json.dumps(
            {"schema_version": 1, "snapshot_hash": snapshot_hash,
             "scalars": scalars},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
    ).encode("utf-8")
    return payload, snapshot_hash


def read_font_health(path: Path) -> FontHealthResult:
    """Validate runtime evidence emitted by the version-matched font plugin.

    Phase 3（协议 v5）：从「单个代表字探测」升级为「逐 scalar 证明」：
    - glyph_verification：需求集每个码点必须被 legacy 或 TMP 链覆盖；
      missing_codepoints 非空即失败（单个代表字通过、另一实际字缺失
      时必须失败）；插件扫描异常写入 error → 失败；
    - snapshot_hash 必须与同目录 required-glyphs.json 一致（插件用的
      需求集与本次部署相同）；
    - session_nonce + last_seen：会话标识与刷新时间；last_seen 超龄 →
      陈旧 attestation 拒绝（插件可能已退出/卡死/被删）；
    - consumers 会计恒等式：covered + missing + failed == chinese。
    """
    path = Path(path)
    try:
        if not path.is_file() or _path_is_link(path):
            return FontHealthResult(False, "font-health.json missing or unsafe")
        size = path.stat().st_size
        if size <= 0 or size > _FONT_HEALTH_MAX_BYTES:
            return FontHealthResult(False, "font-health.json size is invalid")
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return FontHealthResult(False, "font-health.json is not valid UTF-8 JSON")
    if not isinstance(payload, dict):
        return FontHealthResult(False, "font-health.json root must be an object")
    expected_keys = {
        "protocol_version", "plugin_version", "session_nonce", "last_seen",
        "scenes", "adapters", "glyph_probe", "applications",
        "translation_targets", "glyph_verification", "consumers", "failures",
    }
    if set(payload) != expected_keys:
        return FontHealthResult(False, "font-health.json field set is invalid")
    if (
        type(payload.get("protocol_version")) is not int
        or payload["protocol_version"] != _FONT_HEALTH_PROTOCOL_VERSION
    ):
        return FontHealthResult(False, "font-health protocol_version mismatch")
    if payload.get("plugin_version") != _FONT_HEALTH_PLUGIN_VERSION:
        return FontHealthResult(False, "font-health plugin_version mismatch")

    # Phase 3：会话标识 / 刷新时间 / 场景
    session_nonce = payload.get("session_nonce")
    if not isinstance(session_nonce, str) or not session_nonce.strip():
        return FontHealthResult(False, "font-health session_nonce is missing")
    last_seen = payload.get("last_seen")
    if type(last_seen) is not int or last_seen <= 0:
        return FontHealthResult(False, "font-health last_seen is invalid")
    if int(time.time()) - last_seen > _FONT_HEALTH_MAX_AGE_SECONDS:
        return FontHealthResult(False, "font-health attestation is stale")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or any(
            not isinstance(scene, str) for scene in scenes):
        return FontHealthResult(False, "font-health scenes are invalid")

    # Phase 3：逐 scalar 证明（实现重点：需求集每码点必须有覆盖链）
    verification = payload.get("glyph_verification")
    if not isinstance(verification, dict) or (
            set(verification) != _FONT_HEALTH_GLYPH_VERIFICATION_KEYS):
        return FontHealthResult(False, "font-health glyph verification is invalid")
    if verification.get("error"):
        return FontHealthResult(
            False, "font-health glyph verification reported errors")
    for key in ("legacy_total", "legacy_covered", "legacy_missing",
                "tmp_total", "tmp_covered", "tmp_missing"):
        if type(verification.get(key)) is not int or verification[key] < 0:
            return FontHealthResult(False, f"font-health {key} is invalid")
    for side in ("legacy", "tmp"):
        total = verification[f"{side}_total"]
        covered = verification[f"{side}_covered"]
        missing = verification[f"{side}_missing"]
        if covered + missing != total:
            return FontHealthResult(
                False, f"font-health {side} glyph counts are inconsistent")
    snapshot_hash = verification.get("snapshot_hash")
    if not isinstance(snapshot_hash, str) or not snapshot_hash:
        return FontHealthResult(False, "font-health snapshot_hash is missing")
    missing_codepoints = verification.get("missing_codepoints")
    if not isinstance(missing_codepoints, list) or any(
            type(codepoint) is not int or codepoint <= 0
            for codepoint in missing_codepoints):
        return FontHealthResult(False, "font-health missing codepoints are invalid")
    # 核心完成标准：任何一个需求码点缺失 → 证明失败（不能只验代表字）
    if missing_codepoints:
        return FontHealthResult(
            False,
            f"font-health missing codepoints: {len(missing_codepoints)}")
    # 部署需求集比对：插件回传 hash 必须等于同目录 required-glyphs.json
    required_glyphs_path = path.parent / "required-glyphs.json"
    try:
        if (not required_glyphs_path.is_file()
                or _path_is_link(required_glyphs_path)):
            return FontHealthResult(
                False, "required-glyphs.json deployment is missing")
        required_payload = json.loads(
            required_glyphs_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return FontHealthResult(False, "required-glyphs.json is invalid")
    if (not isinstance(required_payload, dict)
            or required_payload.get("snapshot_hash") != snapshot_hash):
        return FontHealthResult(
            False, "font-health snapshot_hash does not match deployment")

    # Phase 3：消费者统计（看见并覆盖中文消费者的证明，P0-6）
    consumers = payload.get("consumers")
    if not isinstance(consumers, dict) or (
            set(consumers) != _FONT_HEALTH_CONSUMERS_KEYS):
        return FontHealthResult(False, "font-health consumers are invalid")
    consumer_values = {
        key: consumers.get(key) for key in _FONT_HEALTH_CONSUMERS_KEYS}
    if any(type(value) is not int or value < 0
           for value in consumer_values.values()):
        return FontHealthResult(False, "font-health consumer counts are invalid")
    if (consumer_values["covered"] + consumer_values["missing"]
            + consumer_values["failed"] != consumer_values["chinese"]):
        return FontHealthResult(
            False, "font-health consumer counts are inconsistent")
    if consumer_values["chinese"] <= 0:
        return FontHealthResult(
            False, "font-health saw no chinese text consumers")
    if consumer_values["discovered"] < consumer_values["chinese"]:
        return FontHealthResult(
            False, "font-health discovered counts are inconsistent")
    failures = payload.get("failures")
    if not isinstance(failures, list) or len(failures) > _FONT_HEALTH_MAX_DETAIL:
        return FontHealthResult(False, "font-health failures are invalid")
    for failure in failures:
        if (not isinstance(failure, dict)
                or set(failure) != _FONT_HEALTH_FAILURE_KEYS
                or not isinstance(failure.get("stable_identity"), str)
                or not failure["stable_identity"]
                or not isinstance(failure.get("kind"), str)
                or not isinstance(failure.get("font_asset"), str)
                or not isinstance(failure.get("missing"), list)
                or any(type(codepoint) is not int
                       for codepoint in failure["missing"])):
            return FontHealthResult(False, "font-health failure record is invalid")
    if consumer_values["failed"] > 0 and not failures:
        return FontHealthResult(False, "font-health failures are missing")

    adapters = payload.get("adapters")
    adapter_names = {"legacy", "tmp", "uitoolkit"}
    if not isinstance(adapters, dict) or set(adapters) != adapter_names:
        return FontHealthResult(False, "font-health adapter set is invalid")
    adapter_usable: dict[str, bool] = {}
    for adapter_name in ("legacy", "tmp", "uitoolkit"):
        adapter = adapters.get(adapter_name)
        if (
            not isinstance(adapter, dict)
            or set(adapter) != {"status", "error", "glyph"}
            or not isinstance(adapter.get("status"), str)
            or not adapter.get("status")
            or not isinstance(adapter.get("error"), str)
            or type(adapter.get("glyph")) is not bool
        ):
            return FontHealthResult(
                False, f"font-health adapter {adapter_name} is invalid")
        adapter_ready = (
            adapter["status"] == "ready" and adapter["error"] == "")
        adapter_usable[adapter_name] = (
            adapter_ready and adapter["glyph"] is True)
    if not any(
            adapters[name]["status"] == "ready"
            and adapters[name]["error"] == ""
            for name in adapter_names):
        return FontHealthResult(False, "font-health adapter evidence is not ready")
    if not any(adapter_usable.values()):
        return FontHealthResult(False, "font-health glyph evidence is missing")

    glyph_probe = payload.get("glyph_probe")
    if not isinstance(glyph_probe, str) or not glyph_probe.strip():
        return FontHealthResult(False, "font-health glyph_probe is missing")
    applications = payload.get("applications")
    application_keys = {
        "tmp", "ui", "uitoolkit", "textmesh", "translations",
        "exact_translations", "normalized_translations",
        "template_translations",
    }
    if not isinstance(applications, dict) or set(applications) != application_keys:
        return FontHealthResult(False, "font-health application evidence is invalid")
    counts = tuple(applications[key] for key in sorted(application_keys))
    if any(type(value) is not int or value < 0 for value in counts):
        return FontHealthResult(False, "font-health application counts are invalid")
    if applications["translations"] != (
            applications["exact_translations"]
            + applications["normalized_translations"]
            + applications["template_translations"]):
        return FontHealthResult(
            False, "font-health translation counts are inconsistent")
    translation_targets = payload.get("translation_targets")
    target_names = {"tmp", "ui", "uitoolkit", "textmesh"}
    mode_names = {"exact", "normalized", "template"}
    if (not isinstance(translation_targets, dict)
            or set(translation_targets) != target_names):
        return FontHealthResult(
            False, "font-health translation target set is invalid")
    target_totals: dict[str, int] = {}
    mode_totals = {mode: 0 for mode in mode_names}
    for target_name in target_names:
        target = translation_targets.get(target_name)
        if (not isinstance(target, dict) or set(target) != mode_names
                or any(type(target.get(mode)) is not int
                       or target[mode] < 0 for mode in mode_names)):
            return FontHealthResult(
                False, f"font-health translation target {target_name} is invalid")
        target_totals[target_name] = sum(target.values())
        for mode in mode_names:
            mode_totals[mode] += target[mode]
    if (mode_totals["exact"] != applications["exact_translations"]
            or mode_totals["normalized"]
            != applications["normalized_translations"]
            or mode_totals["template"]
            != applications["template_translations"]):
        return FontHealthResult(
            False, "font-health translation target counts are inconsistent")
    if sum(counts) <= 0:
        return FontHealthResult(False, "font-health application evidence is empty")
    tmp_applied = applications["tmp"] > 0
    legacy_applied = applications["ui"] + applications["textmesh"] > 0
    ui_toolkit_applied = applications["uitoolkit"] > 0
    if not tmp_applied and not legacy_applied and not ui_toolkit_applied:
        return FontHealthResult(
            False, "font-health font application evidence is empty")
    if tmp_applied and not adapter_usable["tmp"]:
        return FontHealthResult(
            False, "font-health tmp application lacks usable glyph evidence")
    if legacy_applied and not adapter_usable["legacy"]:
        return FontHealthResult(
            False, "font-health legacy application lacks usable glyph evidence")
    if ui_toolkit_applied and not adapter_usable["uitoolkit"]:
        return FontHealthResult(
            False, "font-health uitoolkit application lacks usable glyph evidence")
    target_adapters = {
        "tmp": "tmp", "ui": "legacy", "uitoolkit": "uitoolkit",
        "textmesh": "legacy",
    }
    for target_name, adapter_name in target_adapters.items():
        if target_totals[target_name] > 0 and not adapter_usable[adapter_name]:
            return FontHealthResult(
                False,
                f"font-health {target_name} translation lacks usable glyph evidence",
            )
    return FontHealthResult(True)


def _default_assets() -> FontRuntimeAssets:
    project_root = Path(__file__).resolve().parents[2]
    resource_dir = project_root / "resources" / "font_override"
    payloads = _load_payload_manifest(resource_dir)
    x64 = payloads["x64"]
    x86 = payloads["x86"]
    return FontRuntimeAssets(
        fonts_dir=project_root / "fonts",
        runtime_zip=x64["path"],
        plugin_dll=resource_dir / "Hanhua.FontFallback.dll",
        expected_runtime_sha256=x64["sha256"],
        expected_runtime_size=x64["size"],
        runtime_x86_zip=x86["path"],
        expected_x86_sha256=x86["sha256"],
        expected_x86_size=x86["size"],
    )


def _load_payload_manifest(resource_dir: Path) -> dict[str, dict]:
    manifest_path = resource_dir / "BepInEx_payloads.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if set(manifest) != {
                "schema_version", "version", "source_release", "license_file",
                "required_members", "payloads"}:
            raise ValueError("unexpected manifest fields")
        if manifest["schema_version"] != 1 or manifest["version"] != "5.4.23.5":
            raise ValueError("unsupported payload manifest schema/version")
        if manifest["source_release"] != (
                "https://github.com/BepInEx/BepInEx/releases/tag/v5.4.23.5"):
            raise ValueError("payload source is not the pinned official release")
        license_name = manifest["license_file"]
        if Path(license_name).name != license_name or not (
                resource_dir / license_name).is_file():
            raise ValueError("missing pinned payload license")
        required = manifest["required_members"]
        if required != [
                "winhttp.dll", "doorstop_config.ini",
                "BepInEx/core/BepInEx.dll"]:
            raise ValueError("unexpected required payload members")
        records: dict[str, dict] = {}
        for item in manifest["payloads"]:
            if set(item) != {
                    "architecture", "filename", "download_url", "size", "sha256"}:
                raise ValueError("unexpected payload fields")
            arch = item["architecture"]
            if arch not in {"x86", "x64"} or arch in records:
                raise ValueError("duplicate or unsupported payload architecture")
            expected_name = f"BepInEx_win_{arch}_5.4.23.5.zip"
            if item["filename"] != expected_name:
                raise ValueError("payload filename/architecture mismatch")
            expected_url = (
                "https://github.com/BepInEx/BepInEx/releases/download/"
                f"v5.4.23.5/{expected_name}")
            if item["download_url"] != expected_url:
                raise ValueError("payload URL is not the pinned official asset")
            if (not isinstance(item["size"], int) or item["size"] <= 0
                    or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("invalid payload size/hash")
            path = resource_dir / expected_name
            if path.stat().st_size != item["size"]:
                raise ValueError(f"payload size mismatch: {expected_name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError(f"payload hash mismatch: {expected_name}")
            with zipfile.ZipFile(path) as archive:
                names = {name.replace("\\", "/").casefold()
                         for name in archive.namelist()}
                if archive.testzip() is not None or not {
                        name.casefold() for name in required} <= names:
                    raise ValueError(f"payload members invalid: {expected_name}")
            records[arch] = {**item, "path": path}
        if set(records) != {"x86", "x64"}:
            raise ValueError("both x86 and x64 payloads are required")
        return records
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError,
            zipfile.BadZipFile) as exc:
        raise FontInstallError(
            f"BepInEx payload manifest 无效: {manifest_path}: {exc}") from exc


def _validated_members(
    archive: zipfile.ZipFile,
    out_dir: Path,
) -> list[tuple[zipfile.ZipInfo, Path]]:
    output_root = out_dir.resolve()
    validated: list[tuple[zipfile.ZipInfo, Path]] = []
    member_types: dict[str, bool] = {}
    for member in archive.infolist():
        unix_mode = member.external_attr >> 16
        if stat.S_ISLNK(unix_mode):
            raise FontInstallError(f"运行时压缩包不能包含符号链接: {member.filename}")
        normalized = member.filename.replace("\\", "/")
        windows_path = PureWindowsPath(normalized)
        raw_parts = normalized.split("/")
        if member.is_dir() and raw_parts[-1:] == [""]:
            raw_parts.pop()
        unsafe_part = any(
            part in ("", ".", "..")
            or part.endswith((".", " "))
            or ":" in part
            or any(ord(char) < 32 or char in '<>"|?*' for char in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
            for part in raw_parts
        )
        if (
            not raw_parts
            or normalized.startswith("/")
            or windows_path.is_absolute()
            or windows_path.drive
            or unsafe_part
        ):
            raise FontInstallError(
                f"运行时压缩包包含不安全的 Windows 成员路径: {member.filename}"
            )
        member_key = "/".join(part.casefold() for part in raw_parts)
        if member_key in member_types:
            raise FontInstallError(
                f"运行时压缩包包含 Windows 路径重复或冲突: {member.filename}"
            )
        if any(
            (member_key.startswith(existing_key + "/") and not existing_is_dir)
            or (existing_key.startswith(member_key + "/") and not member.is_dir())
            for existing_key, existing_is_dir in member_types.items()
        ):
            raise FontInstallError(
                f"运行时压缩包包含文件/子路径拓扑冲突: {member.filename}"
            )
        member_types[member_key] = member.is_dir()
        target = output_root.joinpath(*raw_parts).resolve()
        try:
            target.relative_to(output_root)
        except ValueError as exc:
            raise FontInstallError(
                f"运行时压缩包包含不安全的成员路径: {member.filename}"
            ) from exc
        validated.append((member, target))
    return validated


def _detect_pe_architecture(executable: Path) -> str:
    try:
        with executable.open("rb") as stream:
            dos = stream.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                raise ValueError
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            stream.seek(pe_offset)
            header = stream.read(24)
            if len(header) != 24 or header[:4] != b"PE\0\0":
                raise ValueError
            coff = header[4:]
            (
                machine, _section_count, _timestamp, _symbol_table,
                _symbol_count, optional_size, characteristics,
            ) = struct.unpack("<HHIIIHH", coff)
            if optional_size < 0x70:
                raise ValueError
            optional = stream.read(optional_size)
            if len(optional) != optional_size:
                raise ValueError
            optional_magic = struct.unpack_from("<H", optional, 0)[0]
        if not characteristics & 0x0002:
            raise FontInstallError(
                f"Windows PE 未标记为可执行映像: {executable.name}")
        architecture = {
            (0x014C, 0x010B): "x86", (0x8664, 0x020B): "x64",
        }.get((machine, optional_magic))
        if architecture is None:
            raise FontInstallError(
                "游戏可执行文件 machine/optional magic 架构不匹配或不受支持: "
                f"{executable.name} (machine=0x{machine:04X}, "
                f"magic=0x{optional_magic:04X})")
        return architecture
    except (OSError, struct.error, ValueError) as exc:
        raise FontInstallError(
            f"无法解析 Windows PE 游戏可执行文件: {executable.name}") from exc


def _mono_managed_core(managed_dir: Path) -> bool:
    """Unity Mono 托管核心 DLL：Unity 5+ 拆分为 UnityEngine.CoreModule.dll，
    Unity 4.x 老结构仍为整体 UnityEngine.dll（222am 实证，无 CoreModule、
    UnityScript/Boo.Lang 特征）。UnityEngine.dll 只存在于 Unity Mono 构建，
    不会误判非 Unity 游戏。"""
    return (
        (managed_dir / "UnityEngine.CoreModule.dll").is_file()
        or (managed_dir / "UnityEngine.dll").is_file()
    )


def _detect_mono_architecture(game_dir: Path) -> str:
    if game_dir.is_dir() and any(
        child.is_file() and child.name.casefold() == "gameassembly.dll"
        for child in game_dir.iterdir()
    ):
        raise UnsupportedFontProvider(
            "不支持 IL2CPP 字体 provider；静态汉化流程仍可继续"
        )
    executables = sorted(
        (
            child
            for child in game_dir.glob("*.exe")
            if not child.name.casefold().startswith("unitycrashhandler")
        ),
        key=lambda path: path.name.casefold(),
    )
    if not executables:
        raise FontInstallError("游戏根目录中未找到 Unity 游戏可执行文件")
    # 标准 *_Data/Managed 与扁平布局并存支持：老 Unity standalone/
    # WebGL 导出的 Data 内容直接散根目录（hotel-paradise 实证：
    # 「HotelParadise v1.1 WIN.exe」+ 根目录 Managed/Mono/），
    # 无同名 *_Data 宿主目录。
    unity_executables = [
        executable
        for executable in executables
        if (
            _mono_managed_core(
                game_dir / f"{executable.stem}_Data" / "Managed")
            or _mono_managed_core(game_dir / "Managed")
        )
    ]
    if not unity_executables:
        raise FontInstallError(
            "Unity Mono 游戏结构不完整：需要同名 *_Data/Managed/"
            "UnityEngine.CoreModule.dll"
        )
    # MonoBleedingEdge 不是硬性要求：Unity 5.x 老游戏无该目录（Mono
    # 内嵌 UnityPlayer.dll），BepInEx 5.x 同样支持（foxhunt/tiiny-ragdoll 实测）。
    errors: list[FontInstallError] = []
    architectures: set[str] = set()
    for executable in unity_executables:
        try:
            architectures.add(_detect_pe_architecture(executable))
        except FontInstallError as exc:
            errors.append(exc)
    if len(architectures) == 1:
        return next(iter(architectures))
    if len(architectures) > 1:
        raise FontInstallError("检测到混合 x86/x64 Unity player，无法安全选择字体载荷")
    if len(errors) == 1:
        raise errors[0]
    raise FontInstallError(
        "没有找到有效的 Windows Mono Unity 可执行文件："
        + "; ".join(str(error) for error in errors)
    )


def _detect_player_architecture(game_dir: Path) -> str:
    architectures = set()
    executables = sorted(
        game_dir.glob("*.exe"), key=lambda path: path.name.casefold())
    for executable in executables:
        if executable.name.casefold().startswith("unitycrashhandler"):
            continue
        try:
            architectures.add(_detect_pe_architecture(executable))
        except FontInstallError:
            continue
    return next(iter(architectures)) if len(architectures) == 1 else "unknown"


def _font_payload_deployed(game_dir: Path) -> bool:
    required = (
        game_dir / "winhttp.dll",
        game_dir / "doorstop_config.ini",
        game_dir / "BepInEx" / "core" / "BepInEx.dll",
        game_dir / "BepInEx" / "plugins" / "HanhuaFont"
        / "Hanhua.FontFallback.dll",
        game_dir / "BepInEx" / "plugins" / "HanhuaFont" / "font.ttf",
    )
    return all(path.is_file() and not _path_is_link(path) for path in required)


def _resolve_player_root(game_dir: Path, player_root: Path | None) -> Path:
    source_root = Path(game_dir).resolve()
    selected = (
        source_root
        if player_root is None
        else (Path(player_root) if Path(player_root).is_absolute()
              else source_root / player_root).resolve()
    )
    try:
        selected.relative_to(source_root)
    except ValueError as exc:
        raise FontInstallError("Unity player root 必须位于游戏源目录内") from exc
    if not selected.is_dir() or _path_is_link(selected):
        raise FontInstallError("Unity player root 不存在或是不安全的链接")
    return selected


def resolve_font_provider(
    game_dir: Path,
    runtime: str,
    *,
    player_root: str | Path | None = None,
    assets: FontRuntimeAssets | None = None,
) -> FontProviderCapability:
    """Return an explicit, non-deploying runtime-font capability."""
    try:
        game_dir = _resolve_player_root(game_dir, player_root)
    except FontInstallError as exc:
        return FontProviderCapability(
            "unsupported_invalid_player_root", runtime, "unknown", False,
            False, reason=str(exc),
        )
    if runtime == "il2cpp":
        architecture = _detect_player_architecture(game_dir)
        if architecture == "x64":
            return FontProviderCapability(
                "bepinex6_il2cpp_x64", runtime, architecture, False, False,
                reason=("IL2CPP x64 字体 provider 未提供经验证的 "
                        "BepInEx 6/Il2CppInterop 载荷"),
                static_writeback_allowed=True,
            )
        return FontProviderCapability(
            f"unsupported_il2cpp_{architecture}", runtime, architecture,
            False, False,
            reason=f"IL2CPP {architecture} 字体 provider 未经验证",
            static_writeback_allowed=True,
        )
    if runtime != "mono":
        architecture = _detect_player_architecture(game_dir)
        return FontProviderCapability(
            "unsupported_unknown_runtime", runtime, architecture, False, False,
            reason=f"unknown runtime/architecture ({runtime}/{architecture}) 无字体 provider",
        )
    try:
        architecture = _detect_mono_architecture(game_dir)
    except FontInstallError as exc:
        return FontProviderCapability(
            "unsupported_mono_unknown", runtime, "unknown", False, False,
            reason=str(exc),
        )
    try:
        selected_assets = assets or _default_assets()
    except FontInstallError as exc:
        return FontProviderCapability(
            f"bepinex5_mono_{architecture}", runtime, architecture, True, False,
            reason=str(exc),
        )
    runtime_zip = (
        selected_assets.runtime_x86_zip
        if architecture == "x86" else selected_assets.runtime_zip
    )
    payload_available = bool(
        runtime_zip is not None
        and runtime_zip.is_file()
        and selected_assets.plugin_dll.is_file()
    )
    payload_deployed = _font_payload_deployed(game_dir)
    health = read_font_health(
        game_dir / "BepInEx" / "plugins" / "HanhuaFont"
        / "font-health.json")
    return FontProviderCapability(
        f"bepinex5_mono_{architecture}", runtime, architecture, True,
        payload_available,
        payload_deployed=payload_deployed,
        runtime_verified=payload_deployed and health.runtime_verified,
        reason="" if payload_available else "Mono 字体 provider 固定载荷不可用",
    )


def _path_is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _replace_with_retry(source: Path, target: Path, attempts: int = 8) -> None:
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _atomic_copy_file(source: Path, target: Path) -> None:
    temporary = target.parent / f".{target.name}.hanhua-{uuid.uuid4().hex}.tmp"
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        _replace_with_retry(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _commit_install_tree(
        stage_dir: Path, output_root: Path,
        owned_relative: Path | None = None) -> str:
    owned_stage = stage_dir / owned_relative if owned_relative else None
    if owned_stage is not None and (
            not owned_stage.is_dir() or _path_is_link(owned_stage)):
        raise FontInstallError("字体插件 owned staging 目录无效")

    def belongs_to_owned(relative: Path) -> bool:
        return bool(owned_relative) and (
            relative == owned_relative or owned_relative in relative.parents)

    stage_files = sorted(
        (path for path in stage_dir.rglob("*")
         if path.is_file() and not belongs_to_owned(path.relative_to(stage_dir))),
        key=lambda path: str(path.relative_to(stage_dir)).casefold(),
    )
    stage_dirs = sorted(
        (path for path in stage_dir.rglob("*")
         if path.is_dir() and not belongs_to_owned(path.relative_to(stage_dir))),
        key=lambda path: len(path.relative_to(stage_dir).parts),
    )
    relative_files = [path.relative_to(stage_dir) for path in stage_files]
    relative_dirs = [path.relative_to(stage_dir) for path in stage_dirs]

    for relative in relative_files:
        current = output_root
        for part in relative.parts[:-1]:
            current /= part
            if current.exists() and (not current.is_dir() or _path_is_link(current)):
                raise FontInstallError(f"输出目录路径与安装文件冲突: {current}")
        target = output_root / relative
        if target.exists() and (not target.is_file() or _path_is_link(target)):
            raise FontInstallError(f"输出目录路径与安装文件冲突: {target}")

    owned_target = output_root / owned_relative if owned_relative else None
    if owned_target is not None:
        current = output_root
        for part in owned_relative.parts[:-1]:
            current /= part
            if current.exists() and (
                    not current.is_dir() or _path_is_link(current)):
                raise FontInstallError(f"字体插件 owned 目录路径冲突: {current}")
        if owned_target.exists() and (
                not owned_target.is_dir() or _path_is_link(owned_target)):
            raise FontInstallError(f"字体插件 owned 目录路径冲突: {owned_target}")

    backup_root = stage_dir.parent / "backup"
    try:
        for relative in relative_files:
            target = output_root / relative
            if target.exists():
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, backup)
    except OSError as exc:
        raise FontInstallError(f"无法预检输出目录中的既有文件: {exc}") from exc

    required_dirs = {output_root}
    required_dirs.update(output_root / relative for relative in relative_dirs)
    for relative in relative_files:
        parent = (output_root / relative).parent
        while parent != output_root.parent:
            required_dirs.add(parent)
            if parent == output_root:
                break
            parent = parent.parent

    created_dirs: list[Path] = []
    touched_files: list[tuple[Path, Path]] = []
    owned_backup = (owned_target.parent / (
        f".{owned_target.name}.hanhua-backup-{uuid.uuid4().hex}")
        if owned_target is not None else backup_root / "unused-owned-backup")
    owned_old_moved = False
    owned_new_moved = False
    try:
        for directory in sorted(required_dirs, key=lambda path: len(path.parts)):
            if not directory.exists():
                directory.mkdir()
                created_dirs.append(directory)
        for stage_file, relative in zip(stage_files, relative_files, strict=True):
            target = output_root / relative
            touched_files.append((target, backup_root / relative))
            _atomic_copy_file(stage_file, target)
        if owned_target is not None and owned_stage is not None:
            owned_target.parent.mkdir(parents=True, exist_ok=True)
            if owned_target.exists():
                _replace_with_retry(owned_target, owned_backup)
                owned_old_moved = True
            _replace_with_retry(owned_stage, owned_target)
            owned_new_moved = True
    except OSError as exc:
        rollback_errors: list[OSError] = []
        if owned_target is not None:
            try:
                if owned_new_moved and owned_target.exists():
                    shutil.rmtree(owned_target)
                if owned_old_moved and owned_backup.exists():
                    _replace_with_retry(owned_backup, owned_target)
                    owned_old_moved = False
            except OSError as rollback_exc:
                rollback_errors.append(rollback_exc)
        for target, backup in reversed(touched_files):
            try:
                if backup.is_file():
                    _atomic_copy_file(backup, target)
                elif target.exists():
                    target.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(rollback_exc)
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        detail = f"字体运行时或 owned plugin swap 失败，目标已回滚: {exc}"
        if owned_old_moved and owned_backup.exists():
            detail += f"；可恢复 owned backup: {owned_backup.resolve()}"
        if rollback_errors:
            detail += f"；回滚时另有 {len(rollback_errors)} 个错误"
        raise FontInstallError(detail) from exc

    cleanup_pending = ""
    if owned_new_moved and owned_old_moved and owned_backup.exists():
        tombstone = owned_backup.with_name(
            f".{owned_target.name}.hanhua-cleanup-{uuid.uuid4().hex}")
        try:
            _replace_with_retry(owned_backup, tombstone)
            owned_old_moved = False
        except OSError:
            cleanup_pending = str(owned_backup.resolve())
        else:
            try:
                shutil.rmtree(tombstone)
            except OSError:
                cleanup_pending = str(tombstone.resolve())
    return cleanup_pending


def install_font_override(
    game_dir: Path,
    out_dir: Path,
    config: FontConfig,
    *,
    assets: FontRuntimeAssets | None = None,
    translations: dict[str, str] | None = None,
    exclude: set[str] | None = None,
    player_root: Path | None = None,
    tmp_bundle: Path | None = None,
) -> FontInstallResult:
    if not config.enabled:
        return FontInstallResult(False)
    # 旧库兼容：弃用字体路径自动映射新字体（含 dataclasses.replace 语义，
    # 不改动调用方配置对象——仅本次安装生效）
    filename = _normalize_font_filename(config.filename)
    try:
        family = FONT_OPTIONS[filename]
    except KeyError as exc:
        raise FontInstallError(f"字体不在允许的白名单中: {config.filename}") from exc
    source_root = Path(game_dir).resolve()
    selected_root = _resolve_player_root(source_root, player_root)
    player_relative = selected_root.relative_to(source_root)
    runtime = (
        "il2cpp"
        if any(
            child.is_file() and child.name.casefold() == "gameassembly.dll"
            for child in selected_root.iterdir()
        )
        else "mono"
    )
    capability = resolve_font_provider(
        source_root, runtime, assets=assets, player_root=selected_root)
    if not capability.provider_supported and runtime == "il2cpp":
        return FontInstallResult(
            installed=False,
            filename=filename,
            family=family,
            payload_deployed=False,
            runtime_verified=False,
            provider_supported=False,
            unsupported_reason=capability.reason,
            architecture=capability.architecture,
            provider_id=capability.provider_id,
            payload_available=capability.payload_available,
        )
    if not capability.provider_supported:
        raise FontInstallError(capability.reason)
    selected_assets = assets or _default_assets()
    architecture = capability.architecture
    try:
        fonts_root = selected_assets.fonts_dir.resolve(strict=True)
        font_source = (fonts_root / filename).resolve(strict=True)
    except OSError as exc:
        raise FontInstallError(
            f"字体文件不存在或无法解析: {selected_assets.fonts_dir / filename}"
        ) from exc
    if (
        not fonts_root.is_dir()
        or not font_source.is_relative_to(fonts_root)
        or not font_source.is_file()
    ):
        raise FontInstallError(
            f"字体文件必须位于字体根目录内，不能通过符号链接逃出范围: {font_source}"
        )
    if not selected_assets.plugin_dll.is_file():
        raise FontInstallError(f"字体插件文件不存在: {selected_assets.plugin_dll}")
    if architecture == "x86":
        runtime_zip = selected_assets.runtime_x86_zip
        expected_runtime_sha256 = selected_assets.expected_x86_sha256
        expected_runtime_size = selected_assets.expected_x86_size
        if runtime_zip is None:
            raise FontInstallError("Mono x86 游戏缺少固定的 BepInEx x86 字体载荷")
    else:
        runtime_zip = selected_assets.runtime_zip
        expected_runtime_sha256 = selected_assets.expected_runtime_sha256
        expected_runtime_size = selected_assets.expected_runtime_size
    tmp_bundle_payload: bytes | None = None
    if tmp_bundle is not None:
        bundle_path = Path(tmp_bundle)
        try:
            if _path_is_link(bundle_path) or not bundle_path.is_file():
                raise FontInstallError(
                    f"TMP bundle 必须是可读的普通文件: {bundle_path}")
            tmp_bundle_payload = bundle_path.read_bytes()
        except FontInstallError:
            raise
        except OSError as exc:
            raise FontInstallError(
                f"无法读取 TMP bundle: {bundle_path}: {exc}") from exc
    try:
        font_payload = font_source.read_bytes()
        plugin_payload = selected_assets.plugin_dll.read_bytes()
        runtime_payload = runtime_zip.read_bytes()
    except OSError as exc:
        raise FontInstallError(f"无法读取字体运行时载荷: {exc}") from exc
    # D1 部署闸门（问题集 D1，2026-09-04）：插件 font.ttf 载荷必须是真
    # TrueType（glyf，magic 00010000）——CFF（OTTO）被 Unity
    # `new Font(fontPath)` 按 TTF 解析 → 缺字口口口。白名单已只收 TTF，
    # 这是最后一道防线：即使白名单/别名将来配错成 OTF，也在部署前拒绝
    # 而非静默部署坏字体。注意不缩窄 unity/font_replace._ttf_has_magic
    # ——游戏 legacy Font 内嵌 CFF 是合法形态，只在部署端强制。
    if font_payload[:4] not in (b"\x00\x01\x00\x00", b"true", b"ttcf"):
        raise FontInstallError(
            f"插件字体载荷必须是真 TrueType（glyf），拒绝部署: {filename} "
            f"（magic={font_payload[:4]!r}，CFF/OTTO 会导致缺字口口口，"
            "用 scripts/convert_cff_to_ttf.py 转换后再部署）")
    if (
        expected_runtime_size is not None
        and len(runtime_payload) != expected_runtime_size
    ):
        raise FontInstallError(
            "字体运行时压缩包大小校验失败："
            f"expected {expected_runtime_size}, "
            f"got {len(runtime_payload)}"
        )
    if expected_runtime_sha256 is not None:
        actual_hash = hashlib.sha256(runtime_payload).hexdigest()
        if actual_hash.casefold() != expected_runtime_sha256.casefold():
            raise FontInstallError(
                "字体运行时压缩包 SHA-256 校验失败："
                f"expected {expected_runtime_sha256}, got {actual_hash}"
            )

    output_root = out_dir.resolve()
    selected_output_root = output_root / player_relative
    try:
        with zipfile.ZipFile(io.BytesIO(runtime_payload)) as archive:
            members = _validated_members(archive, selected_output_root)
            member_records = [
                (
                    member,
                    target.relative_to(selected_output_root),
                    "/".join(
                        part.casefold()
                        for part in target.relative_to(selected_output_root).parts
                    ),
                )
                for member, target in members
            ]
            member_by_name = {
                key: member
                for member, _relative, key in member_records
                if not member.is_dir()
            }
            required = {
                "winhttp.dll",
                "doorstop_config.ini",
                "bepinex/core/bepinex.dll",
            }
            missing = sorted(required - member_by_name.keys())
            if missing:
                raise FontInstallError(
                    "字体运行时压缩包缺少必要文件: " + ", ".join(missing)
                )
            bad_member = archive.testzip()
            if bad_member is not None:
                raise FontInstallError(
                    f"字体运行时压缩包成员 CRC 校验失败: {bad_member}"
                )
            file_payloads = {
                relative: archive.read(member)
                for member, relative, _key in member_records
                if not member.is_dir()
                # 根级文档（BepInEx 发行包自带 changelog.txt 等）跳过：
                # 覆盖游戏同名文件（Windows 大小写不敏感）会破坏原文件。
                and not (
                    len(relative.parts) == 1
                    and relative.name.casefold() in _RUNTIME_ROOT_DOCS
                )
            }
            bundled_winhttp = archive.read(member_by_name["winhttp.dll"])
            existing_winhttp = selected_output_root / "winhttp.dll"
            if existing_winhttp.exists() and (
                not existing_winhttp.is_file()
                or _path_is_link(existing_winhttp)
                or existing_winhttp.read_bytes() != bundled_winhttp
            ):
                raise FontInstallError(
                    "输出目录中的 winhttp.dll 与字体运行时载荷冲突，已拒绝覆盖"
                )
    except (OSError, zipfile.BadZipFile) as exc:
        raise FontInstallError(f"字体运行时压缩包损坏或无法读取: {exc}") from exc

    runtime_plugin_relative = Path("BepInEx") / "plugins" / "HanhuaFont"
    plugin_relative = player_relative / runtime_plugin_relative
    # W3 运行时排除表：静态写回被回退（保留原文防断链）的逻辑键原文——
    # 插件把这些串再翻译成中文 → 游戏按原名查找断链（按键失灵）。
    # 同一原文若同时是显示文本（静态已写译文），插件翻译表里剔除它后
    # 动态加载的文本保持英文——防断链优先于个别动态文本未翻。
    excluded = {str(s) for s in (exclude or ()) if str(s).strip()}
    exact_translations = {
        str(source): str(target)
        for source, target in (translations or {}).items()
        if str(source) and str(target) and str(source) not in excluded
    }
    translations_payload = (
        json.dumps(
            exact_translations, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    ).encode("utf-8")
    runtime_templates_payload = _runtime_template_payload(exact_translations)
    # Phase 3：逐 scalar 验证输入——插件按本次部署需求集证明每个码点
    required_glyphs_payload, _snapshot_hash = _required_glyphs_payload(
        exact_translations)
    exclude_payload = (
        json.dumps(
            sorted(excluded), ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
    ).encode("utf-8") if excluded else b""
    try:
        with tempfile.TemporaryDirectory(
            prefix="hanhua-font-install-",
            dir=output_root.parent,
            ignore_cleanup_errors=True,
        ) as temp_dir:
            stage_dir = Path(temp_dir) / "install"
            for relative, payload in file_payloads.items():
                stage_file = stage_dir / player_relative / relative
                stage_file.parent.mkdir(parents=True, exist_ok=True)
                stage_file.write_bytes(payload)
            plugin_dir = stage_dir / plugin_relative
            plugin_dir.mkdir(parents=True, exist_ok=True)
            (plugin_dir / "Hanhua.FontFallback.dll").write_bytes(plugin_payload)
            (plugin_dir / "font.ttf").write_bytes(font_payload)
            if tmp_bundle_payload is not None:
                (plugin_dir / "font-tmp.bundle").write_bytes(
                    tmp_bundle_payload)
            (plugin_dir / "font-family.txt").write_bytes(
                f"{family}\n".encode("utf-8")
            )
            (plugin_dir / "translations.json").write_bytes(
                translations_payload)
            (plugin_dir / "runtime-templates.json").write_bytes(
                runtime_templates_payload)
            (plugin_dir / "required-glyphs.json").write_bytes(
                required_glyphs_payload)
            if exclude_payload:
                (plugin_dir / "translations-exclude.json").write_bytes(
                    exclude_payload)
            cleanup_pending = _commit_install_tree(
                stage_dir, output_root, owned_relative=plugin_relative)
    except OSError as exc:
        raise FontInstallError(f"无法准备字体运行时临时安装树: {exc}") from exc
    return FontInstallResult(
        True, filename, family,
        payload_deployed=True,
        runtime_verified=False,
        architecture=architecture,
        cleanup_pending=cleanup_pending,
        provider_id=capability.provider_id,
        payload_available=capability.payload_available,
    )
