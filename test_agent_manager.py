from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import agent_manager as manager


class AgentStoreTest(unittest.TestCase):
    def _store(self, directory: str, *, owner: str = "test-owner") -> manager.AgentStore:
        return manager.AgentStore(Path(directory) / "state" / "agents.sqlite3", owner_id=owner)

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
            self.assertEqual(store.get(queued.agent_id).status, "failed")
            self.assertEqual(store.reconcile_stale(1), 0)
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
