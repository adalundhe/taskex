import asyncio
import sys
import time

import pytest

from taskex import Env, RunStatus, TaskRunner


@pytest.mark.asyncio
async def test_fixed_repeat_callable_schedule_runs_exact_count_and_cleans_up() -> None:
    calls: list[int] = []

    async def job(val: int) -> int:
        calls.append(val)
        return len(calls)

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(job, 99, alias="fixed_sched", schedule="20ms", repeat=3)

        # Immediately querying right after run() must not raise KeyError
        early = await runner.get_task_update(run.token)
        assert early.run_id == run.run_id

        # Wait long enough for 3 iterations (2 sleeps of 20ms)
        await asyncio.sleep(0.15)

        task = runner.tasks["fixed_sched"]
        assert calls == [99, 99, 99]
        assert len(task._runs) == 3
        assert all(r.status == RunStatus.COMPLETE for r in task._runs.values())
        # Schedule dicts must be completely cleaned up (no memory leaks)
        assert len(task._schedules) == 0
        assert len(task._schedule_running_statuses) == 0
        assert len(task._schedule_stop_events) == 0
        assert len(task._schedule_current_runs) == 0


@pytest.mark.asyncio
async def test_fixed_repeat_shell_schedule() -> None:
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.command(
            sys.executable,
            "-c",
            "print('tick')",
            alias="shell_sched",
            schedule="30ms",
            repeat=2,
        )
        first_res = await runner.wait(run.token)
        assert first_res.result == "tick\n"

        await asyncio.sleep(0.15)
        task = runner.tasks["shell_sched"]
        assert len(task._runs) == 2
        assert all(r.status == RunStatus.COMPLETE for r in task._runs.values())
        assert len(task._schedules) == 0


@pytest.mark.asyncio
async def test_always_schedule_stop_schedules_wakes_immediately() -> None:
    count = 0

    async def periodic() -> int:
        nonlocal count
        count += 1
        return count

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(periodic, alias="long_sched", schedule="1h", repeat="ALWAYS")
        await runner.wait(run.token)
        assert count == 1

        task = runner.tasks["long_sched"]
        assert len(task._schedules) == 1

        # Stopping a 1h schedule must wake up and finish immediately
        start = time.monotonic()
        runner.stop_schedules("long_sched")
        await asyncio.sleep(0.02)

        assert time.monotonic() - start < 0.5
        assert len(task._schedules) == 0
        assert len(task._schedule_running_statuses) == 0
        assert len(task._schedule_stop_events) == 0
        assert count == 1


@pytest.mark.asyncio
async def test_cancel_schedule_stops_future_and_active_runs() -> None:
    executions = 0

    async def slow_periodic() -> None:
        nonlocal executions
        executions += 1
        await asyncio.sleep(10)

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(
            slow_periodic,
            alias="cancellable_sched",
            schedule="20ms",
            repeat="ALWAYS",
        )
        await asyncio.sleep(0.06)
        assert executions >= 2

        task = runner.tasks["cancellable_sched"]
        latest_run = task.latest()
        assert latest_run is not None

        # Cancel via initial token (must not crash with walrus precedence bug!)
        await runner.cancel_schedule(run.token)

        assert len(task._schedules) == 0
        assert len(task._schedule_running_statuses) == 0
        assert latest_run.status == RunStatus.CANCELLED

        count_after_cancel = executions
        await asyncio.sleep(0.06)
        assert executions == count_after_cancel


@pytest.mark.asyncio
async def test_count_cleanup_policy_default_and_exact_keep() -> None:
    async def fast(i: int) -> int:
        return i

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="50ms")
    ) as runner:
        # Default keep (10): 15 runs should be trimmed to 10 newest
        default_runs = [runner.run(fast, i, alias="default_keep") for i in range(15)]
        await runner.wait_all([r.token for r in default_runs])

        # Explicit keep=3: 8 runs should be trimmed to 3 newest
        explicit_runs = [
            runner.run(fast, i, alias="keep_three", keep=3, keep_policy="COUNT")
            for i in range(8)
        ]
        await runner.wait_all([r.token for r in explicit_runs])

        # Wait for cleanup loop tick
        await asyncio.sleep(0.12)

        default_task = runner.tasks["default_keep"]
        assert len(default_task._runs) == 10
        assert sorted(default_task._runs.keys()) == [r.run_id for r in default_runs[-10:]]
        # Evicted completed runs must NOT have their status mutated to CANCELLED
        assert default_runs[0].status == RunStatus.COMPLETE

        explicit_task = runner.tasks["keep_three"]
        assert len(explicit_task._runs) == 3
        assert sorted(explicit_task._runs.keys()) == [r.run_id for r in explicit_runs[-3:]]


@pytest.mark.asyncio
async def test_age_and_count_and_age_cleanup_policies() -> None:
    async def fast() -> str:
        return "ok"

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="30ms")
    ) as runner:
        r1 = runner.run(fast, alias="age_task", max_age="60ms", keep_policy="AGE")
        await runner.wait(r1.token)

        # Also test AGE policy when max_age is omitted (must not crash cleanup loop)
        r_no_age = runner.run(fast, alias="no_age_task", keep_policy="AGE")
        await runner.wait(r_no_age.token)

        await asyncio.sleep(0.12)

        assert len(runner.tasks["age_task"]._runs) == 0
        assert len(runner.tasks["no_age_task"]._runs) == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_schedule_even_after_initial_run_evicted() -> None:
    ticks = 0

    async def ticker() -> int:
        nonlocal ticks
        ticks += 1
        return ticks

    runner = TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="30ms")
    )
    initial_run = runner.run(
        ticker,
        alias="evicted_sched",
        schedule="20ms",
        repeat="ALWAYS",
        keep=2,
        keep_policy="COUNT",
    )

    # Let enough iterations run so initial run_id is evicted by keep=2 cleanup
    await asyncio.sleep(0.18)
    task = runner.tasks["evicted_sched"]
    assert ticks >= 4
    assert initial_run.run_id not in task._runs

    await runner.shutdown()
    ticks_after_shutdown = ticks
    await asyncio.sleep(0.08)

    assert ticks == ticks_after_shutdown
    assert len(task._schedules) == 0


@pytest.mark.asyncio
async def test_count_cleanup_does_not_cancel_live_runs() -> None:
    async def slow_job(idx: int) -> int:
        await asyncio.sleep(0.20)
        return idx

    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="none",
            MERCURY_SYNC_CLEANUP_INTERVAL="40ms",
        )
    ) as runner:
        # 25 concurrent live runs exceeding default keep=10 across multiple cleanup sweeps
        runs = [runner.run(slow_job, i, alias="burst_25") for i in range(25)]
        results = await runner.wait_all([r.token for r in runs])

        assert len(results) == 25
        assert all(res.status == RunStatus.COMPLETE for res in results)
        assert [res.result for res in results] == list(range(25))

        # After completion, cleanup trims finished history to default keep=10
        await asyncio.sleep(0.08)
        task = runner.tasks["burst_25"]
        assert len(task._runs) == 10
        assert sorted(task._runs.keys()) == [r.run_id for r in runs[-10:]]


@pytest.mark.asyncio
async def test_age_cleanup_does_not_cancel_live_or_paused_runs() -> None:
    started = asyncio.Event()

    async def slow_age_job() -> str:
        started.set()
        await asyncio.sleep(0.16)
        return "finished"

    async def pauseable_job() -> str:
        from taskex import pause_point

        await pause_point()
        return "resumed-ok"

    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="none",
            MERCURY_SYNC_CLEANUP_INTERVAL="30ms",
        )
    ) as runner:
        r_live = runner.run(
            slow_age_job,
            alias="age_live",
            max_age="50ms",
            keep_policy="AGE",
        )
        r_paused = runner.run(
            pauseable_job,
            alias="age_paused",
            max_age="50ms",
            keep_policy="AGE",
        )
        assert await runner.pause(r_paused.token) is True

        # Wait longer than max_age (50ms) and multiple cleanup sweeps while live/paused
        await started.wait()
        await asyncio.sleep(0.11)
        assert r_live.run_id in runner.tasks["age_live"]._runs
        assert r_paused.run_id in runner.tasks["age_paused"]._runs
        assert r_paused.status == RunStatus.PAUSED

        await runner.resume(r_paused.token)
        res_live, res_paused = await runner.wait_all([r_live.token, r_paused.token])
        assert res_live.status == RunStatus.COMPLETE
        assert res_live.result == "finished"
        assert res_paused.status == RunStatus.COMPLETE
        assert res_paused.result == "resumed-ok"

        # Once terminal and older than max_age, both are evicted cleanly
        await asyncio.sleep(0.10)
        assert len(runner.tasks["age_live"]._runs) == 0
        assert len(runner.tasks["age_paused"]._runs) == 0

