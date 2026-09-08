import asyncio
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agy_server import (
    WorkspaceLockTimeout,
    _queue_timeout_seconds,
    admitted_workspace,
)


class FakeWorkspaceLock:
    """Async fake for WorkspaceLock tracking acquisition timeout and release."""

    def __init__(self, root: Path, access: str, *, should_timeout: bool = False):
        self.root = root
        self.access = access
        self.should_timeout = should_timeout
        self.acquired_timeouts: list[float] = []
        self.released = False

    async def acquire(self, timeout: float) -> None:
        self.acquired_timeouts.append(timeout)
        if self.should_timeout:
            raise WorkspaceLockTimeout("OS lock acquisition timed out")

    def release(self) -> None:
        self.released = True


class MockAgentStore:
    """Mock store simulating durable queue admission lifecycle."""

    def __init__(self, admission_states: list[str] | None = None):
        self.states = list(admission_states) if admission_states is not None else ["acquired"]
        self.enqueued: list[dict] = []
        self.released: list[str] = []
        self.renewed: list[str] = []

    def enqueue_workspace_admission(
        self, root: Path, request_id: str, owner_run_id: str, access: str, lease_seconds: float
    ):
        self.enqueued.append(
            {
                "root": root,
                "request_id": request_id,
                "owner_run_id": owner_run_id,
                "access": access,
                "lease_seconds": lease_seconds,
            }
        )
        snap = MagicMock()
        snap.state = "waiting"
        snap.queue_position = 1
        snap.blocking_owner_run_ids = ["blocking-run-1"]
        snap.lease_expires_at = 999999.0
        return snap

    def try_acquire_workspace_admission(
        self, request_id: str, reader_limit: int, lease_seconds: float
    ):
        state = self.states.pop(0) if self.states else "waiting"
        snap = MagicMock()
        snap.state = state
        snap.queue_position = 0 if state == "acquired" else 1
        snap.blocking_owner_run_ids = [] if state == "acquired" else ["blocking-run-1"]
        snap.lease_expires_at = 999999.0
        return snap

    def release_workspace_admission(self, request_id: str) -> None:
        self.released.append(request_id)

    def renew_workspace_admission(self, request_id: str, lease_seconds: float):
        self.renewed.append(request_id)
        return MagicMock()


class TestQueueTimeoutSeconds(unittest.TestCase):
    """Test environment variable parsing, defaults, bounds, and warnings."""

    def test_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_queue_timeout_seconds(), 60)

    def test_valid_bounds(self):
        valid_inputs = {
            "1": 1,
            "300": 300,
            "60": 60,
            "120": 120,
        }
        for raw, expected in valid_inputs.items():
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"ANTIAGENT_QUEUE_TIMEOUT_SECONDS": raw}):
                    self.assertEqual(_queue_timeout_seconds(), expected)

    def test_invalid_values_return_default_and_log_warning_without_raw_leak(self):
        invalid_inputs = ["0", "301", "-1", "-50", "999", "abc", "60s", "", "   "]
        for raw in invalid_inputs:
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"ANTIAGENT_QUEUE_TIMEOUT_SECONDS": raw}):
                    with self.assertLogs(level="WARNING") as cm:
                        result = _queue_timeout_seconds()
                    self.assertEqual(result, 60)
                    output_text = " ".join(cm.output)
                    self.assertIn("Invalid ANTIAGENT_QUEUE_TIMEOUT_SECONDS", output_text)
                    self.assertEqual(cm.records[0].getMessage(),
                                     "Invalid ANTIAGENT_QUEUE_TIMEOUT_SECONDS; defaulting to 60.")


class TestAdmittedWorkspaceQueueBudget(unittest.IsolatedAsyncioTestCase):
    """Test queue budget bounds, timeout behaviour, and cleanup in admitted_workspace."""

    async def test_consumed_queue_time_is_not_reset_for_os_lock(self):
        """Verify that time spent waiting in the durable queue reduces the OS lock budget."""
        store = MockAgentStore(admission_states=["waiting", "acquired"])
        fake_lock = FakeWorkspaceLock(Path("/dummy"), "exclusive")
        run = MagicMock()
        run.run_id = "test-run-consumed-budget"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 100.0  # Far task deadline; queue budget (1s) governs

        with (
            patch.dict(os.environ, {"ANTIAGENT_QUEUE_TIMEOUT_SECONDS": "1"}),
            patch("agy_server._get_agent_store", return_value=store),
            patch("agy_server.WorkspaceLock", return_value=fake_lock),
            patch("agy_server._emit_lifecycle", new_callable=AsyncMock),
            patch("agy_server._admission_view", return_value={}),
            patch("agy_server._renew_workspace_admission", new_callable=AsyncMock),
        ):
            async with admitted_workspace(
                Path("/dummy"),
                access="exclusive",
                deadline=deadline,
                deadline_at="2026-09-08T12:00:00Z",
                run=run,
                lifecycle=None,
                owner_run_id="owner-consumed-budget",
            ) as admission:
                self.assertIsNotNone(admission)

        self.assertEqual(len(fake_lock.acquired_timeouts), 1)
        remaining_lock_timeout = fake_lock.acquired_timeouts[0]
        # ~0.1s was spent waiting in the queue, so remaining must be reduced below 0.95s
        self.assertLessEqual(remaining_lock_timeout, 0.95)
        self.assertGreater(remaining_lock_timeout, 0.0)

    async def test_blocked_admission_timeout_without_yield(self):
        store = MockAgentStore(admission_states=["waiting"])
        fake_lock = FakeWorkspaceLock(Path("/dummy"), "exclusive")
        run = MagicMock()
        run.run_id = "test-run-blocked"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 100.0  # Far task deadline

        yielded = False
        with (
            patch.dict(os.environ, {"ANTIAGENT_QUEUE_TIMEOUT_SECONDS": "1"}),
            patch("agy_server._get_agent_store", return_value=store),
            patch("agy_server.WorkspaceLock", return_value=fake_lock),
            patch("agy_server._emit_lifecycle", new_callable=AsyncMock),
            patch("agy_server._admission_view", return_value={}),
        ):
            with self.assertRaises(WorkspaceLockTimeout):
                async with admitted_workspace(
                    Path("/dummy"),
                    access="exclusive",
                    deadline=deadline,
                    deadline_at="2026-09-08T12:00:00Z",
                    run=run,
                    lifecycle=None,
                    owner_run_id="owner-blocked",
                ):
                    yielded = True

        self.assertFalse(yielded, "admitted_workspace must not yield when admission times out")
        self.assertIn(
            "test-run-blocked",
            store.released,
            "Durable admission snapshot must be released on admission timeout",
        )
        self.assertFalse(
            fake_lock.released,
            "OS lock was never acquired, so release must not be called on it",
        )

    async def test_os_lock_receives_bounded_timeout(self):
        store = MockAgentStore(admission_states=["acquired"])
        fake_lock = FakeWorkspaceLock(Path("/dummy"), "exclusive")
        run = MagicMock()
        run.run_id = "test-run-bounded"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 500.0  # Far task deadline (500s)

        with (
            patch.dict(os.environ, {"ANTIAGENT_QUEUE_TIMEOUT_SECONDS": "30"}),
            patch("agy_server._get_agent_store", return_value=store),
            patch("agy_server.WorkspaceLock", return_value=fake_lock),
            patch("agy_server._emit_lifecycle", new_callable=AsyncMock),
            patch("agy_server._admission_view", return_value={}),
            patch("agy_server._renew_workspace_admission", new_callable=AsyncMock),
        ):
            async with admitted_workspace(
                Path("/dummy"),
                access="exclusive",
                deadline=deadline,
                deadline_at="2026-09-08T12:00:00Z",
                run=run,
                lifecycle=None,
                owner_run_id="owner-bounded",
            ) as admission:
                self.assertIsNotNone(admission)

        self.assertEqual(len(fake_lock.acquired_timeouts), 1)
        acquired_timeout = fake_lock.acquired_timeouts[0]
        # Bounded by queue budget (30s), not the original 500s task deadline
        self.assertLessEqual(acquired_timeout, 30.0)
        self.assertGreater(acquired_timeout, 20.0)

    async def test_original_deadline_wins(self):
        store = MockAgentStore(admission_states=["acquired"])
        fake_lock = FakeWorkspaceLock(Path("/dummy"), "exclusive")
        run = MagicMock()
        run.run_id = "test-run-orig-deadline"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0  # Task deadline (5s) is tighter than 60s queue budget

        with (
            patch.dict(os.environ, {"ANTIAGENT_QUEUE_TIMEOUT_SECONDS": "60"}),
            patch("agy_server._get_agent_store", return_value=store),
            patch("agy_server.WorkspaceLock", return_value=fake_lock),
            patch("agy_server._emit_lifecycle", new_callable=AsyncMock),
            patch("agy_server._admission_view", return_value={}),
            patch("agy_server._renew_workspace_admission", new_callable=AsyncMock),
        ):
            async with admitted_workspace(
                Path("/dummy"),
                access="exclusive",
                deadline=deadline,
                deadline_at="2026-09-08T12:00:00Z",
                run=run,
                lifecycle=None,
                owner_run_id="owner-orig-deadline",
            ) as admission:
                self.assertIsNotNone(admission)

        self.assertEqual(len(fake_lock.acquired_timeouts), 1)
        acquired_timeout = fake_lock.acquired_timeouts[0]
        # Bounded by the original 5s deadline, not 60s
        self.assertLessEqual(acquired_timeout, 5.0)
        self.assertGreater(acquired_timeout, 0.0)

    async def test_release_on_timeout_during_lock_acquisition(self):
        store = MockAgentStore(admission_states=["acquired"])
        fake_lock = FakeWorkspaceLock(Path("/dummy"), "exclusive", should_timeout=True)
        run = MagicMock()
        run.run_id = "test-run-lock-timeout"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60.0

        yielded = False
        with (
            patch.dict(os.environ, {"ANTIAGENT_QUEUE_TIMEOUT_SECONDS": "60"}),
            patch("agy_server._get_agent_store", return_value=store),
            patch("agy_server.WorkspaceLock", return_value=fake_lock),
            patch("agy_server._emit_lifecycle", new_callable=AsyncMock),
            patch("agy_server._admission_view", return_value={}),
            patch("agy_server._renew_workspace_admission", new_callable=AsyncMock),
        ):
            with self.assertRaises(WorkspaceLockTimeout):
                async with admitted_workspace(
                    Path("/dummy"),
                    access="exclusive",
                    deadline=deadline,
                    deadline_at="2026-09-08T12:00:00Z",
                    run=run,
                    lifecycle=None,
                    owner_run_id="owner-lock-timeout",
                ):
                    yielded = True

        self.assertFalse(yielded)
        self.assertTrue(fake_lock.released, "OS lock must be released on acquisition timeout")
        self.assertIn(
            "test-run-lock-timeout",
            store.released,
            "Durable admission snapshot must be released when OS lock acquisition times out",
        )


if __name__ == "__main__":
    unittest.main()
