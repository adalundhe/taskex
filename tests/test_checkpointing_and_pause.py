import asyncio
import os
import pathlib
import sys
import time
from typing import Any

import pytest

from taskex import (
    Checkpoint,
    Env,
    MemoryCheckpointStore,
    RunStatus,
    SQLiteCheckpointStore,
    ShellProcess,
    TaskRun,
    TaskRunner,
    checkpoint,
    checkpoint_sync,
    get_run_context,
    last_checkpoint,
    pause_point,
    pause_point_sync,
)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _AsyncCustomStore:
    """Minimal async CheckpointStore implementation to verify async store support."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, int], list[Checkpoint]] = {}
        self.closed = False

    async def save(self, cp: Checkpoint) -> None:
        await asyncio.sleep(0)
        self.data.setdefault((cp.task_name, cp.run_id), []).append(cp)

    async def get_latest(self, task_name: str, run_id: int) -> Checkpoint | None:
        await asyncio.sleep(0)
        items = self.data.get((task_name, run_id))
        return items[-1] if items else None

    async def get_latest_for_task(self, task_name: str) -> Checkpoint | None:
        await asyncio.sleep(0)
        latest: Checkpoint | None = None
        for (name, _), items in self.data.items():
            if name == task_name and items:
                if latest is None or items[-1].checkpoint_id > latest.checkpoint_id:
                    latest = items[-1]
        return latest

    async def list_checkpoints(self, task_name: str, run_id: int) -> list[Checkpoint]:
        await asyncio.sleep(0)
        return list(self.data.get((task_name, run_id), []))

    async def delete(self, task_name: str, run_id: int) -> None:
        await asyncio.sleep(0)
        self.data.pop((task_name, run_id), None)

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


# ---------------------------------------------------------------------------
# Checkpoint stores (Memory, SQLite, Custom Async)
# ---------------------------------------------------------------------------


def test_memory_checkpoint_store_crud_and_limit() -> None:
    store = MemoryCheckpointStore(max_checkpoints_per_run=2)
    for seq in range(1, 5):
        store.save(
            Checkpoint(
                checkpoint_id=100 + seq,
                run_id=1,
                task_name="job",
                sequence=seq,
                state={"step": seq},
            )
        )

    listed = store.list_checkpoints("job", 1)
    assert [c.sequence for c in listed] == [3, 4]
    latest = store.get_latest("job", 1)
    assert latest is not None and latest.state == {"step": 4}
    assert store.get_latest_for_task("job") == latest

    store.delete("job", 1)
    assert store.get_latest("job", 1) is None
    assert store.list_checkpoints("job", 1) == []

    with pytest.raises(ValueError):
        MemoryCheckpointStore(max_checkpoints_per_run=0)


def test_sqlite_checkpoint_store_persistence_and_limits(tmp_path: pathlib.Path) -> None:
    db_file = tmp_path / "nested" / "checkpoints.db"

    with SQLiteCheckpointStore(db_file, max_checkpoints_per_run=2) as store:
        for seq in range(1, 4):
            store.save(
                Checkpoint(
                    checkpoint_id=200 + seq,
                    run_id=10,
                    task_name="etl",
                    sequence=seq,
                    state={"cursor": seq * 10},
                    metadata={"stage": f"s{seq}"},
                )
            )
        with pytest.raises(TypeError):
            store.save(
                Checkpoint(
                    checkpoint_id=999,
                    run_id=10,
                    task_name="etl",
                    sequence=4,
                    state={"bad": object()},
                )
            )

    # Re-open from disk to verify durability
    with SQLiteCheckpointStore(db_file) as reopened:
        listed = reopened.list_checkpoints("etl", 10)
        assert [c.sequence for c in listed] == [2, 3]
        assert listed[-1].state == {"cursor": 30}
        assert listed[-1].metadata == {"stage": "s3"}
        assert reopened.get_latest_for_task("etl") == listed[-1]

        reopened.delete("etl", 10)
        assert reopened.get_latest("etl", 10) is None

    with pytest.raises(RuntimeError):
        reopened.get_latest("etl", 10)


@pytest.mark.asyncio
async def test_env_checkpoint_db_path_owned_and_closed_cleanly(
    tmp_path: pathlib.Path,
) -> None:
    db_file = tmp_path / "owned.db"

    async def work() -> str:
        await checkpoint({"done": True})
        return "ok"

    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="none",
            MERCURY_SYNC_CLEANUP_INTERVAL="5s",
            MERCURY_SYNC_CHECKPOINT_DB_PATH=str(db_file),
        )
    ) as runner:
        assert isinstance(runner.checkpoint_store, SQLiteCheckpointStore)
        run = runner.run(work, alias="owned_job")
        res = await runner.wait(run.token)
        assert res.status == RunStatus.COMPLETE
        assert res.checkpoint is not None
        assert res.checkpoint.state == {"done": True}

    # Runner owned the store and closed it on shutdown
    assert runner.checkpoint_store._closed is True

    # Data remains persisted on disk
    with SQLiteCheckpointStore(db_file) as verify:
        cp = verify.get_latest_for_task("owned_job")
        assert cp is not None
        assert cp.state == {"done": True}


# ---------------------------------------------------------------------------
# Async cooperative pause, resume, checkpoints, and pause-aware timeouts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_cooperative_pause_resume_and_checkpoints() -> None:
    reached_step_2 = asyncio.Event()
    resumed_after_step_2 = asyncio.Event()

    async def pipeline(total: int) -> list[int]:
        ctx = get_run_context()
        assert ctx is not None
        results: list[int] = []
        for i in range(1, total + 1):
            await pause_point()
            results.append(i)
            await checkpoint({"step": i, "items": list(results)}, metadata={"i": i})
            if i == 2:
                reached_step_2.set()
                await asyncio.sleep(0.03)
                await pause_point()
                resumed_after_step_2.set()
        return results

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(pipeline, 4, alias="pipe")
        await asyncio.wait_for(reached_step_2.wait(), timeout=2.0)

        paused = await runner.pause(run.token)
        assert paused is True
        assert run.paused is True
        assert run.status == RunStatus.PAUSED
        assert runner.get_run_status(run.token) == RunStatus.PAUSED

        update = await runner.get_task_update(run.token)
        assert update.status == RunStatus.PAUSED
        assert update.complete() is False
        assert update.checkpoint is not None
        assert update.checkpoint.state["step"] >= 2

        # Stay paused long enough to prove execution is suspended
        await asyncio.sleep(0.08)
        assert not resumed_after_step_2.is_set()

        resumed_run = await runner.resume(run.token)
        assert resumed_run is run
        assert run.status == RunStatus.RUNNING

        result = await asyncio.wait_for(runner.wait(run.token), timeout=2.0)
        assert isinstance(result, TaskRun)
        assert result.status == RunStatus.COMPLETE
        assert result.result == [1, 2, 3, 4]
        assert result.paused_duration >= 0.05
        assert result.checkpoint is not None
        assert result.checkpoint.sequence == 4

        all_cps = await runner.list_checkpoints(run.token)
        assert [c.sequence for c in all_cps] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_pause_aware_timeout_does_not_expire_while_paused() -> None:
    started = asyncio.Event()

    async def timed_job() -> str:
        started.set()
        await asyncio.sleep(0.03)
        await pause_point()
        await asyncio.sleep(0.03)
        return "made-it"

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        # Active work takes ~60ms; timeout is 150ms; we pause for 250ms (> timeout!)
        run = runner.run(timed_job, alias="timed_pause", timeout="150ms")
        await started.wait()
        assert await runner.pause(run.token) is True

        await asyncio.sleep(0.25)
        assert run.status == RunStatus.PAUSED

        await runner.resume(run.token)
        res = await runner.wait(run.token)
        assert res.status == RunStatus.COMPLETE
        assert res.result == "made-it"
        assert res.paused_duration >= 0.2


@pytest.mark.asyncio
async def test_context_helpers_outside_run() -> None:
    assert get_run_context() is None
    assert last_checkpoint() is None
    await pause_point()  # no-op outside a run
    pause_point_sync()  # no-op outside a run

    with pytest.raises(RuntimeError):
        await checkpoint({"x": 1})
    with pytest.raises(RuntimeError):
        checkpoint_sync({"x": 1})


# ---------------------------------------------------------------------------
# Sync thread-pool cooperative pause, resume, checkpointing, & cancel-while-paused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_thread_pool_checkpoint_pause_resume_and_cancel() -> None:
    import threading

    entered_step_1 = threading.Event()
    after_pause = threading.Event()

    def sync_worker(items: list[int]) -> list[int]:
        prev = last_checkpoint()
        start_idx = prev.state["idx"] if prev else 0
        out = list(prev.state["out"]) if prev else []
        for idx in range(start_idx, len(items)):
            pause_point_sync()
            out.append(items[idx] * 10)
            checkpoint_sync({"idx": idx + 1, "out": list(out)})
            if idx == 0:
                entered_step_1.set()
                time.sleep(0.04)
                pause_point_sync()
                after_pause.set()
        return out

    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="thread",
            MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2,
            MERCURY_SYNC_CLEANUP_INTERVAL="5s",
        )
    ) as runner:
        run = runner.run(sync_worker, [1, 2, 3], alias="sync_coop")
        await asyncio.to_thread(entered_step_1.wait, 2.0)

        assert await runner.pause(run.token) is True
        await asyncio.sleep(0.08)
        assert not after_pause.is_set()

        await runner.resume(run.token)
        res = await asyncio.wait_for(runner.wait(run.token), timeout=2.0)
        assert res.status == RunStatus.COMPLETE
        assert res.result == [10, 20, 30]
        assert res.checkpoint is not None
        assert res.checkpoint.state == {"idx": 3, "out": [10, 20, 30]}

        # Now verify cancelling a paused sync worker unblocks its thread cleanly
        blocked_evt = threading.Event()
        exited_evt = threading.Event()

        def pause_and_block() -> None:
            blocked_evt.set()
            try:
                while True:
                    pause_point_sync()
                    time.sleep(0.01)
            finally:
                exited_evt.set()

        r2 = runner.run(pause_and_block, alias="sync_cancel_paused")
        await asyncio.to_thread(blocked_evt.wait, 2.0)
        assert await runner.pause(r2.token) is True
        await asyncio.sleep(0.03)
        await runner.cancel(r2.token)

        assert r2.status == RunStatus.CANCELLED
        assert await asyncio.to_thread(exited_evt.wait, 2.0) is True


# ---------------------------------------------------------------------------
# Shell OS-level SIGSTOP / SIGCONT pause, resume, & cancel-while-SIGSTOPped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shell_sigstop_pause_sigcont_resume_and_cancel() -> None:
    script = (
        "import time\n"
        "for i in range(1, 200):\n"
        "    print(i, flush=True)\n"
        "    time.sleep(0.02)\n"
    )
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.command(sys.executable, "-c", script, alias="counter_proc")

        # Wait until process emits at least 2 lines
        for _ in range(50):
            if len((await run.get_stdout()).splitlines()) >= 2:
                break
            await asyncio.sleep(0.02)

        assert await runner.pause(run.token) is True
        assert run.status == RunStatus.PAUSED
        # Give already-flushed bytes a moment to drain, then snapshot count
        await asyncio.sleep(0.04)
        lines_while_paused = len((await run.get_stdout()).splitlines())
        await asyncio.sleep(0.12)
        assert len((await run.get_stdout()).splitlines()) == lines_while_paused

        # Resume with SIGCONT and verify output resumes growing
        resumed = await runner.resume(run.token)
        assert resumed is run
        assert run.status == RunStatus.RUNNING
        await asyncio.sleep(0.10)
        assert len((await run.get_stdout()).splitlines()) > lines_while_paused

        # Pause again and CANCEL while SIGSTOPped -> must terminate promptly without hanging
        assert await runner.pause(run.token) is True
        pid = run.pid
        assert pid is not None
        start_cancel = time.monotonic()
        await runner.cancel(run.token)
        cancel_elapsed = time.monotonic() - start_cancel

        assert cancel_elapsed < 1.0
        assert run.status == RunStatus.CANCELLED
        assert not _pid_exists(pid)


# ---------------------------------------------------------------------------
# Schedule pause & resume (ALWAYS and fixed repeat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_pause_resume_and_cancel_while_paused() -> None:
    ticks = 0

    async def tick() -> int:
        nonlocal ticks
        ticks += 1
        return ticks

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.run(tick, alias="sched_pr", schedule="25ms", repeat="ALWAYS")
        await asyncio.sleep(0.07)
        assert ticks >= 2

        assert await runner.pause_schedule(run.token) is True
        await asyncio.sleep(0.02)
        frozen_ticks = ticks

        # Wait several intervals while paused: no new iterations should fire
        await asyncio.sleep(0.12)
        assert ticks == frozen_ticks

        # Resume and verify ticks continue
        assert await runner.resume_schedule(run.token) is True
        await asyncio.sleep(0.09)
        assert ticks > frozen_ticks

        # Pause again and cancel cleanly
        assert await runner.pause_schedule(run.token) is True
        await runner.cancel_schedule(run.token)
        task = runner.tasks["sched_pr"]
        assert len(task._schedules) == 0
        assert len(task._schedule_resume_events) == 0
        assert len(task._schedule_paused_statuses) == 0


# ---------------------------------------------------------------------------
# Restart-from-checkpoint resume (same runner, cross-runner SQLite, async store)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_from_checkpoint_same_runner_after_failure() -> None:
    fail_once = {"should_fail": True}

    async def resumable_etl(items: list[str]) -> list[str]:
        prev = last_checkpoint()
        start_idx = prev.state["next_idx"] if prev else 0
        out = list(prev.state["out"]) if prev else []

        for idx in range(start_idx, len(items)):
            if idx == 2 and fail_once["should_fail"]:
                fail_once["should_fail"] = False
                raise RuntimeError("transient outage at item 2")
            out.append(items[idx].upper())
            await checkpoint({"next_idx": idx + 1, "out": list(out)})

        return out

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        first_run = runner.run(resumable_etl, ["a", "b", "c", "d"], alias="etl")
        first_res = await runner.wait(first_run.token)

        assert first_res.status == RunStatus.FAILED
        assert first_res.checkpoint is not None
        assert first_res.checkpoint.state == {"next_idx": 2, "out": ["A", "B"]}

        # Resume from failed run's token -> spawns new run seeded with checkpoint!
        second_run = await runner.resume(first_run.token)
        assert second_run is not first_run
        assert second_run.resumed_from_checkpoint_id == first_res.checkpoint.checkpoint_id

        second_res = await runner.wait(second_run.token)
        assert second_res.status == RunStatus.COMPLETE
        assert second_res.result == ["A", "B", "C", "D"]


@pytest.mark.asyncio
async def test_cross_runner_sqlite_recovery_and_external_checkpoint_api(
    tmp_path: pathlib.Path,
) -> None:
    db_file = tmp_path / "recovery.db"

    async def batch_job(total: int) -> dict[str, Any]:
        prev = last_checkpoint()
        start = prev.state["processed"] if prev else 0
        processed = start
        for step in range(start + 1, total + 1):
            await pause_point()
            processed = step
            await checkpoint({"processed": processed}, metadata={"total": total})
            await asyncio.sleep(0.015)
        return {"processed": processed, "resumed_from": start}

    # Runner 1: starts job, records checkpoints, then cancels/shuts down mid-way
    with SQLiteCheckpointStore(db_file) as store1:
        async with TaskRunner(
            config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s"),
            checkpoint_store=store1,
        ) as runner1:
            r1 = runner1.run(batch_job, 6, alias="durable_batch")
            # Wait for at least 2 checkpoints
            for _ in range(50):
                cp = await runner1.get_checkpoint(r1.token)
                if cp is not None and cp.state["processed"] >= 2:
                    break
                await asyncio.sleep(0.01)

            await runner1.cancel(r1.token)
            saved_cp = await runner1.get_checkpoint(r1.token)
            assert saved_cp is not None
            assert saved_cp.state["processed"] >= 2
            old_token = r1.token

    # Runner 2 (fresh instance, new process simulation): resumes from SQLite DB
    with SQLiteCheckpointStore(db_file) as store2:
        async with TaskRunner(
            config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s"),
            checkpoint_store=store2,
        ) as runner2:
            # Verify querying old checkpoint on fresh runner works
            loaded = await runner2.get_checkpoint(old_token)
            assert loaded is not None
            assert loaded.state["processed"] == saved_cp.state["processed"]

            # Resume via runner2.resume(old_token, ..., call=...)
            r2 = await runner2.resume(old_token, 6, call=batch_job)
            res2 = await runner2.wait(r2.token)
            assert res2.status == RunStatus.COMPLETE
            assert res2.result["processed"] == 6
            assert res2.result["resumed_from"] == saved_cp.state["processed"]

            # External checkpoint API + clear_checkpoints
            ext_cp = await runner2.checkpoint(r2.token, {"manual": True}, metadata={"by": "test"})
            assert ext_cp.state == {"manual": True}
            latest = await runner2.get_latest_task_checkpoint("durable_batch")
            assert latest is not None and latest.checkpoint_id == ext_cp.checkpoint_id

            await runner2.clear_checkpoints(r2.token)
            assert await runner2.list_checkpoints(r2.token) == []

            # Also verify run(..., resume_from=old_token) works directly
            r3 = runner2.run(batch_job, 6, alias="durable_batch", resume_from=old_token)
            res3 = await runner2.wait(r3.token)
            assert res3.status == RunStatus.COMPLETE
            assert res3.result["resumed_from"] == saved_cp.state["processed"]


@pytest.mark.asyncio
async def test_custom_async_checkpoint_store_with_runner_and_sync_bridge() -> None:
    store = _AsyncCustomStore()

    async def async_job() -> int:
        prev = last_checkpoint()
        val = (prev.state["v"] if prev else 0) + 1
        await checkpoint({"v": val})
        return val

    def sync_job() -> int:
        checkpoint_sync({"sync_v": 99})
        return 99

    async with TaskRunner(
        config=Env(
            MERCURY_SYNC_EXECUTOR_TYPE="thread",
            MERCURY_SYNC_CLEANUP_INTERVAL="5s",
        ),
        checkpoint_store=store,
    ) as runner:
        r1 = runner.run(async_job, alias="async_store_job")
        res1 = await runner.wait(r1.token)
        assert res1.result == 1

        # Restart via await runner.resume() with async store
        r2 = await runner.resume(r1.token)
        res2 = await runner.wait(r2.token)
        assert res2.result == 2

        # Sync worker thread bridging to async store via run_coroutine_threadsafe
        r3 = runner.run(sync_job, alias="sync_bridge_job")
        res3 = await runner.wait(r3.token)
        assert res3.status == RunStatus.COMPLETE
        assert res3.result == 99
        cp3 = await runner.get_checkpoint(r3.token)
        assert cp3 is not None and cp3.state == {"sync_v": 99}

        # Sync run(..., resume_from=token) with async store raises clear TypeError
        with pytest.raises(TypeError):
            runner.run(async_job, alias="async_store_job", resume_from=r1.token)

    # Caller-supplied store is NOT closed by TaskRunner.shutdown()
    assert store.closed is False


@pytest.mark.asyncio
async def test_pause_edge_cases_and_shell_pause_aware_timeout() -> None:
    ran = asyncio.Event()

    async def fast() -> str:
        ran.set()
        return "ok"

    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        # Pause immediately before task even begins executing
        r1 = runner.run(fast, alias="pause_before_start")
        assert await runner.pause(r1.token) is True
        assert await runner.pause(r1.token) is True  # idempotent
        await asyncio.sleep(0.04)
        assert not ran.is_set()

        await runner.resume(r1.token)
        res1 = await runner.wait(r1.token)
        assert res1.status == RunStatus.COMPLETE
        # Cannot pause an already-completed run
        assert await runner.pause(r1.token) is False
        assert r1.status == RunStatus.COMPLETE

        # Shell command pause-aware timeout: 100ms work, 200ms timeout, 260ms pause
        r_shell = runner.command(
            sys.executable,
            "-c",
            "import time; print('go', flush=True); time.sleep(0.08); print('done', flush=True)",
            alias="shell_timed_pause",
            timeout="200ms",
        )
        for _ in range(50):
            if "go" in await r_shell.get_stdout():
                break
            await asyncio.sleep(0.01)

        assert await runner.pause(r_shell.token) is True
        await asyncio.sleep(0.26)
        assert r_shell.status == RunStatus.PAUSED

        await runner.resume(r_shell.token)
        res_shell = await runner.wait(r_shell.token)
        assert isinstance(res_shell, ShellProcess)
        assert res_shell.status == RunStatus.COMPLETE
        assert "done" in (res_shell.result or "")
        assert res_shell.paused_duration >= 0.2

