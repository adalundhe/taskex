import asyncio
import gc
import signal
import time
import weakref

import pytest

from taskex import Env, RunStatus, TaskRunner


def test_task_runner_can_be_constructed_outside_event_loop() -> None:
    # Must not raise RuntimeError on Python 3.12+/3.14 when no loop is running
    runner = TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"))
    assert runner.tasks == {}
    runner.abort()


@pytest.mark.asyncio
async def test_signal_handlers_do_not_touch_sighup_and_restore_cleanly() -> None:
    orig_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
    orig_sigint = signal.getsignal(signal.SIGINT)
    orig_sigterm = signal.getsignal(signal.SIGTERM)

    runner1 = TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"))
    runner2 = TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"))

    if hasattr(signal, "SIGHUP"):
        assert signal.getsignal(signal.SIGHUP) == orig_sighup

    # Shutting down runner1 keeps handlers active while runner2 is still alive
    await runner1.shutdown()

    # Shutting down runner2 restores original OS handlers cleanly
    await runner2.shutdown()

    assert signal.getsignal(signal.SIGINT) == orig_sigint
    assert signal.getsignal(signal.SIGTERM) == orig_sigterm
    if hasattr(signal, "SIGHUP"):
        assert signal.getsignal(signal.SIGHUP) == orig_sighup


@pytest.mark.asyncio
async def test_task_runner_is_garbage_collectible_without_shutdown() -> None:
    orig_sigint = signal.getsignal(signal.SIGINT)
    orig_sigterm = signal.getsignal(signal.SIGTERM)

    # Verifies event loop signal handler registry does not leak strong references
    # and restores signal handlers when the last runner is garbage-collected.
    def _create() -> weakref.ReferenceType[TaskRunner]:
        r = TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"))
        return weakref.ref(r)

    ref = _create()
    gc.collect()
    assert ref() is None
    assert signal.getsignal(signal.SIGINT) == orig_sigint
    assert signal.getsignal(signal.SIGTERM) == orig_sigterm


@pytest.mark.asyncio
async def test_submitting_after_shutdown_raises_runtime_error() -> None:
    async def noop() -> None:
        pass

    runner = TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"))
    await runner.shutdown()

    with pytest.raises(RuntimeError):
        runner.run(noop)
    with pytest.raises(RuntimeError):
        runner.command("echo", "hi")


@pytest.mark.asyncio
async def test_start_cleanup_is_idempotent_and_shutdown_leaves_no_pending_tasks() -> None:
    runner = TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    )
    runner.start_cleanup()
    first_task = runner._cleanup_task
    runner.start_cleanup()
    assert runner._cleanup_task is first_task

    await runner.shutdown()
    # Calling shutdown a second time must be idempotent
    await runner.shutdown()

    assert runner._cleanup_task is None
    assert first_task is not None and first_task.done()


@pytest.mark.asyncio
async def test_shutdown_does_not_hang_on_cancelled_blocking_sync_task() -> None:
    def block_briefly() -> None:
        time.sleep(0.5)

    runner = TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="thread",
            MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2,
            MERCURY_SYNC_CLEANUP_INTERVAL="5s",
        )
    )
    run = runner.run(block_briefly)
    await asyncio.sleep(0.03)
    await runner.cancel(run.token)

    start = time.monotonic()
    await runner.shutdown()
    elapsed = time.monotonic() - start
    assert elapsed < 0.25


@pytest.mark.asyncio
async def test_stop_keeps_runner_usable() -> None:
    async def slow() -> str:
        await asyncio.sleep(5)
        return "slow"

    async def fast() -> str:
        return "fast"

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        r1 = runner.run(slow)
        await asyncio.sleep(0.02)
        await runner.stop()
        assert r1.status == RunStatus.CANCELLED

        r2 = runner.run(fast)
        res2 = await runner.wait(r2.token)
        assert res2.status == RunStatus.COMPLETE
        assert res2.result == "fast"


@pytest.mark.asyncio
async def test_abort_synchronously_cancels_cleanup_and_tasks() -> None:
    async def sleeper() -> None:
        await asyncio.sleep(10)

    runner = TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="thread", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    )
    run = runner.run(sleeper)
    cleanup_task = runner._cleanup_task
    assert cleanup_task is not None

    runner.abort()
    await asyncio.sleep(0.02)

    assert run.status == RunStatus.CANCELLED
    assert cleanup_task.done()
    assert runner._cleanup_task is None
    assert runner._executor is None
