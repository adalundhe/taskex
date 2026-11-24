# Running Shell Commands

Use `TaskRunner.command()` to execute external binaries or shell expressions asynchronously while capturing `stdout`, `stderr`, exit codes, and timing in a typed `ShellProcess` model.

## Basic Usage

By default (`shell=False`), commands are executed directly via `asyncio.create_subprocess_exec` without spawning an intermediate shell:

```python
import asyncio
from taskex import Env, RunStatus, ShellProcess, TaskRunner


async def main() -> None:
    async with TaskRunner(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none")) as runner:
        run = runner.command(
            "python3",
            "-c",
            "print('hello from subprocess')",
            alias="py_hello",
            cwd=".",
            timeout="5s",
        )
        result: ShellProcess = await runner.wait(run.token)

        assert result.status == RunStatus.COMPLETE
        assert result.return_code == 0
        assert result.result == "hello from subprocess\n"


asyncio.run(main())
```

### Shell Mode (`shell=True`)

When you need shell features (pipes, globbing, shell builtins, environment expansion), pass `shell=True`. Additional positional `*args` are automatically quoted with `shlex.quote`:

```python
run = runner.command(
    "echo",
    "hello world",
    alias="echo_shell",
    shell=True,
)
result = await runner.wait(run.token)
print(result.result)  # "hello world\n"
```

> **Security note:** Avoid passing untrusted user input directly into the `command` string when `shell=True`. Prefer `shell=False` with explicit argument lists whenever possible.

## Environment & Working Directory

- `env`: Optional `dict[str, str]` passed as the child process environment. Note that, matching Python's `subprocess` module, passing `env` replaces the environment rather than merging with `os.environ` — merge explicitly with `{**os.environ, "MY_VAR": "1"}` when you want to inherit existing variables.
- `cwd`: Optional `str` or `pathlib.Path` specifying the working directory for the subprocess.

## Live Output & Pipe Safety

While a subprocess is running, TaskEx continuously drains both `stdout` and `stderr` concurrently into in-memory buffers:

- **No pipe deadlocks:** Commands producing large output (megabytes on both `stdout` and `stderr`) never block on full OS pipe buffers.
- **Non-destructive inspection:** Calling `await runner.get_task_update(run.token)`, `await run.get_stdout()`, or `await run.get_stderr()` returns the accumulated output immediately without blocking or consuming bytes from subsequent reads.
- **Safe decoding:** Output is decoded as UTF-8 with `errors="replace"` so binary or partial multi-byte sequences never raise `UnicodeDecodeError`.

## Exit Codes, Errors, and Spawn Failures

- **Exit code `0`**: `status == RunStatus.COMPLETE`. `result` contains `stdout`; if `stderr` was non-empty, `error` contains `stderr`.
- **Non-zero exit code**: `status == RunStatus.FAILED`, `return_code` holds the exit code, and `error` includes `stderr`.
- **Spawn failure** (e.g., command not found or invalid `cwd`): `status == RunStatus.FAILED`, `process_id` is `None`, and `error` + `trace` describe the exception.

## Timeouts & Cancellation

On POSIX systems, TaskEx launches subprocesses in their own session (`start_new_session=True`) so that timeouts and cancellations terminate the entire process group (including child processes spawned under `shell=True`):

1. Sends `SIGTERM` to the process group and waits briefly for graceful exit.
2. Escalates to `SIGKILL` if the process ignores `SIGTERM`.
3. Reaps the process (`wait()`) and closes reader tasks/transports so no zombie processes or file descriptors leak.

```python
run = runner.command("sleep", "60", alias="long_sleep", timeout="200ms")
result = await runner.wait(run.token)
assert result.status == RunStatus.FAILED
assert "timed out" in (result.error or "")
```
