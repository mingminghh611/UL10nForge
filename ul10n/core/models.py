"""Core domain models for UL10nForge v2."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STATUS_PENDING = "pending"
STATUS_TRANSLATED = "translated"
STATUS_EDITED = "edited"
STATUS_FLAGGED = "flagged"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

VALID_STATUSES = frozenset({
    STATUS_PENDING,
    STATUS_TRANSLATED,
    STATUS_EDITED,
    STATUS_FLAGGED,
    STATUS_FAILED,
    STATUS_SKIPPED,
})


@dataclass
class GameInfo:
    """Detected Unity game metadata (pure read-only inspection)."""
    game_dir: str
    name: str
    executable: str = ""
    data_dir: str = ""
    runtime_type: str = "unknown"   # mono | il2cpp | unknown
    arch: str = "x64"               # x64 | x86
    unity_version: str = ""
    is_unity: bool = False
    recommended_loader: str = "bepinex"  # bepinex | melonloader
    protection_warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GameProject:
    """Registered game project entry in GlobalStore."""
    id: str
    name: str
    game_dir: str
    runtime_type: str = "unknown"
    arch: str = "x64"
    unity_version: str = ""
    loader: str = "bepinex"
    hook_deployed: bool = False
    last_opened: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Entry:
    """Single display text translation entry."""
    id: int | None
    original: str
    translation: str = ""
    status: str = STATUS_PENDING
    source: str = "xuat_live"       # xuat_live | prescan | xuat_import | manual
    locked: bool = False
    review_level: str = ""          # PASS | FIXED | MINOR | MAJOR | CRITICAL | ""
    review_reason: str = ""
    quality_note: str = ""
    updated_at: str = ""
    prev_translation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GlossaryItem:
    """Terminology pair (project-scoped or global)."""
    id: int | None
    term: str
    translation: str
    category: str = "术语"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GameContext:
    """Structured game profile inferred by reasoning model."""
    game_name: str = ""
    genre: str = ""
    setting: str = ""
    summary: str = ""
    style: str = ""
    characters: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        return bool(self.genre or self.setting or self.summary or self.style)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GameContext:
        if not isinstance(data, dict):
            return cls()

        def _fmt_item(x: Any) -> str:
            if isinstance(x, dict):
                name = str(x.get("term") or x.get("name") or x.get("key") or "").strip()
                desc = str(x.get("desc") or x.get("description") or x.get("translation") or x.get("note") or "").strip()
                if name and desc:
                    return f"{name}：{desc}"
                return name or desc
            return str(x).strip()

        return cls(
            game_name=str(data.get("game_name", "") or "").strip(),
            genre=str(data.get("genre", "") or "").strip(),
            setting=str(data.get("setting", "") or "").strip(),
            summary=str(data.get("summary", "") or "").strip(),
            style=str(data.get("style", "") or "").strip(),
            characters=[s for s in (_fmt_item(x) for x in (data.get("characters") or [])) if s][:12],
            terms=[s for s in (_fmt_item(x) for x in (data.get("terms") or [])) if s][:16],
            notes=[s for s in (_fmt_item(x) for x in (data.get("notes") or [])) if s][:6],
        )


@dataclass
class RoleEndpointConfig:
    """Endpoint config for one logical role (translate or reason)."""
    mode: str = "local"             # local | api
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 1024

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RoleEndpointConfig:
        if not isinstance(data, dict):
            return cls()
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclass
class AppSettings:
    """Application settings persisted in global.db."""
    theme: str = "dark"             # dark | light
    target_lang: str = "zh-CN"
    hardware_mode: str = "auto"     # auto | gpu | cpu
    server_port: int = 18765
    auto_sync_xuat: bool = True
    disable_reasoning: bool = False  # True = skip context inference & AI review, direct translate -> deploy
    sound_effects: bool = True       # Subtle UI feedback chimes
    use_single_model: bool = False   # Legacy compatibility field
    translate_role: RoleEndpointConfig = field(
        default_factory=lambda: RoleEndpointConfig(mode="local", temperature=0.25, max_tokens=1024)
    )
    reason_role: RoleEndpointConfig = field(
        default_factory=lambda: RoleEndpointConfig(mode="local", temperature=0.15, max_tokens=1536)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "target_lang": self.target_lang,
            "hardware_mode": self.hardware_mode,
            "server_port": self.server_port,
            "auto_sync_xuat": self.auto_sync_xuat,
            "disable_reasoning": self.disable_reasoning,
            "sound_effects": self.sound_effects,
            "use_single_model": self.use_single_model,
            "translate_role": self.translate_role.to_dict(),
            "reason_role": self.reason_role.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AppSettings:
        if not isinstance(data, dict):
            return cls()
        return cls(
            theme=str(data.get("theme", "dark") or "dark"),
            target_lang=str(data.get("target_lang", "zh-CN") or "zh-CN"),
            hardware_mode=str(data.get("hardware_mode", "auto") or "auto"),
            server_port=int(data.get("server_port", 18765) or 18765),
            auto_sync_xuat=bool(data.get("auto_sync_xuat", True)),
            disable_reasoning=bool(data.get("disable_reasoning", False)),
            sound_effects=bool(data.get("sound_effects", True)),
            use_single_model=bool(data.get("use_single_model", False)),
            translate_role=RoleEndpointConfig.from_dict(data.get("translate_role")),
            reason_role=RoleEndpointConfig.from_dict(data.get("reason_role")),
        )
