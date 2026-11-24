# TaskEx

**A minimal-dependency, pure Python library for running async functions, sync functions, and shell commands as tracked asynchronous tasks.**

[![PyPI version](https://img.shields.io/pypi/v/taskex?color=blue)](https://pypi.org/project/taskex/)
[![License](https://img.shields.io/github/license/adalundhe/taskex)](https://github.com/adalundhe/taskex/blob/main/LICENSE)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/taskex?color=red)](https://pypi.org/project/taskex/)

| Package | TaskEx |
| --- | --- |
| Version | 0.4.0 |
| Python | >= 3.11 |
| Dependencies | `pydantic` |
| Download | https://pypi.org/project/taskex/ |
| Source | https://github.com/adalundhe/taskex |
| Keywords | async, tasks, runner, shell, jobs, executor |

---

## Features

- **Unified async interface** — Run `async def` coroutines, blocking Python functions (via `ProcessPoolExecutor`, `ThreadPoolExecutor`, or the default asyncio thread pool), and subprocess/shell commands through one `TaskRunner` API.
- **Token-based tracking** — Every execution receives a unique Snowflake-based `run.token` (`"<task_name>:<run_id>"`) for status queries, live output inspection, awaiting, or cancellation.
- **Structured Pydantic results** — Inspect runs with typed `TaskRun` and `ShellProcess` models (including status, return code, stdout/stderr, error message, traceback, and monotonic timing).
- **Non-blocking shell I/O & clean reaping** — Subprocess `stdout` and `stderr` are drained concurrently to prevent pipe deadlocks and preserve complete output across polling calls. Timeouts and cancellations terminate and reap process groups cleanly.
- **Scheduling & retention policies** — Run recurring jobs on human-readable intervals (`"500ms"`, `"30s"`, `"1h 30m"`) with fixed or infinite repeats, and bound in-memory run history using `COUNT`, `AGE`, or `COUNT_AND_AGE` cleanup policies.
- **Checkpointing & pause/resume** — Save and recover progress with `checkpoint()` / `pause_point()` / `last_checkpoint()` `ContextVar` helpers (no signature changes), pluggable `MemoryCheckpointStore` and durable stdlib `SQLiteCheckpointStore` backends, POSIX `SIGSTOP`/`SIGCONT` pause/resume for shell commands, schedule suspension, pause-aware timeouts, and restart-from-checkpoint recovery.
- **Graceful lifecycle management** — Use `async with TaskRunner() as runner:`, `stop()` (cancel runs while keeping the runner active), `shutdown()` (graceful teardown), or `abort()` (immediate synchronous teardown).

---

## Installation

```bash
pip install taskex
```

Or with `uv`:

```bash
uv add taskex
```

---

## Quick Start

### Running an async function

```python
import asyncio
from taskex import Env, TaskRunner


async def fetch_value(x: int) -> int:
    await asyncio.sleep(0.1)
    return x * 2


async def main() -> None:
    async with TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none")) as runner:
        run = runner.run(fetch_value, 21)
        result = await runner.wait(run.token)
        print(result.status, result.result)  # RunStatus.COMPLETE 42


asyncio.run(main())
```

### Running a blocking sync function

Synchronous callables are automatically offloaded to the configured executor (`"process"` by default, `"thread"`, or `"none"` for asyncio's default thread pool) and concurrency-limited by `MERCURY_SYNC_TASK_RUNNER_MAX_THREADS`:

```python
import asyncio
from taskex import Env, TaskRunner


def compute_sum(n: int) -> int:
    return sum(range(n))


async def main() -> None:
    config = Env(
        MERCURY_SYNC_EXECUTOR_TYPE="thread",
        MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=4,
    )
    async with TaskRunner(config=config) as runner:
        runs = [runner.run(compute_sum, 100_000) for _ in range(4)]
        results = await runner.wait_all([r.token for r in runs])
        print([r.result for r in results])


asyncio.run(main())
```

### Running a shell command

```python
import asyncio
from taskex import Env, ShellProcess, TaskRunner


async def main() -> None:
    async with TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none")) as runner:
        run = runner.command(
            "ls",
            "-la",
            alias="list_files",
            timeout="10s",
        )
        output: ShellProcess = await runner.wait(run.token)
        print("Exit code:", output.return_code)
        print(output.result)


asyncio.run(main())
```

### Scheduling recurring tasks

```python
import asyncio
from taskex import Env, TaskRunner


async def heartbeat() -> str:
    return "alive"


async def main() -> None:
    async with TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none")) as runner:
        run = runner.run(
            heartbeat,
            alias="heartbeat",
            schedule="500ms",
            repeat=3,
            keep=5,
            keep_policy="COUNT",
        )
        await runner.wait(run.token)
        await asyncio.sleep(1.5)


asyncio.run(main())
```

### Checkpointing, Pause & Resume

```python
import asyncio
from taskex import (
    Env,
    SQLiteCheckpointStore,
    TaskRunner,
    checkpoint,
    last_checkpoint,
    pause_point,
)


async def process_items(items: list[str]) -> list[str]:
    prev = last_checkpoint()
    start = prev.state["index"] if prev else 0
    out = list(prev.state["out"]) if prev else []

    for i in range(start, len(items)):
        await pause_point()
        out.append(items[i].upper())
        await checkpoint({"index": i + 1, "out": out})
        await asyncio.sleep(0.05)

    return out


async def main() -> None:
    with SQLiteCheckpointStore("checkpoints.db") as store:
        async with TaskRunner(
            config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"),
            checkpoint_store=store,
        ) as runner:
            run = runner.run(process_items, ["a", "b", "c", "d"], alias="etl")
            await asyncio.sleep(0.08)

            await runner.pause(run.token)
            # ...later...
            await runner.resume(run.token)

            result = await runner.wait(run.token)
            print(result.status, result.result, result.checkpoint.state)


asyncio.run(main())
```

---

## Configuration Overview

Pass an `Env` model to `TaskRunner(instance_id=0, config=Env(...))`:

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `MERCURY_SYNC_EXECUTOR_TYPE` | `"process" \| "thread" \| "none"` | `"process"` | Executor used for synchronous callables (`"none"` uses asyncio's default thread pool). |
| `MERCURY_SYNC_TASK_RUNNER_MAX_THREADS` | `int` (`>= 1`) | `os.cpu_count() or 1` | Maximum concurrent workers and semaphore permits for sync callables. |
| `MERCURY_SYNC_CLEANUP_INTERVAL` | `str` (duration) | `"1s"` | Interval between background cleanup passes (`COUNT` / `AGE` eviction). |
| `MERCURY_SYNC_CHECKPOINT_DB_PATH` | `str \| None` | `None` | Optional SQLite file path for durable runner-managed checkpoints (defaults to in-memory). |
| `MERCURY_SYNC_LOG_LEVEL` | `str` | `"info"` | Log level configuration value. |

See [docs/configuration.md](docs/configuration.md) for the full configuration reference.

---

## Documentation

- [Documentation Index & Architecture](docs/index.md)
- [Getting Started & Lifecycle](docs/getting-started.md)
- [Running Async & Sync Tasks](docs/running-tasks.md)
- [Running Shell Commands](docs/shell-commands.md)
- [Scheduling, Durations & Cleanup Policies](docs/scheduling-and-cleanup.md)
- [Checkpointing & Pause/Resume](docs/checkpointing-and-pause.md)
- [Configuration, Executors & Snowflake IDs](docs/configuration.md)
- [API Reference](docs/api-reference.md)

## Examples

Runnable scripts are available in [`examples/`](examples/):

- [`examples/run_async_function.py`](examples/run_async_function.py)
- [`examples/run_sync_function.py`](examples/run_sync_function.py)
- [`examples/run_shell_command.py`](examples/run_shell_command.py)
- [`examples/mcp_docker_server_example/server.py`](examples/mcp_docker_server_example/server.py)

---

## Development & Testing

```bash
uv venv
uv pip install pydantic pytest pytest-asyncio
.venv/bin/python -m pytest -v tests/
```

## License

MIT — see [LICENSE](LICENSE).
