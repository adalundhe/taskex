# Checkpointing & Pause/Resume

TaskEx supports durable checkpointing and multi-level pause/resume across async functions, thread-pool sync functions, shell subprocesses, and recurring schedules — with zero additional dependencies beyond `pydantic` and Python's standard library (`sqlite3`, `contextvars`).

---

## Checkpointing

### Inside Async Tasks (`ContextVar` Helpers)

Call `checkpoint()`, `pause_point()`, and `last_checkpoint()` directly inside any `async def` task without changing its function signature:

```python
import asyncio
from taskex import Env, TaskRunner, checkpoint, last_checkpoint, pause_point


async def process_items(items: list[str]) -> list[str]:
    prev = last_checkpoint()
    start_idx = prev.state["index"] if prev else 0
    processed = list(prev.state["processed"]) if prev else []

    for i in range(start_idx, len(items)):
        # Cooperatively suspend here if the run has been paused
        await pause_point()

        processed.append(items[i].upper())

        # Persist state AND honor any pending pause right after saving
        await checkpoint(
            {"index": i + 1, "processed": processed},
            metadata={"progress": (i + 1) / len(items)},
        )

    return processed
```

`checkpoint(state, metadata=None, check_pause=True)` saves the checkpoint to the runner's `CheckpointStore` first, updates `run.latest_checkpoint`, and then awaits `pause_point()` if `check_pause=True`.

### Inside Sync Tasks (Thread-Pool Executors)

For synchronous functions running under `MERCURY_SYNC_EXECUTOR_TYPE="thread"` or `"none"`, TaskEx copies the `contextvars.Context` into the worker thread so the synchronous helpers work identically:

```python
from taskex import checkpoint_sync, last_checkpoint, pause_point_sync


def process_batch(items: list[str]) -> list[str]:
    prev = last_checkpoint()
    start_idx = prev.state["index"] if prev else 0
    processed = list(prev.state["processed"]) if prev else []

    for i in range(start_idx, len(items)):
        pause_point_sync()
        processed.append(items[i].upper())
        checkpoint_sync({"index": i + 1, "processed": processed})

    return processed
```

> **Note on `"process"` executor:** `contextvars.Context` cannot cross OS process boundaries, so in-task `ContextVar` helpers are available for async tasks and thread-pool sync tasks (`"thread"` or `"none"`). For `"process"` tasks, you can still use external checkpoints via `await runner.checkpoint(token, state)`.

### External Checkpointing API

Inspect, create, or clear checkpoints from outside a running task:

```python
cp = await runner.checkpoint(run.token, {"stage": "uploaded"}, metadata={"retries": 0})
latest = await runner.get_checkpoint(run.token)
latest_for_task = await runner.get_latest_task_checkpoint("etl")
history = await runner.list_checkpoints(run.token)
await runner.clear_checkpoints(run.token)
```

Both `TaskRun` and `ShellProcess` snapshots also expose `result.checkpoint` (the latest `Checkpoint`) and `result.paused_duration`.

---

## Checkpoint Stores

### `MemoryCheckpointStore` (Default)

Used automatically when no store or database path is configured. Thread-safe and supports an optional per-run cap:

```python
from taskex import MemoryCheckpointStore, TaskRunner

store = MemoryCheckpointStore(max_checkpoints_per_run=20)
runner = TaskRunner(checkpoint_store=store)
```

### `SQLiteCheckpointStore` (Durable, Stdlib `sqlite3`)

Persists JSON-serializable checkpoint `state` and `metadata` atomically to SQLite in WAL mode. Configure either via `Env` (the runner owns and closes the connection on `shutdown()`/`abort()`) or by passing an explicit store instance (the caller owns `store.close()`):

```python
from taskex import Env, SQLiteCheckpointStore, TaskRunner

# Option 1: Runner-managed SQLite database
config = Env(
    MERCURY_SYNC_EXECUTOR_TYPE="none",
    MERCURY_SYNC_CHECKPOINT_DB_PATH="./state/checkpoints.db",
)
async with TaskRunner(config=config) as runner:
    ...

# Option 2: Caller-managed store (can be shared or inspected after shutdown)
with SQLiteCheckpointStore("./state/checkpoints.db", max_checkpoints_per_run=50) as store:
    async with TaskRunner(checkpoint_store=store) as runner:
        ...
```

### Custom `CheckpointStore` Backends

Implement the `CheckpointStore` protocol (`save`, `get_latest`, `get_latest_for_task`, `list_checkpoints`, `delete`, `close`). Methods may be **synchronous or `async def`** — TaskEx awaits async methods automatically (and bridges them from sync thread-pool tasks via `asyncio.run_coroutine_threadsafe`).

---

## Pause & Resume

### Live Pause / Resume

| Workload | `await runner.pause(token)` | `await runner.resume(token)` |
| --- | --- | --- |
| **Async callable** | Marks `RunStatus.PAUSED`; suspends at `await pause_point()` / `await checkpoint()` (or before start). | Unblocks suspended coroutine and returns the same `Run`. |
| **Sync callable (thread pool)** | Marks `RunStatus.PAUSED`; blocks worker thread at `pause_point_sync()` / `checkpoint_sync()`. | Unblocks worker thread and returns the same `Run`. |
| **Shell command (POSIX)** | Sends `SIGSTOP` to the subprocess process group (or defers spawn if not yet started). | Sends `SIGCONT` to the process group and returns the same `Run`. |
| **Recurring schedule** | `await runner.pause_schedule(token)` suspends future iterations and pauses the active run. | `await runner.resume_schedule(token)` resumes the active run and schedule loop. |

**Pause-aware timeouts & cancellation safety:**

- Configured `timeout` countdowns freeze while a run is `PAUSED`, so pausing a task for longer than its timeout does not spuriously fail it.
- Cancelling a `PAUSED` run cleanly unblocks sleeping sync threads (raising `CancelledError`) and sends `SIGCONT` alongside `SIGTERM` to `SIGSTOP`-paused POSIX subprocesses so they exit and get reaped immediately without hanging.

### Restart-from-Checkpoint Resume

When `await runner.resume(token, ...)` is called on a **failed, cancelled, or completed** run (or on a brand-new `TaskRunner` backed by `SQLiteCheckpointStore`), TaskEx loads the latest checkpoint from the store and starts a **new `Run`** seeded so `last_checkpoint()` returns that checkpoint immediately:

```python
import asyncio
from taskex import Env, RunStatus, SQLiteCheckpointStore, TaskRunner, checkpoint, last_checkpoint


async def etl(items: list[str]) -> list[str]:
    prev = last_checkpoint()
    idx = prev.state["idx"] if prev else 0
    done = list(prev.state["done"]) if prev else []
    for i in range(idx, len(items)):
        done.append(items[i].upper())
        await checkpoint({"idx": i + 1, "done": done})
    return done


async def main() -> None:
    with SQLiteCheckpointStore("etl.db") as store:
        # Process / Runner 1 fails or cancels mid-way
        async with TaskRunner(
            config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"),
            checkpoint_store=store,
        ) as runner1:
            r1 = runner1.run(etl, ["a", "b", "c"], alias="etl")
            ...
            old_token = r1.token

        # Process / Runner 2 recovers from SQLite
        async with TaskRunner(
            config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"),
            checkpoint_store=store,
        ) as runner2:
            # Either pass call= to resume()...
            r2 = await runner2.resume(old_token, ["a", "b", "c"], call=etl)
            # ...or pass resume_from= directly to run():
            # r2 = runner2.run(etl, ["a", "b", "c"], alias="etl", resume_from=old_token)
            result = await runner2.wait(r2.token)
            assert result.status == RunStatus.COMPLETE


asyncio.run(main())
```
