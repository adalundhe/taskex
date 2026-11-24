# Scheduling, Durations & Cleanup Policies

## Duration Syntax (`TimeParser`)

Parameters that accept duration strings (`timeout`, `schedule`, `max_age`, `MERCURY_SYNC_CLEANUP_INTERVAL`) are parsed by `TimeParser`.

Supported units (case-insensitive):

| Unit | Meaning | Example | Parsed seconds |
| --- | --- | --- | --- |
| `ms` | Milliseconds | `"250ms"` | `0.25` |
| `s` (or omitted) | Seconds | `"1.5s"`, `"10"` | `1.5`, `10.0` |
| `m` | Minutes | `"2m"` | `120.0` |
| `h` | Hours | `"1h"` | `3600.0` |
| `d` | Days | `"1d"` | `86400.0` |
| `w` | Weeks | `"1w"` | `604800.0` |

Compound and repeated units are summed:

```python
from taskex import TimeParser

TimeParser("1h 30m 15s").time  # 5415.0
TimeParser("1m 500ms").time    # 60.5
TimeParser("10s 5s").time      # 15.0
```

Invalid or empty duration strings (such as `""`, `"abc"`, `"-5s"`, or `"10x"`) raise `ValueError`.

---

## Scheduling Recurring Tasks

Both `runner.run(...)` and `runner.command(...)` support recurring execution via `schedule` and `repeat`:

- **`repeat="NEVER"`** (default): Runs once immediately.
- **`repeat=<positive int>`**: Executes exactly `repeat` times, waiting `schedule` between iterations.
- **`repeat="ALWAYS"`**: Executes repeatedly every `schedule` until stopped or cancelled.

Providing `repeat != "NEVER"` without a `schedule` raises `ValueError`.

### Fixed-Count Schedule

```python
import asyncio
from taskex import Env, TaskRunner


async def poll_metrics() -> str:
    return "ok"


async def main() -> None:
    async with TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none")) as runner:
        first_run = runner.run(
            poll_metrics,
            alias="metrics",
            schedule="100ms",
            repeat=3,
        )
        await runner.wait(first_run.token)
        await asyncio.sleep(0.35)


asyncio.run(main())
```

### Continuous Schedule (`repeat="ALWAYS"`)

```python
run = runner.command(
    "uptime",
    alias="system_uptime",
    schedule="30s",
    repeat="ALWAYS",
    keep=5,
    keep_policy="COUNT",
)
```

### Stopping vs Cancelling Schedules

- **`runner.stop_schedules(task_name)`** (synchronous): Immediately wakes any sleeping schedule loops for `task_name` so no further iterations are started, while letting the currently executing run finish cleanly.
- **`await runner.cancel_schedule(token)`** (async): Stops future iterations **and** cancels the currently active run (accepts either the initial run's token or the current iteration's token).
- **`await runner.stop()`** / **`await runner.shutdown()`**: Cancels all active schedules and runs across all tasks.

When a schedule finishes or is cancelled, all internal schedule tracking entries are automatically cleaned up.

---

## Retention & Cleanup Policies

`TaskRunner` runs a background cleanup loop every `MERCURY_SYNC_CLEANUP_INTERVAL` (default `"1s"`) to prevent unbounded memory growth in long-running applications.

Configure per-task retention using `keep`, `max_age`, and `keep_policy`:

| `keep_policy` | Behavior |
| --- | --- |
| `"COUNT"` (default) | Keeps the `keep` most recent runs (defaults to `10` if `keep` is omitted) and evicts older runs. |
| `"AGE"` | Evicts runs whose age (`time.monotonic() - run.start`) exceeds `max_age`. |
| `"COUNT_AND_AGE"` | Applies `"AGE"` eviction first, then `"COUNT"` eviction. |

```python
run = runner.run(
    process_event,
    alias="events",
    keep=50,
    max_age="10m",
    keep_policy="COUNT_AND_AGE",
)
```

Eviction behavior:

- Completed, failed, or already-cancelled runs are removed from the task's run dictionary without mutating their terminal status.
- Any evicted run that is still actively running is cancelled and reaped cleanly.
- If a caller is already awaiting `runner.wait(token)` when a run is evicted, `wait()` completes cleanly without raising `KeyError`.
