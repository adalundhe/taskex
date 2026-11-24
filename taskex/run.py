import asyncio
import concurrent.futures
import functools
import inspect
import json
import os
import pathlib
import signal
import time
import traceback
from asyncio.subprocess import Process
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Dict, Optional

from .models import (
    CommandType,
    RunStatus,
    ShellProcess,
    TaskRun,
    TaskType,
)

TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETE,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


def _is_coroutine_callable(obj: Any) -> bool:
    if inspect.iscoroutinefunction(obj):
        return True

    target = obj
    while isinstance(target, functools.partial):
        target = target.func

    if inspect.iscoroutinefunction(target):
        return True

    call_attr = getattr(target, "__call__", None)
    return inspect.iscoroutinefunction(call_attr)


class Run:
    __slots__ = (
        "run_id",
        "task_name",
        "status",
        "error",
        "trace",
        "start",
        "end",
        "elapsed",
        "timeout",
        "call",
        "result",
        "task_type",
        "_task",
        "_process",
        "_args",
        "_env",
        "_working_directory",
        "_command_type",
        "_buffer_size",
        "_stdout_buffer",
        "_stderr_buffer",
        "_reader_tasks",
        "_done_event",
        "_executor",
        "_semaphore",
        "_return_code",
        "_started_new_session",
        "_cancel_grace_period",
        "_active_executor_futures",
    )

    def __init__(
        self,
        run_id: int,
        task_name: str,
        call: Callable[..., Awaitable[Any]] | Callable[..., Any] | str,
        task_type: TaskType,
        executor: ProcessPoolExecutor | ThreadPoolExecutor | None,
        semaphore: asyncio.Semaphore,
        timeout: Optional[int | float] = None,
        active_executor_futures: set[concurrent.futures.Future[Any]] | None = None,
    ) -> None:
        self.run_id = run_id
        self.task_name = task_name
        self.status = RunStatus.CREATED

        self._args: tuple[str, ...] | None = None
        self._env: dict[str, Any] | None = None
        self._working_directory: str | None = None

        self.error: Optional[str] = None
        self.trace: Optional[str] = None
        self.start = time.monotonic()
        self.end: float = 0.0
        self.elapsed: float = 0.0
        self.timeout = timeout

        self.call = call
        self.task_type = task_type
        self.result: Any | None = None

        self._task: Optional[asyncio.Task[Any]] = None
        self._process: Process | None = None
        self._command_type: CommandType = "subprocess"
        self._buffer_size = 8192
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._reader_tasks: list[asyncio.Task[Any]] = []
        self._done_event = asyncio.Event()
        self._executor = executor
        self._semaphore = semaphore
        self._return_code: int | None = None
        self._started_new_session = False
        self._cancel_grace_period = 2.0
        self._active_executor_futures = active_executor_futures

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "task_name": self.task_name}

    def to_serialized_dict(self) -> str:
        return json.dumps(self.to_dict())

    @property
    def token(self) -> str:
        return f"{self.task_name}:{self.run_id}"

    @property
    def running(self) -> bool:
        return self.status == RunStatus.RUNNING

    @property
    def cancelled(self) -> bool:
        return self.status == RunStatus.CANCELLED

    @property
    def failed(self) -> bool:
        return self.status == RunStatus.FAILED

    @property
    def pending(self) -> bool:
        return self.status == RunStatus.PENDING

    @property
    def completed(self) -> bool:
        return self.status == RunStatus.COMPLETE

    @property
    def created(self) -> bool:
        return self.status == RunStatus.CREATED

    @property
    def pid(self) -> int | None:
        if self._process:
            return self._process.pid
        return None

    @property
    def return_code(self) -> int | None:
        return self._return_code

    def _decoded_stdout(self) -> str:
        return bytes(self._stdout_buffer).decode("utf-8", errors="replace")

    def _decoded_stderr(self) -> str:
        return bytes(self._stderr_buffer).decode("utf-8", errors="replace")

    async def get_stdout(self) -> str:
        return self._decoded_stdout()

    async def get_stderr(self) -> str:
        return self._decoded_stderr()

    @property
    def task_running(self) -> bool:
        return bool(
            self._task is not None
            and not self._task.done()
            and not self._task.cancelled()
        )

    def _current_elapsed(self) -> float:
        if self.end:
            return self.elapsed
        return time.monotonic() - self.start

    def _build_shell_process(self) -> ShellProcess:
        stdout = (
            self.result if self.result is not None else self._decoded_stdout()
        )
        stderr = self._decoded_stderr()
        error = self.error if self.error is not None else (stderr or None)

        return ShellProcess(
            run_id=self.run_id,
            task_name=self.task_name,
            process_id=self._process.pid if self._process else None,
            command=str(self.call),
            args=self._args,
            status=self.status,
            return_code=self._return_code,
            env=self._env,
            working_directory=self._working_directory,
            command_type=self._command_type,
            error=error,
            result=stdout,
            trace=self.trace,
            start=self.start,
            end=self.end if self.end else None,
            elapsed=self._current_elapsed(),
        )

    def _build_task_run(self) -> TaskRun:
        return TaskRun(
            run_id=self.run_id,
            task_name=self.task_name,
            status=self.status,
            error=self.error,
            trace=self.trace,
            start=self.start,
            end=self.end if self.end else None,
            elapsed=self._current_elapsed(),
            result=self.result,
        )

    def _build_result(self) -> ShellProcess | TaskRun:
        if self.task_type == TaskType.SHELL:
            return self._build_shell_process()
        return self._build_task_run()

    async def get_run_update(self) -> ShellProcess | TaskRun:
        return self._build_result()

    def update_status(self, status: RunStatus) -> None:
        self.status = status
        self.elapsed = time.monotonic() - self.start
        if status in TERMINAL_STATUSES:
            self.end = time.monotonic()
            self.elapsed = self.end - self.start
            self._done_event.set()

    async def wait(self) -> ShellProcess | TaskRun:
        await self._done_event.wait()
        result = await self.complete()
        if result is not None:
            return result
        return self._build_result()

    async def complete(self) -> ShellProcess | TaskRun | None:
        task_done = self._task is not None and self._task.done()
        if self.status not in TERMINAL_STATUSES and not task_done:
            return None

        if self._task is not None:
            try:
                return await self._task
            except (asyncio.InvalidStateError, asyncio.CancelledError):
                return self._build_result()

        return self._build_result()

    def _signal_process(self, sig: int) -> None:
        proc = self._process
        if proc is None or proc.returncode is not None:
            return

        if self._started_new_session and hasattr(os, "killpg"):
            try:
                os.killpg(proc.pid, sig)
                return
            except ProcessLookupError:
                return
            except (PermissionError, OSError):
                pass

        try:
            if sig == getattr(signal, "SIGKILL", signal.SIGTERM):
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            pass

    def _terminate_process(self) -> None:
        self._signal_process(signal.SIGTERM)

    def _kill_process(self) -> None:
        self._signal_process(getattr(signal, "SIGKILL", signal.SIGTERM))

    async def _reap_process(self) -> None:
        proc = self._process
        if proc is None:
            return

        if proc.returncode is None:
            self._terminate_process()
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=self._cancel_grace_period,
                )
            except asyncio.TimeoutError:
                self._kill_process()
                try:
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=self._cancel_grace_period,
                    )
                except asyncio.TimeoutError:
                    pass

        if proc.returncode is not None and self._return_code is None:
            self._return_code = proc.returncode

    async def _finish_readers(self, wait_timeout: float = 0.5) -> None:
        if not self._reader_tasks:
            return

        tasks = list(self._reader_tasks)
        self._reader_tasks.clear()

        done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
        for task in done:
            if not task.cancelled():
                task.exception()

        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            transport = getattr(self._process, "_transport", None)
            if transport is not None:
                transport.close()

    def _close_unstarted_coroutine(self) -> None:
        if (
            inspect.iscoroutine(self.call)
            and inspect.getcoroutinestate(self.call) == inspect.CORO_CREATED
        ):
            self.call.close()

    async def cancel(self) -> None:
        if self.status in TERMINAL_STATUSES:
            self._done_event.set()
            return

        if self._task is not None and not self._task.done():
            self._terminate_process()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        await self._reap_process()
        await self._finish_readers(wait_timeout=0.5)

        if self.status not in TERMINAL_STATUSES:
            self._close_unstarted_coroutine()
            self.status = RunStatus.CANCELLED
            self.end = time.monotonic()
            self.elapsed = self.end - self.start

        self._done_event.set()

    def abort(self) -> None:
        if self.status in TERMINAL_STATUSES:
            self._done_event.set()
            return

        self._kill_process()

        for reader in self._reader_tasks:
            if not reader.done():
                reader.cancel()
        self._reader_tasks.clear()

        if self._task is not None and not self._task.done():
            self._task.cancel()

        transport = getattr(self._process, "_transport", None)
        if transport is not None:
            transport.close()

        if self.status not in TERMINAL_STATUSES:
            self._close_unstarted_coroutine()
            self.status = RunStatus.CANCELLED
            self.end = time.monotonic()
            self.elapsed = self.end - self.start

        self._done_event.set()

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
        self._done_event.set()

    def execute(self, *args: Any, **kwargs: Any) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"Run {self.run_id} is already executing.")

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._execute(*args, **kwargs))
        self._task.add_done_callback(self._on_task_done)

    def execute_shell(
        self,
        *args: Any,
        poll_interval: int | float = 0.5,
        env: Dict[str, str] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool = False,
        timeout: int | float | None = None,
    ) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError(f"Run {self.run_id} is already executing.")

        str_args = tuple(str(arg) for arg in args)
        self._args = str_args
        self._env = env

        if cwd is not None:
            self._working_directory = str(cwd)

        if timeout is not None:
            self.timeout = timeout

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(
            self._execute_shell(
                *str_args,
                env=env,
                cwd=cwd,
                shell=shell,
                timeout=self.timeout,
                poll_interval=poll_interval,
            )
        )
        self._task.add_done_callback(self._on_task_done)

    async def _drain_stream(
        self,
        stream: asyncio.StreamReader | None,
        buffer: bytearray,
    ) -> None:
        if stream is None:
            return

        while True:
            chunk = await stream.read(self._buffer_size)
            if not chunk:
                break
            buffer.extend(chunk)

    async def _execute_shell(
        self,
        *args: str,
        poll_interval: int | float = 0.5,
        env: Dict[str, str] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool = False,
        timeout: int | float | None = None,
    ) -> ShellProcess:
        effective_timeout = timeout if timeout is not None else self.timeout
        self.timeout = effective_timeout

        if shell:
            self._command_type = "shell"

        working_directory: pathlib.Path | None = (
            pathlib.Path(cwd) if cwd is not None else None
        )
        start_new_session = os.name == "posix"

        try:
            if shell:
                command = [str(self.call), *args]
                self._process = await asyncio.create_subprocess_shell(
                    " ".join(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=working_directory,
                    start_new_session=start_new_session,
                )
            else:
                self._process = await asyncio.create_subprocess_exec(
                    str(self.call),
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=working_directory,
                    start_new_session=start_new_session,
                )

            self._started_new_session = start_new_session
            self.status = RunStatus.RUNNING

            loop = asyncio.get_running_loop()
            stdout_reader = loop.create_task(
                self._drain_stream(self._process.stdout, self._stdout_buffer)
            )
            stderr_reader = loop.create_task(
                self._drain_stream(self._process.stderr, self._stderr_buffer)
            )
            self._reader_tasks = [stdout_reader, stderr_reader]

            async def _wait_and_drain() -> int:
                assert self._process is not None
                rc = await self._process.wait()
                await asyncio.gather(stdout_reader, stderr_reader)
                return rc

            if effective_timeout is not None:
                self._return_code = await asyncio.wait_for(
                    _wait_and_drain(),
                    timeout=effective_timeout,
                )
            else:
                self._return_code = await _wait_and_drain()

        except asyncio.TimeoutError:
            await self._reap_process()
            await self._finish_readers(wait_timeout=0.5)
            self.result = self._decoded_stdout()
            self.error = (
                f"Err. - Task Run - {self.run_id} - timed out. "
                f"Exceeded deadline of - {self.timeout} - seconds."
            )
            self.end = time.monotonic()
            self.elapsed = self.end - self.start
            self.status = RunStatus.FAILED
            return self._build_shell_process()

        except asyncio.CancelledError:
            try:
                await self._reap_process()
                await self._finish_readers(wait_timeout=0.5)
            finally:
                self.result = self._decoded_stdout()
                self.end = time.monotonic()
                self.elapsed = self.end - self.start
                self.status = RunStatus.CANCELLED
            raise

        except Exception as err:
            await self._reap_process()
            await self._finish_readers(wait_timeout=0.5)
            self.result = self._decoded_stdout()
            self.error = (
                f"Err. - Task Run - {self.run_id} - encountered error {str(err)}."
            )
            self.trace = traceback.format_exc()
            self.end = time.monotonic()
            self.elapsed = self.end - self.start
            self.status = RunStatus.FAILED
            return self._build_shell_process()

        finally:
            self._reader_tasks.clear()

        stdout = self._decoded_stdout()
        stderr = self._decoded_stderr()

        self.result = stdout
        if stderr:
            self.error = stderr

        self.end = time.monotonic()
        self.elapsed = self.end - self.start

        if self.return_code != 0:
            self.error = (
                f"Err. - Task Run - {self.run_id} - failed. "
                f"Encountered exception - {stderr}."
            )
            self.status = RunStatus.FAILED
        else:
            self.status = RunStatus.COMPLETE

        return self._build_shell_process()

    async def _execute(self, *args: Any, **kwargs: Any) -> TaskRun:
        loop = asyncio.get_running_loop()
        try:
            self.status = RunStatus.RUNNING

            if inspect.isawaitable(self.call) and not callable(self.call):
                if self.timeout is not None:
                    self.result = await asyncio.wait_for(
                        self.call,
                        timeout=self.timeout,
                    )
                else:
                    self.result = await self.call

            elif _is_coroutine_callable(self.call):
                coro = self.call(*args, **kwargs)
                if self.timeout is not None:
                    self.result = await asyncio.wait_for(
                        coro,
                        timeout=self.timeout,
                    )
                else:
                    self.result = await coro

            else:
                async with self._semaphore:
                    bound_call = functools.partial(
                        self.call,
                        *args,
                        **kwargs,
                    )
                    if self._executor is not None:
                        cf_fut = self._executor.submit(bound_call)
                        if self._active_executor_futures is not None:
                            self._active_executor_futures.add(cf_fut)
                            cf_fut.add_done_callback(
                                self._active_executor_futures.discard
                            )
                        fut: asyncio.Future[Any] = asyncio.wrap_future(
                            cf_fut,
                            loop=loop,
                        )
                    else:
                        fut = loop.run_in_executor(None, bound_call)

                    if self.timeout is not None:
                        self.result = await asyncio.wait_for(
                            fut,
                            timeout=self.timeout,
                        )
                    else:
                        self.result = await fut

            self.end = time.monotonic()
            self.elapsed = self.end - self.start
            self.status = RunStatus.COMPLETE

        except asyncio.TimeoutError:
            self.error = (
                f"Err. - Task Run - {self.run_id} - timed out. "
                f"Exceeded deadline of - {self.timeout} - seconds."
            )
            self.end = time.monotonic()
            self.elapsed = self.end - self.start
            self.status = RunStatus.FAILED

        except asyncio.CancelledError:
            self.end = time.monotonic()
            self.elapsed = self.end - self.start
            self.status = RunStatus.CANCELLED
            raise

        except Exception as e:
            self.error = (
                f"Err. - Task Run - {self.run_id} - failed. "
                f"Encountered exception - {str(e)}."
            )
            self.trace = traceback.format_exc()
            self.end = time.monotonic()
            self.elapsed = self.end - self.start
            self.status = RunStatus.FAILED

        return self._build_task_run()
