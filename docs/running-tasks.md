# Running Async & Sync Tasks

Use `TaskRunner.run()` to execute Python callables as tracked background tasks.

## Async Functions

Any `async def` function, bound async method, callable object with `async def __call__`, `functools.partial` wrapping an async function, or raw coroutine object runs directly on the active `asyncio` event loop:

```python
import asyncio
from taskex import Env, RunStatus, TaskRunner


async def double(value: int, delay: float = 0.05) -> int:
    await asyncio.sleep(delay)
    return value * 2


async def main() -> None:
    async with TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none")) as runner:
        run = runner.run(double, 10, delay=0.02, alias="double_job")
        result = await runner.wait(run.token)

        assert result.status == RunStatus.COMPLETE
        assert result.result == 20


asyncio.run(main())
```

## Sync Functions

Regular (blocking) Python functions are automatically offloaded to the runner's executor and bounded by `MERCURY_SYNC_TASK_RUNNER_MAX_THREADS`:

```python
import asyncio
import hashlib
from taskex import Env, TaskRunner


def hash_payload(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def main() -> None:
    # Default executor is "process"; use "thread" for I/O-bound or non-picklable objects
    config = Env(MERCURY_SYNC_EXECUTOR_TYPE="process", MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=4)
    async with TaskRunner(config=config) as runner:
        run = runner.run(hash_payload, b"hello taskex")
        result = await runner.wait(run.token)
        print(result.result)


asyncio.run(main())
```

> **Pickling note for `"process"` executor:** When `MERCURY_SYNC_EXECUTOR_TYPE="process"` (the default), synchronous callables and their arguments/return values must be picklable (e.g., module-level functions rather than local closures or live socket clients). For non-picklable objects such as SDK clients or database connections, use `MERCURY_SYNC_EXECUTOR_TYPE="thread"` or `"none"`.

## Bundling Arguments & Reusing Aliases

`runner.bundle(fn, *args, **kwargs)` returns a `functools.partial` that binds positional and keyword arguments up front. This is useful when dispatching heterogeneous calls under a shared task `alias`:

```python
r1 = runner.run(runner.bundle(client.push, "app:v1"), alias="docker_push")
r2 = runner.run(runner.bundle(client.push, "app:v2"), alias="docker_push")
results = await runner.wait_all([r1.token, r2.token])
```

Each run captures its own callable and arguments without mutating the bound object or interfering with earlier runs under the same alias.

## Waiting & Polling

### `wait()` and `wait_all()`

- `await runner.wait(token)` suspends until the run reaches a terminal state (`COMPLETE`, `FAILED`, or `CANCELLED`) and returns a `TaskRun` (or `ShellProcess`). It wakes up immediately on completion without polling delay.
- `await runner.wait_all(tokens)` awaits a list of tokens concurrently and returns their results in order.
- Cancelling a `wait()` call (for example, wrapping it in `asyncio.wait_for(runner.wait(token), timeout=1.0)`) only cancels the waiter — the underlying task continues running unless you explicitly call `runner.cancel(token)`.

### Non-blocking inspection

- `await runner.get_task_update(token)` — Returns a fresh `TaskRun` snapshot immediately without waiting for completion.
- `runner.get_run_status(token)` — Synchronous lookup returning the `RunStatus` for a single run (or `None` if unknown).
- `runner.get_task_status(task_name)` — Synchronous lookup returning the `RunStatus` of the latest run for `task_name` (or `RunStatus.IDLE` if it has no runs, or `None` if the task does not exist).
- `await runner.complete(token)` — Returns the final `TaskRun` if the run has finished, or `None` if it is still running.

## Timeouts & Error Handling

Pass `timeout` as a duration string (e.g. `"500ms"`, `"30s"`, `"2m"`) or a number of seconds (`int | float`):

```python
run = runner.run(slow_operation, timeout="2s")
result = await runner.wait(run.token)

if result.status == RunStatus.FAILED:
    print("Error:", result.error)
    if result.trace:
        print("Traceback:\n", result.trace)
```

Exceptions raised inside a task do **not** crash the event loop; they are caught and recorded on `result.error` and `result.trace` with `result.status == RunStatus.FAILED`.

## Cancelling a Run

```python
await runner.cancel(run.token)
result = await runner.wait(run.token)
assert result.status == RunStatus.CANCELLED
```

Cancelling an async task injects `asyncio.CancelledError` into the coroutine and waits for it to finish. Cancelling a sync task cancels its future if queued and releases its concurrency semaphore permit immediately so the runner never deadlocks.
