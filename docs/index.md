# TaskEx Documentation

TaskEx is a lightweight, pure Python library built on `asyncio` and `pydantic` for launching, tracking, scheduling, and cancelling asynchronous functions, synchronous functions, and shell commands.

## Guides

1. **[Getting Started & Lifecycle](getting-started.md)** — Installation, tokens, `RunStatus`, async context manager usage, and choosing between `stop()`, `shutdown()`, and `abort()`.
2. **[Running Async & Sync Tasks](running-tasks.md)** — Submitting coroutines, sync functions, bound methods, `bundle()`, waiting on results, and polling status updates.
3. **[Running Shell Commands](shell-commands.md)** — Executing subprocesses with `shell=False` or `shell=True`, reading `stdout`/`stderr`, timeouts, and process-group cancellation.
4. **[Scheduling, Durations & Cleanup Policies](scheduling-and-cleanup.md)** — `TimeParser` duration syntax, fixed and infinite schedules, stopping/cancelling schedules, and `COUNT`/`AGE` retention policies.
5. **[Checkpointing & Pause/Resume](checkpointing-and-pause.md)** — `ContextVar` checkpoint helpers, `MemoryCheckpointStore` & `SQLiteCheckpointStore`, cooperative & `SIGSTOP`/`SIGCONT` pause/resume, schedule suspension, and restart-from-checkpoint recovery.
6. **[Configuration, Executors & Snowflake IDs](configuration.md)** — Full `Env` reference, choosing an executor, signal handling, and `SnowflakeGenerator`.
7. **[API Reference](api-reference.md)** — Complete method and model reference for all public classes.

---

## Architecture Overview

TaskEx models execution in three layers:

```text
TaskRunner
  └── tasks: dict[str, Task]        # Named tasks (grouped by alias or callable/command name)
        ├── _runs: dict[int, Run]   # Individual executions keyed by Snowflake run_id
        └── _schedules              # Active recurring schedule loops
```

- **`TaskRunner`** is the main entry point. It owns the optional `ProcessPoolExecutor` / `ThreadPoolExecutor`, the concurrency semaphore, the Snowflake ID generator, signal cleanup hooks, and the background retention cleanup loop.
- **`Task`** groups all runs belonging to a single logical task name (`alias`, callable `__name__`, or command string). It manages run history, retention policies (`COUNT`, `AGE`, `COUNT_AND_AGE`), and scheduled repeats.
- **`Run`** encapsulates a single execution — either a Python callable (`TaskType.CALLABLE`) or a subprocess (`TaskType.SHELL`). It tracks status, timing, captured output, errors/tracebacks, and handles cancellation and reaping.
- **`TaskRun`** and **`ShellProcess`** are immutable-style Pydantic snapshots returned by `wait()`, `complete()`, and `get_task_update()`.
