"""Clean SQLite persistence: GlobalStore (settings/projects/memory) + ProjectStore (per-game)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ul10n.core.models import (
    AppSettings,
    Entry,
    GameContext,
    GameProject,
    GlossaryItem,
    STATUS_EDITED,
    STATUS_FAILED,
    STATUS_FLAGGED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_TRANSLATED,
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def project_id_for_dir(game_dir: str | Path) -> str:
    norm = str(Path(game_dir).resolve()).casefold()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


class GlobalStore:
    """Global SQLite store: app settings, registered projects, cross-game memory & glossary."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "global.db"
        self._lock = threading.RLock()
        self.conn = _connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    game_dir TEXT UNIQUE NOT NULL,
                    runtime_type TEXT DEFAULT 'unknown',
                    arch TEXT DEFAULT 'x64',
                    unity_version TEXT DEFAULT '',
                    loader TEXT DEFAULT 'bepinex',
                    hook_deployed INTEGER DEFAULT 0,
                    last_opened TEXT DEFAULT '',
                    created_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS global_memory(
                    src_hash TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    original TEXT NOT NULL,
                    translation TEXT NOT NULL,
                    source TEXT DEFAULT 'ai',
                    updated_at TEXT DEFAULT '',
                    PRIMARY KEY (src_hash, target_lang)
                );
                CREATE INDEX IF NOT EXISTS idx_gmem_lang ON global_memory(target_lang);
                CREATE TABLE IF NOT EXISTS global_glossary(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term TEXT UNIQUE NOT NULL,
                    translation TEXT NOT NULL,
                    category TEXT DEFAULT '术语',
                    note TEXT DEFAULT ''
                );
            """)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── Settings ──────────────────────────────────────────────────────────

    def load_settings(self) -> AppSettings:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key='app_settings'"
            ).fetchone()
        if not row:
            return AppSettings()
        try:
            return AppSettings.from_dict(json.loads(row["value"]))
        except (ValueError, TypeError):
            return AppSettings()

    def save_settings(self, settings: AppSettings) -> None:
        payload = json.dumps(settings.to_dict(), ensure_ascii=False)
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES('app_settings', ?)",
                (payload,),
            )
            self.conn.commit()

    # ── Projects ──────────────────────────────────────────────────────────

    def upsert_project(self, project: GameProject) -> GameProject:
        now = _now()
        with self._lock:
            existing = self.conn.execute(
                "SELECT created_at, hook_deployed, loader FROM projects WHERE id=?",
                (project.id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else (project.created_at or now)
            self.conn.execute(
                """
                INSERT INTO projects(
                    id, name, game_dir, runtime_type, arch, unity_version,
                    loader, hook_deployed, last_opened, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    game_dir=excluded.game_dir,
                    runtime_type=excluded.runtime_type,
                    arch=excluded.arch,
                    unity_version=excluded.unity_version,
                    loader=excluded.loader,
                    hook_deployed=excluded.hook_deployed,
                    last_opened=excluded.last_opened
                """,
                (
                    project.id,
                    project.name,
                    project.game_dir,
                    project.runtime_type,
                    project.arch,
                    project.unity_version,
                    project.loader,
                    int(project.hook_deployed),
                    now,
                    created_at,
                ),
            )
            self.conn.commit()
        project.created_at = created_at
        project.last_opened = now
        return project

    def list_projects(self) -> list[GameProject]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM projects ORDER BY last_opened DESC, created_at DESC"
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project(self, project_id: str) -> GameProject | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        return self._row_to_project(row) if row else None

    def set_hook_deployed(self, project_id: str, deployed: bool, loader: str | None = None) -> None:
        with self._lock:
            if loader:
                self.conn.execute(
                    "UPDATE projects SET hook_deployed=?, loader=?, last_opened=? WHERE id=?",
                    (int(deployed), loader, _now(), project_id),
                )
            else:
                self.conn.execute(
                    "UPDATE projects SET hook_deployed=?, last_opened=? WHERE id=?",
                    (int(deployed), _now(), project_id),
                )
            self.conn.commit()

    def delete_project(self, project_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            self.conn.commit()

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> GameProject:
        return GameProject(
            id=row["id"],
            name=row["name"],
            game_dir=row["game_dir"],
            runtime_type=row["runtime_type"],
            arch=row["arch"],
            unity_version=row["unity_version"],
            loader=row["loader"],
            hook_deployed=bool(row["hook_deployed"]),
            last_opened=row["last_opened"],
            created_at=row["created_at"],
        )

    # ── Global Translation Memory ─────────────────────────────────────────

    def get_memory(self, original: str, target_lang: str = "zh-CN") -> str | None:
        h = _hash_text(original)
        with self._lock:
            row = self.conn.execute(
                "SELECT translation FROM global_memory WHERE src_hash=? AND target_lang=?",
                (h, target_lang),
            ).fetchone()
        return str(row["translation"]) if row else None

    def put_memory(
        self, original: str, translation: str, target_lang: str = "zh-CN", source: str = "ai"
    ) -> None:
        if not original or not translation:
            return
        h = _hash_text(original)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO global_memory(src_hash, target_lang, original, translation, source, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(src_hash, target_lang) DO UPDATE SET
                    translation=excluded.translation,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (h, target_lang, original, translation, source, _now()),
            )
            self.conn.commit()

    def list_memory(self, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            if query.strip():
                like = f"%{query.strip()}%"
                rows = self.conn.execute(
                    """
                    SELECT src_hash, original, translation, source, updated_at
                    FROM global_memory
                    WHERE original LIKE ? OR translation LIKE ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (like, like, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT src_hash, original, translation, source, updated_at
                    FROM global_memory ORDER BY updated_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, src_hash: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM global_memory WHERE src_hash=?", (src_hash,))
            self.conn.commit()

    # ── Global Glossary ───────────────────────────────────────────────────

    def list_glossary(self) -> list[GlossaryItem]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, term, translation, category, note FROM global_glossary ORDER BY id DESC"
            ).fetchall()
        return [GlossaryItem(r["id"], r["term"], r["translation"], r["category"], r["note"]) for r in rows]

    def upsert_glossary(self, term: str, translation: str, category: str = "术语", note: str = "") -> GlossaryItem:
        t, tr = term.strip(), translation.strip()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO global_glossary(term, translation, category, note) VALUES(?,?,?,?)
                ON CONFLICT(term) DO UPDATE SET
                    translation=excluded.translation,
                    category=excluded.category,
                    note=excluded.note
                """,
                (t, tr, category.strip() or "术语", note.strip()),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT id, term, translation, category, note FROM global_glossary WHERE term=?", (t,)
            ).fetchone()
        return GlossaryItem(row["id"], row["term"], row["translation"], row["category"], row["note"])

    def delete_glossary(self, item_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM global_glossary WHERE id=?", (item_id,))
            self.conn.commit()


class ProjectStore:
    """Per-game SQLite store: entries, project glossary, game context & deploy manifest."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.conn = _connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS entries(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original TEXT UNIQUE NOT NULL,
                    translation TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    source TEXT DEFAULT 'xuat_live',
                    locked INTEGER DEFAULT 0,
                    review_level TEXT DEFAULT '',
                    review_reason TEXT DEFAULT '',
                    quality_note TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    prev_translation TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
                CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source);
                CREATE TABLE IF NOT EXISTS glossary(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term TEXT UNIQUE NOT NULL,
                    translation TEXT NOT NULL,
                    category TEXT DEFAULT '术语',
                    note TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS kv(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            # Safe migration for pre-existing project DBs lacking prev_translation
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(entries)").fetchall()}
            if "prev_translation" not in cols:
                self.conn.execute("ALTER TABLE entries ADD COLUMN prev_translation TEXT DEFAULT ''")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── Entries ───────────────────────────────────────────────────────────

    def get_by_original(self, original: str) -> Entry | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM entries WHERE original=?", (original,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_by_id(self, entry_id: int) -> Entry | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def upsert_entry(
        self,
        original: str,
        translation: str = "",
        *,
        status: str | None = None,
        source: str = "xuat_live",
        locked: bool | None = None,
        review_level: str | None = None,
        review_reason: str | None = None,
        quality_note: str | None = None,
        preserve_locked: bool = True,
    ) -> Entry:
        now = _now()
        with self._lock:
            existing = self.conn.execute(
                "SELECT * FROM entries WHERE original=?", (original,)
            ).fetchone()
            if existing is None:
                st = status or (STATUS_TRANSLATED if translation else STATUS_PENDING)
                self.conn.execute(
                    """
                    INSERT INTO entries(
                        original, translation, status, source, locked,
                        review_level, review_reason, quality_note, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        original,
                        translation,
                        st,
                        source,
                        int(bool(locked)),
                        review_level or "",
                        review_reason or "",
                        quality_note or "",
                        now,
                    ),
                )
            else:
                if preserve_locked and existing["locked"] and locked is not False:
                    if source == "xuat_live":
                        self.conn.execute(
                            "UPDATE entries SET source='xuat_live', updated_at=? WHERE id=?",
                            (now, existing["id"]),
                        )
                        self.conn.commit()
                        refreshed = self.conn.execute(
                            "SELECT * FROM entries WHERE id=?", (existing["id"],)
                        ).fetchone()
                        return self._row_to_entry(refreshed)
                    return self._row_to_entry(existing)
                new_tr = translation if translation != "" or status in (STATUS_PENDING, STATUS_FAILED) else existing["translation"]
                new_st = status or existing["status"]
                new_src = "xuat_live" if source == "xuat_live" else existing["source"]
                new_lock = int(existing["locked"] if locked is None else locked)
                new_rl = existing["review_level"] if review_level is None else review_level
                new_rr = existing["review_reason"] if review_reason is None else review_reason
                new_qn = existing["quality_note"] if quality_note is None else quality_note
                old_tr = str(existing["translation"] or "")
                prev_tr = old_tr if (old_tr and new_tr and old_tr != new_tr) else str(existing["prev_translation"] or "")
                self.conn.execute(
                    """
                    UPDATE entries SET
                        translation=?, status=?, source=?, locked=?,
                        review_level=?, review_reason=?, quality_note=?, updated_at=?,
                        prev_translation=?
                    WHERE id=?
                    """,
                    (new_tr, new_st, new_src, new_lock, new_rl, new_rr, new_qn, now, prev_tr, existing["id"]),
                )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM entries WHERE original=?", (original,)
            ).fetchone()
        return self._row_to_entry(row)

    def bulk_import_pairs(self, pairs: list[tuple[str, str]], source: str = "xuat_import") -> dict[str, int]:
        """Batch import (original, translation) pairs without overwriting manual edits."""
        added = 0
        updated = 0
        skipped = 0
        now = _now()
        with self._lock:
            for original, translation in pairs:
                if not original:
                    continue
                existing = self.conn.execute(
                    "SELECT id, translation, status, locked FROM entries WHERE original=?",
                    (original,),
                ).fetchone()
                has_tr = bool(translation and translation != original)
                if existing is None:
                    self.conn.execute(
                        """
                        INSERT INTO entries(original, translation, status, source, updated_at)
                        VALUES(?,?,?,?,?)
                        """,
                        (
                            original,
                            translation if has_tr else "",
                            STATUS_TRANSLATED if has_tr else STATUS_PENDING,
                            source,
                            now,
                        ),
                    )
                    added += 1
                elif existing["locked"] or existing["status"] == STATUS_EDITED:
                    skipped += 1
                elif has_tr and not existing["translation"]:
                    self.conn.execute(
                        "UPDATE entries SET translation=?, status=?, updated_at=? WHERE id=?",
                        (translation, STATUS_TRANSLATED, now, existing["id"]),
                    )
                    updated += 1
                else:
                    skipped += 1
            self.conn.commit()
        return {"added": added, "updated": updated, "skipped": skipped}

    def update_entry_by_id(
        self,
        entry_id: int,
        *,
        translation: str | None = None,
        status: str | None = None,
        locked: bool | None = None,
        review_level: str | None = None,
        review_reason: str | None = None,
        quality_note: str | None = None,
    ) -> Entry | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if not row:
                return None
            new_tr = row["translation"] if translation is None else translation
            new_st = row["status"] if status is None else status
            new_lk = row["locked"] if locked is None else int(locked)
            new_rl = row["review_level"] if review_level is None else review_level
            new_rr = row["review_reason"] if review_reason is None else review_reason
            new_qn = row["quality_note"] if quality_note is None else quality_note
            old_tr = str(row["translation"] or "")
            prev_tr = old_tr if (translation is not None and old_tr and new_tr and old_tr != new_tr) else str(row["prev_translation"] or "")
            self.conn.execute(
                """
                UPDATE entries SET
                    translation=?, status=?, locked=?,
                    review_level=?, review_reason=?, quality_note=?, updated_at=?,
                    prev_translation=?
                WHERE id=?
                """,
                (new_tr, new_st, new_lk, new_rl, new_rr, new_qn, _now(), prev_tr, entry_id),
            )
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return self._row_to_entry(updated)

    def restore_prev_translation(self, entry_id: int) -> Entry | None:
        """Restore `prev_translation` (swap current <-> prev) and lock it."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if not row:
                return None
            prev_tr = str(row["prev_translation"] or "").strip()
            if not prev_tr:
                return None
            cur_tr = str(row["translation"] or "")
            self.conn.execute(
                """
                UPDATE entries SET
                    translation=?, prev_translation=?, status=?, locked=1,
                    review_level='', review_reason='已还原至上一版译文', quality_note='', updated_at=?
                WHERE id=?
                """,
                (prev_tr, cur_tr, STATUS_EDITED, _now(), entry_id),
            )
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return self._row_to_entry(updated)

    def bulk_replace_translations(
        self, find_text: str, replace_text: str, *, only_unlocked: bool = False
    ) -> list[Entry]:
        """Replace substring across translated/edited entries while preserving prev_translation."""
        if not find_text:
            return []
        now = _now()
        updated_entries: list[Entry] = []
        with self._lock:
            sql = "SELECT * FROM entries WHERE translation LIKE ?"
            if only_unlocked:
                sql += " AND locked = 0"
            rows = self.conn.execute(sql, (f"%{find_text}%",)).fetchall()
            for row in rows:
                old_tr = str(row["translation"] or "")
                new_tr = old_tr.replace(find_text, replace_text)
                if new_tr == old_tr:
                    continue
                self.conn.execute(
                    """
                    UPDATE entries SET
                        translation=?, prev_translation=?, status=?, updated_at=?
                    WHERE id=?
                    """,
                    (new_tr, old_tr, STATUS_EDITED, now, row["id"]),
                )
                refreshed = self.conn.execute("SELECT * FROM entries WHERE id=?", (row["id"],)).fetchone()
                if refreshed:
                    updated_entries.append(self._row_to_entry(refreshed))
            self.conn.commit()
        return updated_entries

    def delete_entry(self, entry_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
            self.conn.commit()

    def clear_entries(self) -> int:
        """Delete all translation entries in this project and return deleted count."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM entries")
            self.conn.commit()
            return int(cur.rowcount or 0)

    def mark_live_seen(self, entry_id: int) -> Entry | None:
        """Promote an entry into the real-time live feed with fresh timestamp."""
        with self._lock:
            self.conn.execute(
                "UPDATE entries SET source='xuat_live', updated_at=? WHERE id=?",
                (_now(), entry_id),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return self._row_to_entry(row) if row else None

    def query_entries(
        self,
        *,
        status: str = "all",
        source: str = "all",
        search: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Entry], int]:
        where: list[str] = []
        params: list[Any] = []
        src = (source or "all").strip().lower()
        st = (status or "all").strip().lower()
        is_live = st == "live" or src in ("live", "xuat_live")

        # Source filter (static vs live)
        if st == "live" or src in ("live", "xuat_live"):
            where.append("source = 'xuat_live'")
        elif st == "static" or src == "static":
            where.append("source != 'xuat_live'")
        elif src and src != "all":
            where.append("source = ?")
            params.append(src)

        # Status filter (independent of source)
        if st and st not in ("all", "live", "static"):
            where.append("status = ?")
            params.append(st)

        if search.strip():
            like = f"%{search.strip()}%"
            where.append("(original LIKE ? OR translation LIKE ?)")
            params.extend([like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order = "ORDER BY updated_at DESC, id DESC" if is_live else "ORDER BY id DESC"
        with self._lock:
            total = int(
                self.conn.execute(f"SELECT COUNT(*) AS c FROM entries{clause}", params).fetchone()["c"]
            )
            rows = self.conn.execute(
                f"SELECT * FROM entries{clause} {order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return [self._row_to_entry(r) for r in rows], total

    def all_entries(self, statuses: tuple[str, ...] | None = None) -> list[Entry]:
        with self._lock:
            if statuses:
                qs = ",".join("?" for _ in statuses)
                rows = self.conn.execute(
                    f"SELECT * FROM entries WHERE status IN ({qs}) ORDER BY id ASC",
                    statuses,
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM entries ORDER BY id ASC").fetchall()
        return [self._row_to_entry(r) for r in rows]

    def stats(self) -> dict[str, int]:
        counts = {
            "total": 0,
            STATUS_PENDING: 0,
            STATUS_TRANSLATED: 0,
            STATUS_EDITED: 0,
            STATUS_FLAGGED: 0,
            STATUS_FAILED: 0,
            STATUS_SKIPPED: 0,
        }
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM entries GROUP BY status"
            ).fetchall()
            live_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) AS c FROM entries WHERE source = 'xuat_live'"
                ).fetchone()["c"]
            )
        for r in rows:
            st = r["status"]
            c = int(r["c"])
            counts["total"] += c
            if st in counts:
                counts[st] = c
        counts["live"] = live_count
        counts["static"] = max(0, counts["total"] - live_count)
        counts["done"] = counts[STATUS_TRANSLATED] + counts[STATUS_EDITED]
        actionable = max(0, counts["total"] - counts[STATUS_SKIPPED])
        counts["percent"] = round(counts["done"] * 100 / actionable) if actionable else 0
        return counts

    # ── Glossary ──────────────────────────────────────────────────────────

    def list_glossary(self) -> list[GlossaryItem]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, term, translation, category, note FROM glossary ORDER BY id DESC"
            ).fetchall()
        return [GlossaryItem(r["id"], r["term"], r["translation"], r["category"], r["note"]) for r in rows]

    def upsert_glossary(self, term: str, translation: str, category: str = "术语", note: str = "") -> GlossaryItem:
        t, tr = term.strip(), translation.strip()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO glossary(term, translation, category, note) VALUES(?,?,?,?)
                ON CONFLICT(term) DO UPDATE SET
                    translation=excluded.translation,
                    category=excluded.category,
                    note=excluded.note
                """,
                (t, tr, category.strip() or "术语", note.strip()),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT id, term, translation, category, note FROM glossary WHERE term=?", (t,)
            ).fetchone()
        return GlossaryItem(row["id"], row["term"], row["translation"], row["category"], row["note"])

    def delete_glossary(self, item_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM glossary WHERE id=?", (item_id,))
            self.conn.commit()

    # ── KV (GameContext / Hook Manifest) ──────────────────────────────────

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    def set_kv(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, payload)
            )
            self.conn.commit()

    def load_context(self) -> GameContext:
        return GameContext.from_dict(self.get_kv("game_context"))

    def save_context(self, ctx: GameContext) -> None:
        self.set_kv("game_context", ctx.to_dict())

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> Entry:
        keys = row.keys()
        return Entry(
            id=row["id"],
            original=row["original"],
            translation=row["translation"],
            status=row["status"],
            source=row["source"],
            locked=bool(row["locked"]),
            review_level=row["review_level"],
            review_reason=row["review_reason"],
            quality_note=row["quality_note"],
            updated_at=row["updated_at"],
            prev_translation=row["prev_translation"] if "prev_translation" in keys else "",
        )
