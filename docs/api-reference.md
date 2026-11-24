# API Reference

All primary classes are re-exported from the top-level `taskex` package:

```python
from taskex import (
    Checkpoint,
    CheckpointStore,
    CommandType,
    Env,
    MemoryCheckpointStore,
    Run,
    RunContext,
    RunStatus,
    SQLiteCheckpointStore,
    ShellProcess,
    Task,
    TaskRun,
    TaskRunner,
    TaskStatus,
    TaskType,
    TimeParser,
    checkpoint,
    checkpoint_sync,
    get_run_context,
    last_checkpoint,
    pause_point,
    pause_point_sync,
)
```

---

## `TaskRunner`

```python
class TaskRunner:
    def __init__(
        self,
        instance_id: int | None = None,
        config: Env | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None
```

Supports `async with TaskRunner(...) as runner:`. Exposes `runner.checkpoint_store`.

### Submission & Helpers

- **`run(call, *args, alias=None, run_id=None, timeout=None, schedule=None, trigger="MANUAL", repeat="NEVER", keep=None, max_age=None, keep_policy="COUNT", resume_from=None, **kwargs) -> Run`**
  Schedules a Python callable (async or sync) or coroutine object and returns its `Run`. Optional `resume_from` accepts a `Checkpoint`, a run token, or a task name to seed `last_checkpoint()`.
- **`command(command, *args, alias=None, env=None, cwd=None, shell=False, run_id=None, timeout=None, schedule=None, trigger="MANUAL", repeat="NEVER", keep=None, max_age=None, keep_policy="COUNT", resume_from=None) -> Run`**
  Schedules a subprocess (`shell=False`) or shell command (`shell=True`) and returns its `Run`.
- **`bundle(call, *args, **kwargs) -> functools.partial`**
  Returns a `functools.partial` binding `args` and `kwargs` to `call`.
- **`create_task_id() -> int`**
  Generates a new Snowflake ID from the runner's generator.
- **`all_tasks() -> Iterator[Task]`**
  Yields all registered `Task` objects.
- **`start_cleanup() -> None`**
  Starts the background cleanup loop if not already running (called automatically on the first `run()` or `command()`).

### Waiting & Inspection

- **`async wait(token: str) -> ShellProcess | TaskRun`**
  Awaits completion (or cancellation) of the run identified by `token` and returns its result snapshot.
- **`async wait_all(tokens: list[str]) -> list[ShellProcess | TaskRun]`**
  Concurrently awaits all tokens and returns their result snapshots in order.
- **`async get_task_update(token: str) -> ShellProcess | TaskRun`**
  Returns an immediate snapshot of the run (including accumulated `stdout`/`stderr` for shell runs).
- **`get_run_status(token: str) -> RunStatus | None`**
  Returns the current `RunStatus` for `token`, or `None` if unknown.
- **`get_task_status(task_name: str) -> RunStatus | None`**
  Returns the `RunStatus` of the latest run for `task_name` (`RunStatus.IDLE` if no runs exist, or `None` if the task is unknown).
- **`async complete(token: str) -> ShellProcess | TaskRun | None`**
  Returns the terminal snapshot if the run is complete, otherwise `None`.

### Checkpointing, Pause & Resume

- **`async checkpoint(token: str, state: Any, *, metadata: dict[str, Any] | None = None) -> Checkpoint`**
  Saves a checkpoint for the run identified by `token`.
- **`async get_checkpoint(token: str) -> Checkpoint | None`**
  Returns the latest `Checkpoint` for `token` (queries the store even after restart).
- **`async get_latest_task_checkpoint(task_name: str) -> Checkpoint | None`**
  Returns the most recent `Checkpoint` recorded across all runs of `task_name`.
- **`async list_checkpoints(token: str) -> list[Checkpoint]`**
  Returns all stored checkpoints for `token` in sequence order.
- **`async clear_checkpoints(token: str) -> None`**
  Deletes stored checkpoints for `token`.
- **`async pause(token: str) -> bool`**
  Pauses a live run cooperatively (callables) or via POSIX `SIGSTOP` (shell commands).
- **`async resume(token: str, *args, call=None, env=None, cwd=None, shell=None, timeout=None, **kwargs) -> Run`**
  Resumes a live `PAUSED` run in place, or launches a new `Run` seeded with the latest checkpoint if the run is finished or from a prior runner instance.
- **`async pause_schedule(token: str) -> bool`**
  Suspends future iterations of a recurring schedule and pauses its active run.
- **`async resume_schedule(token: str) -> bool`**
  Resumes a paused schedule and its active run.

### Cancellation & Lifecycle

- **`async cancel(token: str) -> None`**
  Cancels a single run and reaps any subprocess.
- **`async cancel_schedule(token: str) -> None`**
  Stops a recurring schedule and cancels its active run.
- **`stop_schedules(task_name: str) -> None`**
  Synchronously stops future scheduled iterations for `task_name` without cancelling in-flight runs.
- **`async stop() -> None`**
  Cancels all running tasks and schedules while keeping the `TaskRunner` usable.
- **`async shutdown() -> None`**
  Gracefully stops cleanup, cancels all tasks/schedules, restores signal handlers, and shuts down the executor. Idempotent.
- **`abort() -> None`**
  Synchronously kills subprocesses, cancels tasks/schedules/cleanup, restores signal handlers, and shuts down the executor without waiting.

---

## `Run`

Returned by `TaskRunner.run()` and `TaskRunner.command()`.

### Attributes & Properties

| Name | Type | Description |
| --- | --- | --- |
| `run_id` | `int` | Snowflake ID of this run. |
| `task_name` | `str` | Owning task name/alias. |
| `token` | `str` | Tracking token `"<task_name>:<run_id>"`. |
| `status` | `RunStatus` | Current lifecycle state. |
| `task_type` | `TaskType` | `TaskType.CALLABLE` or `TaskType.SHELL`. |
| `result` | `Any \| None` | Return value or captured `stdout`. |
| `error` | `str \| None` | Error message or captured `stderr`. |
| `trace` | `str \| None` | Formatted exception traceback, if any. |
| `start` | `float` | `time.monotonic()` timestamp when created. |
| `end` | `float` | `time.monotonic()` timestamp when finished (`0.0` while running). |
| `elapsed` | `float` | Total elapsed seconds once finished. |
| `paused_duration` | `float` | Total seconds spent in `RunStatus.PAUSED`. |
| `latest_checkpoint` | `Checkpoint \| None` | Most recent checkpoint (or initial resumed checkpoint). |
| `resumed_from_checkpoint_id` | `int \| None` | Checkpoint ID this run was resumed from, if any. |
| `timeout` | `int \| float \| None` | Effective timeout in seconds. |
| `pid` | `int \| None` | Subprocess PID (shell runs only). |
| `return_code` | `int \| None` | Subprocess exit code (shell runs only). |
| `task_running` | `bool` | `True` while the underlying `asyncio.Task` is pending/running. |
| `created`, `pending`, `running`, `paused`, `completed`, `failed`, `cancelled` | `bool` | Convenience status checks. |

### Methods

- `async wait() -> ShellProcess | TaskRun`
- `async complete() -> ShellProcess | TaskRun | None`
- `async get_run_update() -> ShellProcess | TaskRun`
- `async get_stdout() -> str`
- `async get_stderr() -> str`
- `async pause() -> bool`
- `async resume() -> bool`
- `async pause_point() -> None`
- `pause_point_sync() -> None`
- `async checkpoint(state, *, metadata=None) -> Checkpoint`
- `checkpoint_sync(state, *, metadata=None) -> Checkpoint`
- `async get_checkpoint() -> Checkpoint | None`
- `async list_checkpoints() -> list[Checkpoint]`
- `async cancel() -> None`
- `abort() -> None`
- `to_dict() -> dict[str, Any]`
- `to_serialized_dict() -> str`

---

## In-Task Context Helpers

- `get_run_context() -> RunContext | None`
- `last_checkpoint() -> Checkpoint | None`
- `async checkpoint(state, *, metadata=None, check_pause=True) -> Checkpoint`
- `checkpoint_sync(state, *, metadata=None, check_pause=True) -> Checkpoint`
- `async pause_point() -> None`
- `pause_point_sync() -> None`

---

## Checkpoint Stores

- **`CheckpointStore`** (`typing.Protocol`): `save`, `get_latest`, `get_latest_for_task`, `list_checkpoints`, `delete`, `close` (sync or async implementations supported).
- **`MemoryCheckpointStore(*, max_checkpoints_per_run: int | None = None)`**: Thread-safe in-memory store.
- **`SQLiteCheckpointStore(path: str | Path = ":memory:", *, max_checkpoints_per_run: int | None = None)`**: Thread-safe durable `sqlite3` store (supports `with SQLiteCheckpointStore(...) as store:`).

---

## Models

### `Checkpoint(BaseModel)`

- `checkpoint_id: int`
- `run_id: int`
- `task_name: str`
- `sequence: int = 1`
- `state: Any = None`
- `metadata: dict[str, Any] = {}`
- `created_at: float`
- `status: RunStatus = RunStatus.RUNNING`

### `TaskRun(BaseModel)`

Snapshot for `TaskType.CALLABLE` runs:

- `run_id: int`
- `task_name: str`
- `status: RunStatus`
- `error: str | None = None`
- `trace: str | None = None`
- `start: int | float`
- `end: int | float | None = None`
- `elapsed: int | float = 0`
- `paused_duration: int | float = 0.0`
- `result: Any | None = None`
- `checkpoint: Checkpoint | None = None`
- `task_type: TaskType = TaskType.CALLABLE`
- `complete() -> bool`

### `ShellProcess(BaseModel)`

Snapshot for `TaskType.SHELL` runs:

- `run_id: int`
- `task_name: str`
- `process_id: int | None = None`
- `command: str`
- `status: RunStatus`
- `args: tuple[str, ...] | None = None`
- `return_code: int | None = None`
- `env: dict[str, Any] | None = None`
- `working_directory: str | None = None`
- `command_type: Literal["shell", "subprocess"] = "subprocess"`
- `error: str | bytes | None = None`
- `trace: str | bytes | None = None`
- `start: int | float`
- `end: int | float | None = None`
- `elapsed: int | float = 0`
- `paused_duration: int | float = 0.0`
- `result: str | bytes | None = None`
- `checkpoint: Checkpoint | None = None`
- `task_type: TaskType = TaskType.SHELL`
- `complete() -> bool`

### Enums

- **`RunStatus`**: `CREATED`, `PENDING`, `IDLE`, `RUNNING`, `PAUSED`, `COMPLETE`, `CANCELLED`, `FAILED`
- **`TaskStatus`**: `CREATED`, `PENDING`, `IDLE`, `RUNNING`, `PAUSED`, `COMPLETE`, `CANCELLED`, `FAILED`
- **`TaskType`**: `CALLABLE`, `SHELL`

---

## Utilities

### `TimeParser`

```python
TimeParser(time_amount: str).time -> float
```

Parses human-readable duration strings (`ms`, `s`, `m`, `h`, `d`, `w`) into seconds. Raises `ValueError` on invalid inputs.

### `SnowflakeGenerator` & `Snowflake`

Imported from `taskex.snowflake`:

- `SnowflakeGenerator(instance: int, *, seq: int = 0, epoch: int = 0, timestamp: int | None = None)`
  - `generate() -> int`
  - `SnowflakeGenerator.from_snowflake(sf: Snowflake) -> SnowflakeGenerator`
  - Implements `__iter__()` and `__next__() -> int`.
- `Snowflake(timestamp: int, instance: int, epoch: int = 0, seq: int = 0)`
  - `Snowflake.parse(snowflake: int, epoch: int = 0) -> Snowflake`
  - Properties: `milliseconds`, `seconds`, `datetime`, `timedelta`, `value`, `__int__()`, and method `datetime_tz(tz=None)`.
