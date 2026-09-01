"""Small durable store for future Antigravity agent lifecycle tools."""

from __future__ import annotations

import getpass
import json
import os
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
TERMINAL_STATUSES = ("completed", "failed", "interrupted")
MAX_OUTPUT_BYTES = 256 * 1024
MAX_HISTORY = 1000
MAX_ACTIVE_AGENTS = 32
STATE_DIR_ENV = "ANTIAGENT_STATE_DIR"
_STATE_DIRECTORY: Path | None = None


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
                manager_error TEXT
            )"""
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
        )

    def _fetch(self, agent_id: str) -> AgentSnapshot | None:
        row = self._db.execute(
            "SELECT * FROM agents WHERE agent_id=? AND owner_id=?",
            (agent_id, self.owner_id),
        ).fetchone()
        return self._snapshot(row)

    def _begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

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
                        status,cancel_requested,conversation_id,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        agent_id, parent_agent_id, self.owner_id, workspace,
                        thinking_level, mode, "queued", 0, conversation_id,
                        created_at, updated_at,
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
                self._db.execute(
                    """UPDATE agents SET status='running',started_at=?,updated_at=?
                       WHERE agent_id=? AND owner_id=? AND status='queued'""",
                    (started_at, updated_at, agent_id, self.owner_id),
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
                self._db.execute(
                    """UPDATE agents SET status=?,finished_at=?,updated_at=?,output_json=?,
                       conversation_id=COALESCE(?,conversation_id),manager_error=?
                       WHERE agent_id=? AND owner_id=? AND status NOT IN ('completed','failed','interrupted')""",
                    (
                        status, finished_at, updated_at, output_json, conversation_id,
                        manager_error, agent_id, self.owner_id,
                    ),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return self._fetch(agent_id)

    def request_cancel(self, agent_id: str) -> AgentSnapshot | None:
        with self._lock:
            try:
                self._begin()
                self._db.execute(
                    """UPDATE agents SET cancel_requested=1,updated_at=?
                       WHERE agent_id=? AND owner_id=? AND status NOT IN ('completed','failed','interrupted')""",
                    (time.time(), agent_id, self.owner_id),
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
            try:
                self._begin()
                cursor = self._db.execute(
                    """UPDATE agents SET status='failed',finished_at=?,updated_at=?,
                       manager_error='manager_lost'
                       WHERE owner_id=? AND status IN ('queued','running') AND updated_at<?""",
                    (finished_at, time.time(), self.owner_id, cutoff),
                )
                self._db.commit()
                return cursor.rowcount
            except Exception:
                self._db.rollback()
                raise


__all__ = [
    "AgentCapacityError", "AgentSnapshot", "AgentStore", "default_db_path",
    "default_state_dir", "prepare_state_dir",
]
