import json
import pathlib
import sqlite3
import threading
from typing import Awaitable, Protocol, runtime_checkable

from .models import Checkpoint, RunStatus


@runtime_checkable
class CheckpointStore(Protocol):
    """Protocol for persisting and retrieving run checkpoints.

    Implementations may be synchronous or asynchronous (methods may return
    awaitables); TaskEx awaits results automatically when called from async code.
    """

    def save(self, checkpoint: Checkpoint) -> None | Awaitable[None]: ...

    def get_latest(
        self,
        task_name: str,
        run_id: int,
    ) -> Checkpoint | None | Awaitable[Checkpoint | None]: ...

    def get_latest_for_task(
        self,
        task_name: str,
    ) -> Checkpoint | None | Awaitable[Checkpoint | None]: ...

    def list_checkpoints(
        self,
        task_name: str,
        run_id: int,
    ) -> list[Checkpoint] | Awaitable[list[Checkpoint]]: ...

    def delete(
        self,
        task_name: str,
        run_id: int,
    ) -> None | Awaitable[None]: ...

    def close(self) -> None | Awaitable[None]: ...


class MemoryCheckpointStore:
    """Thread-safe in-memory checkpoint store."""

    def __init__(self, *, max_checkpoints_per_run: int | None = None) -> None:
        if max_checkpoints_per_run is not None and max_checkpoints_per_run < 1:
            raise ValueError("max_checkpoints_per_run must be >= 1 or None.")

        self._max_checkpoints_per_run = max_checkpoints_per_run
        self._lock = threading.RLock()
        self._runs: dict[tuple[str, int], list[Checkpoint]] = {}

    def save(self, checkpoint: Checkpoint) -> None:
        key = (checkpoint.task_name, checkpoint.run_id)
        with self._lock:
            entries = self._runs.setdefault(key, [])
            entries.append(checkpoint)
            if (
                self._max_checkpoints_per_run is not None
                and len(entries) > self._max_checkpoints_per_run
            ):
                del entries[: len(entries) - self._max_checkpoints_per_run]

    def get_latest(self, task_name: str, run_id: int) -> Checkpoint | None:
        with self._lock:
            entries = self._runs.get((task_name, run_id))
            if not entries:
                return None
            return entries[-1]

    def get_latest_for_task(self, task_name: str) -> Checkpoint | None:
        with self._lock:
            latest: Checkpoint | None = None
            for (stored_task_name, _), entries in self._runs.items():
                if stored_task_name != task_name or not entries:
                    continue
                candidate = entries[-1]
                if latest is None or candidate.checkpoint_id > latest.checkpoint_id:
                    latest = candidate
            return latest

    def list_checkpoints(self, task_name: str, run_id: int) -> list[Checkpoint]:
        with self._lock:
            entries = self._runs.get((task_name, run_id))
            return list(entries) if entries else []

    def delete(self, task_name: str, run_id: int) -> None:
        with self._lock:
            self._runs.pop((task_name, run_id), None)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()

    def close(self) -> None:
        pass


class SQLiteCheckpointStore:
    """Durable, thread-safe checkpoint store backed by stdlib sqlite3."""

    def __init__(
        self,
        path: str | pathlib.Path = ":memory:",
        *,
        max_checkpoints_per_run: int | None = None,
    ) -> None:
        if max_checkpoints_per_run is not None and max_checkpoints_per_run < 1:
            raise ValueError("max_checkpoints_per_run must be >= 1 or None.")

        self._max_checkpoints_per_run = max_checkpoints_per_run
        self._lock = threading.RLock()
        self._closed = False

        path_str = str(path)
        self._path = path_str
        if path_str != ":memory:" and not path_str.startswith("file:"):
            pathlib.Path(path_str).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            path_str,
            check_same_thread=False,
            uri=path_str.startswith("file:"),
        )
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock, self._conn:
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS taskex_checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    task_name TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_taskex_checkpoints_run
                    ON taskex_checkpoints (task_name, run_id, sequence)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_taskex_checkpoints_task
                    ON taskex_checkpoints (task_name, checkpoint_id)
                """
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteCheckpointStore is closed.")

    @staticmethod
    def _row_to_checkpoint(row: tuple) -> Checkpoint:
        (
            checkpoint_id,
            run_id,
            task_name,
            sequence,
            state_json,
            metadata_json,
            created_at,
            status_val,
        ) = row
        return Checkpoint(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            task_name=task_name,
            sequence=sequence,
            state=json.loads(state_json),
            metadata=json.loads(metadata_json),
            created_at=created_at,
            status=RunStatus(status_val),
        )

    def save(self, checkpoint: Checkpoint) -> None:
        try:
            state_json = json.dumps(checkpoint.state)
            metadata_json = json.dumps(checkpoint.metadata)
        except TypeError as err:
            raise TypeError(
                "SQLiteCheckpointStore requires JSON-serializable state and metadata."
            ) from err

        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO taskex_checkpoints (
                        checkpoint_id,
                        run_id,
                        task_name,
                        sequence,
                        state_json,
                        metadata_json,
                        created_at,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.checkpoint_id,
                        checkpoint.run_id,
                        checkpoint.task_name,
                        checkpoint.sequence,
                        state_json,
                        metadata_json,
                        checkpoint.created_at,
                        checkpoint.status.value,
                    ),
                )

                if self._max_checkpoints_per_run is not None:
                    self._conn.execute(
                        """
                        DELETE FROM taskex_checkpoints
                        WHERE task_name = ? AND run_id = ? AND checkpoint_id NOT IN (
                            SELECT checkpoint_id
                            FROM taskex_checkpoints
                            WHERE task_name = ? AND run_id = ?
                            ORDER BY sequence DESC, checkpoint_id DESC
                            LIMIT ?
                        )
                        """,
                        (
                            checkpoint.task_name,
                            checkpoint.run_id,
                            checkpoint.task_name,
                            checkpoint.run_id,
                            self._max_checkpoints_per_run,
                        ),
                    )

    def get_latest(self, task_name: str, run_id: int) -> Checkpoint | None:
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute(
                """
                SELECT checkpoint_id, run_id, task_name, sequence,
                       state_json, metadata_json, created_at, status
                FROM taskex_checkpoints
                WHERE task_name = ? AND run_id = ?
                ORDER BY sequence DESC, checkpoint_id DESC
                LIMIT 1
                """,
                (task_name, run_id),
            )
            row = cur.fetchone()
            return self._row_to_checkpoint(row) if row is not None else None

    def get_latest_for_task(self, task_name: str) -> Checkpoint | None:
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute(
                """
                SELECT checkpoint_id, run_id, task_name, sequence,
                       state_json, metadata_json, created_at, status
                FROM taskex_checkpoints
                WHERE task_name = ?
                ORDER BY checkpoint_id DESC
                LIMIT 1
                """,
                (task_name,),
            )
            row = cur.fetchone()
            return self._row_to_checkpoint(row) if row is not None else None

    def list_checkpoints(self, task_name: str, run_id: int) -> list[Checkpoint]:
        with self._lock:
            self._ensure_open()
            cur = self._conn.execute(
                """
                SELECT checkpoint_id, run_id, task_name, sequence,
                       state_json, metadata_json, created_at, status
                FROM taskex_checkpoints
                WHERE task_name = ? AND run_id = ?
                ORDER BY sequence ASC, checkpoint_id ASC
                """,
                (task_name, run_id),
            )
            return [self._row_to_checkpoint(row) for row in cur.fetchall()]

    def delete(self, task_name: str, run_id: int) -> None:
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute(
                    """
                    DELETE FROM taskex_checkpoints
                    WHERE task_name = ? AND run_id = ?
                    """,
                    (task_name, run_id),
                )

    def clear(self) -> None:
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute("DELETE FROM taskex_checkpoints")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> "SQLiteCheckpointStore":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()
