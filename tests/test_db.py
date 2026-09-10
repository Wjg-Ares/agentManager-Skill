"""连接参数、迁移、注册表。三个 PRAGMA 是并发正确性的地基，必须实测。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import fresh_db, register_worker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from poolkit import config, db, registry  # noqa: E402
from poolkit.errors import NotSetUp, UsageError  # noqa: E402
from poolkit.models import WorkerStatus  # noqa: E402


class ConnectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, self.path = fresh_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_wal_is_on(self) -> None:
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal", "没有 WAL，多进程读写会互相阻塞")

    def test_foreign_keys_are_on(self) -> None:
        """SQLite 默认关外键，不显式打开约束形同虚设。"""
        on = self.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(on, 1)

    def test_busy_timeout_is_set(self) -> None:
        value = self.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(value, config.BUSY_TIMEOUT_MS)

    def test_foreign_key_actually_rejects_bad_rows(self) -> None:
        with self.assertRaises(Exception):
            self.conn.execute(
                "INSERT INTO claims(task_id, worker, file_path, display_path, claimed_at) "
                "VALUES (9999, 'worker-9', 'x', 'x', '2026-01-01T00:00:00Z')"
            )

    def test_migration_is_idempotent(self) -> None:
        self.assertEqual(db.schema_version(self.conn), config.SCHEMA_VERSION)
        self.assertEqual(db.migrate(self.conn), config.SCHEMA_VERSION)

    def test_missing_db_raises_not_setup(self) -> None:
        with self.assertRaises(NotSetUp):
            db.connect(self.path.parent / "nope.db")

    def test_settings_fall_back_to_defaults(self) -> None:
        self.assertEqual(db.get_int_setting(self.conn, "max_workers"), 3)
        db.set_setting(self.conn, "max_workers", "2", "2026-01-01T00:00:00Z")
        self.assertEqual(db.get_int_setting(self.conn, "max_workers"), 2)


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn, _ = fresh_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_reregister_updates_address(self) -> None:
        """worker 重启后会话名变了，role 不变 —— 这层翻译就是为这个存在的。"""
        register_worker(self.conn, "worker-1")
        registry.register(
            self.conn,
            role="worker-1",
            session_id="sid-new",
            session_name="cdc-9x",
            pid=2222,
            cwd="D:/workspace/demo",
        )
        reg = registry.require(self.conn, "worker-1")
        self.assertEqual(reg.session_name, "cdc-9x")
        self.assertEqual(reg.session_id, "sid-new")
        self.assertEqual(len(registry.workers(self.conn)), 1)

    def test_one_session_cannot_hold_two_roles(self) -> None:
        registry.register(
            self.conn, role="worker-1", session_id="same", session_name="s", pid=1, cwd="."
        )
        registry.register(
            self.conn, role="worker-2", session_id="same", session_name="s", pid=1, cwd="."
        )
        self.assertIsNone(registry.get(self.conn, "worker-1"))
        self.assertEqual(registry.require(self.conn, "worker-2").session_id, "same")

    def test_lookup_by_session_id(self) -> None:
        register_worker(self.conn, "worker-2")
        reg = registry.by_session(self.conn, "sid-worker-2")
        self.assertIsNotNone(reg)
        self.assertEqual(reg.role, "worker-2")
        self.assertTrue(reg.is_worker)

    def test_bad_role_is_rejected(self) -> None:
        for role in ("worker", "worker-0", "Worker-1", "boss", "worker-1x"):
            with self.subTest(role=role):
                with self.assertRaises(UsageError):
                    registry.validate_role(role)

    def test_status_transition(self) -> None:
        register_worker(self.conn, "worker-1")
        with db.transaction(self.conn):
            registry.set_status(self.conn, "worker-1", WorkerStatus.DEAD, now="2026-01-01T00:00:00Z")
        self.assertIs(registry.require(self.conn, "worker-1").status, WorkerStatus.DEAD)


if __name__ == "__main__":
    unittest.main()
