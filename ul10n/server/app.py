"""FastAPI server exposing XUAT real-time translation, REST API, SSE stream, exports, and WebUI."""
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ul10n import VERSION
from ul10n.core.exporter import export_entries_as_format
from ul10n.core.fonts import get_font_option, list_fonts
from ul10n.core.game_detector import detect_protection_markers
from ul10n.core.hook_deployer import get_xuat_paths
from ul10n.core.models import (
    AppSettings,
    GameContext,
    STATUS_EDITED,
    STATUS_FAILED,
    STATUS_FLAGGED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_TRANSLATED,
    VALID_STATUSES,
)
from ul10n.core.quality import check_translation_quality, is_translatable, lookup_builtin_ui
from ul10n.core.xuat_codec import decode_xuat_line
from ul10n.engine.context_builder import infer_game_context, sync_context_terms_to_glossary
from ul10n.engine.llm_client import LLMClient, is_hy_mt_model
from ul10n.engine.reviewer import is_high_risk_for_review, review_batch_and_refine
from ul10n.engine.translator import TranslationPipeline
from ul10n.server.state import AppServices

_MASK_PREFIX = "••••"
_LOCAL_ORIGINS = frozenset({"127.0.0.1", "localhost", "::1"})


class OpenProjectReq(BaseModel):
    game_dir: str


class DeployReq(BaseModel):
    loader: str | None = None


class LaunchReq(BaseModel):
    borderless: bool = False


class ClearProjectReq(BaseModel):
    clear_game_dict: bool = True
    clear_context: bool = True
    rescan: bool = False


class CreateEntryReq(BaseModel):
    original: str
    translation: str = ""


class UpdateEntryReq(BaseModel):
    translation: str | None = None
    status: str | None = None
    locked: bool | None = None


class RetranslateReq(BaseModel):
    hint: str = ""


class BulkReplaceReq(BaseModel):
    find: str
    replace: str
    only_unlocked: bool = False


class ImportTextReq(BaseModel):
    content: str


class GlossaryReq(BaseModel):
    term: str
    translation: str
    category: str = "术语"
    note: str = ""


class ContextReq(BaseModel):
    game_name: str = ""
    genre: str = ""
    setting: str = ""
    summary: str = ""
    style: str = ""
    characters: list[str] = []
    terms: list[str] = []
    notes: list[str] = []


class BatchTranslateReq(BaseModel):
    include_failed: bool = True
    retranslate_all: bool = False


class BatchReviewReq(BaseModel):
    limit: int = 200
    only_unreviewed: bool = True
    mode: str = "smart"  # smart | full


class AutoLocalizeReq(BaseModel):
    run_review: bool = True
    deploy_hook: bool = True
    review_limit: int = 200


class RuntimeRoleReq(BaseModel):
    role: str = "translate"


class TestConnectionReq(BaseModel):
    mode: str = "local"
    role: str = "translate"
    base_url: str = ""
    api_key: str = ""
    model: str = ""


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 6:
        return _MASK_PREFIX
    return f"{_MASK_PREFIX}{k[-4:]}"


def _mask_settings_dict(settings: AppSettings) -> dict[str, Any]:
    d = settings.to_dict()
    for role_key in ("translate_role", "reason_role"):
        if isinstance(d.get(role_key), dict):
            raw = str(d[role_key].get("api_key") or "")
            d[role_key]["api_key"] = _mask_key(raw)
            d[role_key]["has_api_key"] = bool(raw.strip())
    return d


def _pick_folder_native() -> str:
    """Open Windows native folder picker via stdlib tkinter (zero extra dependencies)."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    selected = filedialog.askdirectory(title="选择 Unity 游戏文件夹")
    root.destroy()
    return selected or ""


def _content_disposition(filename: str) -> str:
    ascii_fallback = "".join(c if c.isascii() and c not in '"\\/' else "_" for c in filename).strip("_") or "export"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _safe_prewarm(svc: AppServices, hardware_mode: str) -> None:
    try:
        svc.runtime.ensure_local_server("translate", hardware_mode=hardware_mode)
    except Exception:
        pass


def create_app(
    app_root: str | Path | None = None,
    data_dir: str | Path | None = None,
    services: AppServices | None = None,
) -> FastAPI:
    root_path = Path(app_root).resolve() if app_root else Path(__file__).resolve().parents[2]
    svc = services or AppServices(root_path, data_dir=data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        svc.bus.attach_loop(asyncio.get_running_loop())
        yield
        svc.shutdown()

    app = FastAPI(title="UL10nForge", version=VERSION, lifespan=lifespan)
    app.state.services = svc

    # Guard local mutating API endpoints against cross-site requests & disable stale browser cache on UI assets
    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin:
                host = (urlparse(origin).hostname or "").casefold()
                if host not in _LOCAL_ORIGINS:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "拒绝来自非本机页面的跨站请求"},
                    )
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    web_dir = Path(__file__).resolve().parents[1] / "web"
    if web_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/")
    async def serve_index():
        index = web_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="前端页面缺失")
        return FileResponse(str(index))

    # ── XUAT Real-Time CustomTranslate Endpoint ───────────────────────────

    def _resolve_live_project_id(project_id: str | None) -> str:
        if project_id and svc.global_store.get_project(project_id):
            return project_id
        if svc.active_project_id and svc.global_store.get_project(svc.active_project_id):
            return svc.active_project_id
        projects = svc.global_store.list_projects()
        if projects:
            return projects[0].id
        scratch_dir = svc.data_dir / "_default_game"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch, _ = svc.open_game_dir(str(scratch_dir))
        return scratch.id

    def _handle_xuat_text(project_id: str | None, text: str) -> str:
        if not text:
            return ""
        pid = _resolve_live_project_id(project_id)
        store = svc.project_store(pid)
        cfg = svc.settings()

        # Fast-path cache check before acquiring live/LLM slot
        existing = store.get_by_original(text)
        if existing and existing.status == STATUS_SKIPPED:
            return text
        if existing and existing.translation and (
            existing.locked or existing.status in (STATUS_EDITED, STATUS_TRANSLATED)
        ):
            live_entry = store.mark_live_seen(existing.id) if existing.id else existing
            live_entry = live_entry or existing
            svc.bus.publish(
                "live_entry",
                {
                    "project_id": pid,
                    "entry": live_entry.to_dict(),
                    "stats": store.stats(),
                    "from_cache": True,
                },
            )
            return live_entry.translation

        # 0ms zero-model fast paths BEFORE ever touching llama-server / cloud API
        if not is_translatable(text):
            return text
        builtin = lookup_builtin_ui(text)
        if builtin:
            entry = store.upsert_entry(
                text, builtin, status=STATUS_TRANSLATED, source="xuat_live", quality_note=""
            )
            svc.schedule_xuat_sync(pid)
            svc.bus.publish(
                "live_entry",
                {"project_id": pid, "entry": entry.to_dict(), "stats": store.stats(), "from_cache": True},
            )
            return builtin
        mem_hit = svc.global_store.get_memory(text, cfg.target_lang)
        if mem_hit and check_translation_quality(text, mem_hit).passed:
            entry = store.upsert_entry(
                text, mem_hit, status=STATUS_TRANSLATED, source="xuat_live", quality_note=""
            )
            svc.schedule_xuat_sync(pid)
            svc.bus.publish(
                "live_entry",
                {"project_id": pid, "entry": entry.to_dict(), "stats": store.stats(), "from_cache": True},
            )
            return mem_hit

        # Singleflight dedup: identical concurrent live requests wait for the leader
        sf_key = (pid, text)
        is_leader, sf_evt = svc.acquire_singleflight(sf_key)
        if not is_leader:
            sf_evt.wait(timeout=30.0)
            cached = store.get_by_original(text)
            if cached and cached.status == STATUS_SKIPPED:
                return text
            if cached and cached.translation:
                return cached.translation

        svc.begin_live_request()
        client: LLMClient | None = None
        outcome = None
        try:
            pipeline = TranslationPipeline(
                svc.global_store,
                store,
                target_lang=cfg.target_lang,
                temperature=cfg.translate_role.temperature,
                max_tokens=cfg.translate_role.max_tokens,
            )
            client = svc.runtime.create_client_for_role("translate", cfg)
            outcome = pipeline.translate_one(text, client, source="xuat_live")
        except Exception as exc:
            # Never return HTTP 500 or empty string to XUAT (prevents 3x retry + SPAM SHUTDOWN)
            err_entry = store.upsert_entry(
                text, "", status=STATUS_FAILED, source="xuat_live", quality_note=str(exc)[:120]
            )
            svc.bus.publish(
                "live_entry",
                {
                    "project_id": pid,
                    "entry": err_entry.to_dict(),
                    "stats": store.stats(),
                    "from_cache": False,
                },
            )
            return text
        finally:
            if client:
                client.close()
            svc.end_live_request()
            if is_leader:
                svc.release_singleflight(sf_key, sf_evt)

        if outcome.ok and not outcome.from_cache:
            svc.schedule_xuat_sync(pid)
        svc.bus.publish(
            "live_entry",
            {
                "project_id": pid,
                "entry": outcome.entry.to_dict(),
                "stats": store.stats(),
                "from_cache": outcome.from_cache,
            },
        )
        return (outcome.translation or "").strip() or text

    @app.get("/xuat/translate")
    @app.get("/xuat/translate/{project_id}")
    async def xuat_translate_get(
        project_id: str | None = None,
        text: str = Query(default=""),
        from_lang: str = Query(default="en", alias="from"),
        to_lang: str = Query(default="zh", alias="to"),
    ):
        _ = (from_lang, to_lang)
        translated = await asyncio.to_thread(_handle_xuat_text, project_id, text)
        return PlainTextResponse(translated)

    @app.post("/xuat/translate")
    @app.post("/xuat/translate/{project_id}")
    async def xuat_translate_post(request: Request, project_id: str | None = None):
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("texts"), list):
                items = [str(item) for item in body["texts"]]
                # Run batch items concurrently so inventory/skill screens appear much faster
                tasks = [asyncio.to_thread(_handle_xuat_text, project_id, t) for t in items]
                results = await asyncio.gather(*tasks)
                return {"translations": list(results)}
            text = str((body or {}).get("text") or "")
            translated = await asyncio.to_thread(_handle_xuat_text, project_id, text)
            return {"translation": translated}
        raw = (await request.body()).decode("utf-8", errors="replace")
        translated = await asyncio.to_thread(_handle_xuat_text, project_id, raw)
        return PlainTextResponse(translated)

    # ── Projects & Hook Management ────────────────────────────────────────

    @app.get("/api/projects")
    async def list_projects():
        projects = svc.global_store.list_projects()
        items = []
        for p in projects:
            st = svc.project_store(p.id).stats()
            d = p.to_dict()
            d["stats"] = st
            items.append(d)
        return {
            "projects": items,
            "active_project_id": svc.active_project_id,
            "version": VERSION,
        }

    @app.post("/api/projects/pick-folder")
    async def pick_folder():
        selected = await asyncio.to_thread(_pick_folder_native)
        return {"path": selected}

    @app.post("/api/projects/open")
    async def open_project(req: OpenProjectReq):
        p = Path(req.game_dir.strip().strip('"'))
        if not p.is_dir():
            raise HTTPException(status_code=400, detail="请选择有效的游戏文件夹路径")
        project, import_stats = await asyncio.to_thread(svc.open_game_dir, str(p))
        store = svc.project_store(project.id)
        return {
            "project": project.to_dict(),
            "stats": store.stats(),
            "context": store.load_context().to_dict(),
            "imported": import_stats,
        }

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        svc.active_project_id = project_id
        store = svc.project_store(project_id)
        p_dict = project.to_dict()
        p_dict["protection_warning"] = (
            detect_protection_markers(Path(project.game_dir))
            if Path(project.game_dir).is_dir()
            else ""
        )
        return {
            "project": p_dict,
            "stats": store.stats(),
            "context": store.load_context().to_dict(),
        }

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str):
        await asyncio.to_thread(svc.remove_project, project_id)
        return {"ok": True}

    @app.post("/api/projects/{project_id}/clear")
    async def clear_project(project_id: str, req: ClearProjectReq):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        res = await asyncio.to_thread(
            svc.clear_project_records,
            project_id,
            clear_game_dict=req.clear_game_dict,
            clear_context=req.clear_context,
            rescan=req.rescan,
        )
        return {"ok": True, **res}

    @app.post("/api/projects/{project_id}/deploy")
    async def deploy_project(project_id: str, req: DeployReq):
        try:
            res = await asyncio.to_thread(svc.deploy_project_hook, project_id, req.loader)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        project = svc.global_store.get_project(project_id)
        return {"ok": True, "deploy": res, "project": project.to_dict() if project else None}

    @app.post("/api/projects/{project_id}/uninstall")
    async def uninstall_project(project_id: str):
        try:
            res = await asyncio.to_thread(svc.uninstall_project_hook, project_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        project = svc.global_store.get_project(project_id)
        return {"ok": True, "uninstall": res, "project": project.to_dict() if project else None}

    @app.post("/api/projects/{project_id}/import-xuat")
    async def import_xuat(project_id: str):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        res = await asyncio.to_thread(svc.import_xuat_from_game, project_id)
        return {"ok": True, "imported": res, "stats": svc.project_store(project_id).stats()}

    @app.post("/api/projects/{project_id}/scan")
    async def scan_project_texts(project_id: str):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        imp = await asyncio.to_thread(svc.import_xuat_from_game, project_id)
        scanned = await asyncio.to_thread(svc.scan_game_texts, project_id)
        added = imp["added"] + scanned["added"]
        return {
            "ok": True,
            "added": added,
            "updated": imp["updated"],
            "by_source": scanned.get("by_source", {}),
            "stats": svc.project_store(project_id).stats(),
        }

    @app.post("/api/projects/{project_id}/launch")
    async def launch_game(project_id: str, req: LaunchReq | None = None):
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        game_dir = Path(project.game_dir)
        exe = game_dir / f"{project.name}.exe"
        if not exe.is_file():
            cands = [
                p for p in sorted(game_dir.glob("*.exe"))
                if "crashhandler" not in p.name.casefold() and "unins" not in p.name.casefold()
            ]
            exe = cands[0] if cands else None
        if exe is None or not exe.is_file():
            raise HTTPException(status_code=404, detail="未在游戏目录找到可执行程序（.exe）")

        # Auto-ensure hook is deployed, safe config refreshed & dictionary synced before launching
        auto_deployed = False
        if not project.hook_deployed and project.runtime_type in ("mono", "il2cpp"):
            try:
                await asyncio.to_thread(svc.deploy_project_hook, project_id, project.loader)
                auto_deployed = True
            except Exception:
                pass
        else:
            await asyncio.to_thread(svc.refresh_project_hook_config, project_id)
            await asyncio.to_thread(svc.export_xuat_to_game, project_id)

        # Pre-warm local translation engine asynchronously so first in-game live text never cold-stalls
        cfg = svc.settings()
        if cfg.translate_role.mode == "local":
            threading.Thread(
                target=lambda: _safe_prewarm(svc, cfg.hardware_mode),
                daemon=True,
            ).start()

        cmd = [str(exe)]
        if req and req.borderless:
            cmd.append("-popupwindow")
        try:
            subprocess.Popen(cmd, cwd=str(game_dir))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"启动游戏失败：{exc}") from exc
        return {
            "ok": True,
            "executable": exe.name,
            "borderless": bool(req and req.borderless),
            "auto_deployed": auto_deployed,
        }

    @app.post("/api/projects/{project_id}/import-text")
    async def import_text(project_id: str, req: ImportTextReq):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        pairs: list[tuple[str, str]] = []
        for raw_line in (req.content or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            decoded = decode_xuat_line(line)
            if decoded is not None:
                pairs.append(decoded)
            else:
                pairs.append((line, ""))
        res = svc.project_store(project_id).bulk_import_pairs(pairs, source="manual")
        return {"ok": True, "imported": res, "stats": svc.project_store(project_id).stats()}

    @app.post("/api/projects/{project_id}/export-xuat")
    async def export_xuat(project_id: str):
        stats = await asyncio.to_thread(svc.export_xuat_to_game, project_id)
        if stats is None:
            raise HTTPException(status_code=404, detail="项目或游戏目录不存在")
        return {
            "ok": True,
            "exported": stats.exported,
            "regex_rules": stats.regex_rules,
            "skipped_lossy": stats.skipped_lossy,
            "path": str(stats.path),
        }

    @app.get("/api/projects/{project_id}/export-text")
    async def export_text_file(
        project_id: str,
        fmt: str = Query(default="json"),
        scope: str = Query(default="done"),
    ):
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        entries = svc.project_store(project_id).all_entries()
        try:
            raw, media_type, ext = export_entries_as_format(project, entries, fmt=fmt, scope=scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        safe_name = "".join(c for c in project.name if c not in r'\/:*?"<>|').strip() or "game"
        suffix = "_对照" if fmt == "bilingual" else ""
        filename = f"{safe_name}_汉化文本{suffix}.{ext}"
        return Response(
            content=raw,
            media_type=media_type,
            headers={"Content-Disposition": _content_disposition(filename)},
        )

    @app.get("/api/projects/{project_id}/export-patch")
    async def export_standalone_patch(project_id: str):
        try:
            raw_zip, filename = await asyncio.to_thread(svc.export_standalone_patch, project_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=raw_zip,
            media_type="application/zip",
            headers={"Content-Disposition": _content_disposition(filename)},
        )

    # ── Entries (View / Search / Edit / Lock / Restore / Skip / Replace / Retranslate) ──

    @app.get("/api/projects/{project_id}/entries")
    async def list_entries(
        project_id: str,
        status: str = "all",
        source: str = "all",
        search: str = "",
        offset: int = 0,
        limit: int = 50,
    ):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)
        entries, total = store.query_entries(
            status=status,
            source=source,
            search=search,
            offset=max(0, offset),
            limit=min(200, max(1, limit)),
        )
        return {
            "entries": [e.to_dict() for e in entries],
            "total": total,
            "stats": store.stats(),
        }

    @app.post("/api/projects/{project_id}/entries")
    async def create_entry(project_id: str, req: CreateEntryReq):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        orig = req.original.strip()
        if not orig:
            raise HTTPException(status_code=400, detail="原文不能为空")
        store = svc.project_store(project_id)
        tr = req.translation.strip()
        entry = store.upsert_entry(
            orig,
            tr,
            status=STATUS_EDITED if tr else STATUS_PENDING,
            source="manual",
            locked=bool(tr),
            preserve_locked=False,
        )
        return {"entry": entry.to_dict(), "stats": store.stats()}

    @app.patch("/api/projects/{project_id}/entries/{entry_id}")
    async def update_entry(project_id: str, entry_id: int, req: UpdateEntryReq):
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)
        if req.status is not None and req.status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail="无效状态")

        new_status = req.status
        new_locked = req.locked
        if req.translation is not None and new_status is None:
            new_status = STATUS_EDITED if req.translation.strip() else STATUS_PENDING
        if req.translation is not None and req.translation.strip() and new_locked is None:
            new_locked = True

        entry = store.update_entry_by_id(
            entry_id,
            translation=req.translation,
            status=new_status,
            locked=new_locked,
            quality_note="" if req.translation is not None else None,
            review_reason="" if new_status == STATUS_EDITED else None,
        )
        if not entry:
            raise HTTPException(status_code=404, detail="条目不存在")

        cfg = svc.settings()
        if req.translation is not None and entry.translation.strip():
            svc.global_store.put_memory(
                entry.original, entry.translation.strip(), cfg.target_lang, source="manual"
            )
            paths = get_xuat_paths(project.game_dir, project.loader)
            if cfg.auto_sync_xuat and (project.hook_deployed or paths["translation_dir"].is_dir()):
                await asyncio.to_thread(svc.export_xuat_to_game, project_id)

        svc.bus.publish(
            "live_entry",
            {
                "project_id": project_id,
                "entry": entry.to_dict(),
                "stats": store.stats(),
                "from_cache": True,
            },
        )
        return {"entry": entry.to_dict(), "stats": store.stats()}

    @app.post("/api/projects/{project_id}/entries/{entry_id}/restore")
    async def restore_entry(project_id: str, entry_id: int):
        entry = await asyncio.to_thread(svc.restore_entry_translation, project_id, entry_id)
        if not entry:
            raise HTTPException(status_code=400, detail="该词条暂无可还原的上一版译文")
        return {"entry": entry.to_dict(), "stats": svc.project_store(project_id).stats()}

    @app.post("/api/projects/{project_id}/entries/{entry_id}/skip")
    async def skip_entry_route(project_id: str, entry_id: int):
        entry = await asyncio.to_thread(svc.skip_entry, project_id, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="条目不存在")
        return {"entry": entry.to_dict(), "stats": svc.project_store(project_id).stats()}

    @app.post("/api/projects/{project_id}/entries/{entry_id}/unskip")
    async def unskip_entry_route(project_id: str, entry_id: int):
        entry = await asyncio.to_thread(svc.unskip_entry, project_id, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="条目不存在")
        return {"entry": entry.to_dict(), "stats": svc.project_store(project_id).stats()}

    @app.post("/api/projects/{project_id}/replace")
    async def bulk_replace(project_id: str, req: BulkReplaceReq):
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        if not req.find:
            raise HTTPException(status_code=400, detail="查找内容不能为空")
        store = svc.project_store(project_id)
        changed = store.bulk_replace_translations(
            req.find, req.replace, only_unlocked=req.only_unlocked
        )
        if changed:
            cfg = svc.settings()
            paths = get_xuat_paths(project.game_dir, project.loader)
            if cfg.auto_sync_xuat and (project.hook_deployed or paths["translation_dir"].is_dir()):
                await asyncio.to_thread(svc.export_xuat_to_game, project_id)
        return {"ok": True, "replaced": len(changed), "stats": store.stats()}

    @app.post("/api/projects/{project_id}/entries/{entry_id}/retranslate")
    async def retranslate_entry(project_id: str, entry_id: int, req: RetranslateReq):
        entry, ok, note = await asyncio.to_thread(
            svc.retranslate_entry_now, project_id, entry_id, req.hint
        )
        if entry is None:
            raise HTTPException(status_code=404, detail=note or "条目不存在")
        return {
            "entry": entry.to_dict(),
            "ok": ok,
            "note": note,
            "stats": svc.project_store(project_id).stats(),
        }

    @app.delete("/api/projects/{project_id}/entries/{entry_id}")
    async def delete_entry(project_id: str, entry_id: int):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)
        store.delete_entry(entry_id)
        return {"ok": True, "stats": store.stats()}

    # ── Shared Concurrent Batch Translation Helper ────────────────────────

    def _run_concurrent_translate(
        project_id: str,
        targets: list,
        task,
        cancel: threading.Event,
        emit,
        *,
        retranslate_all: bool = False,
        base_offset: int = 0,
    ) -> tuple[int, int]:
        """Run concurrent translation with neighbor context window and live-first preemption."""
        cfg = svc.settings()
        store = svc.project_store(project_id)
        ctx = store.load_context()
        glossary = store.list_glossary() + svc.global_store.list_glossary()
        pipeline = TranslationPipeline(
            svc.global_store,
            store,
            target_lang=cfg.target_lang,
            temperature=cfg.translate_role.temperature,
            max_tokens=cfg.translate_role.max_tokens,
        )
        # Neighbor map for dialogue coherence
        prev_map: dict[int, list[str]] = {}
        for i, e in enumerate(targets):
            if e.id is not None:
                prev_map[e.id] = [targets[j].original for j in range(max(0, i - 2), i)]

        workers = 4 if len(targets) >= 4 else 1
        client = svc.runtime.create_client_for_role("translate", cfg)
        done_n = 0
        fail_n = 0
        last_push = 0.0
        progress_lock = threading.Lock()
        use_cloud_batch = (
            bool(getattr(client, "is_cloud", False))
            and not is_hy_mt_model(client.model)
            and len(targets) >= 2
        )

        def _work_one(entry):
            if cancel.is_set():
                return None
            svc.wait_if_live_busy(cancel)
            if cancel.is_set():
                return None
            return pipeline.translate_one(
                entry.original,
                client,
                source=entry.source,
                force_retranslate=retranslate_all,
                context=ctx,
                glossary=glossary,
                prev_originals=prev_map.get(entry.id or -1),
            )

        try:
            if use_cloud_batch:
                def _on_batch_item(outcome):
                    nonlocal done_n, fail_n, last_push
                    if outcome.ok:
                        done_n += 1
                        task.done += 1
                    else:
                        fail_n += 1
                        task.failed += 1
                    task.current = base_offset + done_n + fail_n
                    now = time.monotonic()
                    is_last = (done_n + fail_n) >= len(targets)
                    if is_last or (now - last_push) >= 0.35:
                        last_push = now
                        svc.bus.publish(
                            "live_entry",
                            {
                                "project_id": project_id,
                                "entry": outcome.entry.to_dict(),
                                "stats": store.stats(),
                                "from_cache": outcome.from_cache,
                            },
                        )
                    emit()

                pipeline.translate_batch_cloud(
                    targets,
                    client,
                    force_retranslate=retranslate_all,
                    context=ctx,
                    glossary=glossary,
                    is_cancelled=cancel.is_set,
                    on_item=_on_batch_item,
                )
            elif workers == 1:
                for entry in targets:
                    outcome = _work_one(entry)
                    if outcome is None:
                        break
                    task.current = base_offset + done_n + fail_n + 1
                    if outcome.ok:
                        done_n += 1
                        task.done += 1
                    else:
                        fail_n += 1
                        task.failed += 1
                    svc.bus.publish(
                        "live_entry",
                        {
                            "project_id": project_id,
                            "entry": outcome.entry.to_dict(),
                            "stats": store.stats(),
                            "from_cache": outcome.from_cache,
                        },
                    )
                    emit()
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(_work_one, e) for e in targets]
                    for fut in as_completed(futures):
                        outcome = fut.result()
                        if outcome is None:
                            continue
                        with progress_lock:
                            if outcome.ok:
                                done_n += 1
                                task.done += 1
                            else:
                                fail_n += 1
                                task.failed += 1
                            task.current = base_offset + done_n + fail_n
                            now = time.monotonic()
                            is_last = (done_n + fail_n) >= len(targets)
                            if is_last or (now - last_push) >= 0.35:
                                last_push = now
                                svc.bus.publish(
                                    "live_entry",
                                    {
                                        "project_id": project_id,
                                        "entry": outcome.entry.to_dict(),
                                        "stats": store.stats(),
                                        "from_cache": outcome.from_cache,
                                    },
                                )
                            emit()
            used_tokens = int(getattr(client, "total_tokens", 0))
            is_cloud = bool(getattr(client, "is_cloud", False))
        finally:
            client.close()
        return done_n, fail_n, (used_tokens if is_cloud else 0)

    # ── Background Tasks (Auto Pipeline / Translate / Context / Review) ───

    @app.post("/api/projects/{project_id}/tasks/auto")
    async def start_auto_localize(project_id: str, req: AutoLocalizeReq):
        """One-click end-to-end localization pipeline: Scan -> Context -> Translate -> Review -> Deploy."""
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")

        def worker(task, cancel, emit):
            store = svc.project_store(project_id)
            cfg = svc.settings()

            # Stage 1: Read-only static scan
            task.message = "① 正在扫描游戏静态文本..."
            emit()
            svc.import_xuat_from_game(project_id)
            svc.scan_game_texts(project_id)
            all_entries = store.all_entries()
            if not all_entries:
                if req.deploy_hook and not project.hook_deployed:
                    task.message = "未扫描到静态文本，正在接入实时汉化插件..."
                    emit()
                    svc.deploy_project_hook(project_id)
                task.message = "已完成接入，当前无静态文本，请启动游戏以实时捕获翻译"
                return

            # Stage 2: Context inference + auto glossary extraction (skipped when disable_reasoning=True)
            skip_reasoning = bool(cfg.disable_reasoning)
            ctx = store.load_context()
            if not skip_reasoning and not ctx.is_ready and not cancel.is_set():
                task.message = "② 正在分析游戏语境与专有名词..."
                emit()
                r_cli = svc.runtime.create_client_for_role("reason", cfg)
                try:
                    ctx = infer_game_context(r_cli, all_entries, game_name=project.name)
                    store.save_context(ctx)
                    sync_context_terms_to_glossary(ctx, store)
                    svc.bus.publish("context_updated", {"project_id": project_id, "context": ctx.to_dict()})
                finally:
                    r_cli.close()

            # Stage 3: Concurrent batch translation
            targets = [
                e for e in store.all_entries((STATUS_PENDING, STATUS_FAILED)) if not e.locked
            ]
            do_review = bool(req.run_review) and not skip_reasoning
            review_est = min(req.review_limit, len(targets) + store.stats().get("translated", 0)) if do_review else 0
            task.total = max(1, len(targets) + review_est)
            tr_done = 0
            cloud_tokens = 0
            if targets and not cancel.is_set():
                step_tr = "②" if skip_reasoning else "③"
                task.message = f"{step_tr} 正在批量翻译 ({len(targets)} 条)..."
                emit()
                tr_done, _tr_fail, tok = _run_concurrent_translate(
                    project_id, targets, task, cancel, emit, retranslate_all=False, base_offset=0
                )
                cloud_tokens += tok

            # Stage 4: Fast batched AI review & auto-fix (skipped when disable_reasoning=True)
            fixed_count = 0
            if do_review and not cancel.is_set():
                rev_candidates = [
                    e for e in store.all_entries((STATUS_TRANSLATED, STATUS_FLAGGED))
                    if not e.locked and (not e.review_level or e.status == STATUS_FLAGGED)
                ][: max(1, req.review_limit)]
                task.total = max(1, len(targets) + len(rev_candidates))
                if rev_candidates:
                    task.message = f"④ 正在批量审校并自动修正 ({len(rev_candidates)} 条)..."
                    emit()
                    reason_cli = svc.runtime.create_client_for_role("reason", cfg)
                    tr_cli = svc.runtime.create_client_for_role("translate", cfg)
                    try:
                        pipeline = TranslationPipeline(
                            svc.global_store, store, target_lang=cfg.target_lang
                        )

                        rev_last_push = 0.0

                        def _on_rev(updated, _verdict):
                            nonlocal fixed_count, rev_last_push
                            task.current += 1
                            if updated.review_level == "FIXED":
                                fixed_count += 1
                            now = time.monotonic()
                            if (now - rev_last_push) >= 0.35 or task.current >= task.total:
                                rev_last_push = now
                                svc.bus.publish(
                                    "live_entry",
                                    {
                                        "project_id": project_id,
                                        "entry": updated.to_dict(),
                                        "stats": store.stats(),
                                        "from_cache": False,
                                    },
                                )
                            emit()

                        review_batch_and_refine(
                            rev_candidates,
                            reason_cli,
                            tr_cli,
                            pipeline,
                            store.load_context(),
                            batch_size=10,
                            mode="smart",
                            is_cancelled=cancel.is_set,
                            on_item=_on_rev,
                        )
                        if getattr(reason_cli, "is_cloud", False):
                            cloud_tokens += int(getattr(reason_cli, "total_tokens", 0))
                        if getattr(tr_cli, "is_cloud", False):
                            cloud_tokens += int(getattr(tr_cli, "total_tokens", 0))
                    finally:
                        reason_cli.close()
                        tr_cli.close()

            # Stage 5: Deploy Hook & export curated dictionary
            if not cancel.is_set():
                step_dep = "③" if skip_reasoning else "⑤"
                refreshed_proj = svc.global_store.get_project(project_id)
                if req.deploy_hook and refreshed_proj and not refreshed_proj.hook_deployed:
                    task.message = f"{step_dep} 正在接入无损汉化插件并同步词典..."
                    emit()
                    svc.deploy_project_hook(project_id)
                else:
                    task.message = f"{step_dep} 正在同步词典到游戏目录..."
                    emit()
                    svc.export_xuat_to_game(project_id)

                st = store.stats()
                summary = f"自动汉化完成：共 {st['done']} 条就绪"
                if tr_done:
                    summary += f"（新译 {tr_done} 条"
                    if fixed_count:
                        summary += f"，自动修正 {fixed_count} 条"
                    summary += "）"
                elif fixed_count:
                    summary += f"（自动修正 {fixed_count} 条）"
                if cloud_tokens > 0:
                    summary += f" · 云端消耗 {cloud_tokens:,} tokens"
                task.current = task.total
                task.message = summary

        try:
            task = svc.tasks.start("auto", project_id, worker, initial_message="正在启动一键自动汉化...")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": task}

    @app.post("/api/projects/{project_id}/tasks/translate")
    async def start_batch_translate(project_id: str, req: BatchTranslateReq):
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)

        if store.stats()["total"] == 0:
            await asyncio.to_thread(svc.scan_game_texts, project_id)

        if req.retranslate_all:
            targets_preview = [e for e in store.all_entries() if not e.locked and e.status != "skipped"]
        else:
            statuses = (STATUS_PENDING, STATUS_FAILED) if req.include_failed else (STATUS_PENDING,)
            targets_preview = [e for e in store.all_entries(statuses) if not e.locked]
        if not targets_preview:
            msg = (
                "当前没有待翻译词条（已有词条均已翻译完成）"
                if store.stats()["total"] > 0
                else "未扫描到静态文本，请先接入汉化并启动游戏以实时捕获文本"
            )
            raise HTTPException(status_code=400, detail=msg)

        def worker(task, cancel, emit):
            cfg = svc.settings()
            if req.retranslate_all:
                targets = [e for e in store.all_entries() if not e.locked and e.status != "skipped"]
            else:
                statuses = (STATUS_PENDING, STATUS_FAILED) if req.include_failed else (STATUS_PENDING,)
                targets = [e for e in store.all_entries(statuses) if not e.locked]
            task.total = len(targets)
            task.message = "正在启动翻译引擎..."
            emit()
            task.message = "正在批量翻译..."
            emit()
            done_n, fail_n, cloud_tokens = _run_concurrent_translate(
                project_id, targets, task, cancel, emit, retranslate_all=req.retranslate_all
            )
            if cfg.auto_sync_xuat and project.hook_deployed and done_n > 0:
                svc.export_xuat_to_game(project_id)
            tok_note = f" · 云端消耗 {cloud_tokens:,} tokens" if cloud_tokens > 0 else ""
            task.message = f"翻译完成：{done_n} 条成功" + (f"，{fail_n} 条待处理" if fail_n else "") + tok_note

        try:
            task = svc.tasks.start("translate", project_id, worker, initial_message="准备批量翻译...")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": task}

    @app.post("/api/projects/{project_id}/tasks/context")
    async def start_context_build(project_id: str):
        if svc.settings().disable_reasoning:
            raise HTTPException(
                status_code=400,
                detail="当前已开启「关闭推理与审校」模式，如需识别语境请先在设置中关闭该选项",
            )
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)

        if store.stats()["total"] == 0:
            await asyncio.to_thread(svc.scan_game_texts, project_id)
        if not store.all_entries():
            raise HTTPException(
                status_code=400,
                detail="暂无游戏文本样本，请先扫描静态文本、启动游戏捕获或手动添加词条",
            )

        def worker(task, cancel, emit):
            _ = cancel
            cfg = svc.settings()
            entries = store.all_entries()
            task.total = 1
            task.message = "正在分析游戏语境与专有名词..."
            emit()
            client = svc.runtime.create_client_for_role("reason", cfg)
            try:
                ctx = infer_game_context(client, entries, game_name=project.name)
                store.save_context(ctx)
                synced = sync_context_terms_to_glossary(ctx, store)
                task.current = 1
                task.done = 1
                task.message = (
                    f"语境档案已生成，并自动同步 {synced} 条术语到本游戏词库"
                    if synced
                    else "游戏语境档案已生成"
                )
                svc.bus.publish("context_updated", {"project_id": project_id, "context": ctx.to_dict()})
            finally:
                client.close()

        try:
            task = svc.tasks.start("context", project_id, worker, initial_message="正在分析游戏语境...")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": task}

    @app.post("/api/projects/{project_id}/tasks/review")
    async def start_batch_review(project_id: str, req: BatchReviewReq):
        if svc.settings().disable_reasoning:
            raise HTTPException(
                status_code=400,
                detail="当前已开启「关闭推理与审校」模式，如需 AI 审校请先在设置中关闭该选项",
            )
        project = svc.global_store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)
        all_glossary = store.list_glossary() + svc.global_store.list_glossary()

        def _select_review_targets() -> tuple[list, bool]:
            all_eligible = [
                e for e in store.all_entries((STATUS_TRANSLATED, STATUS_FLAGGED))
                if not e.locked
            ]
            if not all_eligible:
                return [], False
            if not req.only_unreviewed or req.mode == "full":
                return all_eligible[: max(1, req.limit)], True
            unreviewed = [
                e for e in all_eligible
                if not e.review_level or e.status == STATUS_FLAGGED
            ]
            if unreviewed:
                return unreviewed[: max(1, req.limit)], False
            # All entries were reviewed previously: allow re-reviewing high-risk entries automatically
            high_risk = [e for e in all_eligible if is_high_risk_for_review(e, all_glossary)]
            if high_risk:
                return high_risk[: max(1, req.limit)], True
            return [], True

        all_translated_cnt = len([
            e for e in store.all_entries((STATUS_TRANSLATED, STATUS_FLAGGED)) if not e.locked
        ])
        targets_preview, already_reviewed_all = _select_review_targets()
        if not targets_preview:
            if all_translated_cnt == 0:
                edited_cnt = store.stats().get("edited", 0)
                if edited_cnt > 0:
                    detail = f"当前 {edited_cnt} 条译文均为人工精修保护状态，无需 AI 审校"
                else:
                    detail = "当前没有已翻译词条，请先完成翻译"
            else:
                detail = (
                    f"已有 {all_translated_cnt} 条译文均已通过审校，且无高风险长句需要复审；"
                    "如需强制逐条复查，请点击「更多 ▾ → 深度全量审校」"
                )
            raise HTTPException(status_code=400, detail=detail)

        def worker(task, cancel, emit):
            cfg = svc.settings()
            targets, is_rereview = _select_review_targets()
            task.total = max(1, len(targets))
            task.message = "正在拉起审校模型..."
            emit()
            reason_client = svc.runtime.create_client_for_role("reason", cfg)
            translate_client = svc.runtime.create_client_for_role("translate", cfg)
            try:
                pipeline = TranslationPipeline(
                    svc.global_store,
                    store,
                    target_lang=cfg.target_lang,
                    temperature=cfg.translate_role.temperature,
                    max_tokens=cfg.translate_role.max_tokens,
                )
                ctx = store.load_context()
                if req.mode == "full":
                    task.message = f"正在深度全量审校 ({len(targets)} 条)..."
                elif is_rereview:
                    task.message = f"全部词条曾审校过，正在复审 {len(targets)} 条高风险译文..."
                else:
                    task.message = f"正在智能审校并自动修正 ({len(targets)} 条)..."
                emit()
                counters = {"passed": 0, "fixed": 0}
                rev_last_push = 0.0

                def _on_reviewed(updated, verdict):
                    nonlocal rev_last_push
                    task.current += 1
                    if updated.review_level == "FIXED":
                        task.done += 1
                        counters["fixed"] += 1
                    elif verdict.level in ("PASS", "MINOR"):
                        task.done += 1
                        counters["passed"] += 1
                    else:
                        task.failed += 1
                    now = time.monotonic()
                    if (now - rev_last_push) >= 0.35 or task.current >= task.total:
                        rev_last_push = now
                        svc.bus.publish(
                            "live_entry",
                            {
                                "project_id": project_id,
                                "entry": updated.to_dict(),
                                "stats": store.stats(),
                                "from_cache": False,
                            },
                        )
                    emit()

                review_batch_and_refine(
                    targets,
                    reason_client,
                    translate_client,
                    pipeline,
                    ctx,
                    glossary=all_glossary,
                    batch_size=10,
                    mode=req.mode,
                    is_cancelled=cancel.is_set,
                    on_item=_on_reviewed,
                )
                if cfg.auto_sync_xuat and project.hook_deployed:
                    svc.export_xuat_to_game(project_id)
                cloud_tokens = 0
                if getattr(reason_client, "is_cloud", False):
                    cloud_tokens += int(getattr(reason_client, "total_tokens", 0))
                if getattr(translate_client, "is_cloud", False):
                    cloud_tokens += int(getattr(translate_client, "total_tokens", 0))
                parts = [f"{counters['passed']} 条通过"]
                if counters["fixed"]:
                    parts.append(f"{counters['fixed']} 条已自动修正")
                if task.failed:
                    parts.append(f"{task.failed} 条待人工复核")
                tok_note = f" · 云端消耗 {cloud_tokens:,} tokens" if cloud_tokens > 0 else ""
                task.message = "审校完成：" + "，".join(parts) + tok_note
            finally:
                reason_client.close()
                translate_client.close()

        try:
            task = svc.tasks.start("review", project_id, worker, initial_message="准备批量审校...")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": task}

    @app.get("/api/tasks/current")
    async def get_current_task():
        return {"task": svc.tasks.current_task()}

    @app.post("/api/tasks/cancel")
    async def cancel_task():
        return {"cancelled": svc.tasks.cancel()}

    # ── In-Game Floating Overlay HUD (Global Hotkey Alt+L) ────────────────

    @app.get("/api/overlay/status")
    async def overlay_status():
        if svc.overlay is None:
            return {"enabled": False, "visible": False, "hotkey": "Alt+L", "hotkey_active": False}
        return svc.overlay.status

    @app.post("/api/overlay/toggle")
    async def overlay_toggle():
        if svc.overlay is None:
            from ul10n.overlay.hud import InGameOverlayHUD

            svc.overlay = InGameOverlayHUD(svc)
            svc.overlay.start()
        visible = svc.overlay.toggle()
        return {"ok": True, "visible": visible, "hotkey": svc.overlay.hotkey_label}

    # ── Context / Glossary / Memory ───────────────────────────────────────

    @app.put("/api/projects/{project_id}/context")
    async def save_context(project_id: str, req: ContextReq):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        ctx = GameContext.from_dict(req.model_dump())
        store = svc.project_store(project_id)
        store.save_context(ctx)
        synced = sync_context_terms_to_glossary(ctx, store)
        return {"context": ctx.to_dict(), "synced_terms": synced}

    @app.post("/api/projects/{project_id}/context/sync-glossary")
    async def sync_context_glossary(project_id: str):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)
        added = sync_context_terms_to_glossary(store.load_context(), store)
        return {"ok": True, "added": added}

    def _parse_glossary_import_lines(content: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for raw in (content or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            for sep in ("=", "：", "\t"):
                if sep in line:
                    left, right = line.split(sep, 1)
                    t, tr = left.strip(), right.strip()
                    if t and tr:
                        out.append((t, tr, "术语"))
                    break
        return out

    @app.get("/api/glossary/summary")
    async def glossary_summary(project_id: str = ""):
        proj_cnt = 0
        if project_id and svc.global_store.get_project(project_id):
            proj_cnt = len(svc.project_store(project_id).list_glossary())
        return {
            "project_glossary": proj_cnt,
            "global_glossary": len(svc.global_store.list_glossary()),
            "global_memory": svc.global_store.count_memory(),
        }

    @app.get("/api/projects/{project_id}/glossary")
    async def list_project_glossary(project_id: str, search: str = ""):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        items = svc.project_store(project_id).list_glossary()
        q = search.strip().casefold()
        if q:
            items = [i for i in items if q in i.term.casefold() or q in i.translation.casefold() or q in i.category.casefold()]
        return {"items": [i.to_dict() for i in items]}

    @app.post("/api/projects/{project_id}/glossary")
    async def upsert_project_glossary(project_id: str, req: GlossaryReq):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        if not req.term.strip() or not req.translation.strip():
            raise HTTPException(status_code=400, detail="原文术语和译名均不能为空")
        item = svc.project_store(project_id).upsert_glossary(
            req.term, req.translation, req.category, req.note
        )
        return {"item": item.to_dict()}

    @app.put("/api/projects/{project_id}/glossary/{item_id}")
    async def update_project_glossary(project_id: str, item_id: int, req: GlossaryReq):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        if not req.term.strip() or not req.translation.strip():
            raise HTTPException(status_code=400, detail="原文术语和译名均不能为空")
        item = svc.project_store(project_id).update_glossary(
            item_id, req.term, req.translation, req.category, req.note
        )
        if not item:
            raise HTTPException(status_code=404, detail="术语不存在")
        return {"item": item.to_dict()}

    @app.post("/api/projects/{project_id}/glossary/import")
    async def import_project_glossary(project_id: str, req: ImportTextReq):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        store = svc.project_store(project_id)
        pairs = _parse_glossary_import_lines(req.content)
        for t, tr, cat in pairs:
            store.upsert_glossary(t, tr, cat)
        return {"ok": True, "imported": len(pairs)}

    @app.delete("/api/projects/{project_id}/glossary")
    async def clear_project_glossary(project_id: str):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        removed = svc.project_store(project_id).clear_glossary()
        return {"ok": True, "removed": removed}

    @app.delete("/api/projects/{project_id}/glossary/{item_id}")
    async def delete_project_glossary(project_id: str, item_id: int):
        if not svc.global_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        svc.project_store(project_id).delete_glossary(item_id)
        return {"ok": True}

    @app.get("/api/glossary")
    async def list_global_glossary(search: str = ""):
        items = svc.global_store.list_glossary()
        q = search.strip().casefold()
        if q:
            items = [i for i in items if q in i.term.casefold() or q in i.translation.casefold() or q in i.category.casefold()]
        return {"items": [i.to_dict() for i in items]}

    @app.post("/api/glossary")
    async def upsert_global_glossary(req: GlossaryReq):
        if not req.term.strip() or not req.translation.strip():
            raise HTTPException(status_code=400, detail="原文术语和译名均不能为空")
        item = svc.global_store.upsert_glossary(req.term, req.translation, req.category, req.note)
        return {"item": item.to_dict()}

    @app.put("/api/glossary/{item_id}")
    async def update_global_glossary(item_id: int, req: GlossaryReq):
        if not req.term.strip() or not req.translation.strip():
            raise HTTPException(status_code=400, detail="原文术语和译名均不能为空")
        item = svc.global_store.update_glossary(item_id, req.term, req.translation, req.category, req.note)
        if not item:
            raise HTTPException(status_code=404, detail="术语不存在")
        return {"item": item.to_dict()}

    @app.post("/api/glossary/import")
    async def import_global_glossary(req: ImportTextReq):
        pairs = _parse_glossary_import_lines(req.content)
        for t, tr, cat in pairs:
            svc.global_store.upsert_glossary(t, tr, cat)
        return {"ok": True, "imported": len(pairs)}

    @app.delete("/api/glossary")
    async def clear_global_glossary():
        removed = svc.global_store.clear_glossary()
        return {"ok": True, "removed": removed}

    @app.delete("/api/glossary/{item_id}")
    async def delete_global_glossary(item_id: int):
        svc.global_store.delete_glossary(item_id)
        return {"ok": True}

    @app.get("/api/memory")
    async def list_memory(search: str = "", limit: int = 80):
        return {
            "items": svc.global_store.list_memory(search, min(200, max(1, limit))),
            "total": svc.global_store.count_memory(),
        }

    @app.patch("/api/memory/{src_hash}")
    async def update_memory_item(src_hash: str, payload: dict[str, Any]):
        tr = str((payload or {}).get("translation") or "").strip()
        if not tr:
            raise HTTPException(status_code=400, detail="译文不能为空")
        updated = svc.global_store.update_memory(src_hash, tr)
        if not updated:
            raise HTTPException(status_code=404, detail="记忆条目不存在")
        return {"item": updated}

    @app.delete("/api/memory")
    async def clear_all_memory():
        removed = svc.global_store.clear_memory()
        return {"ok": True, "removed": removed}

    @app.delete("/api/memory/{src_hash}")
    async def delete_memory(src_hash: str):
        svc.global_store.delete_memory(src_hash)
        return {"ok": True}

    # ── Settings, Fonts & Runtime ─────────────────────────────────────────

    @app.get("/api/fonts")
    async def get_fonts():
        fonts = [f.to_dict() for f in list_fonts(svc.app_root)]
        return {"fonts": fonts, "active_font_id": svc.settings().font_id}

    @app.get("/api/fonts/{font_id}/file")
    async def get_font_preview_file(font_id: str):
        opt = get_font_option(svc.app_root, font_id)
        if opt.id != font_id.strip().lower() or opt.preview_ttf_path is None or not opt.preview_ttf_path.is_file():
            raise HTTPException(status_code=404, detail="字体预览文件不存在")
        ext = opt.preview_ttf_path.suffix.lower()
        media_type = "font/otf" if ext == ".otf" else "font/ttf"
        return FileResponse(
            str(opt.preview_ttf_path),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/settings")
    async def get_settings():
        return {"settings": _mask_settings_dict(svc.settings()), "runtime": svc.runtime_status()}

    @app.put("/api/settings")
    async def save_settings(payload: dict[str, Any]):
        old = svc.settings()
        merged = old.to_dict()
        for k, v in payload.items():
            if k not in merged:
                continue
            if k in ("translate_role", "reason_role") and isinstance(v, dict):
                role_merged = dict(merged[k])
                old_key = str(role_merged.get("api_key") or "")
                for rk, rv in v.items():
                    if rk == "api_key":
                        sval = str(rv or "").strip()
                        # Preserve stored key if client submits the masked placeholder
                        if sval.startswith(_MASK_PREFIX):
                            if not old_key and k == "reason_role":
                                tr_key = str(merged.get("translate_role", {}).get("api_key") or "")
                                if tr_key and sval == _mask_secret(tr_key):
                                    role_merged["api_key"] = tr_key
                            continue
                        role_merged["api_key"] = sval
                    elif rk in role_merged:
                        role_merged[rk] = rv
                merged[k] = role_merged
            else:
                merged[k] = v
        updated = AppSettings.from_dict(merged)
        updated.font_id = get_font_option(svc.app_root, updated.font_id).id
        svc.global_store.save_settings(updated)
        if old.hardware_mode != updated.hardware_mode:
            await asyncio.to_thread(svc.runtime.stop_all)
        elif updated.disable_reasoning and not old.disable_reasoning:
            await asyncio.to_thread(svc.runtime.stop_role, "reason")
        if old.font_id != updated.font_id and svc.active_project_id:
            proj = svc.global_store.get_project(svc.active_project_id)
            if proj and proj.hook_deployed:
                try:
                    await asyncio.to_thread(svc.refresh_project_hook_config, proj.id)
                except Exception:
                    pass
        return {"settings": _mask_settings_dict(updated), "runtime": svc.runtime_status()}

    @app.get("/api/runtime/status")
    async def get_runtime_status():
        return svc.runtime_status()

    @app.post("/api/runtime/start")
    async def start_runtime(req: RuntimeRoleReq):
        role = req.role if req.role in ("translate", "reason") else "translate"
        try:
            await asyncio.to_thread(
                svc.runtime.ensure_local_server, role, hardware_mode=svc.settings().hardware_mode
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "runtime": svc.runtime_status()}

    @app.post("/api/runtime/stop")
    async def stop_runtime(req: RuntimeRoleReq):
        if req.role in ("translate", "reason"):
            await asyncio.to_thread(svc.runtime.stop_role, req.role)
        else:
            await asyncio.to_thread(svc.runtime.stop_all)
        return {"ok": True, "runtime": svc.runtime_status()}

    @app.post("/api/runtime/test")
    async def test_runtime(req: TestConnectionReq):
        def _test():
            if req.mode == "api":
                cfg = svc.settings()
                base_url = (req.base_url or "").strip()
                model = (req.model or "").strip()
                key = (req.api_key or "").strip()
                if req.role == "reason" and (not base_url or not model):
                    base_url = base_url or cfg.translate_role.base_url
                    model = model or cfg.translate_role.model
                if not base_url or not model:
                    raise ValueError("请填写接口地址和模型名称")
                if key.startswith(_MASK_PREFIX):
                    key = cfg.translate_role.api_key if req.role == "translate" else (cfg.reason_role.api_key or cfg.translate_role.api_key)
                elif not key and req.role == "reason" and base_url.rstrip("/") == cfg.translate_role.base_url.rstrip("/"):
                    key = cfg.translate_role.api_key
                client = LLMClient(base_url, key, model, timeout=20.0, max_retries=1)
            else:
                client = svc.runtime.create_client_for_role(req.role, svc.settings())
            try:
                return client.test_connection()
            finally:
                client.close()

        try:
            reply = await asyncio.to_thread(_test)
            return {"ok": True, "reply": reply}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── SSE Event Stream ──────────────────────────────────────────────────

    @app.get("/api/events")
    async def sse_events(request: Request):
        queue = svc.bus.subscribe()

        async def event_gen():
            try:
                init = {
                    "type": "hello",
                    "data": {
                        "runtime": svc.runtime_status(),
                        "task": svc.tasks.current_task(),
                    },
                }
                yield f"data: {json.dumps(init, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=20.0)
                        yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                svc.bus.unsubscribe(queue)

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    return app
