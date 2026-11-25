"""Application service container tying together stores, runtime manager, priority gate, and task runner."""
from __future__ import annotations

from collections import deque
import threading
from pathlib import Path
from typing import Any

from ul10n.core.db import GlobalStore, ProjectStore, project_id_for_dir
from ul10n.core.game_detector import detect_game
from ul10n.core.hook_deployer import (
    build_standalone_patch_zip,
    deploy_hook,
    get_xuat_paths,
    refresh_hook_config,
    uninstall_hook,
)
from ul10n.core.models import (
    AppSettings,
    Entry,
    GameContext,
    GameProject,
    GlossaryItem,
    STATUS_EDITED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_TRANSLATED,
)
from ul10n.core.text_scanner import scan_unity_static_texts_detailed
from ul10n.core.xuat_codec import (
    ExportStats,
    export_xuat_file,
    parse_xuat_file,
    prune_xuat_auto_file,
    render_xuat_dict_text,
)
from ul10n.engine.runtime_manager import LlamaRuntimeManager
from ul10n.engine.tasks import EventBus, TaskManager
from ul10n.engine.translator import TranslationPipeline


class AppServices:
    """Central dependency container for the FastAPI application & in-game overlay."""

    def __init__(
        self,
        app_root: str | Path,
        data_dir: str | Path | None = None,
    ):
        self.app_root = Path(app_root).resolve()
        self.data_dir = Path(data_dir).resolve() if data_dir else (Path.home() / ".ul10nforge")
        self.global_store = GlobalStore(self.data_dir)
        self.bus = EventBus()
        self.tasks = TaskManager(self.bus)
        self.runtime = LlamaRuntimeManager(
            self.app_root,
            on_status_change=lambda _: self.bus.publish("runtime_status", self.runtime_status()),
        )
        self._store_lock = threading.RLock()
        self._project_stores: dict[str, ProjectStore] = {}
        self.active_project_id: str | None = None
        self.overlay: Any = None

        # Live-first priority preemption + Singleflight dedup for concurrent in-game XUAT requests
        self._live_cond = threading.Condition()
        self._live_active_count = 0
        self._inflight_lock = threading.Lock()
        self._inflight_live: dict[tuple[str, str], threading.Event] = {}
        self._sync_lock = threading.Lock()
        self._sync_timers: dict[str, threading.Timer] = {}
        self._export_locks: dict[str, threading.Lock] = {}
        self._live_cache_lock = threading.Lock()
        self._live_ctx_cache: dict[str, tuple[GameContext, list[GlossaryItem]]] = {}
        self._live_recent_lines: dict[str, deque[str]] = {}

        # Restore most recently opened project if available
        projects = self.global_store.list_projects()
        if projects:
            self.active_project_id = projects[0].id

    # ── Live-First Priority & Singleflight Helpers ────────────────────────

    def begin_live_request(self) -> None:
        with self._live_cond:
            self._live_active_count += 1

    def end_live_request(self) -> None:
        with self._live_cond:
            self._live_active_count = max(0, self._live_active_count - 1)
            if self._live_active_count == 0:
                self._live_cond.notify_all()

    def wait_if_live_busy(self, cancel: threading.Event | None = None, timeout: float = 0.25) -> None:
        """Yield batch translation slot briefly whenever the player has an active in-game request."""
        with self._live_cond:
            while self._live_active_count > 0:
                if cancel and cancel.is_set():
                    return
                self._live_cond.wait(timeout=timeout)

    def acquire_singleflight(self, key: tuple[str, str]) -> tuple[bool, threading.Event]:
        """Return `(is_leader, event)` so identical concurrent live strings share 1 LLM call."""
        with self._inflight_lock:
            existing = self._inflight_live.get(key)
            if existing is not None:
                return False, existing
            evt = threading.Event()
            self._inflight_live[key] = evt
            return True, evt

    def release_singleflight(self, key: tuple[str, str], evt: threading.Event) -> None:
        with self._inflight_lock:
            if self._inflight_live.get(key) is evt:
                self._inflight_live.pop(key, None)
        evt.set()

    def get_live_context_and_glossary(self, project_id: str) -> tuple[GameContext, list[GlossaryItem]]:
        """Return cached `(GameContext, project+global glossary)` with 0ms lookup for live translation."""
        with self._live_cache_lock:
            cached = self._live_ctx_cache.get(project_id)
            if cached is not None:
                return cached
        store = self.project_store(project_id)
        ctx = store.load_context()
        terms = store.list_glossary() + self.global_store.list_glossary()
        pair = (ctx, terms)
        with self._live_cache_lock:
            self._live_ctx_cache[project_id] = pair
        return pair

    def invalidate_translation_cache(self, project_id: str | None = None) -> None:
        """Invalidate cached context/glossary (pass None when global glossary changes)."""
        with self._live_cache_lock:
            if project_id is None:
                self._live_ctx_cache.clear()
            else:
                self._live_ctx_cache.pop(project_id, None)

    def record_live_line(self, project_id: str, original: str) -> None:
        text = (original or "").strip()
        if not text:
            return
        with self._live_cache_lock:
            buf = self._live_recent_lines.get(project_id)
            if buf is None:
                buf = deque(maxlen=4)
                self._live_recent_lines[project_id] = buf
            if not buf or buf[-1] != text:
                buf.append(text)

    def recent_live_lines(self, project_id: str, *, exclude: str = "") -> list[str]:
        ex = (exclude or "").strip()
        with self._live_cache_lock:
            buf = self._live_recent_lines.get(project_id)
            if not buf:
                return []
            return [line for line in buf if line != ex][-3:]

    # ── Settings & Stores ─────────────────────────────────────────────────

    def settings(self) -> AppSettings:
        return self.global_store.load_settings()

    def runtime_status(self) -> dict[str, Any]:
        return self.runtime.status_snapshot(self.settings())

    def project_store(self, project_id: str) -> ProjectStore:
        with self._store_lock:
            store = self._project_stores.get(project_id)
            if store is None:
                db_path = self.data_dir / "projects" / f"{project_id}.db"
                store = ProjectStore(db_path)
                self._project_stores[project_id] = store
            return store

    def remove_project(self, project_id: str) -> None:
        """Remove a project cleanly, closing its SQLite handle and deleting its project DB."""
        with self._store_lock:
            st = self._project_stores.pop(project_id, None)
            if st is not None:
                try:
                    st.close()
                except Exception:
                    pass
        self.global_store.delete_project(project_id)
        if self.active_project_id == project_id:
            rest = self.global_store.list_projects()
            self.active_project_id = rest[0].id if rest else None
        for suffix in ("", "-wal", "-shm"):
            fp = self.data_dir / "projects" / f"{project_id}.db{suffix}"
            if fp.is_file():
                try:
                    fp.unlink()
                except OSError:
                    pass

    def clear_project_records(
        self,
        project_id: str,
        *,
        clear_game_dict: bool = True,
        clear_context: bool = True,
        rescan: bool = False,
    ) -> dict[str, Any]:
        """Clear all saved translation entries (and optionally game dict files & context)."""
        project = self.global_store.get_project(project_id)
        if not project:
            raise FileNotFoundError("未找到该项目")
        store = self.project_store(project_id)
        cleared = store.clear_entries()
        if clear_context:
            store.set_kv("game_context", {})
        self.invalidate_translation_cache(project_id)
        with self._live_cache_lock:
            self._live_recent_lines.pop(project_id, None)
        # Mark prescan version done so reopening won't unexpectedly re-run unless user scans
        store.set_kv("prescan_version", 2)

        cleared_dict_files = 0
        if clear_game_dict and Path(project.game_dir).is_dir():
            paths = get_xuat_paths(project.game_dir, project.loader)
            if paths["curated_file"].is_file():
                try:
                    paths["curated_file"].write_text(
                        "// Generated by UL10nForge v2 — cleared\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    cleared_dict_files += 1
                except OSError:
                    pass
            if paths["auto_file"].is_file():
                try:
                    paths["auto_file"].write_text("", encoding="utf-8", newline="\n")
                    cleared_dict_files += 1
                except OSError:
                    pass

        rescanned = 0
        if rescan:
            scan_res = self.scan_game_texts(project_id)
            rescanned = scan_res.get("added", 0)

        return {
            "cleared": cleared,
            "cleared_dict_files": cleared_dict_files,
            "rescanned": rescanned,
            "stats": store.stats(),
            "context": store.load_context().to_dict(),
        }

    # ── Project & Scanner Lifecycle ───────────────────────────────────────

    def open_game_dir(self, game_dir: str) -> tuple[GameProject, dict[str, int]]:
        info = detect_game(game_dir)
        pid = project_id_for_dir(info.game_dir)
        existing = self.global_store.get_project(pid)
        loader = existing.loader if existing else info.recommended_loader
        paths = get_xuat_paths(info.game_dir, loader)
        hook_deployed = bool(
            (existing and existing.hook_deployed) or paths["config"].is_file()
        )
        project = self.global_store.upsert_project(
            GameProject(
                id=pid,
                name=info.name,
                game_dir=info.game_dir,
                runtime_type=info.runtime_type,
                arch=info.arch,
                unity_version=info.unity_version,
                loader=loader,
                hook_deployed=hook_deployed,
            )
        )
        self.active_project_id = pid
        import_stats = self.import_xuat_from_game(pid)
        store = self.project_store(pid)
        if store.get_kv("prescan_version") != 2 and info.is_unity:
            scan_res = self.scan_game_texts(pid)
            import_stats["added"] += scan_res["added"]
            store.set_kv("prescan_version", 2)
        return project, import_stats

    def scan_game_texts(self, project_id: str) -> dict[str, Any]:
        """Strictly read-only static scan of Unity UI, assets, text files, and code strings."""
        project = self.global_store.get_project(project_id)
        if not project:
            return {"added": 0, "updated": 0, "skipped": 0, "by_source": {}}
        report = scan_unity_static_texts_detailed(project.game_dir)
        if not report.texts:
            return {"added": 0, "updated": 0, "skipped": 0, "by_source": report.by_source}
        pairs = [(t, "") for t in report.texts]
        res = self.project_store(project_id).bulk_import_pairs(pairs, source="prescan")
        self.project_store(project_id).set_kv("prescan_version", 2)
        return {**res, "by_source": report.by_source}

    def import_xuat_from_game(self, project_id: str) -> dict[str, int]:
        project = self.global_store.get_project(project_id)
        if not project:
            return {"added": 0, "updated": 0, "skipped": 0}
        store = self.project_store(project_id)
        paths = get_xuat_paths(project.game_dir, project.loader)
        tr_dir = paths["translation_dir"]
        totals = {"added": 0, "updated": 0, "skipped": 0}
        if not tr_dir.is_dir():
            return totals
        files = sorted(tr_dir.glob("*.txt"), key=lambda p: p.name.casefold())
        for fp in files:
            pairs = parse_xuat_file(fp)
            if pairs:
                res = store.bulk_import_pairs(pairs, source="xuat_import")
                for k in totals:
                    totals[k] += res[k]
        return totals

    def export_xuat_to_game(self, project_id: str) -> ExportStats | None:
        project = self.global_store.get_project(project_id)
        if not project:
            return None
        store = self.project_store(project_id)
        paths = get_xuat_paths(project.game_dir, project.loader)
        if not Path(project.game_dir).is_dir():
            return None
        with self._sync_lock:
            lock = self._export_locks.setdefault(project_id, threading.Lock())
        with lock:
            paths["translation_dir"].mkdir(parents=True, exist_ok=True)
            if not paths["auto_file"].exists():
                paths["auto_file"].write_text("", encoding="utf-8")
            entries = store.all_entries()
            # Prune managed/edited/skipped keys out of _AutoGeneratedTranslations.txt so XUAT
            # hot-reload ALWAYS reflects manual edits & skips with zero alphabetical shadowing!
            managed_keys = {
                e.original
                for e in entries
                if e.status in (STATUS_TRANSLATED, STATUS_EDITED, STATUS_SKIPPED)
            }
            prune_xuat_auto_file(paths["auto_file"], managed_keys)
            return export_xuat_file(entries, paths["curated_file"])

    def _sync_xuat_if_enabled(self, project: GameProject) -> None:
        cfg = self.settings()
        if not cfg.auto_sync_xuat:
            return
        paths = get_xuat_paths(project.game_dir, project.loader)
        if project.hook_deployed or paths["translation_dir"].is_dir():
            self.export_xuat_to_game(project.id)

    def schedule_xuat_sync(self, project_id: str, delay: float = 1.2) -> None:
        """Debounced write of live translations to `00_UL10nForge.txt` for instant persistence & IL2CPP hot-reload."""
        if not self.settings().auto_sync_xuat:
            return

        def _flush() -> None:
            with self._sync_lock:
                self._sync_timers.pop(project_id, None)
            proj = self.global_store.get_project(project_id)
            if proj:
                try:
                    self._sync_xuat_if_enabled(proj)
                except Exception:
                    pass

        with self._sync_lock:
            prev = self._sync_timers.pop(project_id, None)
            if prev is not None:
                prev.cancel()
            t = threading.Timer(delay, _flush)
            t.daemon = True
            self._sync_timers[project_id] = t
            t.start()

    def _publish_entry_update(self, project_id: str, entry: Entry, *, from_cache: bool = True) -> None:
        store = self.project_store(project_id)
        self.bus.publish(
            "live_entry",
            {
                "project_id": project_id,
                "entry": entry.to_dict(),
                "stats": store.stats(),
                "from_cache": from_cache,
            },
        )

    # ── Shared Entry Actions (Used by Both WebUI & Alt+L Overlay HUD) ────

    def save_manual_translation(self, project_id: str, entry_id: int, translation: str) -> Entry | None:
        """Save a manual edit, lock it, record global memory, and hot-sync into the game."""
        project = self.global_store.get_project(project_id)
        if not project:
            return None
        store = self.project_store(project_id)
        tr = translation
        new_status = "edited" if tr.strip() else "pending"
        entry = store.update_entry_by_id(
            entry_id,
            translation=tr,
            status=new_status,
            locked=bool(tr.strip()),
            quality_note="",
            review_reason="",
        )
        if not entry:
            return None
        cfg = self.settings()
        if entry.translation.strip():
            self.global_store.put_memory(
                entry.original, entry.translation.strip(), cfg.target_lang, source="manual"
            )
            self._sync_xuat_if_enabled(project)
        self._publish_entry_update(project_id, entry, from_cache=True)
        return entry

    def restore_entry_translation(self, project_id: str, entry_id: int) -> Entry | None:
        """Restore `prev_translation` for an entry and hot-sync back into the game."""
        project = self.global_store.get_project(project_id)
        if not project:
            return None
        store = self.project_store(project_id)
        entry = store.restore_prev_translation(entry_id)
        if not entry:
            return None
        cfg = self.settings()
        if entry.translation.strip():
            self.global_store.put_memory(
                entry.original, entry.translation.strip(), cfg.target_lang, source="manual"
            )
            self._sync_xuat_if_enabled(project)
        self._publish_entry_update(project_id, entry, from_cache=True)
        return entry

    def skip_entry(self, project_id: str, entry_id: int) -> Entry | None:
        """Mark an entry as not-translated: clear translation (saving to prev_translation) & sync."""
        project = self.global_store.get_project(project_id)
        if not project:
            return None
        store = self.project_store(project_id)
        entry = store.update_entry_by_id(
            entry_id,
            translation="",
            status=STATUS_SKIPPED,
            locked=True,
            quality_note="",
            review_level="",
            review_reason="",
        )
        if not entry:
            return None
        self._sync_xuat_if_enabled(project)
        self._publish_entry_update(project_id, entry, from_cache=True)
        return entry

    def unskip_entry(self, project_id: str, entry_id: int) -> Entry | None:
        """Restore a skipped entry back to translated (from prev_translation) or pending."""
        project = self.global_store.get_project(project_id)
        if not project:
            return None
        store = self.project_store(project_id)
        cur = store.get_by_id(entry_id)
        if not cur:
            return None
        restored_tr = (cur.prev_translation or "").strip()
        next_status = STATUS_TRANSLATED if restored_tr else STATUS_PENDING
        entry = store.update_entry_by_id(
            entry_id,
            translation=restored_tr,
            status=next_status,
            locked=False,
            quality_note="",
            review_reason="",
        )
        if not entry:
            return None
        self._sync_xuat_if_enabled(project)
        self._publish_entry_update(project_id, entry, from_cache=True)
        return entry

    def retranslate_entry_now(
        self, project_id: str, entry_id: int, fix_hint: str = ""
    ) -> tuple[Entry | None, bool, str]:
        """Force-retranslate one entry with AI and hot-sync into the game."""
        project = self.global_store.get_project(project_id)
        if not project:
            return None, False, "项目不存在"
        store = self.project_store(project_id)
        entry = store.get_by_id(entry_id)
        if not entry:
            return None, False, "条目不存在"
        cfg = self.settings()
        pipeline = TranslationPipeline(
            self.global_store,
            store,
            target_lang=cfg.target_lang,
            temperature=cfg.translate_role.temperature,
            max_tokens=cfg.translate_role.max_tokens,
        )
        client = self.runtime.create_client_for_role("translate", cfg)
        try:
            outcome = pipeline.translate_one(
                entry.original,
                client,
                source=entry.source,
                force_retranslate=True,
                fix_hint=fix_hint,
            )
        finally:
            client.close()
        if outcome.ok:
            self._sync_xuat_if_enabled(project)
        self._publish_entry_update(project_id, outcome.entry, from_cache=False)
        return outcome.entry, outcome.ok, outcome.note

    # ── Hook Deployment & Standalone Offline Patch Export ─────────────────

    def deploy_project_hook(self, project_id: str, loader: str | None = None) -> dict[str, Any]:
        project = self.global_store.get_project(project_id)
        if not project:
            raise FileNotFoundError("未找到该项目")
        target_loader = (loader or project.loader or "bepinex").lower()
        # Cleanly uninstall previous loader first if user switches BepInEx <-> MelonLoader
        if project.hook_deployed and project.loader and target_loader != project.loader.lower():
            try:
                self.uninstall_project_hook(project_id)
            except Exception:
                pass
        info = detect_game(project.game_dir)
        cfg = self.settings()
        result = deploy_hook(
            info,
            app_root=self.app_root,
            project_id=project_id,
            server_port=cfg.server_port,
            loader=target_loader,
            target_lang=cfg.target_lang,
            font_id=cfg.font_id,
        )
        store = self.project_store(project_id)
        store.set_kv("hook_manifest", result.manifest)
        self.global_store.set_hook_deployed(project_id, True, loader=result.loader)
        self.export_xuat_to_game(project_id)
        return {
            "loader": result.loader,
            "runtime_type": result.runtime_type,
            "arch": result.arch,
            "files_written": result.files_written,
            "font_bundle": result.font_bundle,
        }

    def refresh_project_hook_config(self, project_id: str) -> None:
        project = self.global_store.get_project(project_id)
        if not project or not Path(project.game_dir).is_dir():
            return
        info = detect_game(project.game_dir)
        cfg = self.settings()
        manifest = self.project_store(project_id).get_kv("hook_manifest") or {}
        refresh_hook_config(
            info,
            app_root=self.app_root,
            project_id=project_id,
            server_port=cfg.server_port,
            loader=project.loader,
            target_lang=cfg.target_lang,
            preexisting_loader=bool(manifest.get("preexisting_loader", False)),
            font_id=cfg.font_id,
        )

    def uninstall_project_hook(self, project_id: str) -> dict[str, Any]:
        project = self.global_store.get_project(project_id)
        if not project:
            raise FileNotFoundError("未找到该项目")
        store = self.project_store(project_id)
        manifest = store.get_kv("hook_manifest")
        removed = uninstall_hook(project.game_dir, manifest)
        store.set_kv("hook_manifest", None)
        self.global_store.set_hook_deployed(project_id, False)
        return {"removed_files": removed}

    def export_standalone_patch(self, project_id: str) -> tuple[bytes, str]:
        """Build a standalone offline Chinese patch zip and return `(zip_bytes, filename)`."""
        project = self.global_store.get_project(project_id)
        if not project:
            raise FileNotFoundError("未找到该项目")
        info = detect_game(project.game_dir)
        cfg = self.settings()
        store = self.project_store(project_id)
        dict_text, _exported, _skipped, _regex = render_xuat_dict_text(store.all_entries())
        zip_bytes = build_standalone_patch_zip(
            info,
            app_root=self.app_root,
            curated_dict_text=dict_text,
            loader=project.loader,
            target_lang=cfg.target_lang,
            font_id=cfg.font_id,
        )
        safe_name = "".join(c for c in project.name if c not in r'\/:*?"<>|').strip() or "UnityGame"
        return zip_bytes, f"{safe_name}_中文离线补丁.zip"

    def shutdown(self) -> None:
        with self._sync_lock:
            for t in self._sync_timers.values():
                t.cancel()
            self._sync_timers.clear()
        if self.overlay is not None:
            try:
                self.overlay.stop()
            except Exception:
                pass
        self.tasks.cancel()
        self.runtime.stop_all()
        with self._store_lock:
            for st in self._project_stores.values():
                st.close()
            self._project_stores.clear()
        self.global_store.close()
