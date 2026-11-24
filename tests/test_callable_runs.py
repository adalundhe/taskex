import asyncio
import dataclasses
import time
import warnings

import pytest

from taskex import Env, RunStatus, TaskRun, TaskRunner


def _sync_add(a: int, b: int) -> int:
    return a + b


def _sync_sleep(duration: float, value: str = "ok") -> str:
    time.sleep(duration)
    return value


def _sync_fail(message: str) -> None:
    raise RuntimeError(message)


class _SlottedService:
    __slots__ = ("prefix",)

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def format_value(self, value: int) -> str:
        return f"{self.prefix}:{value}"

    async def async_format(self, value: int) -> str:
        await asyncio.sleep(0.005)
        return f"async:{self.prefix}:{value}"


@dataclasses.dataclass(frozen=True)
class _FrozenWorker:
    factor: int

    def multiply(self, x: int) -> int:
        return self.factor * x


class _AsyncCallableObject:
    def __init__(self, base: int) -> None:
        self.base = base

    async def __call__(self, offset: int) -> int:
        await asyncio.sleep(0.005)
        return self.base + offset


@pytest.mark.asyncio
async def test_async_function_execution_and_elapsed_freezes() -> None:
    async def compute(x: int, y: int = 1) -> int:
        await asyncio.sleep(0.02)
        return x * y

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(compute, 6, y=7)
        result = await runner.wait(run.token)

        assert isinstance(result, TaskRun)
        assert result.status == RunStatus.COMPLETE
        assert result.result == 42
        assert result.error is None
        assert result.elapsed > 0
        assert result.end is not None
        assert result.elapsed == pytest.approx(result.end - result.start, rel=1e-6)

        await asyncio.sleep(0.03)
        later_update = await runner.get_task_update(run.token)
        assert later_update.elapsed == result.elapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_type", ["thread", "process", "none"])
async def test_sync_function_all_executors(executor_type: str) -> None:
    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE=executor_type,
            MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2,
            MERCURY_SYNC_CLEANUP_INTERVAL="5s",
        )
    ) as runner:
        run = runner.run(_sync_add, 19, 23)
        result = await runner.wait(run.token)

        assert result.status == RunStatus.COMPLETE
        assert result.result == 42


@pytest.mark.asyncio
async def test_sync_and_async_timeouts() -> None:
    async def slow_async() -> str:
        await asyncio.sleep(5)
        return "late"

    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="thread",
            MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2,
            MERCURY_SYNC_CLEANUP_INTERVAL="5s",
        )
    ) as runner:
        r_async = runner.run(slow_async, timeout="50ms")
        r_sync = runner.run(_sync_sleep, 0.3, timeout=0.05)

        res_async, res_sync = await runner.wait_all([r_async.token, r_sync.token])

        assert res_async.status == RunStatus.FAILED
        assert "timed out" in (res_async.error or "")

        assert res_sync.status == RunStatus.FAILED
        assert "timed out" in (res_sync.error or "")


@pytest.mark.asyncio
async def test_semaphore_does_not_leak_on_failures_timeouts_or_cancellations() -> None:
    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="thread",
            MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2,
            MERCURY_SYNC_CLEANUP_INTERVAL="5s",
        )
    ) as runner:
        # Trigger more failures than max_threads
        fail_runs = [runner.run(_sync_fail, f"boom-{i}") for i in range(5)]
        fail_results = await runner.wait_all([r.token for r in fail_runs])
        assert all(r.status == RunStatus.FAILED for r in fail_results)

        # Trigger more timeouts than max_threads
        timeout_runs = [
            runner.run(_sync_sleep, 0.08, timeout=0.01) for _ in range(4)
        ]
        timeout_results = await runner.wait_all([r.token for r in timeout_runs])
        assert all(r.status == RunStatus.FAILED for r in timeout_results)

        # Trigger cancellations
        cancel_runs = [runner.run(_sync_sleep, 0.05) for _ in range(4)]
        for r in cancel_runs:
            await runner.cancel(r.token)

        # Subsequent sync tasks must still run without deadlocking
        ok_runs = [runner.run(_sync_add, i, 10) for i in range(4)]
        ok_results = await asyncio.wait_for(
            runner.wait_all([r.token for r in ok_runs]),
            timeout=2.0,
        )
        assert [r.result for r in ok_results] == [10, 11, 12, 13]


@pytest.mark.asyncio
async def test_bound_methods_partials_and_alias_reuse_no_mutation() -> None:
    service = _SlottedService("svc")
    frozen = _FrozenWorker(factor=3)
    callable_obj = _AsyncCallableObject(base=100)

    class _PlainObj:
        def tag(self, label: str) -> str:
            return f"tag:{label}"

    plain = _PlainObj()
    assert "tag" not in plain.__dict__

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="thread", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        # Slotted bound methods (sync + async)
        r1 = runner.run(service.format_value, 1)
        r2 = runner.run(service.async_format, 2)

        # Frozen dataclass bound method
        r3 = runner.run(frozen.multiply, 9)

        # Plain object bound method via bundle/partial reused under SAME alias
        r4 = runner.run(runner.bundle(plain.tag, "first"), alias="tagger")
        r5 = runner.run(runner.bundle(plain.tag, "second"), alias="tagger")

        # Callable object without __name__
        r6 = runner.run(callable_obj, 23)

        results = await runner.wait_all(
            [r1.token, r2.token, r3.token, r4.token, r5.token, r6.token]
        )

        assert [r.result for r in results] == [
            "svc:1",
            "async:svc:2",
            27,
            "tag:first",
            "tag:second",
            123,
        ]
        # Verify caller objects were NOT mutated
        assert "tag" not in plain.__dict__
        assert plain.tag("direct") == "tag:direct"


@pytest.mark.asyncio
async def test_cancel_actually_cancels_running_async_task() -> None:
    ticks = 0
    cancelled_seen = False

    async def ticker() -> None:
        nonlocal ticks, cancelled_seen
        try:
            while True:
                ticks += 1
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            cancelled_seen = True
            raise

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(ticker)
        await asyncio.sleep(0.05)
        await runner.cancel(run.token)

        ticks_at_cancel = ticks
        await asyncio.sleep(0.06)

        assert cancelled_seen is True
        assert ticks == ticks_at_cancel
        assert run.status == RunStatus.CANCELLED
        assert run.task_running is False

        wait_res = await runner.wait(run.token)
        assert wait_res.status == RunStatus.CANCELLED
        complete_res = await runner.complete(run.token)
        assert complete_res is not None
        assert complete_res.status == RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_completed_run_does_not_overwrite_status() -> None:
    async def fast() -> str:
        return "done"

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(fast)
        res = await runner.wait(run.token)
        assert res.status == RunStatus.COMPLETE

        await runner.cancel(run.token)
        assert run.status == RunStatus.COMPLETE
        assert runner.get_run_status(run.token) == RunStatus.COMPLETE


@pytest.mark.asyncio
async def test_cancelling_waiter_does_not_cancel_underlying_run() -> None:
    async def work() -> str:
        await asyncio.sleep(0.1)
        return "finished"

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(work)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(runner.wait(run.token), timeout=0.02)

        assert run.task_running is True
        res = await runner.wait(run.token)
        assert res.status == RunStatus.COMPLETE
        assert res.result == "finished"


@pytest.mark.asyncio
async def test_alias_with_colons_and_raw_coroutine_cancel_no_warning() -> None:
    async def sample() -> str:
        await asyncio.sleep(0.01)
        return "colon-ok"

    async def unused() -> str:
        await asyncio.sleep(1)
        return "never"

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(sample, alias="ns:job:v1")
        res = await runner.wait(run.token)
        assert res.result == "colon-ok"
        assert runner.get_run_status(run.token) == RunStatus.COMPLETE

        coro = unused()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            coro_run = runner.run(coro, alias="raw:coro")
            await runner.cancel(coro_run.token)

        assert coro_run.status == RunStatus.CANCELLED
        assert not any("was never awaited" in str(w.message) for w in caught)


@pytest.mark.asyncio
async def test_repeat_without_schedule_raises_value_error() -> None:
    async def noop() -> None:
        pass

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        with pytest.raises(ValueError):
            runner.run(noop, repeat=3)
        with pytest.raises(ValueError):
            runner.command("echo", "hi", repeat="ALWAYS")
