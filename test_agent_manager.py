from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import agent_manager as manager


class AgentStoreTest(unittest.TestCase):
    def _store(self, directory: str, *, owner: str = "test-owner") -> manager.AgentStore:
        return manager.AgentStore(Path(directory) / "state" / "agents.sqlite3", owner_id=owner)

    def test_configured_state_dir_is_absolute_and_owns_default_database(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "configured-state"
            with patch.object(manager, "_STATE_DIRECTORY", None), patch.dict(
                os.environ, {manager.STATE_DIR_ENV: str(state)}
            ):
                self.assertEqual(manager.default_state_dir(), state.resolve())
                self.assertEqual(
                    manager.default_db_path(), state.resolve() / "agents.sqlite3"
                )
                store = manager.AgentStore(owner_id="configured-state-test")
                self.assertEqual(store.path, state.resolve() / "agents.sqlite3")
                store.close()

            state_file = Path(directory) / "not-a-directory"
            state_file.write_text("x", encoding="utf-8")
            for invalid in ("relative-state", str(state_file)):
                with self.subTest(invalid=invalid), patch.object(
                    manager, "_STATE_DIRECTORY", None
                ), patch.dict(
                    os.environ, {manager.STATE_DIR_ENV: invalid}
                ):
                    with self.assertRaises((OSError, ValueError)):
                        manager.default_state_dir()

    def test_database_path_rejects_directory_on_all_platforms(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agents.sqlite3"
            database.mkdir()
            with self.assertRaises(OSError):
                manager.AgentStore(database, owner_id="invalid-database-test")

    def test_persists_snapshots_without_owner_or_database_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agents.sqlite3"
            store = manager.AgentStore(path, owner_id="owner-a")
            created = store.create(
                workspace=directory, thinking_level="medium", mode="plan",
                parent_agent_id="parent-1", conversation_id="conversation-1",
            )
            store.close()
            restored = manager.AgentStore(path, owner_id="owner-a")
            self.assertEqual(restored.get(created.agent_id), created)
            self.assertNotIn("owner_id", created.__dataclass_fields__)
            self.assertNotIn("path", created.__dataclass_fields__)
            columns = {
                row[1] for row in restored._db.execute("PRAGMA table_info(agents)")
            }
            self.assertNotIn("task", columns)
            self.assertNotIn("context", columns)
            self.assertNotIn("verification", columns)
            restored.close()

    def test_state_transitions_and_terminal_immutability(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            created = store.create(workspace=directory, thinking_level="low", mode="plan")
            self.assertEqual(created.status, "queued")
            running = store.mark_running(created.agent_id)
            self.assertIsNotNone(running)
            assert running is not None
            self.assertEqual(running.status, "running")
            finished = store.finish(
                created.agent_id, status="completed", output={"result": "done"},
                conversation_id="conv-1",
            )
            self.assertIsNotNone(finished)
            assert finished is not None
            self.assertEqual(finished.status, "completed")
            self.assertEqual(finished.output, {"result": "done"})
            self.assertEqual(store.mark_running(created.agent_id), finished)
            self.assertEqual(
                store.finish(
                    created.agent_id, status="failed", output={"changed": True},
                    manager_error="late",
                ),
                finished,
            )
            with self.assertRaises(ValueError):
                store.finish(created.agent_id, status="running")  # type: ignore[arg-type]
            store.close()

    def test_cancel_is_conditional_and_terminal_rows_do_not_change(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            queued = store.create(workspace=directory, thinking_level="low", mode="accept-edits")
            cancelled = store.request_cancel(queued.agent_id)
            self.assertIsNotNone(cancelled)
            self.assertTrue(store.cancel_requested(queued.agent_id))
            self.assertEqual(store.mark_running(queued.agent_id).status, "queued")
            finished = store.finish(queued.agent_id, status="interrupted")
            self.assertIsNotNone(finished)
            assert finished is not None
            self.assertEqual(store.request_cancel(queued.agent_id), finished)
            self.assertTrue(store.cancel_requested(queued.agent_id))
            self.assertIsNone(store.request_cancel("missing"))
            store.close()

    def test_filters_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            other = str(Path(directory) / "other")
            first = store.create(workspace=directory, thinking_level="low", mode="plan")
            store.create(workspace=other, thinking_level="high", mode="plan")
            store.mark_running(first.agent_id)
            self.assertEqual(
                [row.agent_id for row in store.list(status="running")], [first.agent_id]
            )
            self.assertEqual(
                [row.workspace for row in store.list(workspace=directory)],
                [manager._canonical_workspace(directory)],
            )
            self.assertLessEqual(len(store.list(limit=100)), 100)
            with self.assertRaises(ValueError):
                store.list(limit=101)
            with self.assertRaises(ValueError):
                store.list(limit=0)
            store.close()

    def test_reconcile_stale_running_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            created = store.create(workspace=directory, thinking_level="low", mode="plan")
            store.mark_running(created.agent_id)
            queued = store.create(workspace=directory, thinking_level="low", mode="plan")
            store._db.execute(
                "UPDATE agents SET updated_at=? WHERE agent_id IN (?,?)",
                (0, created.agent_id, queued.agent_id),
            )
            self.assertEqual(store.reconcile_stale(1), 2)
            stale = store.get(created.agent_id)
            self.assertIsNotNone(stale)
            assert stale is not None
            self.assertEqual(stale.status, "failed")
            self.assertEqual(stale.manager_error, "manager_lost")
            self.assertEqual(stale.progress["phase"], "failed")
            self.assertEqual(stale.progress["progress_percent"], 100)
            self.assertEqual(stale.progress["recent_events"][-1]["code"], "manager_lost")
            self.assertEqual(store.get(queued.agent_id).status, "failed")
            self.assertEqual(store.reconcile_stale(1), 0)
            store.close()

    def test_finish_atomically_honors_prior_cross_store_cancel(self):
        with tempfile.TemporaryDirectory(prefix="agent-cancel-finish-") as directory:
            path = Path(directory) / "agents.sqlite3"
            first = manager.AgentStore(path, owner_id="owner")
            second = manager.AgentStore(path, owner_id="owner")
            try:
                created = first.create(Path(directory), "low", "plan")
                first.mark_running(created.agent_id)
                second.request_cancel(created.agent_id)
                terminal = first.finish(
                    created.agent_id,
                    "completed",
                    output={"status": "SUCCESS", "result": "too late"},
                )
                self.assertEqual(terminal.status, "interrupted")
                self.assertEqual(terminal.manager_error, "cancelled")
                self.assertIsNone(terminal.output)
            finally:
                second.close()
                first.close()

    def test_reconcile_stale_fast_path_is_read_only_when_no_rows_are_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            created = store.create(
                workspace=directory, thinking_level="low", mode="plan"
            )
            before = store.get(created.agent_id)
            total_changes = store._db.total_changes

            with patch.object(
                store,
                "_begin",
                side_effect=AssertionError("read-only fast path opened a write transaction"),
            ):
                self.assertEqual(store.reconcile_stale(60), 0)

            self.assertEqual(store._db.total_changes, total_changes)
            self.assertEqual(store.get(created.agent_id), before)
            store.close()

    def test_progress_is_persisted_bounded_monotonic_and_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agents.sqlite3"
            store = manager.AgentStore(path, owner_id="progress-owner")
            created = store.create(workspace=directory, thinking_level="high", mode="plan")
            self.assertEqual(created.progress["phase"], "queued")
            store.mark_running(created.agent_id)
            for _ in range(manager.MAX_PROGRESS_EVENTS + 5):
                store.record_progress(
                    created.agent_id,
                    phase="executing",
                    progress_percent=50,
                    code="heartbeat",
                    next_action="postflight_started",
                    heartbeat=True,
                )
            progressed = store.record_progress(
                created.agent_id,
                phase="executing",
                progress_percent=25,
                code="cli_step",
                step={"index": 7, "state": "DONE", "type": "agent_response"},
            )
            assert progressed is not None and progressed.progress is not None
            self.assertEqual(progressed.progress["progress_percent"], 50)
            self.assertLessEqual(
                len(progressed.progress["recent_events"]), manager.MAX_PROGRESS_EVENTS
            )
            self.assertIsNotNone(progressed.progress["heartbeat_at"])
            store.close()

            restored = manager.AgentStore(path, owner_id="progress-owner")
            persisted = restored.get(created.agent_id)
            assert persisted is not None and persisted.progress is not None
            self.assertEqual(persisted.progress["step"]["index"], 7)
            terminal = restored.finish(created.agent_id, "completed")
            assert terminal is not None and terminal.progress is not None
            self.assertEqual(terminal.progress["phase"], "completed")
            self.assertEqual(terminal.progress["progress_percent"], 100)
            unchanged = restored.record_progress(
                created.agent_id, phase="executing", progress_percent=50,
                code="heartbeat", heartbeat=True,
            )
            self.assertEqual(unchanged, terminal)
            restored.close()

    def test_progress_rejects_free_form_telemetry_and_migrates_legacy_db(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.sqlite3"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE agents (
                agent_id TEXT PRIMARY KEY,parent_agent_id TEXT,owner_id TEXT NOT NULL,
                workspace TEXT NOT NULL,thinking_level TEXT NOT NULL,mode TEXT NOT NULL,
                status TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0,
                conversation_id TEXT,created_at TEXT NOT NULL,started_at TEXT,
                finished_at TEXT,updated_at REAL NOT NULL,output_json TEXT,
                manager_error TEXT)""")
            db.commit()
            db.close()
            store = manager.AgentStore(path, owner_id="legacy-owner")
            columns = {row[1] for row in store._db.execute("PRAGMA table_info(agents)")}
            self.assertIn("progress_json", columns)
            self.assertIsNotNone(
                store._db.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table' AND name='workspace_admissions'"""
                ).fetchone()
            )
            created = store.create(workspace=directory, thinking_level="low", mode="plan")
            with self.assertRaises(ValueError):
                store.record_progress(
                    created.agent_id, phase="executing", progress_percent=50,
                    code="TOKEN=secret",
                )
            safe = store.record_progress(
                created.agent_id, phase="executing", progress_percent=50,
                code="cli_step",
                step={"index": 1, "state": "TOKEN=secret", "type": "TOKEN=secret"},
                next_action="TOKEN=secret",
            )
            assert safe is not None
            self.assertNotIn("TOKEN=secret", repr(safe.progress))
            store.close()

    def test_oversized_output_is_valid_small_json_and_keeps_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            created = store.create(workspace=directory, thinking_level="low", mode="plan")
            finished = store.finish(
                created.agent_id, status="completed", output={"result": "x" * (manager.MAX_OUTPUT_BYTES * 2)},
                conversation_id="conversation-id",
            )
            assert finished is not None
            self.assertEqual(finished.conversation_id, "conversation-id")
            self.assertEqual(finished.output["error_type"], "output_truncated")
            self.assertTrue(finished.output["result_truncated"])
            size = store._db.execute(
                "SELECT length(CAST(output_json AS BLOB)) FROM agents WHERE agent_id=?",
                (created.agent_id,),
            ).fetchone()[0]
            self.assertLessEqual(size, manager.MAX_OUTPUT_BYTES)
            store.close()

    def test_active_capacity_is_atomic_and_terminal_rows_free_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            active = [
                store.create(workspace=directory, thinking_level="low", mode="plan")
                for _ in range(manager.MAX_ACTIVE_AGENTS)
            ]
            with self.assertRaises(manager.AgentCapacityError):
                store.create(workspace=directory, thinking_level="low", mode="plan")
            store.finish(active[0].agent_id, "completed")
            replacement = store.create(
                workspace=directory, thinking_level="low", mode="plan"
            )
            self.assertEqual(replacement.status, "queued")
            store.close()

    def test_create_prunes_oldest_terminal_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            first_id = None
            for index in range(manager.MAX_HISTORY + 1):
                created = store.create(
                    workspace=directory, thinking_level="low", mode="plan",
                    agent_id=f"agent-{index}",
                )
                first_id = first_id or created.agent_id
                store.finish(created.agent_id, "completed")
            store.create(workspace=directory, thinking_level="low", mode="plan")
            self.assertIsNone(store.get(first_id))
            self.assertEqual(
                len(store.list(status="completed", limit=100)), 100
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM agents WHERE status IN ('completed','failed','interrupted')"
                ).fetchone()[0],
                manager.MAX_HISTORY,
            )
            store.close()

    def test_workspace_admission_schema_is_idempotent_and_shared_can_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agents.sqlite3"
            store = manager.AgentStore(path, owner_id="admission-owner")
            columns = {
                row[1]
                for row in store._db.execute(
                    "PRAGMA table_info(workspace_admissions)"
                )
            }
            self.assertEqual(
                columns,
                {
                    "ticket", "request_id", "owner_id", "owner_run_id",
                    "workspace", "access", "state", "enqueued_at",
                    "acquired_at", "heartbeat_at", "lease_expires_at",
                },
            )
            first = store.enqueue_workspace_admission(
                directory, "request-1", "run-1", "shared", 60
            )
            repeated = store.enqueue_workspace_admission(
                Path(directory) / ".", "request-1", "run-1", "shared", 60
            )
            second = store.enqueue_workspace_admission(
                directory, "request-2", "run-2", "shared", 60
            )
            self.assertEqual(first, repeated)
            self.assertEqual(first.workspace, manager._canonical_workspace(directory))
            self.assertEqual(first.queue_position, 1)
            self.assertEqual(second.queue_position, 2)
            self.assertEqual(
                store.try_acquire_workspace_admission(
                    first.request_id, reader_limit=2, lease_seconds=60
                ).state,
                "acquired",
            )
            self.assertEqual(
                store.try_acquire_workspace_admission(
                    second.request_id, reader_limit=2, lease_seconds=60
                ).state,
                "acquired",
            )
            store.close()

            reopened = manager.AgentStore(path, owner_id="admission-owner")
            self.assertEqual(
                reopened.inspect_workspace_admission("request-1").state,
                "acquired",
            )
            reopened.close()

    def test_workspace_admission_reader_limit_reports_current_blockers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            first = store.enqueue_workspace_admission(
                directory, "reader-1", "reader-run-1", "shared", 60
            )
            second = store.enqueue_workspace_admission(
                directory, "reader-2", "reader-run-2", "shared", 60
            )
            acquired = store.try_acquire_workspace_admission(
                first.request_id, reader_limit=1, lease_seconds=60
            )
            blocked = store.try_acquire_workspace_admission(
                second.request_id, reader_limit=1, lease_seconds=60
            )
            self.assertEqual(acquired.state, "acquired")
            self.assertEqual(blocked.state, "queued")
            self.assertEqual(blocked.queue_position, 1)
            self.assertEqual(
                blocked.blocking_owner_run_ids, ("reader-run-1",)
            )
            self.assertEqual(
                store.inspect_workspace_admission(
                    second.request_id, reader_limit=1
                ).blocking_owner_run_ids,
                ("reader-run-1",),
            )
            self.assertTrue(store.release_workspace_admission(first.request_id))
            self.assertEqual(
                store.try_acquire_workspace_admission(
                    second.request_id, reader_limit=1, lease_seconds=60
                ).state,
                "acquired",
            )
            store.close()

    def test_workspace_admission_writer_waits_and_blocks_later_readers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            reader = store.enqueue_workspace_admission(
                directory, "reader", "reader-run", "shared", 60
            )
            store.try_acquire_workspace_admission(
                reader.request_id, reader_limit=4, lease_seconds=60
            )
            writer = store.enqueue_workspace_admission(
                directory, "writer", "writer-run", "exclusive", 60
            )
            late_reader = store.enqueue_workspace_admission(
                directory, "late-reader", "late-reader-run", "shared", 60
            )

            waiting_writer = store.try_acquire_workspace_admission(
                writer.request_id, reader_limit=4, lease_seconds=60
            )
            waiting_reader = store.try_acquire_workspace_admission(
                late_reader.request_id, reader_limit=4, lease_seconds=60
            )
            self.assertEqual(waiting_writer.state, "queued")
            self.assertEqual(
                waiting_writer.blocking_owner_run_ids, ("reader-run",)
            )
            self.assertEqual(waiting_reader.state, "queued")
            self.assertEqual(waiting_reader.queue_position, 2)
            self.assertEqual(
                waiting_reader.blocking_owner_run_ids, ("writer-run",)
            )

            store.release_workspace_admission(reader.request_id)
            self.assertEqual(
                store.try_acquire_workspace_admission(
                    writer.request_id, reader_limit=4, lease_seconds=60
                ).state,
                "acquired",
            )
            still_waiting = store.try_acquire_workspace_admission(
                late_reader.request_id, reader_limit=4, lease_seconds=60
            )
            self.assertEqual(still_waiting.state, "queued")
            self.assertEqual(
                still_waiting.blocking_owner_run_ids, ("writer-run",)
            )
            store.release_workspace_admission(writer.request_id)
            self.assertEqual(
                store.try_acquire_workspace_admission(
                    late_reader.request_id, reader_limit=4, lease_seconds=60
                ).state,
                "acquired",
            )
            store.close()

    def test_workspace_admission_renew_release_cancel_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            with patch("agent_manager.time.time", return_value=100.0):
                queued = store.enqueue_workspace_admission(
                    directory, "renewed", "renewed-run", "shared", 10
                )
            self.assertEqual(queued.heartbeat_at, 100.0)
            self.assertEqual(queued.lease_expires_at, 110.0)
            with patch("agent_manager.time.time", return_value=101.0):
                acquired = store.try_acquire_workspace_admission(
                    queued.request_id, reader_limit=1, lease_seconds=10
                )
            self.assertEqual(acquired.acquired_at, 101.0)
            with patch("agent_manager.time.time", return_value=102.0):
                renewed = store.renew_workspace_admission(
                    queued.request_id, lease_seconds=20
                )
            self.assertEqual(renewed.heartbeat_at, 102.0)
            self.assertEqual(renewed.lease_expires_at, 122.0)
            self.assertTrue(store.release_workspace_admission(queued.request_id))
            self.assertFalse(store.release_workspace_admission(queued.request_id))

            cancellable = store.enqueue_workspace_admission(
                directory, "cancelled", "cancelled-run", "exclusive", 60
            )
            self.assertTrue(store.cancel_workspace_admission(cancellable.request_id))
            self.assertIsNone(
                store.inspect_workspace_admission(cancellable.request_id)
            )
            self.assertFalse(store.cancel_workspace_admission(cancellable.request_id))

            with patch("agent_manager.time.time", return_value=200.0):
                expired = store.enqueue_workspace_admission(
                    directory, "expired", "expired-run", "exclusive", 5
                )
                store.try_acquire_workspace_admission(
                    expired.request_id, reader_limit=1, lease_seconds=5
                )
            with patch("agent_manager.time.time", return_value=206.0):
                self.assertIsNone(
                    store.inspect_workspace_admission(expired.request_id)
                )
                self.assertEqual(store.reconcile_expired_admissions(), 1)
                self.assertEqual(store.reconcile_expired_admissions(), 0)
            store.close()

    def test_workspace_admission_cross_store_acquisition_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agents.sqlite3"
            first_store = manager.AgentStore(path, owner_id="shared-owner")
            second_store = manager.AgentStore(path, owner_id="shared-owner")
            first_store.enqueue_workspace_admission(
                directory, "writer-1", "writer-run-1", "exclusive", 60
            )
            second_store.enqueue_workspace_admission(
                directory, "writer-2", "writer-run-2", "exclusive", 60
            )
            barrier = threading.Barrier(2)

            def acquire(store, request_id):
                barrier.wait(timeout=2)
                return store.try_acquire_workspace_admission(
                    request_id, reader_limit=2, lease_seconds=60
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(
                    acquire, first_store, "writer-1"
                )
                second_future = executor.submit(
                    acquire, second_store, "writer-2"
                )
                first = first_future.result(timeout=3)
                second = second_future.result(timeout=3)
            self.assertEqual(first.state, "acquired")
            self.assertEqual(second.state, "queued")
            self.assertEqual(
                second.blocking_owner_run_ids, ("writer-run-1",)
            )
            first_store.release_workspace_admission("writer-1")
            self.assertEqual(
                second_store.try_acquire_workspace_admission(
                    "writer-2", reader_limit=2, lease_seconds=60
                ).state,
                "acquired",
            )
            first_store.close()
            second_store.close()

    def test_workspace_admission_cross_store_reader_limit_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agents.sqlite3"
            first_store = manager.AgentStore(path, owner_id="shared-owner")
            second_store = manager.AgentStore(path, owner_id="shared-owner")
            first_store.enqueue_workspace_admission(
                directory, "reader-a", "reader-run-a", "shared", 60
            )
            second_store.enqueue_workspace_admission(
                directory, "reader-b", "reader-run-b", "shared", 60
            )
            barrier = threading.Barrier(2)

            def acquire(store, request_id):
                barrier.wait(timeout=2)
                return store.try_acquire_workspace_admission(
                    request_id, reader_limit=1, lease_seconds=60
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(
                    acquire, first_store, "reader-a"
                )
                second_future = executor.submit(
                    acquire, second_store, "reader-b"
                )
                snapshots = (
                    first_future.result(timeout=3),
                    second_future.result(timeout=3),
                )
            self.assertEqual(
                sorted(snapshot.state for snapshot in snapshots),
                ["acquired", "queued"],
            )
            queued = next(
                snapshot for snapshot in snapshots if snapshot.state == "queued"
            )
            acquired = next(
                snapshot for snapshot in snapshots if snapshot.state == "acquired"
            )
            self.assertEqual(
                queued.blocking_owner_run_ids, (acquired.owner_run_id,)
            )
            first_store.close()
            second_store.close()

    @unittest.skipUnless(os.name != "nt", "POSIX permissions only")
    def test_rejects_non_private_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "state"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o755)
            with self.assertRaises(OSError):
                manager.AgentStore(parent / "agents.sqlite3")

if __name__ == "__main__":
    unittest.main()
