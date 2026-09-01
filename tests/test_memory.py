import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from hanhua.core.memory import ProjectStore
from hanhua.core.models import GameProfile


def _store():
    return ProjectStore(Path(tempfile.mkdtemp()) / "p.db")


def test_store_closes_connection_when_initialization_fails(tmp_path, monkeypatch):
    class FailingConnection:
        def __init__(self):
            self.closed = False

        @property
        def row_factory(self):
            return None

        @row_factory.setter
        def row_factory(self, _value):
            raise RuntimeError("forced initialization failure")

        def close(self):
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(
        "hanhua.core.memory.sqlite3.connect", lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(RuntimeError, match="forced initialization failure"):
        ProjectStore(tmp_path / "project.db")

    assert connection.closed is True


def test_store_passes_configured_timeout_to_sqlite(tmp_path, monkeypatch):
    captured = {}

    class Connection:
        row_factory = None

        def execute(self, *_args):
            return None

        def close(self):
            pass

    def connect(*args, **kwargs):
        captured.update(kwargs)
        return Connection()

    monkeypatch.setattr("hanhua.core.memory.sqlite3.connect", connect)

    store = ProjectStore(tmp_path / "project.db", timeout=0.25)
    store.close()

    assert captured["timeout"] == 0.25


def test_project_store_enables_wal_mode(tmp_path):
    """2026-08-14 卡顿优化：WAL + synchronous=NORMAL——journal 模式每
    commit 一次 fsync（翻译每批 2 次 commit，万级条目约 5000 次 fsync
    阻塞 worker 与所有 DB 访问）；WAL 下 commit 免逐次 fsync、
    checkpoint 批量落盘。"""
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    sync = store.conn.execute("PRAGMA synchronous").fetchone()[0]
    assert sync == 1            # NORMAL
    store.close()


def test_schema_tables_reports_initialized_project_tables():
    store = _store()
    store.init_schema()

    tables = store.schema_tables()

    assert isinstance(tables, frozenset)
    assert {"files", "entries", "memory", "profile"} <= tables


def test_store_lifecycle():
    store = _store()
    store.init_schema()
    store.add_file("f1", "Localization/en.json", "json", "utf-8", "\n")
    store.upsert_entries([
        {"file_id": "f1", "key_path": "a", "original": "Hello"},
        {"file_id": "f1", "key_path": "b", "original": "World"},
    ])
    assert store.count("pending") == 2
    store.update_translation("f1", "a", "你好")
    assert store.count("translated") == 1
    store.add_memory("Hello", "你好", "m1", "en→zh-CN")
    hits = store.get_memory_hits(["Hello", "World"], model="m1", lang="en→zh-CN")
    assert hits == {"Hello": "你好"}
    hits2 = store.get_memory_hits(["Hello"], model="m1", lang="en→zh-CN")
    assert hits2 == {"Hello": "你好"}
    # 不同语言对不命中
    assert store.get_memory_hits(["Hello"], model="m1", lang="ja→zh-CN") == {}


def test_locked_and_manual():
    store = _store()
    store.init_schema()
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "Hello", "meta": {}}])
    store.set_locked("f1", "a", True)
    assert store.get_entries()[0]["locked"] == 1
    store.set_manual("f1", "a", "用户译文")
    row = store.get_entries()[0]
    assert row["translation"] == "用户译文" and row["status"] == "translated"


def test_clear_translation_memory_preserves_project_data():
    store = _store()
    store.init_schema()
    store.upsert_entries([{
        "file_id": "f1", "key_path": "a", "original": "Hello", "meta": {},
    }])
    store.set_manual("f1", "a", "用户译文")
    store.set_locked("f1", "a", True)
    store.set_profile(GameProfile(game_name="Fixture Game"))
    store.add_memory("Hello", "你好", "m1", "en→zh-CN")
    store.add_memory("World", "世界", "m1", "en→zh-CN")

    cleared_rows = store.clear_translation_memory()

    assert cleared_rows == 2
    assert store.get_memory_hits(["Hello", "World"], "m1", "en→zh-CN") == {}
    assert store.get_entries()[0]["translation"] == "用户译文"
    assert store.get_entries()[0]["locked"] == 1
    assert store.get_profile().game_name == "Fixture Game"
    assert store.clear_translation_memory() == 0


def test_clear_translation_memory_rolls_back_when_delete_fails():
    store = _store()
    store.init_schema()
    store.add_memory("Hello", "你好", "m1", "en→zh-CN")
    with sqlite3.connect(store.db) as setup_connection:
        setup_connection.executescript("""
            CREATE TRIGGER abort_memory_delete
            BEFORE DELETE ON memory
            BEGIN
                SELECT RAISE(ABORT, 'forced');
            END;
        """)

    with pytest.raises(sqlite3.DatabaseError, match="forced"):
        store.clear_translation_memory()

    with sqlite3.connect(store.db) as cleanup_connection:
        cleanup_connection.execute("DROP TRIGGER abort_memory_delete")
    assert store.get_memory_hits(["Hello"], "m1", "en→zh-CN") == {
        "Hello": "你好",
    }


def test_clearing_manual_translation_restores_pending_status():
    store = _store()
    store.init_schema()
    store.upsert_entries([{
        "file_id": "f1", "key_path": "a", "original": "Hello", "meta": {},
    }])
    store.set_manual("f1", "a", "用户译文")

    store.set_manual("f1", "a", "")

    row = store.get_entries()[0]
    assert row["translation"] == ""
    assert row["status"] == "pending"
    assert json.loads(row["meta"])["quality_passed"] is False


def test_upsert_idempotent():
    store = _store()
    store.init_schema()
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "Hello", "meta": {}}])
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "Hello", "meta": {}}])
    assert len(store.get_entries()) == 1


def test_upsert_entries_select_count_is_batch_bounded():
    class CountingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.execute_calls = 0
            self.executemany_calls = 0
            self.commit_calls = 0

        def execute(self, *args, **kwargs):
            self.execute_calls += 1
            return self.connection.execute(*args, **kwargs)

        def executemany(self, *args, **kwargs):
            self.executemany_calls += 1
            return self.connection.executemany(*args, **kwargs)

        def commit(self):
            self.commit_calls += 1
            return self.connection.commit()

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def call_counts(row_count):
        store = _store()
        store.init_schema()
        store.conn.executemany(
            "INSERT INTO entries(file_id,key_path,original,translation,status,locked,meta) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (f"f{index}", "old", f"Text {index}", f"译文 {index}",
                 "translated", 1, json.dumps({"quality_passed": True}))
                for index in range(row_count)
            ],
        )
        store.conn.commit()
        connection = store.conn
        counted = CountingConnection(connection)
        store.conn = counted
        statements = []
        store.conn.set_trace_callback(statements.append)

        store.upsert_entries([
            {
                "file_id": f"f{index}",
                "key_path": "new",
                "original": f"Text {index}",
                "status": "pending",
                "meta": {},
            }
            for index in range(row_count)
        ])
        store.conn.set_trace_callback(None)
        counts = (
            counted.execute_calls,
            counted.executemany_calls,
            counted.commit_calls,
        )
        store.conn = connection
        migrated = {
            row["file_id"]: row for row in store.get_entries()
            if row["key_path"] == "new"
        }
        assert len(migrated) == row_count
        assert all(migrated[f"f{index}"]["translation"] == f"译文 {index}"
                   for index in range(row_count))

        select_count = sum(statement.lstrip().upper().startswith("SELECT ")
                           for statement in statements)
        return select_count, counts

    one_row_selects, one_row_calls = call_counts(1)
    full_batch_selects, full_batch_calls = call_counts(256)

    assert full_batch_selects == one_row_selects
    assert full_batch_selects <= 2
    assert one_row_calls == full_batch_calls
    execute_calls, executemany_calls, commit_calls = full_batch_calls
    assert execute_calls <= 2
    assert executemany_calls == 1
    assert commit_calls == 1


def test_upsert_entries_batch_matches_sequential_merge_order():
    batch_store, sequential_store = _store(), _store()
    for store in (batch_store, sequential_store):
        store.init_schema()
        store.upsert_entries([{
            "file_id": "f", "key_path": "a", "original": "Hello",
            "status": "pending", "meta": {},
        }])
        store.set_manual("f", "a", "你好")
        store.set_locked("f", "a", True)

    rows = [
        {"file_id": "f", "key_path": "a", "original": "Hello",
         "status": "pending", "meta": {"locator": 1}},
        {"file_id": "f", "key_path": "b", "original": "Hello",
         "status": "pending", "meta": {}},
        {"file_id": "f", "key_path": "b", "original": "CodeKey",
         "status": "skipped", "meta": {"reason": "structural"}},
        {"file_id": "f", "key_path": "a", "original": "Changed",
         "status": "pending", "meta": {"locator": 2}},
    ]

    batch_store.upsert_entries(rows)
    for row in rows:
        sequential_store.upsert_entries([row])

    def stable(store):
        return sorted(
            [
                {key: value for key, value in row.items() if key != "id"}
                for row in store.get_entries()
            ],
            key=lambda row: (row["file_id"], row["key_path"]),
        )

    assert stable(batch_store) == stable(sequential_store)


def test_upsert_entries_holds_write_transaction_across_prefetch(tmp_path):
    store = ProjectStore(tmp_path / "project.db")
    store.init_schema()
    store.upsert_entries([{
        "file_id": "f", "key_path": "a", "original": "Hello",
        "status": "pending", "meta": {},
    }])
    store.set_manual("f", "a", "你好")
    competing = sqlite3.connect(store.db, timeout=0.01)
    attempts = []

    class InterleavingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args, **kwargs):
            cursor = self.connection.execute(sql, *args, **kwargs)
            if not sql.lstrip().upper().startswith("SELECT ID, FILE_ID"):
                return cursor

            class Cursor:
                def __iter__(self):
                    yield from cursor
                    try:
                        competing.execute(
                            "UPDATE entries SET translation='并发译文', locked=1 "
                            "WHERE file_id='f' AND key_path='a'",
                        )
                        competing.commit()
                        attempts.append("committed")
                    except sqlite3.OperationalError as exc:
                        competing.rollback()
                        attempts.append("locked" if "locked" in str(exc) else str(exc))

            return Cursor()

        def __getattr__(self, name):
            return getattr(self.connection, name)

    connection = store.conn
    store.conn = InterleavingConnection(connection)
    try:
        store.upsert_entries([{
            "file_id": "f", "key_path": "a", "original": "Hello",
            "status": "pending", "meta": {"locator": 1},
        }])
    finally:
        store.conn = connection

    assert attempts == ["locked"]
    row = store.get_entries()[0]
    assert (row["translation"], row["locked"]) == ("你好", 0)
    competing.execute(
        "UPDATE entries SET locked=1 WHERE file_id='f' AND key_path='a'",
    )
    competing.commit()
    competing.close()


def test_upsert_entries_rolls_back_partial_batch_on_write_failure():
    store = _store()
    store.init_schema()
    store.conn.executescript("""
        CREATE TRIGGER abort_bad_entry
        BEFORE INSERT ON entries
        WHEN NEW.key_path = 'bad'
        BEGIN
            SELECT RAISE(ABORT, 'forced batch failure');
        END;
    """)

    with pytest.raises(sqlite3.DatabaseError, match="forced batch failure"):
        store.upsert_entries([
            {"file_id": "f", "key_path": "good", "original": "Hello",
             "status": "pending", "meta": {}},
            {"file_id": "f", "key_path": "bad", "original": "World",
             "status": "pending", "meta": {}},
        ])

    assert store.get_entries() == []
    store.conn.execute("DROP TRIGGER abort_bad_entry")
    store.conn.commit()
    store.upsert_entries([{
        "file_id": "f", "key_path": "good", "original": "Hello",
        "status": "pending", "meta": {},
    }])
    assert len(store.get_entries()) == 1


def test_upsert_preserves_skipped_status():
    store = _store()
    store.init_schema()
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "12345",
                           "status": "skipped", "meta": {}}])
    assert store.count("skipped") == 1
    assert store.count("pending") == 0


def test_upsert_skipped_overrides_translated():
    """键位置条目：即使旧状态是 translated（被误翻译），重扫为 skipped 时必须降级。"""
    store = _store()
    store.init_schema()
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "NEW GAME",
                           "status": "pending", "meta": {}}])
    store.set_manual("f1", "a", "开始游戏")          # 用户误翻译了键位置
    assert store.count("translated") == 1
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "NEW GAME",
                           "status": "skipped", "meta": {}}])
    assert store.count("skipped") == 1
    assert store.count("translated") == 0
    assert store.get_entries()[0]["translation"] == ""   # 键译文被清除


def test_upsert_pending_keeps_translation():
    """值位置条目：重扫为 pending 时，已翻译的译文必须保留（断点续传）。"""
    store = _store()
    store.init_schema()
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "Hello",
                           "status": "pending", "meta": {}}])
    store.set_manual("f1", "a", "你好")
    store.upsert_entries([{"file_id": "f1", "key_path": "a", "original": "Hello",
                           "status": "pending", "meta": {}}])
    row = store.get_entries()[0]
    assert row["status"] == "translated" and row["translation"] == "你好"
    meta = json.loads(row["meta"])
    assert meta["quality_passed"] is True
    assert meta["quality_source"] == "manual_api"
    assert meta["confidence_promoted"] is True


def test_upsert_migrates_unique_translation_to_new_key():
    """提取器升级键路径时，同文件同原文的唯一译文应无损迁移。"""
    store = _store()
    store.init_schema()
    store.upsert_entries([{
        "file_id": "table.bundle",
        "key_path": "asset#7/str/1",
        "original": "VOLUME",
        "status": "pending",
        "meta": {"kind": "rawstr"},
    }])
    store.set_manual("table.bundle", "asset#7/str/1", "音量")

    store.upsert_entries([{
        "file_id": "table.bundle",
        "key_path": "asset#7/loc/99",
        "original": "VOLUME",
        "status": "pending",
        "meta": {"kind": "localization", "entry_id": 99},
    }])

    row = next(e for e in store.get_entries() if e["key_path"].endswith("/loc/99"))
    assert (row["translation"], row["status"]) == ("音量", "translated")


def test_upsert_migration_merges_ordered_candidate_evidence():
    store = _store()
    store.init_schema()
    store.conn.executemany(
        "INSERT INTO entries(file_id,key_path,original,translation,status,locked,meta) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("f", "old/1", "VOLUME", "音量", "reviewed", 0,
             json.dumps({"quality_passed": False, "quality_source": "rejected"})),
            ("f", "old/2", "VOLUME", "音量", "pending", 0,
             json.dumps({"quality_passed": True, "quality_source": "first-good",
                         "quality_reasons": ["verified"], "confidence_promoted": True})),
            ("f", "old/3", "VOLUME", "音量", "translated", 1,
             json.dumps({"quality_passed": True, "quality_source": "later-good"})),
            ("g", "old/1", "START", "开始", "reviewed", 0, "{}"),
            ("g", "old/2", "START", "开始", "pending", 0, "{}"),
        ],
    )
    store.conn.commit()

    store.upsert_entries([
        {"file_id": "f", "key_path": "old/2", "original": "VOLUME",
         "status": "pending", "meta": {"refreshed": True}},
        {"file_id": "g", "key_path": "old/1", "original": "START",
         "status": "pending", "meta": {"refreshed": True}},
        {"file_id": "f", "key_path": "new", "original": "VOLUME",
         "status": "pending", "meta": {"locator": 9, "quality_source": "incoming"}},
        {"file_id": "g", "key_path": "new", "original": "START",
         "status": "pending", "meta": {}},
    ])

    rows = {(row["file_id"], row["key_path"]): row for row in store.get_entries()}
    migrated = rows[("f", "new")]
    assert (migrated["translation"], migrated["status"], migrated["locked"]) == (
        "音量", "translated", 1,
    )
    meta = json.loads(migrated["meta"])
    assert meta["locator"] == 9
    assert meta["quality_passed"] is True
    assert meta["quality_source"] == "first-good"
    assert meta["quality_reasons"] == ["verified"]
    assert meta["confidence_promoted"] is True
    assert rows[("g", "new")]["status"] == "reviewed"


def test_upsert_migration_does_not_copy_failed_quality_evidence():
    store = _store()
    store.init_schema()
    store.conn.execute(
        "INSERT INTO entries(file_id,key_path,original,translation,status,locked,meta) "
        "VALUES (?,?,?,?,?,?,?)",
        ("f", "old", "VOLUME", "音量", "translated", 0,
         json.dumps({"quality_passed": False, "quality_source": "bad"})),
    )
    store.conn.commit()

    store.upsert_entries([{
        "file_id": "f", "key_path": "new", "original": "VOLUME",
        "status": "pending", "meta": {"locator": 7},
    }])

    row = next(row for row in store.get_entries() if row["key_path"] == "new")
    assert row["translation"] == "音量"
    assert json.loads(row["meta"]) == {"locator": 7}


def test_upsert_tolerates_malformed_stored_and_incoming_meta():
    store = _store()
    store.init_schema()
    store.conn.executemany(
        "INSERT INTO entries(file_id,key_path,original,translation,status,locked,meta) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("f", "same", "Hello", "你好", "translated", 1, "{bad"),
            ("g", "old", "START", "开始", "translated", 1, "[bad"),
            ("i", "same", "OLD", "旧译", "translated", 1,
             json.dumps({"quality_passed": True})),
        ],
    )
    store.conn.commit()

    store.upsert_entries([
        {"file_id": "f", "key_path": "same", "original": "Hello",
         "status": "pending", "meta": "{also bad"},
        {"file_id": "g", "key_path": "new", "original": "START",
         "status": "pending", "meta": ["not", "a dict"]},
        {"file_id": "h", "key_path": "new", "original": "STRUCTURAL",
         "status": "skipped", "meta": "{bad"},
        {"file_id": "i", "key_path": "same", "original": "NEW",
         "status": "pending", "meta": "{bad"},
    ])

    rows = {(row["file_id"], row["key_path"]): row for row in store.get_entries()}
    same = rows[("f", "same")]
    assert (same["translation"], same["status"], same["locked"]) == (
        "你好", "translated", 1,
    )
    assert json.loads(same["meta"]) == {}
    migrated = rows[("g", "new")]
    assert (migrated["translation"], migrated["status"], migrated["locked"]) == (
        "开始", "translated", 1,
    )
    assert json.loads(migrated["meta"]) == {}
    assert json.loads(rows[("h", "new")]["meta"]) == {}
    changed = rows[("i", "same")]
    assert (changed["original"], changed["translation"], changed["status"],
            changed["locked"]) == ("NEW", "", "pending", 1)
    assert json.loads(changed["meta"]) == {}


def test_upsert_normalizes_stored_meta_in_noop_status_branches():
    store = _store()
    store.init_schema()
    store.conn.executemany(
        "INSERT INTO entries(file_id,key_path,original,translation,status,locked,meta) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("f", "key", "STRUCTURAL", "", "skipped", 0, "{bad"),
            ("g", "key", "Hello", "你好", "translated", 1, "[bad"),
        ],
    )
    store.conn.commit()

    store.upsert_entries([
        {"file_id": "f", "key_path": "key", "original": "STRUCTURAL",
         "status": "skipped", "meta": {"incoming": "ignored"}},
        {"file_id": "g", "key_path": "key", "original": "Hello",
         "status": "reviewed", "meta": {"incoming": "ignored"}},
    ])

    rows = {row["file_id"]: row for row in store.get_entries()}
    assert json.loads(rows["f"]["meta"]) == {}
    assert json.loads(rows["g"]["meta"]) == {}
    assert (rows["g"]["translation"], rows["g"]["status"], rows["g"]["locked"]) == (
        "你好", "translated", 1,
    )


def test_upsert_does_not_guess_between_conflicting_translations():
    """同原文存在冲突译文时宁可保留 pending，也不能任意迁移。"""
    store = _store()
    store.init_schema()
    for key, translation in (("old/1", "音量"), ("old/2", "声量")):
        store.upsert_entries([{
            "file_id": "table.bundle", "key_path": key,
            "original": "VOLUME", "status": "pending", "meta": {},
        }])
        store.set_manual("table.bundle", key, translation)

    store.upsert_entries([{
        "file_id": "table.bundle", "key_path": "new/99",
        "original": "VOLUME", "status": "pending", "meta": {},
    }])

    row = next(e for e in store.get_entries() if e["key_path"] == "new/99")
    assert (row["translation"], row["status"]) == ("", "pending")


# ── Phase B PendingEvidence：审后提交/撤销（审计 §5 P0-3）────────────

def test_pending_memory_staged_until_promoted():
    """翻译批记忆入 pending 桶：promote 前不参与任何命中（坏译不得在
    深审前污染下一轮 prompt）；promote 后可见。"""
    store = _store()
    store.init_schema()
    store.batch_add_memory([("Open Door", "打开门", "m", "en→zh-CN")])
    # 未提交：不可命中 + 可观测 pending 计数
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {}
    assert store.count_pending_memory() == 1
    # 审核通过 → promote → 可命中、pending 清零
    store.promote_memory([("Open Door", "打开门", "m", "en→zh-CN")])
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门"}
    assert store.count_pending_memory() == 0


def test_revoke_pending_memory_deletes_it():
    """审核判坏 → 撤销：pending 记忆直接删除，不残留待审垃圾。"""
    store = _store()
    store.init_schema()
    store.batch_add_memory([("Open Door", "坏译文", "m", "en→zh-CN")])
    store.remove_memory("Open Door", "m", "en→zh-CN")
    assert store.count_pending_memory() == 0
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {}


def test_explicit_add_memory_is_committed_immediately():
    """显式 add_memory（人工/审核直达写入）→ pending=0 立即可命中；
    旧行为保持（测试与人工写入不因 pending 协议失效）。"""
    store = _store()
    store.init_schema()
    store.add_memory("Open Door", "打开门", "m", "en→zh-CN")
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门"}


def test_add_memory_blocks_builtin_conflict(tmp_path):
    """BUILTIN 冲突门禁（2026-09-01 污染系统性根治）：add_memory 单
    token Disabled→残疾人士（UI 状态标签误判「残疾」）→ 不落库——权威
    译文（已禁用）由确定性直填在运行期恒胜出；权威译文与非冲突词照常
    提交。"""
    store = _store()
    store.init_schema()
    store.add_memory("Disabled", "残疾人士", "m", "en→zh-CN")
    assert store.get_memory_hits(["Disabled"], "m", "en→zh-CN") == {}
    # 权威译文 / 非冲突词照常
    store.add_memory("Disabled", "已禁用", "m", "en→zh-CN")
    store.add_memory("Enabled", "已启用", "m", "en→zh-CN")
    store.add_memory("Press any key", "按任意键", "m", "en→zh-CN")
    store.add_memory("itch", "itch", "m", "en→zh-CN")
    hits = store.get_memory_hits(
        ["Disabled", "Enabled", "Press any key", "itch"], "m", "en→zh-CN")
    assert hits == {"Disabled": "已禁用", "Enabled": "已启用",
                    "Press any key": "按任意键", "itch": "itch"}


def test_batch_add_memory_blocks_builtin_conflict(tmp_path):
    """批量 pending 桶同样拦截：Disabled→残疾人士 不入库——即使审核
    关闭被 settle promote 也不会成为可命中记忆。"""
    store = _store()
    store.init_schema()
    store.batch_add_memory([
        ("Disabled", "残疾人士", "m", "en→zh-CN"),
        ("Open Door", "打开门", "m", "en→zh-CN"),
    ])
    assert store.count_pending_memory() == 1            # 只有合法词对
    store.promote_memory([("Disabled", "残疾人士", "m", "en→zh-CN"),
                          ("Open Door", "打开门", "m", "en→zh-CN")])
    # promote 同样拦截冲突词对：提交条数只算合法词
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门"}
    assert store.get_memory_hits(["Disabled"], "m", "en→zh-CN") == {}


def test_promote_memory_blocks_builtin_conflict(tmp_path):
    """promote（审核通过后提交）直接返回过滤后条数——调用方（settle）
    据此补记 revoked，守恒不破。"""
    store = _store()
    store.init_schema()
    store.batch_add_memory([
        ("Disabled", "残疾人士", "m", "en→zh-CN"),
        ("Start Game", "开始游戏", "m", "en→zh-CN"),
    ])
    promoted = store.promote_memory([
        ("Disabled", "残疾人士", "m", "en→zh-CN"),
        ("Start Game", "开始游戏", "m", "en→zh-CN"),
    ])
    assert promoted == 1                                 # 只提升合法词
    assert store.count_pending_memory() == 0
    assert store.get_memory_hits(["Start Game"], "m", "en→zh-CN") == {
        "Start Game": "开始游戏"}
    assert store.get_memory_hits(["Disabled"], "m", "en→zh-CN") == {}


def test_settle_builtin_conflict_promoted_zero(tmp_path):
    """settle_translation_memory 守恒：冲突词对被 promote 过滤 → 计数为
    revoked（不成为可命中记忆），promoted+revoked 恒等于 committed。"""
    from hanhua.core.models import TextEntry
    from hanhua.core.memory import settle_translation_memory

    def entry(original, translation):
        return TextEntry("f", original, original, translation=translation,
                         status="translated", meta={"confidence": "high"})

    store = _store()
    store.init_schema()
    store.batch_add_memory([("Disabled", "残疾人士", "m", "en→zh-CN"),
                            ("Start Game", "开始游戏", "m", "en→zh-CN")])
    result = settle_translation_memory(
        store, [entry("Disabled", "残疾人士"),
                entry("Start Game", "开始游戏")], "m", "en→zh-CN")
    assert result["promoted"] == 1                        # 只提升 Start Game
    assert result["revoked"] == 1                         # Disabled 被过滤
    assert result["promoted"] + result["revoked"] == 2    # 守恒
    assert store.get_memory_hits(["Disabled"], "m", "en→zh-CN") == {}
    assert store.get_memory_hits(["Start Game"], "m", "en→zh-CN") == {
        "Start Game": "开始游戏"}


def test_old_schema_migrates_pending_column():
    """旧库（无 pending 列）init_schema 迁移：列补齐，旧记忆视为已提交。"""
    import hashlib as _hashlib
    import sqlite3 as _sqlite3
    path = Path(tempfile.mkdtemp()) / "old.db"
    conn = _sqlite3.connect(str(path))
    conn.execute("CREATE TABLE memory("
                 "src_hash TEXT PRIMARY KEY, original TEXT,"
                 " translation TEXT, model TEXT, lang TEXT,"
                 " created_at TEXT DEFAULT (datetime('now')))")
    h = _hashlib.md5("Open Door".encode("utf-8")).hexdigest()
    conn.execute("INSERT INTO memory(src_hash, original, translation,"
                 " model, lang) VALUES (?, 'Open Door', '打开门',"
                 " 'm', 'en→zh-CN')", (h,))
    conn.commit()
    conn.close()
    store = ProjectStore(path)
    store.init_schema()
    assert store.get_memory_hits(["Open Door"], "m", "en→zh-CN") == {
        "Open Door": "打开门"}     # 旧数据 = 已提交


def test_settle_promotes_approved_and_no_outcome_revokes_rejected():
    """审后结算唯一决策点（settle_translation_memory）：
    APPROVED → promote；BLOCKED 等非发布终态 → 撤销；无终态（审核
    关闭/不可用）→ 机械门即最后裁决 promote。"""
    from hanhua.core.models import TextEntry

    def entry(original, translation, outcome=None, status="translated"):
        meta = {"confidence": "high"}
        if outcome:
            meta["review_outcome"] = outcome
        return TextEntry("f", original, original, translation=translation,
                         status=status, meta=meta)

    from hanhua.core.memory import settle_translation_memory
    store = _store()
    store.init_schema()
    store.batch_add_memory([("A", "坏译文A", "m", "en→zh-CN"),
                            ("B", "坏译文B", "m", "en→zh-CN")])
    entries = [
        entry("A", "好译文A", outcome="APPROVED"),
        entry("B", "坏译文B", outcome="BLOCKED"),
        entry("C", "好译文C"),                       # 无终态 → promote
        entry("D", "回显D", outcome="APPROVED"),     # echo → 不产生记忆
    ]
    entries[3].meta["echo_exempt"] = "proper_name"
    result = settle_translation_memory(store, entries, "m", "en→zh-CN")
    assert result["promoted"] == 2                   # A(覆盖坏译文) + C
    assert result["revoked"] == 1                    # B
    assert store.get_memory_hits(["A"], "m", "en→zh-CN") == {"A": "好译文A"}
    assert store.get_memory_hits(["B"], "m", "en→zh-CN") == {}
    assert store.get_memory_hits(["C"], "m", "en→zh-CN") == {"C": "好译文C"}
    assert store.get_memory_hits(["D"], "m", "en→zh-CN") == {}
    assert store.count_pending_memory() == 0
