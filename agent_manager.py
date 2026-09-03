"""Small durable store for future Antigravity agent lifecycle tools."""

from __future__ import annotations

import getpass
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


AgentStatus = Literal["queued", "running", "completed", "failed", "interrupted"]
WorkspaceAccess = Literal["shared", "exclusive"]
WorkspaceAdmissionState = Literal["queued", "acquired"]
TERMINAL_STATUSES = ("completed", "failed", "interrupted")
MAX_OUTPUT_BYTES = 256 * 1024
MAX_HISTORY = 1000
MAX_ACTIVE_AGENTS = 32
MAX_PROGRESS_EVENTS = 16
STATE_DIR_ENV = "ANTIAGENT_STATE_DIR"
_STATE_DIRECTORY: Path | None = None
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_PROGRESS_MESSAGES = {
    "scheduled": "Agent scheduled",
    "manager_started": "Agent manager started",
    "preflight_started": "Preflight checks started",
    "waiting_for_workspace": "Waiting for workspace lock",
    "workspace_acquired": "Workspace lock acquired",
    "cli_started": "Antigravity CLI started",
    "cli_step": "Antigravity CLI reported a step",
    "heartbeat": "Agent is still running",
    "postflight_started": "Postflight review started",
    "result_validation": "Result validation started",
    "cancel_requested": "Cancellation requested",
    "completed": "Agent completed",
    "failed": "Agent failed",
    "interrupted": "Agent interrupted",
    "manager_lost": "Agent manager heartbeat was lost",
}
_PROGRESS_PHASES = {
    "queued", "preflight", "waiting_for_workspace", "executing", "postflight",
    "validating_result", "completed", "failed", "interrupted",
}
_STEP_STATES = {"PENDING", "RUNNING", "DONE", "FAILED", "CANCELLED", "UNKNOWN"}
_STEP_TYPES = {"user_input", "agent_response", "tool", "thinking", "plan", "unknown"}


@dataclass(frozen=True)
class AgentSnapshot:
    agent_id: str
    parent_agent_id: str | None
    workspace: str
    thinking_level: str
    mode: str
    status: AgentStatus
    cancel_requested: bool
    conversation_id: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: float
    output: dict[str, Any] | None
    manager_error: str | None
    progress: dict[str, Any] | None


@dataclass(frozen=True)
class WorkspaceAdmissionSnapshot:
    request_id: str
    owner_run_id: str
    workspace: str
    access: WorkspaceAccess
    state: WorkspaceAdmissionState
    queue_position: int | None
    blocking_owner_run_ids: tuple[str, ...]
    enqueued_at: float
    acquired_at: float | None
    heartbeat_at: float
    lease_expires_at: float


class AgentCapacityError(RuntimeError):
    """The owner already has the maximum number of active agents."""


def _resolve_default_state_dir() -> Path:
    configured = os.environ.get(STATE_DIR_ENV, "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError(f"{STATE_DIR_ENV} must be an absolute path")
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            raise OSError(f"{STATE_DIR_ENV} must not be a link")
        if path.exists() and not path.is_dir():
            raise OSError(f"{STATE_DIR_ENV} must be a directory")
        return path.resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return (Path(base) / "antiagent").resolve()


def default_state_dir() -> Path:
    """Return the process-wide state root resolved on first use."""
    global _STATE_DIRECTORY
    if _STATE_DIRECTORY is None:
        _STATE_DIRECTORY = _resolve_default_state_dir()
    return _STATE_DIRECTORY


def prepare_state_dir() -> Path:
    """Create and validate the shared process state root."""
    directory = default_state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or (
        hasattr(directory, "is_junction") and directory.is_junction()
    ) or not directory.is_dir():
        raise OSError("state root is not a real directory")
    if os.name != "nt":
        directory_stat = directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise OSError("state root is not a directory")
        if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
            raise OSError("state root is not private")
    return directory


def default_db_path() -> Path:
    return prepare_state_dir() / "agents.sqlite3"


def _canonical_workspace(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(value)))


def _now() -> tuple[str, float]:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        time.time(),
    )


def _prepare_path(path: Path) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or (
        hasattr(parent, "is_junction") and parent.is_junction()
    ) or not parent.is_dir():
        raise OSError("database parent is not a real directory")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ) or not path.is_file():
            raise OSError("database file is not a regular file")
    if os.name != "nt":
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("database parent is not a private directory")
        if info.st_mode & 0o077:
            raise OSError("database parent directory is not private")
        if path.exists():
            os.chmod(path, 0o600)
        else:
            os.chmod(parent, 0o700)
    return path


class AgentStore:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        owner_id: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.path = _prepare_path(Path(path) if path is not None else default_db_path())
        self.owner_id = owner_id or getpass.getuser()
        if not self.owner_id:
            raise ValueError("owner_id must be non-empty")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                parent_agent_id TEXT,
                owner_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                thinking_level TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','interrupted')),
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                conversation_id TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at REAL NOT NULL,
                output_json TEXT,
                manager_error TEXT,
                progress_json TEXT
            )"""
        )
        columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(agents)")
        }
        if "progress_json" not in columns:
            self._db.execute("ALTER TABLE agents ADD COLUMN progress_json TEXT")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS workspace_admissions (
                ticket INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                owner_run_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                access TEXT NOT NULL CHECK(access IN ('shared','exclusive')),
                state TEXT NOT NULL CHECK(state IN ('queued','acquired')),
                enqueued_at REAL NOT NULL,
                acquired_at REAL,
                heartbeat_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL
            )"""
        )
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS workspace_admissions_queue
               ON workspace_admissions(workspace,state,ticket)"""
        )
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "AgentStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _snapshot(self, row: sqlite3.Row | None) -> AgentSnapshot | None:
        if row is None:
            return None
        output: dict[str, Any] | None = None
        if row["output_json"] is not None:
            try:
                parsed = json.loads(row["output_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                output = parsed
        progress: dict[str, Any] | None = None
        if row["progress_json"] is not None:
            try:
                parsed_progress = json.loads(row["progress_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_progress = None
            if isinstance(parsed_progress, dict):
                progress = parsed_progress
        return AgentSnapshot(
            agent_id=row["agent_id"],
            parent_agent_id=row["parent_agent_id"],
            workspace=row["workspace"],
            thinking_level=row["thinking_level"],
            mode=row["mode"],
            status=row["status"],
            cancel_requested=bool(row["cancel_requested"]),
            conversation_id=row["conversation_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=float(row["updated_at"]),
            output=output,
            manager_error=row["manager_error"],
            progress=progress,
        )

    def _fetch(self, agent_id: str) -> AgentSnapshot | None:
        row = self._db.execute(
            "SELECT * FROM agents WHERE agent_id=? AND owner_id=?",
            (agent_id, self.owner_id),
        ).fetchone()
        return self._snapshot(row)

    def _begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _validate_opaque_id(value: str, field: str) -> str:
        if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
            raise ValueError(f"{field} must be an opaque 1..128 character ID")
        return value

    @staticmethod
    def _validate_lease_seconds(value: float) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("lease_seconds must be a positive finite number")
        return float(value)

    @staticmethod
    def _validate_reader_limit(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("reader_limit must be a positive integer")
        return value

    def _admission_row(
        self, request_id: str, *, now: float
    ) -> sqlite3.Row | None:
        return self._db.execute(
            """SELECT * FROM workspace_admissions
               WHERE request_id=? AND owner_id=? AND lease_expires_at>?""",
            (request_id, self.owner_id, now),
        ).fetchone()

    def _admission_snapshot(
        self,
        row: sqlite3.Row | None,
        *,
        now: float,
        reader_limit: int | None = None,
    ) -> WorkspaceAdmissionSnapshot | None:
        if row is None:
            return None
        queue_position: int | None = None
        blockers: list[str] = []
        if row["state"] == "queued":
            queue_position = int(self._db.execute(
                """SELECT COUNT(*) FROM workspace_admissions
                   WHERE workspace=? AND state='queued' AND ticket<=?
                     AND lease_expires_at>?""",
                (row["workspace"], row["ticket"], now),
            ).fetchone()[0])
            if row["access"] == "exclusive":
                blocking_rows = self._db.execute(
                    """SELECT ticket,owner_run_id FROM workspace_admissions
                       WHERE workspace=? AND request_id<>? AND lease_expires_at>?
                         AND (state='acquired' OR (state='queued' AND ticket<?))
                       ORDER BY ticket""",
                    (row["workspace"], row["request_id"], now, row["ticket"]),
                ).fetchall()
                blockers.extend(item["owner_run_id"] for item in blocking_rows)
            else:
                blocking_rows = self._db.execute(
                    """SELECT ticket,owner_run_id FROM workspace_admissions
                       WHERE workspace=? AND request_id<>? AND lease_expires_at>?
                         AND ((state='acquired' AND access='exclusive')
                           OR (state='queued' AND access='exclusive' AND ticket<?))
                       ORDER BY ticket""",
                    (row["workspace"], row["request_id"], now, row["ticket"]),
                ).fetchall()
                blockers.extend(item["owner_run_id"] for item in blocking_rows)
                if reader_limit is not None:
                    shared_rows = self._db.execute(
                        """SELECT ticket,owner_run_id FROM workspace_admissions
                           WHERE workspace=? AND state='acquired' AND access='shared'
                             AND request_id<>? AND lease_expires_at>?
                           ORDER BY ticket""",
                        (row["workspace"], row["request_id"], now),
                    ).fetchall()
                    if len(shared_rows) >= reader_limit:
                        blockers.extend(item["owner_run_id"] for item in shared_rows)
        return WorkspaceAdmissionSnapshot(
            request_id=row["request_id"],
            owner_run_id=row["owner_run_id"],
            workspace=row["workspace"],
            access=row["access"],
            state=row["state"],
            queue_position=queue_position,
            blocking_owner_run_ids=tuple(dict.fromkeys(blockers)),
            enqueued_at=float(row["enqueued_at"]),
            acquired_at=(
                float(row["acquired_at"])
                if row["acquired_at"] is not None
                else None
            ),
            heartbeat_at=float(row["heartbeat_at"]),
            lease_expires_at=float(row["lease_expires_at"]),
        )

    def enqueue_workspace_admission(
        self,
        workspace: str | os.PathLike[str],
        request_id: str,
        owner_run_id: str,
        access: WorkspaceAccess,
        lease_seconds: float,
    ) -> WorkspaceAdmissionSnapshot:
        request_id = self._validate_opaque_id(request_id, "request_id")
        owner_run_id = self._validate_opaque_id(owner_run_id, "owner_run_id")
        if access not in ("shared", "exclusive"):
            raise ValueError("access must be shared or exclusive")
        lease_seconds = self._validate_lease_seconds(lease_seconds)
        workspace = _canonical_workspace(workspace)
        now = time.time()
        with self._lock:
            try:
                self._begin()
                self._db.execute(
                    "DELETE FROM workspace_admissions WHERE lease_expires_at<=?",
                    (now,),
                )
                existing = self._db.execute(
                    "SELECT * FROM workspace_admissions WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["owner_id"] != self.owner_id
                        or existing["owner_run_id"] != owner_run_id
                        or existing["workspace"] != workspace
                        or existing["access"] != access
                    ):
                        raise ValueError("request_id is already in use")
                else:
                    self._db.execute(
                        """INSERT INTO workspace_admissions (
                            request_id,owner_id,owner_run_id,workspace,access,state,
                            enqueued_at,heartbeat_at,lease_expires_at
                        ) VALUES (?,?,?,?,?,'queued',?,?,?)""",
                        (
                            request_id, self.owner_id, owner_run_id, workspace,
                            access, now, now, now + lease_seconds,
                        ),
                    )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            row = self._admission_row(request_id, now=now)
            snapshot = self._admission_snapshot(row, now=now)
            assert snapshot is not None
            return snapshot

    def inspect_workspace_admission(
        self, request_id: str, *, reader_limit: int | None = None
    ) -> WorkspaceAdmissionSnapshot | None:
        request_id = self._validate_opaque_id(request_id, "request_id")
        if reader_limit is not None:
            reader_limit = self._validate_reader_limit(reader_limit)
        now = time.time()
        with self._lock:
            row = self._admission_row(request_id, now=now)
            return self._admission_snapshot(
                row, now=now, reader_limit=reader_limit
            )

    def try_acquire_workspace_admission(
        self,
        request_id: str,
        *,
        reader_limit: int,
        lease_seconds: float,
    ) -> WorkspaceAdmissionSnapshot | None:
        request_id = self._validate_opaque_id(request_id, "request_id")
        reader_limit = self._validate_reader_limit(reader_limit)
        lease_seconds = self._validate_lease_seconds(lease_seconds)
        now = time.time()
        with self._lock:
            try:
                self._begin()
                self._db.execute(
                    "DELETE FROM workspace_admissions WHERE lease_expires_at<=?",
                    (now,),
                )
                row = self._db.execute(
                    """SELECT * FROM workspace_admissions
                       WHERE request_id=? AND owner_id=?""",
                    (request_id, self.owner_id),
                ).fetchone()
                if row is not None and row["state"] == "queued":
                    if row["access"] == "shared":
                        exclusive_blockers = int(self._db.execute(
                            """SELECT COUNT(*) FROM workspace_admissions
                               WHERE workspace=? AND lease_expires_at>?
                                 AND ((state='acquired' AND access='exclusive')
                                   OR (state='queued' AND access='exclusive'
                                       AND ticket<?))""",
                            (row["workspace"], now, row["ticket"]),
                        ).fetchone()[0])
                        active_readers = int(self._db.execute(
                            """SELECT COUNT(*) FROM workspace_admissions
                               WHERE workspace=? AND state='acquired'
                                 AND access='shared' AND lease_expires_at>?""",
                            (row["workspace"], now),
                        ).fetchone()[0])
                        eligible = (
                            exclusive_blockers == 0
                            and active_readers < reader_limit
                        )
                    else:
                        blockers = int(self._db.execute(
                            """SELECT COUNT(*) FROM workspace_admissions
                               WHERE workspace=? AND request_id<>?
                                 AND lease_expires_at>?
                                 AND (state='acquired'
                                   OR (state='queued' AND ticket<?))""",
                            (
                                row["workspace"], row["request_id"], now,
                                row["ticket"],
                            ),
                        ).fetchone()[0])
                        eligible = blockers == 0
                    if eligible:
                        self._db.execute(
                            """UPDATE workspace_admissions
                               SET state='acquired',acquired_at=?,heartbeat_at=?,
                                   lease_expires_at=?
                               WHERE request_id=? AND owner_id=? AND state='queued'""",
                            (
                                now, now, now + lease_seconds, request_id,
                                self.owner_id,
                            ),
                        )
                    else:
                        self._db.execute(
                            """UPDATE workspace_admissions
                               SET heartbeat_at=?,lease_expires_at=?
                               WHERE request_id=? AND owner_id=? AND state='queued'""",
                            (now, now + lease_seconds, request_id, self.owner_id),
                        )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            row = self._admission_row(request_id, now=now)
            return self._admission_snapshot(
                row, now=now, reader_limit=reader_limit
            )

    def renew_workspace_admission(
        self, request_id: str, *, lease_seconds: float
    ) -> WorkspaceAdmissionSnapshot | None:
        request_id = self._validate_opaque_id(request_id, "request_id")
        lease_seconds = self._validate_lease_seconds(lease_seconds)
        now = time.time()
        with self._lock:
            try:
                self._begin()
                self._db.execute(
                    "DELETE FROM workspace_admissions WHERE lease_expires_at<=?",
                    (now,),
                )
                self._db.execute(
                    """UPDATE workspace_admissions
                       SET heartbeat_at=?,lease_expires_at=?
                       WHERE request_id=? AND owner_id=?""",
                    (now, now + lease_seconds, request_id, self.owner_id),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            row = self._admission_row(request_id, now=now)
            return self._admission_snapshot(row, now=now)

    def _remove_workspace_admission(self, request_id: str) -> bool:
        request_id = self._validate_opaque_id(request_id, "request_id")
        with self._lock:
            try:
                self._begin()
                cursor = self._db.execute(
                    """DELETE FROM workspace_admissions
                       WHERE request_id=? AND owner_id=?""",
                    (request_id, self.owner_id),
                )
                self._db.commit()
                return cursor.rowcount > 0
            except Exception:
                self._db.rollback()
                raise

    def release_workspace_admission(self, request_id: str) -> bool:
        return self._remove_workspace_admission(request_id)

    def cancel_workspace_admission(self, request_id: str) -> bool:
        return self._remove_workspace_admission(request_id)

    def reconcile_expired_admissions(self) -> int:
        now = time.time()
        with self._lock:
            candidate = self._db.execute(
                """SELECT 1 FROM workspace_admissions
                   WHERE lease_expires_at<=? LIMIT 1""",
                (now,),
            ).fetchone()
            if candidate is None:
                return 0
            try:
                self._begin()
                cursor = self._db.execute(
                    "DELETE FROM workspace_admissions WHERE lease_expires_at<=?",
                    (now,),
                )
                self._db.commit()
                return cursor.rowcount
            except Exception:
                self._db.rollback()
                raise

    def create(
        self,
        workspace: str | os.PathLike[str],
        thinking_level: str,
        mode: str,
        parent_agent_id: str | None = None,
        conversation_id: str | None = None,
        agent_id: str | None = None,
    ) -> AgentSnapshot:
        agent_id = agent_id or uuid.uuid4().hex
        created_at, updated_at = _now()
        workspace = _canonical_workspace(workspace)
        progress_json = self._initial_progress(created_at)
        with self._lock:
            try:
                self._begin()
                active = self._db.execute(
                    """SELECT COUNT(*) FROM agents
                       WHERE owner_id=? AND status IN ('queued','running')""",
                    (self.owner_id,),
                ).fetchone()[0]
                if active >= MAX_ACTIVE_AGENTS:
                    raise AgentCapacityError("active agent limit reached")
                self._db.execute(
                    """INSERT INTO agents (
                        agent_id,parent_agent_id,owner_id,workspace,thinking_level,mode,
                        status,cancel_requested,conversation_id,created_at,updated_at,
                        progress_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        agent_id, parent_agent_id, self.owner_id, workspace,
                        thinking_level, mode, "queued", 0, conversation_id,
                        created_at, updated_at, progress_json,
                    ),
                )
                # ponytail: retain 1000 terminal rows; add archival storage only when history needs exceed this cap.
                self._db.execute(
                    """DELETE FROM agents
                       WHERE owner_id=? AND status IN ('completed','failed','interrupted')
                         AND agent_id NOT IN (
                           SELECT agent_id FROM agents WHERE owner_id=?
                             AND status IN ('completed','failed','interrupted')
                           ORDER BY updated_at DESC LIMIT ?
                         )""",
                    (self.owner_id, self.owner_id, MAX_HISTORY),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            snapshot = self._fetch(agent_id)
            assert snapshot is not None
            return snapshot

    def get(self, agent_id: str) -> AgentSnapshot | None:
        with self._lock:
            return self._fetch(agent_id)

    def list(
        self,
        status: AgentStatus | None = None,
        workspace: str | os.PathLike[str] | None = None,
        limit: int = 100,
    ) -> list[AgentSnapshot]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if status is not None and status not in (
            "queued", "running", "completed", "failed", "interrupted"
        ):
            raise ValueError("invalid status")
        clauses = ["owner_id=?"]
        params: list[object] = [self.owner_id]
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if workspace is not None:
            clauses.append("workspace=?")
            params.append(_canonical_workspace(workspace))
        params.append(limit)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM agents WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [snapshot for row in rows if (snapshot := self._snapshot(row)) is not None]

    def mark_running(self, agent_id: str) -> AgentSnapshot | None:
        started_at, updated_at = _now()
        with self._lock:
            try:
                self._begin()
                row = self._db.execute(
                    "SELECT * FROM agents WHERE agent_id=? AND owner_id=?",
                    (agent_id, self.owner_id),
                ).fetchone()
                progress_json = self._updated_progress_json(
                    row, phase="preflight", progress_percent=10,
                    code="manager_started", at=started_at,
                    next_action="preflight_started",
                )
                self._db.execute(
                    """UPDATE agents SET status='running',started_at=?,updated_at=?,
                       progress_json=? WHERE agent_id=? AND owner_id=?
                       AND status='queued' AND cancel_requested=0""",
                    (started_at, updated_at, progress_json, agent_id, self.owner_id),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return self._fetch(agent_id)

    @staticmethod
    def _initial_progress(at: str) -> str:
        return json.dumps({
            "version": 1,
            "phase": "queued",
            "progress_percent": 5,
            "progress_basis": "wrapper_phase",
            "indeterminate": True,
            "last_event_at": at,
            "heartbeat_at": None,
            "deadline_at": None,
            "recent_events": [{
                "sequence": 1, "timestamp": at, "code": "scheduled",
                "message": _PROGRESS_MESSAGES["scheduled"],
                "progress_percent": 5,
            }],
            "blocker": None,
            "next_action": "preflight_started",
            "step": None,
            "workspace_admission": None,
        }, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_progress(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None or row["progress_json"] is None:
            return {}
        try:
            value = json.loads(row["progress_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _updated_progress_json(
        cls,
        row: sqlite3.Row | None,
        *,
        phase: str,
        progress_percent: int,
        code: str,
        at: str,
        blocker_code: str | None = None,
        next_action: str | None = None,
        deadline_at: str | None = None,
        step: dict[str, Any] | None = None,
        workspace_admission: dict[str, Any] | None = None,
        heartbeat: bool = False,
    ) -> str:
        if code not in _PROGRESS_MESSAGES:
            raise ValueError("invalid progress event code")
        if phase not in _PROGRESS_PHASES:
            raise ValueError("invalid progress phase")
        if next_action is not None and next_action not in _PROGRESS_MESSAGES:
            next_action = None
        safe_step = None
        if isinstance(step, dict):
            index = step.get("index")
            state = step.get("state")
            step_type = step.get("type")
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                safe_step = {
                    "index": index,
                    "state": state if state in _STEP_STATES else "UNKNOWN",
                    "type": step_type if step_type in _STEP_TYPES else "unknown",
                }
        safe_admission = current_admission = cls._decode_progress(row).get(
            "workspace_admission"
        )
        if not isinstance(current_admission, dict):
            safe_admission = None
        if isinstance(workspace_admission, dict):
            access = workspace_admission.get("access")
            state = workspace_admission.get("state")
            position = workspace_admission.get("queue_position")
            blockers = workspace_admission.get("blocking_owner_run_ids")
            timestamps = {
                key: workspace_admission.get(key)
                for key in (
                    "enqueued_at", "acquired_at", "heartbeat_at",
                    "lease_expires_at",
                )
            }
            if (
                access in ("shared", "exclusive")
                and state in ("queued", "acquired")
                and (position is None or (
                    isinstance(position, int) and not isinstance(position, bool)
                    and position >= 1
                ))
                and isinstance(blockers, list)
                and len(blockers) <= MAX_ACTIVE_AGENTS
                and all(
                    isinstance(item, str) and _OPAQUE_ID.fullmatch(item)
                    for item in blockers
                )
                and all(
                    value is None or isinstance(value, str)
                    for value in timestamps.values()
                )
            ):
                safe_admission = {
                    "access": access,
                    "state": state,
                    "queue_position": position,
                    "blocking_owner_run_ids": blockers,
                    **timestamps,
                }
        current = cls._decode_progress(row)
        old_percent = current.get("progress_percent", 0)
        if not isinstance(old_percent, int) or isinstance(old_percent, bool):
            old_percent = 0
        percent = max(old_percent, min(100, max(0, int(progress_percent))))
        events = current.get("recent_events")
        events = list(events) if isinstance(events, list) else []
        event = {
            "sequence": (
                max((item.get("sequence", 0) for item in events if isinstance(item, dict)), default=0) + 1
            ),
            "timestamp": at,
            "code": code,
            "message": _PROGRESS_MESSAGES[code],
            "progress_percent": percent,
        }
        if heartbeat and events and isinstance(events[-1], dict) and events[-1].get("code") == "heartbeat":
            event["sequence"] = events[-1].get("sequence", event["sequence"])
            events[-1] = event
        else:
            events.append(event)
        events = events[-MAX_PROGRESS_EVENTS:]
        blocker = None
        if blocker_code:
            if blocker_code not in _PROGRESS_MESSAGES:
                raise ValueError("invalid blocker code")
            blocker = {
                "code": blocker_code,
                "message": _PROGRESS_MESSAGES[blocker_code],
                "retryable": blocker_code == "waiting_for_workspace",
            }
        value = {
            "version": 1,
            "phase": phase,
            "progress_percent": percent,
            "progress_basis": "wrapper_phase",
            "indeterminate": phase not in ("completed", "failed", "interrupted"),
            "last_event_at": at,
            "heartbeat_at": at if heartbeat else current.get("heartbeat_at"),
            "deadline_at": deadline_at if deadline_at is not None else current.get("deadline_at"),
            "recent_events": events,
            "blocker": blocker,
            "next_action": next_action,
            "step": safe_step,
            "workspace_admission": safe_admission,
        }
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def record_progress(
        self,
        agent_id: str,
        *,
        phase: str,
        progress_percent: int,
        code: str,
        blocker_code: str | None = None,
        next_action: str | None = None,
        deadline_at: str | None = None,
        step: dict[str, Any] | None = None,
        workspace_admission: dict[str, Any] | None = None,
        heartbeat: bool = False,
    ) -> AgentSnapshot | None:
        at, updated_at = _now()
        with self._lock:
            try:
                self._begin()
                row = self._db.execute(
                    "SELECT * FROM agents WHERE agent_id=? AND owner_id=?",
                    (agent_id, self.owner_id),
                ).fetchone()
                if row is not None and row["status"] not in TERMINAL_STATUSES:
                    progress_json = self._updated_progress_json(
                        row, phase=phase, progress_percent=progress_percent,
                        code=code, at=at, blocker_code=blocker_code,
                        next_action=next_action, deadline_at=deadline_at,
                        step=step, workspace_admission=workspace_admission,
                        heartbeat=heartbeat,
                    )
                    self._db.execute(
                        "UPDATE agents SET progress_json=?,updated_at=? WHERE agent_id=? AND owner_id=?",
                        (progress_json, updated_at, agent_id, self.owner_id),
                    )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return self._fetch(agent_id)

    @staticmethod
    def _encode_output(output: dict[str, Any] | None) -> str | None:
        if output is None:
            return None
        encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= MAX_OUTPUT_BYTES:
            return encoded.decode("utf-8")
        return json.dumps({
            "status": "ERROR",
            "error_type": "output_truncated",
            "result": "Agent output exceeded the storage limit",
            "result_truncated": True,
        }, separators=(",", ":"))

    def finish(
        self,
        agent_id: str,
        status: AgentStatus,
        output: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        manager_error: str | None = None,
    ) -> AgentSnapshot | None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("finish status must be terminal")
        finished_at, updated_at = _now()
        output_json = self._encode_output(output)
        with self._lock:
            try:
                self._begin()
                row = self._db.execute(
                    "SELECT * FROM agents WHERE agent_id=? AND owner_id=?",
                    (agent_id, self.owner_id),
                ).fetchone()
                if (
                    row is not None
                    and bool(row["cancel_requested"])
                    and status != "interrupted"
                ):
                    status = "interrupted"
                    output_json = None
                    conversation_id = None
                    manager_error = "cancelled"
                progress_json = self._updated_progress_json(
                    row, phase=status, progress_percent=100, code=status,
                    at=finished_at, next_action=None,
                )
                self._db.execute(
                    """UPDATE agents SET status=?,finished_at=?,updated_at=?,output_json=?,
                       conversation_id=COALESCE(?,conversation_id),manager_error=?,
                       progress_json=?
                       WHERE agent_id=? AND owner_id=? AND status NOT IN ('completed','failed','interrupted')""",
                    (
                        status, finished_at, updated_at, output_json, conversation_id,
                        manager_error, progress_json, agent_id, self.owner_id,
                    ),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return self._fetch(agent_id)

    def request_cancel(self, agent_id: str) -> AgentSnapshot | None:
        at, updated_at = _now()
        with self._lock:
            try:
                self._begin()
                row = self._db.execute(
                    "SELECT * FROM agents WHERE agent_id=? AND owner_id=?",
                    (agent_id, self.owner_id),
                ).fetchone()
                if row is None or row["status"] in TERMINAL_STATUSES:
                    self._db.commit()
                    return self._fetch(agent_id)
                progress_json = self._updated_progress_json(
                    row,
                    phase="queued" if row["status"] == "queued" else "executing",
                    progress_percent=0, code="cancel_requested", at=at,
                    next_action="interrupted",
                )
                self._db.execute(
                    """UPDATE agents SET cancel_requested=1,updated_at=?,progress_json=?
                       WHERE agent_id=? AND owner_id=? AND status NOT IN ('completed','failed','interrupted')""",
                    (updated_at, progress_json, agent_id, self.owner_id),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return self._fetch(agent_id)

    def cancel_requested(self, agent_id: str) -> bool:
        snapshot = self.get(agent_id)
        return bool(snapshot and snapshot.cancel_requested)

    def reconcile_stale(self, max_age_seconds: float) -> int:
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        cutoff = time.time() - max_age_seconds
        finished_at, _ = _now()
        with self._lock:
            candidate = self._db.execute(
                """SELECT 1 FROM agents WHERE owner_id=?
                   AND status IN ('queued','running') AND updated_at<? LIMIT 1""",
                (self.owner_id, cutoff),
            ).fetchone()
            if candidate is None:
                return 0
            try:
                self._begin()
                rows = self._db.execute(
                    """SELECT * FROM agents WHERE owner_id=?
                       AND status IN ('queued','running') AND updated_at<?""",
                    (self.owner_id, cutoff),
                ).fetchall()
                updated_at = time.time()
                for row in rows:
                    progress_json = self._updated_progress_json(
                        row, phase="failed", progress_percent=100,
                        code="manager_lost", at=finished_at,
                    )
                    self._db.execute(
                        """UPDATE agents SET status='failed',finished_at=?,updated_at=?,
                           manager_error='manager_lost',progress_json=?
                           WHERE agent_id=? AND owner_id=?""",
                        (finished_at, updated_at, progress_json, row["agent_id"], self.owner_id),
                    )
                self._db.commit()
                return len(rows)
            except Exception:
                self._db.rollback()
                raise


__all__ = [
    "AgentCapacityError", "AgentSnapshot", "AgentStore",
    "WorkspaceAdmissionSnapshot", "default_db_path", "default_state_dir",
    "prepare_state_dir",
]
