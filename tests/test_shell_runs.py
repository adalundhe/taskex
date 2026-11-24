import asyncio
import os
import sys
import time

import pytest

from taskex import Env, RunStatus, ShellProcess, TaskRunner


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@pytest.mark.asyncio
async def test_shell_exec_and_shell_true_basic() -> None:
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        r1 = runner.command(
            sys.executable,
            "-c",
            "import os, sys; print(os.environ.get('TASKEX_TEST', 'none')); print('err-msg', file=sys.stderr)",
            alias="py_exec",
            env={**os.environ, "TASKEX_TEST": "hello"},
        )
        # Immediately after command(), before spawn, update must still be ShellProcess
        early = await runner.get_task_update(r1.token)
        assert isinstance(early, ShellProcess)

        out1 = await runner.wait(r1.token)
        assert isinstance(out1, ShellProcess)
        assert out1.status == RunStatus.COMPLETE
        assert out1.return_code == 0
        assert out1.result == "hello\n"
        assert out1.error == "err-msg\n"
        assert out1.elapsed > 0
        assert out1.end is not None

        # Subsequent get_task_update must preserve full output (not empty!)
        after = await runner.get_task_update(r1.token)
        assert isinstance(after, ShellProcess)
        assert after.result == "hello\n"
        assert after.return_code == 0

        # shell=True with args containing spaces
        r2 = runner.command("echo", "hello world", alias="echo_cmd", shell=True)
        out2 = await runner.wait(r2.token)
        assert out2.status == RunStatus.COMPLETE
        assert out2.result == "hello world\n"


@pytest.mark.asyncio
async def test_shell_large_output_does_not_deadlock_or_lose_data() -> None:
    # Output >256KB to BOTH stdout and stderr (well above 64KB OS pipe buffer)
    script = (
        "import sys; "
        "sys.stdout.write('A' * 300_000); sys.stdout.flush(); "
        "sys.stderr.write('B' * 300_000); sys.stderr.flush()"
    )
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.command(sys.executable, "-c", script, alias="big_io")

        # Polling get_task_update mid-run must NOT steal stdout/stderr or block for 2s
        start = time.monotonic()
        update = await runner.get_task_update(run.token)
        assert time.monotonic() - start < 0.5
        assert isinstance(update, ShellProcess)

        result = await asyncio.wait_for(runner.wait(run.token), timeout=5.0)
        assert result.status == RunStatus.COMPLETE
        assert len(result.result or "") == 300_000
        assert result.result == "A" * 300_000
        assert len(result.error or "") == 300_000
        assert result.error == "B" * 300_000


@pytest.mark.asyncio
async def test_shell_nonzero_exit_and_spawn_failure() -> None:
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        r_fail = runner.command(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('bad input\\n'); sys.exit(3)",
            alias="fail_exit",
        )
        res_fail = await runner.wait(r_fail.token)
        assert res_fail.status == RunStatus.FAILED
        assert res_fail.return_code == 3
        assert "bad input" in (res_fail.error or "")

        r_missing = runner.command(
            "definitely_nonexistent_taskex_binary_xyz_123",
            alias="missing_bin",
        )
        res_missing = await runner.wait(r_missing.token)
        assert isinstance(res_missing, ShellProcess)
        assert res_missing.status == RunStatus.FAILED
        assert res_missing.process_id is None
        assert res_missing.error is not None
        assert res_missing.trace is not None
        assert res_missing.elapsed >= 0


@pytest.mark.asyncio
async def test_shell_timeout_kills_and_reaps_process() -> None:
    script = (
        "import sys, time; "
        "print('started', flush=True); "
        "time.sleep(30)"
    )
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.command(
            sys.executable,
            "-c",
            script,
            alias="timeout_cmd",
            timeout="150ms",
        )
        res = await asyncio.wait_for(runner.wait(run.token), timeout=5.0)

        assert isinstance(res, ShellProcess)
        assert res.status == RunStatus.FAILED
        assert "timed out" in (res.error or "")
        assert res.elapsed > 0
        assert res.process_id is not None
        assert not _pid_exists(res.process_id)


@pytest.mark.asyncio
async def test_shell_cancel_kills_process_tree_and_sigterm_ignorer() -> None:
    # Child ignores SIGTERM so Run._reap_process must escalate to SIGKILL
    script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); "
        "time.sleep(30)"
    )
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run = runner.command(sys.executable, "-c", script, alias="stubborn")
        # Use short grace period for fast test
        run._cancel_grace_period = 0.1

        # Wait until child has printed 'ready'
        for _ in range(50):
            if "ready" in await run.get_stdout():
                break
            await asyncio.sleep(0.02)

        pid = run.pid
        assert pid is not None
        assert _pid_exists(pid)

        await runner.cancel(run.token)

        assert run.status == RunStatus.CANCELLED
        assert not _pid_exists(pid)

        # Also test shell=True spawns child process that dies with process group
        shell_run = runner.command(
            f"{sys.executable} -c 'import time; time.sleep(30)'",
            alias="shell_tree",
            shell=True,
        )
        for _ in range(50):
            if shell_run.pid is not None:
                break
            await asyncio.sleep(0.02)
        shell_pid = shell_run.pid
        assert shell_pid is not None
        await runner.cancel(shell_run.token)
        assert shell_run.status == RunStatus.CANCELLED
        assert not _pid_exists(shell_pid)


@pytest.mark.asyncio
async def test_shell_alias_reuse_runs_new_command() -> None:
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        r1 = runner.command("echo first", alias="reused_shell", shell=True)
        o1 = await runner.wait(r1.token)

        r2 = runner.command("echo second", alias="reused_shell", shell=True)
        o2 = await runner.wait(r2.token)

        assert o1.result == "first\n"
        assert o2.result == "second\n"


@pytest.mark.asyncio
async def test_shell_non_utf8_output_and_abort_running_process() -> None:
    async with TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    ) as runner:
        run_bin = runner.command(
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfe')",
            alias="binary_out",
        )
        res_bin = await runner.wait(run_bin.token)
        assert res_bin.status == RunStatus.COMPLETE
        assert res_bin.result is not None
        assert res_bin.result.startswith("ok")

    runner2 = TaskRunner(
        config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none", MERCURY_SYNC_CLEANUP_INTERVAL="5s")
    )
    run_sleep = runner2.command(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        alias="abort_me",
    )
    for _ in range(50):
        if run_sleep.pid is not None:
            break
        await asyncio.sleep(0.02)

    pid = run_sleep.pid
    assert pid is not None
    runner2.abort()
    await asyncio.sleep(0.05)

    assert run_sleep.status == RunStatus.CANCELLED
    assert not _pid_exists(pid)

