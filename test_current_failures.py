from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_manager
import agy_server as server
import runtime_identity


class CurrentFailureRegressionTest(unittest.TestCase):
    def tearDown(self):
        runtime_identity._reset_process_runtime_guard_for_tests()

    def test_32_plan_runs_share_workspace_without_lock_timeout(self):
        async def scenario(store):
            active = 0
            peak = 0
            all_started = asyncio.Event()
            progress: list[list[server.LifecycleUpdate]] = [[] for _ in range(32)]

            async def run(argv, _cwd, _timeout):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if active == 32:
                    all_started.set()
                try:
                    await asyncio.wait_for(all_started.wait(), 2)
                    return server.CliRunResult(
                        0,
                        json.dumps({
                            "status": "SUCCESS",
                            "response": f"ok:{argv[2]}",
                            "usage": {"output_tokens": 1},
                        }),
                        False,
                    )
                finally:
                    active -= 1

            async def one(index):
                async def lifecycle(update):
                    progress[index].append(update)

                return await server.execute_with_antigravity_cli(
                    workspace=Path.cwd(),
                    prompt=f"task-{index}",
                    thinking_level="low",
                    mode="plan",
                    lifecycle=lifecycle,
                    owner_run_id=f"agent-{index}",
                )

            with patch.object(server, "_AGENT_STORE", store), patch(
                "agy_server._resolve_cli", return_value=Path("C:/fake/agy.exe")
            ), patch(
                "agy_server._probe_cli_version", return_value="1.1.25"
            ), patch("agy_server._run_cli", new=run):
                results = await asyncio.gather(*(one(index) for index in range(32)))
            return peak, progress, results

        with tempfile.TemporaryDirectory(prefix="antiagent-32-plan-") as directory:
            state = Path(directory)
            store = agent_manager.AgentStore(
                state / "agents.sqlite3", owner_id="current-failure-test"
            )
            try:
                with patch.object(server, "_LOCK_DIRECTORY", state / "locks"):
                    peak, progress, results = asyncio.run(scenario(store))
            finally:
                store.close()

        self.assertEqual(peak, 32)
        self.assertTrue(all(item["status"] == "SUCCESS" for item in results))
        self.assertNotIn(
            "workspace_lock_timeout", {item["error_type"] for item in results}
        )
        for updates in progress:
            acquired = [
                update for update in updates if update.code == "workspace_acquired"
            ]
            self.assertEqual(len(acquired), 1)
            self.assertEqual(acquired[0].workspace_admission["access"], "shared")
            self.assertEqual(acquired[0].workspace_admission["state"], "acquired")

    def test_cancelled_waiter_releases_durable_admission(self):
        async def scenario(store):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3
            holder_run = server.RunInfo()
            waiter_run = server.RunInfo()
            holder_ready = asyncio.Event()

            async def holder():
                async with server.admitted_workspace(
                    Path.cwd(), access="exclusive", deadline=deadline,
                    deadline_at=server._timestamp(), run=holder_run,
                    lifecycle=None, owner_run_id="holder",
                ):
                    holder_ready.set()
                    await asyncio.Event().wait()

            async def waiter():
                await holder_ready.wait()
                async with server.admitted_workspace(
                    Path.cwd(), access="shared", deadline=deadline,
                    deadline_at=server._timestamp(), run=waiter_run,
                    lifecycle=None, owner_run_id="waiter",
                ):
                    self.fail("waiter acquired while exclusive holder was active")

            holder_task = asyncio.create_task(holder())
            waiter_task = asyncio.create_task(waiter())
            await holder_ready.wait()
            for _ in range(50):
                if store.inspect_workspace_admission(waiter_run.run_id) is not None:
                    break
                await asyncio.sleep(0.01)
            waiter_task.cancel()
            await asyncio.gather(waiter_task, return_exceptions=True)
            released = store.inspect_workspace_admission(waiter_run.run_id)
            holder_task.cancel()
            await asyncio.gather(holder_task, return_exceptions=True)
            return released, store.inspect_workspace_admission(holder_run.run_id)

        with tempfile.TemporaryDirectory(prefix="antiagent-cancel-admission-") as directory:
            state = Path(directory)
            store = agent_manager.AgentStore(
                state / "agents.sqlite3", owner_id="current-failure-test"
            )
            try:
                with patch.object(server, "_AGENT_STORE", store), patch.object(
                    server, "_LOCK_DIRECTORY", state / "locks"
                ):
                    waiter, holder = asyncio.run(scenario(store))
            finally:
                store.close()
        self.assertIsNone(waiter)
        self.assertIsNone(holder)

    def test_expired_admission_lease_is_reclaimed_before_writer_acquires(self):
        with tempfile.TemporaryDirectory(prefix="antiagent-stale-admission-") as directory:
            store = agent_manager.AgentStore(
                Path(directory) / "agents.sqlite3", owner_id="current-failure-test"
            )
            try:
                workspace = Path(directory) / "workspace"
                workspace.mkdir()
                stale = store.enqueue_workspace_admission(
                    workspace, "stale-request", "stale-owner", "shared", 0.05
                )
                self.assertEqual(stale.state, "queued")
                stale = store.try_acquire_workspace_admission(
                    stale.request_id, reader_limit=32, lease_seconds=0.05
                )
                self.assertEqual(stale.state, "acquired")
                writer = store.enqueue_workspace_admission(
                    workspace, "writer-request", "writer-owner", "exclusive", 1.0
                )
                self.assertEqual(writer.queue_position, 1)
                time.sleep(0.08)
                acquired = store.try_acquire_workspace_admission(
                    "writer-request", reader_limit=32, lease_seconds=1.0
                )
                self.assertIsNotNone(acquired)
                self.assertEqual(acquired.state, "acquired")
                self.assertIsNone(store.inspect_workspace_admission("stale-request"))
            finally:
                store.close()

    def test_queued_writer_blocks_later_shared_reader_until_writer_releases(self):
        with tempfile.TemporaryDirectory(prefix="antiagent-writer-fairness-") as directory:
            store = agent_manager.AgentStore(
                Path(directory) / "agents.sqlite3", owner_id="current-failure-test"
            )
            try:
                workspace = Path(directory) / "workspace"
                workspace.mkdir()
                first_reader = store.enqueue_workspace_admission(
                    workspace, "reader-one", "reader-one-owner", "shared", 5.0
                )
                first_reader = store.try_acquire_workspace_admission(
                    first_reader.request_id, reader_limit=32, lease_seconds=5.0
                )
                self.assertEqual(first_reader.state, "acquired")
                writer = store.enqueue_workspace_admission(
                    workspace, "writer", "writer-owner", "exclusive", 5.0
                )
                later_reader = store.enqueue_workspace_admission(
                    workspace, "reader-two", "reader-two-owner", "shared", 5.0
                )
                blocked = store.try_acquire_workspace_admission(
                    later_reader.request_id, reader_limit=32, lease_seconds=5.0
                )
                self.assertEqual(blocked.state, "queued")
                self.assertIn("writer-owner", blocked.blocking_owner_run_ids)

                store.release_workspace_admission(first_reader.request_id)
                writer_acquired = store.try_acquire_workspace_admission(
                    writer.request_id, reader_limit=32, lease_seconds=5.0
                )
                self.assertEqual(writer_acquired.state, "acquired")
                still_blocked = store.try_acquire_workspace_admission(
                    later_reader.request_id, reader_limit=32, lease_seconds=5.0
                )
                self.assertEqual(still_blocked.state, "queued")

                store.release_workspace_admission(writer.request_id)
                reader_acquired = store.try_acquire_workspace_admission(
                    later_reader.request_id, reader_limit=32, lease_seconds=5.0
                )
                self.assertEqual(reader_acquired.state, "acquired")
            finally:
                store.close()

    def test_100_marker_results_and_current_error_contract_are_specific_and_safe(self):
        marker = "PRIVATE_MARKER_MUST_NOT_LEAK"
        for index in range(100):
            result = server._success_result(
                {
                    "status": "SUCCESS",
                    "response": f"result-{index} {marker}",
                    "usage": {"output_tokens": 2},
                },
                "low",
                "plan",
                run_info=server.RunInfo(),
                cli_version="1.1.25",
                exit_code=0,
                expected_marker=marker,
            )
            self.assertEqual(result["status"], "SUCCESS")
            self.assertIsNone(result["error_type"])

        missing = server._success_result(
            {
                "status": "SUCCESS",
                "response": "",
                "usage": {"output_tokens": 99},
            },
            "low",
            "plan",
            run_info=server.RunInfo(),
            cli_version="1.1.25",
            exit_code=0,
        )
        mismatch = server._success_result(
            {"status": "SUCCESS", "response": "safe review body"},
            "low",
            "plan",
            run_info=server.RunInfo(),
            cli_version="1.1.25",
            exit_code=0,
            expected_marker=marker,
        )
        self.assertEqual(missing["error_type"], "final_block_missing")
        self.assertTrue(missing["retryable"])
        self.assertEqual(mismatch["error_type"], "verification_failed")
        self.assertEqual(
            mismatch["verification"]["failure_kind"], "marker_mismatch"
        )
        self.assertFalse(mismatch["verification"]["expected_marker_found"])
        self.assertNotIn(marker, json.dumps(mismatch))


if __name__ == "__main__":
    unittest.main()
