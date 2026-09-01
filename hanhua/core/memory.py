from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path

from hanhua.core.models import STATUS_TRANSLATED, GameProfile
from hanhua.core.review_outcome import PUBLISHABLE as _PUBLISHABLE_OUTCOMES
from hanhua.core.translator import builtin_ui_conflict


def _builtin_clean(rows: list[tuple]) -> list[tuple]:
    """过滤与内置 UI 权威译名冲突的记忆行（2026-09-01 污染系统性根治）。

    单 token 原文命中内置权威表且译文与权威不符（Disabled→残疾人士）
    是历史坏记忆的注入源——promote（pending=0 可命中）前丢弃，不让
    坏译文跨游戏污染。与 agent_memory.propose 门禁（入口不沉淀）闭环。
    多词短语/保留型专名（itch→itch）不作冲突，照常通过。
    """
    return [r for r in rows if not builtin_ui_conflict(r[0], r[1])]


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ProjectStore:
    """单项目 SQLite：条目状态 + 翻译记忆 + 断点续传。所有方法线程安全。"""

    def __init__(self, db_path: str | Path, timeout: float = 5.0):
        self.db = Path(db_path)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        connection = sqlite3.connect(
            str(self.db), check_same_thread=False, timeout=timeout,
        )
        try:
            connection.row_factory = sqlite3.Row
            # 2026-08-14 卡顿优化：WAL + synchronous=NORMAL。journal 模式
            # 每 commit 一次 fsync——翻译每批 2 次 commit（结果+记忆），
            # 万级条目约 5000 次 fsync，磁盘等待阻塞 worker 与所有 DB
            # 访问（单连接 RLock 串行）；WAL 下 commit 免逐次 fsync、
            # checkpoint 批量落盘。写不阻塞读，读不阻塞写。
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError:
                pass   # 文件系统不支持 WAL（网络盘等）→ 保持默认 journal
            self.conn = connection
        except Exception:
            connection.close()
            raise

    def init_schema(self):
        with self._lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS files(
                id TEXT PRIMARY KEY, rel_path TEXT, format TEXT,
                encoding TEXT, eol TEXT, meta TEXT
            );
            CREATE TABLE IF NOT EXISTS entries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT, key_path TEXT, original TEXT,
                translation TEXT DEFAULT '', status TEXT DEFAULT 'pending',
                locked INTEGER DEFAULT 0, meta TEXT DEFAULT '{}',
                UNIQUE(file_id, key_path)
            );
            CREATE TABLE IF NOT EXISTS memory(
                src_hash TEXT PRIMARY KEY, original TEXT, translation TEXT,
                model TEXT, lang TEXT, pending INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
            -- file_id 索引（2026-08-19 扫描性能修复）：UNIQUE(file_id,
            -- key_path) 只服务唯一约束（键序 file_id 在前其实可用于前缀
            -- 查询，但显式单列索引让 upsert 的 IN 批查与 get_entry_key_paths
            -- 的点查走确定路径——大库无索引时每文件全表扫）。
            CREATE INDEX IF NOT EXISTS idx_entries_file ON entries(file_id);
            CREATE TABLE IF NOT EXISTS profile(
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT DEFAULT (datetime('now')), kind TEXT,
                file_id TEXT, key_path TEXT, original TEXT,
                before_translation TEXT, after_translation TEXT,
                model TEXT, lang TEXT, note TEXT
            );
            CREATE TABLE IF NOT EXISTS vector_outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT DEFAULT (datetime('now')),
                kind TEXT, file_id TEXT, key_path TEXT,
                original TEXT, translation TEXT
            );
            """)
            # 迁移：旧库无 pending 列（Phase B PendingEvidence，2026-08-13）。
            # 旧数据视为已提交（pending=0）——历史记忆不因新协议失效。
            try:
                self.conn.execute(
                    "ALTER TABLE memory ADD COLUMN pending INTEGER DEFAULT 0")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # 列已存在（新库或已迁移）
            self.conn.commit()

    def schema_tables(self) -> frozenset[str]:
        """Return existing table names without creating or migrating schema."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        return frozenset(row["name"] for row in rows)

    def add_file(self, file_id, rel_path, fmt, encoding, eol, meta=None):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                (file_id, rel_path, fmt, encoding, eol, json.dumps(meta or {})))
            self.conn.commit()

    def upsert_entries(self, rows: list[dict]):
        """插入/更新条目。状态合并规则：
        - skipped 强制覆盖（键位置等不该翻译的条目，即使旧状态是 translated 也降级）
        - pending 不覆盖已翻译（译文继承，断点续传）
        """
        quality_keys = {
            "quality_passed", "quality_reasons", "quality_source",
            "confidence_promoted",
        }

        def decoded_meta(raw) -> dict:
            try:
                value = json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})
            except (json.JSONDecodeError, TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        def with_quality(incoming, existing_meta) -> dict:
            merged = decoded_meta(incoming)
            previous = decoded_meta(existing_meta)
            merged.update({key: previous[key] for key in quality_keys if key in previous})
            return merged

        rows = list(rows)
        if not rows:
            return

        def perform_upsert():
            file_ids = sorted({row["file_id"] for row in rows})
            by_key = {}
            by_original = {}
            next_order = 0

            def add_to_original(state):
                if state["translation"]:
                    original_key = (state["file_id"], state["original"])
                    by_original.setdefault(original_key, {})[
                        state["key_path"]
                    ] = state

            def remove_from_original(state):
                if not state["translation"]:
                    return
                original_key = (state["file_id"], state["original"])
                candidates = by_original.get(original_key)
                if candidates is None:
                    return
                candidates.pop(state["key_path"], None)
                if not candidates:
                    by_original.pop(original_key, None)

            for offset in range(0, len(file_ids), 400):
                chunk = file_ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                cursor = self.conn.execute(
                    "SELECT id, file_id, key_path, original, translation, status, locked, meta "
                    f"FROM entries WHERE file_id IN ({placeholders}) ORDER BY id",
                    chunk,
                )
                for existing_row in cursor:
                    state = dict(existing_row)
                    state["order"] = next_order
                    next_order += 1
                    key = (state["file_id"], state["key_path"])
                    by_key[key] = state
                    add_to_original(state)

            touched = {}
            for r in rows:
                new_status = r.get("status", "pending")
                incoming_meta = decoded_meta(r.get("meta", {}))
                key = (r["file_id"], r["key_path"])
                existing = by_key.get(key)
                previous = dict(existing) if existing is not None else None
                if existing is not None:
                    existing["meta"] = json.dumps(
                        decoded_meta(existing["meta"]), ensure_ascii=False,
                    )

                if existing is None and new_status == "pending":
                    candidates = sorted(by_original.get(
                        (r["file_id"], r["original"]), {}
                    ).values(), key=lambda row: row["order"])
                    translations = {row["translation"] for row in candidates}
                    if len(translations) == 1:
                        translation = translations.pop()
                        status = "translated" if any(
                            row["status"] == "translated" for row in candidates) else candidates[0]["status"]
                        locked = int(any(row["locked"] for row in candidates))
                        source_meta = next(
                            (row["meta"] for row in candidates
                             if row["translation"] == translation
                             and decoded_meta(row["meta"]).get("quality_passed") is True),
                            {},
                        )
                        migrated_meta = with_quality(incoming_meta, source_meta)
                        existing = {
                            "file_id": r["file_id"],
                            "key_path": r["key_path"],
                            "original": r["original"],
                            "translation": translation,
                            "status": status,
                            "locked": locked,
                            "meta": json.dumps(migrated_meta, ensure_ascii=False),
                            "order": next_order,
                        }
                if existing is None:
                    existing = {
                        "file_id": r["file_id"],
                        "key_path": r["key_path"],
                        "original": r["original"],
                        "translation": "",
                        "status": new_status,
                        "locked": 0,
                        "meta": json.dumps(incoming_meta, ensure_ascii=False),
                        "order": next_order,
                    }
                if previous is None:
                    next_order += 1

                if new_status == "skipped":
                    # 键位置：强制跳过（丢弃旧译文，键不可翻译）
                    if existing["status"] != "skipped":
                        existing["status"] = "skipped"
                        existing["translation"] = ""
                elif new_status == "pending":
                    # 重扫后始终刷新原文定位与元数据；已翻译的译文和状态保持不动。
                    # 这让规则升级后的 obj_has_values / offset 等防护信息能作用于历史译文。
                    if (previous is not None
                            and previous["original"] != r["original"]):
                        if existing["status"] != "skipped":
                            existing["original"] = r["original"]
                            existing["translation"] = ""
                            existing["status"] = "pending"
                            existing["meta"] = json.dumps(
                                incoming_meta, ensure_ascii=False,
                            )
                    elif existing["status"] != "skipped":
                        preserved_meta = with_quality(
                            incoming_meta, existing["meta"],
                        )
                        existing["original"] = r["original"]
                        existing["meta"] = json.dumps(
                            preserved_meta, ensure_ascii=False,
                        )

                if previous is not None:
                    remove_from_original(previous)
                by_key[key] = existing
                add_to_original(existing)
                touched[key] = existing

            self.conn.executemany(
                "INSERT INTO entries(file_id,key_path,original,translation,status,locked,meta) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id,key_path) DO UPDATE SET "
                "original=excluded.original, translation=excluded.translation, "
                "status=excluded.status, locked=excluded.locked, meta=excluded.meta",
                (
                    (
                        state["file_id"], state["key_path"], state["original"],
                        state["translation"], state["status"], state["locked"],
                        state["meta"],
                    )
                    for state in touched.values()
                ),
            )

        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                perform_upsert()
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def update_translation(self, file_id, key_path, translation, status="translated"):
        with self._lock:
            row = self.conn.execute(
                "SELECT meta FROM entries WHERE file_id=? AND key_path=?",
                (file_id, key_path),
            ).fetchone()
            try:
                meta = json.loads(row["meta"] or "{}") if row else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            meta.update({
                "quality_passed": status == "translated" and bool(translation),
                "quality_reasons": [],
                "quality_source": "manual_api",
                "confidence_promoted": True,
            })
            self.conn.execute(
                "UPDATE entries SET translation=?, status=?, meta=? "
                "WHERE file_id=? AND key_path=?",
                (translation, status, json.dumps(meta, ensure_ascii=False),
                 file_id, key_path))
            self.conn.commit()

    def batch_update_translations(self, rows: list[tuple]) -> None:
        """批量写入译文状态（executemany + 单次提交，翻译大项目时避免逐条 commit）。"""
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                "UPDATE entries SET translation=?, status=? WHERE file_id=? AND key_path=?",
                rows)
            self.conn.commit()

    def batch_update_translation_results(self, entries) -> None:
        """原子保存译文、状态和质量元数据，同时保留扫描器定位信息。

        2026-08-19 性能修复：原实现逐条 SELECT meta + Python 合并 +
        UPDATE——翻译 flush() 每批一次，万级条目累计 O(N²) 次点查与
        JSON 序列化（翻译越久越卡的 DB 侧根因之一）。改为 SQL 内
        json_patch 合并（UPDATE 的 SET 表达式中 entries.meta 引用旧值，
        patch 键覆盖旧键——与 dict.update 语义一致），单次 executemany
        + 单次 commit；合并结果回写 entry.meta 用批量 IN 查询（每批
        一次而非每条一次）。json_patch 不可用（极旧 SQLite）→ 兜底
        旧的逐条合并路径，行为不变只是慢。"""
        rows = list(entries)
        if not rows:
            return
        with self._lock:
            values = [
                (entry.translation, entry.status,
                 json.dumps(entry.meta, ensure_ascii=False),
                 entry.file_id, entry.key_path)
                for entry in rows
            ]
            try:
                self.conn.executemany(
                    "UPDATE entries SET "
                    "translation=?, "
                    "status=?, "
                    "meta=json_patch(COALESCE(entries.meta,'{}'), json(?)) "
                    "WHERE file_id=? AND key_path=?",
                    values,
                )
            except sqlite3.OperationalError:
                self._batch_update_translation_results_slow(rows)
            self.conn.commit()
            # 合并后的 meta 回写 entry（旧实现副作用兼容——后续 reviewer
            # 读 entry.meta 需要含扫描器字段的完整视图）。批量 IN 查询，
            # 每批一次而非每条一次。
            by_key = {(e.file_id, e.key_path): e for e in rows}
            keys = list(by_key)
            for offset in range(0, len(keys), 400):
                chunk = keys[offset:offset + 400]
                placeholders = ",".join("(?,?)" for _ in chunk)
                flat = [v for pair in chunk for v in pair]
                cursor = self.conn.execute(
                    f"SELECT file_id, key_path, meta FROM entries "
                    f"WHERE (file_id, key_path) IN ({placeholders})",
                    flat)
                for r in cursor:
                    entry = by_key.get((r["file_id"], r["key_path"]))
                    if entry is None:
                        continue
                    try:
                        merged = json.loads(r["meta"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(merged, dict):
                        entry.meta = merged

    def _batch_update_translation_results_slow(self, rows) -> None:
        """json_patch 不可用时的兜底：逐条 SELECT 合并 UPDATE（旧实现）。"""
        for entry in rows:
            current = self.conn.execute(
                "SELECT meta FROM entries WHERE file_id=? AND key_path=?",
                (entry.file_id, entry.key_path),
            ).fetchone()
            try:
                merged_meta = json.loads(
                    current["meta"] or "{}") if current else {}
            except (json.JSONDecodeError, TypeError):
                merged_meta = {}
            merged_meta.update(entry.meta)
            entry.meta = merged_meta
            self.conn.execute(
                "UPDATE entries SET translation=?, status=?, meta=? "
                "WHERE file_id=? AND key_path=?",
                (entry.translation, entry.status,
                 json.dumps(merged_meta, ensure_ascii=False),
                 entry.file_id, entry.key_path))

    def update_entry_metas(self, rows: list[tuple[str, str, dict]]) -> int:
        """批量合并条目 meta（保留既有字段，逐条 merge 后单次提交）。

        rows: [(file_id, key_path, fields)]——翻译 C6 语义审核结论
        （review_issue/review_suggestion）落 store 用；审校页可据此
        筛选「需要优化」条目。返回实际更新的条数。
        """
        if not rows:
            return 0
        with self._lock:
            updated = 0
            for file_id, key_path, fields in rows:
                row = self.conn.execute(
                    "SELECT meta FROM entries WHERE file_id=? AND key_path=?",
                    (file_id, key_path),
                ).fetchone()
                if row is None:
                    continue
                try:
                    meta = json.loads(row["meta"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta.update(fields or {})
                self.conn.execute(
                    "UPDATE entries SET meta=? WHERE file_id=? AND key_path=?",
                    (json.dumps(meta, ensure_ascii=False),
                     file_id, key_path))
                updated += 1
            self.conn.commit()
            return updated

    def set_status(self, file_id, key_path, status):
        with self._lock:
            self.conn.execute("UPDATE entries SET status=? WHERE file_id=? AND key_path=?",
                              (status, file_id, key_path))
            self.conn.commit()

    def reset_to_pending(self, file_id, key_path):
        """标记重译：status→pending 并清空旧审核终态（#9）。

        只 set_status 的旧做法让 BLOCKED/NEEDS_REVISION 残留（review_outcome、
        review_blocked 等）继续 fail-closed 拒绝重译成功的译文——用户
        「重试失败/标记为待翻译」后重译成功仍发布被拒，失败文本无法
        自己处理。重译是新的开始：清终态 + 质量门字段，由重译结果
        重新判定。
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT meta FROM entries WHERE file_id=? AND key_path=?",
                (file_id, key_path)).fetchone()
            if row is None:
                return
            try:
                meta = json.loads(row["meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            for field in self._REVIEW_STATE_CLEAR:
                meta.pop(field, None)
            for field in ("review_outcome", "quality_passed", "review_issue"):
                meta.pop(field, None)
            self.conn.execute(
                "UPDATE entries SET status='pending', meta=? "
                "WHERE file_id=? AND key_path=?",
                (json.dumps(meta, ensure_ascii=False), file_id, key_path))
            self.conn.commit()

    def set_locked(self, file_id, key_path, locked: bool):
        with self._lock:
            self.conn.execute("UPDATE entries SET locked=? WHERE file_id=? AND key_path=?",
                              (1 if locked else 0, file_id, key_path))
            self.conn.commit()

    # 人工修正要清理的旧审核状态字段（Phase B-2，审计 §6 P1-6）：
    # BLOCKED/REVIEW_ERROR/NEEDS_REVISION 等残留会让发布门 fail-closed
    # 拒绝（被人工改正后仍不可写回），旧 APPROVED 残留会让新译文未经
    # 复查就带发布资格——全部清除，由 apply_manual_correction 重写终态。
    _REVIEW_STATE_CLEAR = (
        "review_blocked", "review_error", "need_revision", "need_retranslate",
        "review_level", "review_reason", "review_suggestion",
        "review_error_kind", "review_blocked_rounds", "rejected_candidate",
        "quality_reasons",
    )

    def apply_manual_correction(self, file_id, key_path, translation) -> dict:
        """人工修正原子写入（审计 §6 P1-6）：清理旧审核状态 → 人工终态。

        审校页直接编辑译文过去只改 translation/status（set_manual）：
        review_outcome / review_blocked / need_* 旧审核状态残留——被
        人工改正的坏译文仍被发布门拒绝，或旧 approved 状态未刷新。
        人改即终局：
        - 非空译文 → status=translated、review_outcome=APPROVED、
          review_level=MANUAL、quality_passed=True、
          confidence_promoted=True（低置信条目经人工提升可写回），
          manual_corrected 记录修正时间与旧译文；
        - 空输入（清空译文）→ status=pending、review_outcome 移除、
          quality_passed=False，等同重置待译。
        返回 {applied, original, before_translation, translation, status}。
        """
        normalized = str(translation).strip()
        with self._lock:
            row = self.conn.execute(
                "SELECT original, translation, meta FROM entries "
                "WHERE file_id=? AND key_path=?",
                (file_id, key_path)).fetchone()
            if row is None:
                return {"applied": False}
            try:
                meta = json.loads(row["meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            before = row["translation"] or ""
            original = row["original"] or ""
            # 先清旧审核状态，再写人工终态（避免 MANUAL 被清掉）
            for field in self._REVIEW_STATE_CLEAR:
                meta.pop(field, None)
            if normalized:
                status = STATUS_TRANSLATED
                meta["review_outcome"] = "APPROVED"
                meta["review_level"] = "MANUAL"
                meta["quality_passed"] = True
                meta["confidence_promoted"] = True
                meta["quality_source"] = "manual_api"
                meta["manual_corrected"] = {
                    "at": _now(), "before": before,
                }
            else:
                status = "pending"
                meta.pop("review_outcome", None)
                meta.pop("manual_corrected", None)
                meta["quality_passed"] = False
            self.conn.execute(
                "UPDATE entries SET translation=?, status=?, meta=? "
                "WHERE file_id=? AND key_path=?",
                (normalized, status, json.dumps(meta, ensure_ascii=False),
                 file_id, key_path))
            self.conn.commit()
        return {"applied": True, "original": original,
                "before_translation": before, "translation": normalized,
                "status": status}

    def set_manual(self, file_id, key_path, translation):
        """人工写入（历史 API）：委托 apply_manual_correction 统一回流。

        Phase B-2（审计 §6 P1-6）后所有人工/规则修正写入走同一原子
        路径：清理旧审核状态 + 重写人工终态（旧实现只改
        translation/status，审核残留与坏记忆不清理）。返回结果 dict
        （忽略返回值的旧调用方不受影响）。
        """
        return self.apply_manual_correction(file_id, key_path, translation)

    def record_audit(self, *, kind, file_id, key_path, original="",
                     before="", after="", model="", lang="", note="") -> None:
        """审计日志（Phase B-2：人工修正/清空全程可追溯）。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit_log(kind, file_id, key_path, original,"
                " before_translation, after_translation, model, lang, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (kind, file_id, key_path, original, before, after,
                 model, lang, note))
            self.conn.commit()

    def enqueue_vector(self, *, kind, file_id, key_path,
                       original="", translation="") -> None:
        """矢量索引 outbox 出队（Phase C 消费）：修正与清空都入队。

        translation 为空 = 消费端应删除该条目的矢量（原文不再有效）。
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO vector_outbox(kind, file_id, key_path,"
                " original, translation) VALUES (?,?,?,?,?)",
                (kind, file_id, key_path, original, translation))
            self.conn.commit()

    def get_entries(self, status: str | None = None) -> list[dict]:
        with self._lock:
            if status:
                return [dict(r) for r in self.conn.execute("SELECT * FROM entries WHERE status=?", (status,))]
            return [dict(r) for r in self.conn.execute("SELECT * FROM entries")]

    def get_entry_key_paths(self, file_id: str) -> set[str]:
        """单文件条目键集（轻量）：scan_v2 重扫清理用——只取 key_path
        一列，不构造完整行 dict（大游戏几万条目时全量 get_entries +
        Python 过滤是 O(全库) 每文件一次，O(N×M) 累计）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT key_path FROM entries WHERE file_id=?", (file_id,))
            return {r["key_path"] for r in rows}

    def count(self, status: str) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) c FROM entries WHERE status=?", (status,)).fetchone()
            return row["c"] if row else 0

    def count_by_files(self, file_ids: list[str]) -> int:
        """给定文件 id 集合的条目总数（轻量 COUNT，不构造行 dict）。

        2026-08-19 扫描性能修复：scan/scan_v2 的绑定判定原是
        get_entries() 全库 SELECT * + Python any()——大游戏几万条目
        每次扫描两遍全表。SQL 端 COUNT 只返回一个整数。"""
        if not file_ids:
            return 0
        with self._lock:
            total = 0
            for offset in range(0, len(file_ids), 400):
                chunk = file_ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                row = self.conn.execute(
                    f"SELECT COUNT(*) c FROM entries "
                    f"WHERE file_id IN ({placeholders})", chunk).fetchone()
                total += row["c"] if row else 0
            return total

    def get_memory_hits(self, originals: list[str], model: str, lang: str) -> dict[str, str]:
        """返回 {原文: 译文} 命中缓存（单条 IN 查询，替代逐条 SELECT）。

        Phase B（PendingEvidence，2026-08-13 审计 §5 P0-3）：只返回已
        提交（pending=0）记忆——翻译管线批量写入的记忆是 pending 待审
        状态，深审前不参与命中（坏译不得进入下一轮 prompt）；审核通过
        由 promote_memory / settle_translation_memory 提交后才可见。
        """
        if not originals:
            return {}
        hashes = [(hashlib.md5(s.encode("utf-8")).hexdigest(), s) for s in originals]
        with self._lock:
            rows = self.conn.execute(
                "SELECT src_hash, translation FROM memory WHERE model=? AND lang=? "
                "AND pending = 0 "
                "AND src_hash IN (%s)" % ",".join("?" * len(hashes)),
                (model, lang, *(h for h, _ in hashes))).fetchall()
        by_hash = {r["src_hash"]: r["translation"] for r in rows}
        return {s: by_hash[h] for h, s in hashes if h in by_hash}

    #: TM 归一化键（#43 阶段 C）：大小写/空白/标点无关（同 glossary
    #: term_norm 语义，保留 CJK 字符）
    _TM_NORM = re.compile(r"[^0-9a-z一-鿿]+")
    #: 模糊查询扫描上限（按最新记忆，防全表扫爆内存）
    _FUZZY_SCAN_LIMIT = 2000

    @staticmethod
    def _tm_norm_key(text: str) -> str:
        return ProjectStore._TM_NORM.sub("", text.casefold())

    @staticmethod
    def _tm_tokens(text: str) -> set[str]:
        """分词：英文单词（casefold）+ CJK 单字。相似度比较基础。"""
        words = set(re.findall(r"[a-z0-9]+", text.casefold()))
        cjk = set(ch for ch in text if "一" <= ch <= "鿿")
        return words | cjk

    def get_memory_similar(self, original: str, model: str, lang: str,
                           min_similarity: float = 0.6) -> list[dict]:
        """TM 模糊查询（重构指令 §6 第 6 路召回）：归一化命中 + token 相似。

        TM 与 Terminology 分层（指令 §三-2）：记忆是「同原文/近原文的
        历史译文」，不覆盖术语强制，只作 references 参考。

        返回按置信降序的 [{original, translation, confidence, kind}]：
          normalized  归一化命中（去空格/大小写/标点）→ 0.95
          similar     token 重叠率（Jaccard）→ 0.7 × overlap
        只查 pending=0 已提交记忆（坏译不得进入 prompt——Phase B 语义
        延续）；低于 min_similarity 不返回；空 original/无记忆返回 []。
        """
        if not original or min_similarity <= 0:
            return []
        target_norm = self._tm_norm_key(original)
        target_tokens = self._tm_tokens(original)
        if not target_norm and not target_tokens:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT original, translation FROM memory"
                " WHERE model=? AND lang=? AND pending=0"
                " ORDER BY created_at DESC LIMIT ?",
                (model, lang, self._FUZZY_SCAN_LIMIT)).fetchall()
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            cand = str(r["original"] or "")
            if not cand or cand in seen:
                continue
            seen.add(cand)
            if self._tm_norm_key(cand) == target_norm:
                out.append({"original": cand, "translation": r["translation"],
                            "confidence": 0.95, "kind": "normalized"})
                continue
            if not target_tokens:
                continue
            overlap = (len(target_tokens & self._tm_tokens(cand))
                       / len(target_tokens | self._tm_tokens(cand)))
            conf = 0.7 * overlap
            if overlap >= min_similarity:
                out.append({"original": cand, "translation": r["translation"],
                            "confidence": round(conf, 3), "kind": "similar"})
        out.sort(key=lambda d: d["confidence"], reverse=True)
        return out

    def remove_memory(self, original: str, model: str, lang: str) -> None:
        source_hash = hashlib.md5(original.encode("utf-8")).hexdigest()
        with self._lock:
            self.conn.execute(
                "DELETE FROM memory WHERE src_hash=? AND model=? AND lang=?",
                (source_hash, model, lang),
            )
            self.conn.commit()

    def remove_memory_all(self, original: str) -> int:
        """按原文删除全部记忆行（不限 model/lang）。

        Phase B-2：人工清空译文 = 重置待译，旧坏译文可能在不同
        model/lang 组合下都命中过——全部撤销。
        """
        source_hash = hashlib.md5(original.encode("utf-8")).hexdigest()
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM memory WHERE src_hash=?", (source_hash,))
            self.conn.commit()
            return cursor.rowcount

    def clear_translation_memory(self) -> int:
        """Atomically clear cached translations and return deleted row count."""
        with self._lock, self.conn:
            cursor = self.conn.execute("DELETE FROM memory")
            return cursor.rowcount

    def clear_records(self) -> tuple[int, int]:
        """清空识别与翻译记录(files/entries/memory),保留游戏档案。

        返回 (识别条目数, 翻译记忆条数),单事务提交。
        """
        with self._lock, self.conn:
            entries_rows = self.conn.execute("DELETE FROM entries").rowcount
            self.conn.execute("DELETE FROM files")
            memory_rows = self.conn.execute("DELETE FROM memory").rowcount
        return entries_rows, memory_rows

    def add_memory(self, original, translation, model, lang):
        """显式提交记忆（pending=0）：审核通过/人工写入——立即可命中。

        BUILTIN 冲突门禁：与内置 UI 权威译名冲突的单 token 对不落库
        （Disabled→残疾人士 历史污染根因；权威译名由确定性直填 + Q1
        语义门在运行期恒胜出，冲突记忆只会覆盖它）。
        """
        if builtin_ui_conflict(original, translation):
            return
        h = hashlib.md5(original.encode("utf-8")).hexdigest()
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO memory(src_hash, original, translation, "
                "model, lang, pending) VALUES (?,?,?,?,?,0)",
                (h, original, translation, model, lang))
            self.conn.commit()

    def batch_add_memory(self, rows: list[tuple]) -> None:
        """批量写入翻译记忆（pending=1 待审）。

        Phase B（PendingEvidence）：翻译管线机械门通过后先入 pending 桶，
        深审通过（promote）或未开启审核（settle）前不参与任何命中——
        坏译不得在深审前污染记忆。

        BUILTIN 冲突门禁（2026-09-01）：与内置权威冲突的单 token 对
        （Disabled→残疾人士）不入 pending 桶——坏译即使 pending 也会
        在审核关闭时被 settle promote 成可命中记忆。
        """
        if not rows:
            return
        rows = _builtin_clean(rows)
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO memory(src_hash, original, translation, "
                "model, lang, pending) VALUES (?,?,?,?,?,1)",
                [(hashlib.md5(o.encode("utf-8")).hexdigest(), o, t, m, l)
                 for o, t, m, l in rows])
            self.conn.commit()

    def promote_memory(self, rows: list[tuple]) -> int:
        """审后提交：批量把记忆提升为已提交（pending=0，可命中）。

        rows: [(original, translation, model, lang)]——upsert 语义，
        未入 pending 桶的条目（如审核期间反馈重译的新译文）直接提交。
        返回提交条数。

        BUILTIN 冲突门禁（2026-09-01）：与内置权威冲突的单 token 对
        不提升（Disabled→残疾人士 历史污染根因）——bad 译文不得成为
        可命中记忆。
        """
        if not rows:
            return 0
        rows = _builtin_clean(rows)
        if not rows:
            return 0
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO memory(src_hash, original, translation, "
                "model, lang, pending) VALUES (?,?,?,?,?,0)",
                [(hashlib.md5(o.encode("utf-8")).hexdigest(), o, t, m, l)
                 for o, t, m, l in rows])
            self.conn.commit()
        return len(rows)

    def count_pending_memory(self) -> int:
        """pending 待审记忆条数（Phase B 可观测性）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM memory WHERE pending = 1"
            ).fetchone()
        return int(row["n"])

    def get_files(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM files")]

    def remove_file(self, file_id: str):
        """删除一个文件及其全部条目（用于规则升级后清理已淘汰的噪音文件）。"""
        with self._lock:
            self.conn.execute("DELETE FROM entries WHERE file_id=?", (file_id,))
            self.conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            self.conn.commit()

    def remove_entries(self, file_id: str, key_paths: list[str]):
        """删除指定文件中的特定条目（重扫后不再存在的旧条目，如已被过滤的键）。

        2026-08-19 扫描性能修复：逐条 DELETE 每条一个语句——改为
        executemany 单次提交（几百键时数百次 execute → 1 次）。"""
        if not key_paths:
            return
        with self._lock:
            self.conn.executemany(
                "DELETE FROM entries WHERE file_id=? AND key_path=?",
                [(file_id, kp) for kp in key_paths])
            self.conn.commit()

    # ── 项目级游戏档案 ──
    def get_profile(self) -> GameProfile:
        with self._lock:
            row = self.conn.execute("SELECT value FROM profile WHERE key='game_profile'").fetchone()
            if not row:
                return GameProfile()
            try:
                data = json.loads(row["value"])
                return GameProfile(**{k: v for k, v in data.items()
                                      if k in GameProfile.__dataclass_fields__})
            except (json.JSONDecodeError, TypeError):
                return GameProfile()

    def set_profile(self, profile: GameProfile):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO profile(key, value) VALUES ('game_profile', ?)",
                (json.dumps(profile.__dict__, ensure_ascii=False),))
            self.conn.commit()

    # ── 通用 profile key-value（扫描绑定清单持久化，2026-08-12） ──
    # --resume 续跑跳过扫描，但 write_all 输入闸门要求 _last_source_manifest
    # 非 None、IL2CPP 写回要求规范输入证据——成功扫描后把清单存库，
    # 续跑时恢复（faerie 续跑实证：resume 写回被「缺少成功扫描绑定的
    # 完整输入清单」拒绝）。
    def get_profile_value(self, key: str, default=None):
        """读取通用 profile 值（JSON 解析失败返回 default）。"""
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT value FROM profile WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return default   # profile 表尚未 init_schema（旧库/全新库）
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_profile_value(self, key: str, value) -> None:
        """写入通用 profile 值（JSON 序列化，覆盖旧值）。"""
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO profile(key, value) VALUES (?, ?)",
                    (key, json.dumps(value, ensure_ascii=False)))
                self.conn.commit()
        except sqlite3.OperationalError:
            pass   # 表不存在时无法持久化——下次扫描（init_schema 后）重写

    def del_profile_value(self, key: str) -> None:
        """删除通用 profile 值（扫描失败清空绑定，防陈旧清单误用）。"""
        try:
            with self._lock:
                self.conn.execute("DELETE FROM profile WHERE key=?", (key,))
                self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def close(self):
        with self._lock:
            self.conn.close()


def settle_translation_memory(store, entries, model: str, lang: str) -> dict:
    """审后记忆结算（Phase B PendingEvidence 的唯一结算点）。

    由 GUI/headless 在审核步骤结束后各调用一次；与 review_entries 内部
    的逐条记忆门禁（_memory_apply）互补且幂等：

    - 条目有审核终态 APPROVED/APPROVED_MINOR → promote（pending=0，
      可命中；反馈重译产生的新译文经 upsert 直接提交）；
    - 条目有审核终态（其余全部）→ 撤销（remove，坏译连 pending 也不留）；
    - 条目无审核终态（审核关闭/审核器不可用/未送审）→ 机械质量门已是
      最后裁决，promote（保持既有行为——审核关闭时记忆仍可用）。

    返回 {"promoted": n, "revoked": n}（供报告可观测性）。
    """
    committed: list[tuple] = []
    revoked = 0
    for e in entries:
        translation = str(e.translation or "")
        if not translation or e.status != STATUS_TRANSLATED:
            continue                    # 未通过机械门/无译文不结算
        meta = e.meta or {}
        if meta.get("echo_exempt") or meta.get("language_source_kept"):
            continue   # 回显/语言保持条目不产生记忆（与翻译批入桶规则一致）
        outcome = meta.get("review_outcome")
        if outcome is None:
            committed.append((e.original, translation, model, lang))
        elif outcome in _PUBLISHABLE_OUTCOMES:
            committed.append((e.original, translation, model, lang))
        else:
            store.remove_memory(e.original, model, lang)
            revoked += 1
    promoted = store.promote_memory(committed) if committed else 0
    # BUILTIN 冲突门禁（2026-09-01）：promote 内过滤冲突对，返回条数
    # 已剔除——这里补记 revoked 口径（promoted+revoked 守恒）。
    revoked += max(0, len(committed) - promoted)
    return {"promoted": promoted, "revoked": revoked}
