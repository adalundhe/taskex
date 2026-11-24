import asyncio
import concurrent.futures
import functools
import pathlib
import shlex
import signal
import weakref
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    Literal,
    Optional,
    TypeVar,
)

from .env import Env
from .models import RunStatus, ShellProcess, TaskRun, TaskType
from .run import Run
from .snowflake import SnowflakeGenerator
from .task import Task
from .util.time_parser import TimeParser

T = TypeVar("T")

_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_ACTIVE_RUNNERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.WeakSet["TaskRunner"]
] = weakref.WeakKeyDictionary()
_SAVED_SIGNAL_HANDLERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[int, Any]
] = weakref.WeakKeyDictionary()


def _get_callable_name(call: Any) -> str:
    target = call
    while isinstance(target, functools.partial):
        target = target.func
    return getattr(target, "__name__", type(target).__name__)


def shutdown_executor(
    sig: int,
    executor: ThreadPoolExecutor | ProcessPoolExecutor | None,
    default_handler: Callable[..., Any] | int | None,
) -> None:
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)

    if default_handler is not None:
        try:
            signal.signal(sig, default_handler)
        except (ValueError, OSError):
            pass


def _cleanup_signal_handlers_if_empty(
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop],
) -> None:
    loop = loop_ref()
    if loop is None:
        return

    runners = _ACTIVE_RUNNERS.get(loop)
    if runners is not None and any(True for _ in runners):
        return

    _ACTIVE_RUNNERS.pop(loop, None)
    saved = _SAVED_SIGNAL_HANDLERS.pop(loop, {})
    for sig, prev_handler in saved.items():
        if not loop.is_closed():
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError, RuntimeError):
                pass
        try:
            signal.signal(sig, prev_handler)
        except (ValueError, OSError):
            pass


def _handle_loop_signal(
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop],
    sig: int,
) -> None:
    loop = loop_ref()
    if loop is None:
        return

    runners = list(_ACTIVE_RUNNERS.get(loop, ()))
    for runner in runners:
        if runner._executor is not None:
            try:
                runner._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            runner._executor = None

    saved = _SAVED_SIGNAL_HANDLERS.get(loop)
    prev_handler = saved.pop(sig, signal.SIG_DFL) if saved is not None else signal.SIG_DFL

    try:
        loop.remove_signal_handler(sig)
    except (NotImplementedError, ValueError, RuntimeError):
        pass

    try:
        signal.signal(sig, prev_handler)
    except (ValueError, OSError):
        pass

    signal.raise_signal(sig)


class TaskRunner:
    def __init__(
        self,
        instance_id: int | None = None,
        config: Env | None = None,
    ) -> None:
        if instance_id is None:
            instance_id = 0

        if config is None:
            config = Env()

        self.tasks: Dict[str, Task[Any]] = {}
        self.results: Dict[str, Any] = {}
        self._cleanup_interval = TimeParser(
            config.MERCURY_SYNC_CLEANUP_INTERVAL
        ).time
        self._cleanup_task: Optional[asyncio.Task[Any]] = None
        self._run_cleanup: bool = False
        self._snowflake_generator = SnowflakeGenerator(instance_id)

        self._executor: ThreadPoolExecutor | ProcessPoolExecutor | None = None
        if config.MERCURY_SYNC_EXECUTOR_TYPE == "thread":
            self._executor = ThreadPoolExecutor(
                max_workers=config.MERCURY_SYNC_TASK_RUNNER_MAX_THREADS
            )

        elif config.MERCURY_SYNC_EXECUTOR_TYPE == "process":
            self._executor = ProcessPoolExecutor(
                max_workers=config.MERCURY_SYNC_TASK_RUNNER_MAX_THREADS
            )

        self._executor_semaphore = asyncio.Semaphore(
            value=config.MERCURY_SYNC_TASK_RUNNER_MAX_THREADS
        )
        self._active_executor_futures: set[concurrent.futures.Future[Any]] = set()
        self._signal_loop: asyncio.AbstractEventLoop | None = None
        self._signal_finalizer: weakref.finalize | None = None
        self._is_shutdown: bool = False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self._register_signal_handlers(loop)

    async def __aenter__(self) -> "TaskRunner":
        self._ensure_loop_setup()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.shutdown()

    def _ensure_loop_setup(self) -> None:
        if self._signal_loop is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._register_signal_handlers(loop)

    def _register_signal_handlers(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        if self._signal_loop is not None:
            return

        runners = _ACTIVE_RUNNERS.get(loop)
        if runners is None:
            runners = weakref.WeakSet()
            _ACTIVE_RUNNERS[loop] = runners
            saved: dict[int, Any] = {}
            loop_ref = weakref.ref(loop)

            for sig in _HANDLED_SIGNALS:
                try:
                    prev_handler = signal.getsignal(sig)
                    if prev_handler == signal.SIG_IGN:
                        continue
                    loop.add_signal_handler(
                        sig,
                        functools.partial(_handle_loop_signal, loop_ref, sig),
                    )
                    saved[sig] = prev_handler
                except (NotImplementedError, ValueError, RuntimeError):
                    pass

            _SAVED_SIGNAL_HANDLERS[loop] = saved

        runners.add(self)
        self._signal_loop = loop
        self._signal_finalizer = weakref.finalize(
            self,
            _cleanup_signal_handlers_if_empty,
            weakref.ref(loop),
        )

    def _unregister_signal_handlers(self) -> None:
        loop = self._signal_loop
        if loop is None:
            return
        self._signal_loop = None

        if self._signal_finalizer is not None:
            self._signal_finalizer.detach()
            self._signal_finalizer = None

        runners = _ACTIVE_RUNNERS.get(loop)
        if runners is not None:
            runners.discard(self)

        _cleanup_signal_handlers_if_empty(weakref.ref(loop))

    @staticmethod
    def _parse_token(token: str) -> tuple[str, int]:
        try:
            task_name, run_id_str = token.rsplit(":", maxsplit=1)
            return task_name, int(run_id_str)
        except ValueError as err:
            raise ValueError(f"Invalid run token: {token!r}") from err

    def all_tasks(self) -> Iterator[Task[Any]]:
        for task in list(self.tasks.values()):
            yield task

    def start_cleanup(self) -> None:
        self._ensure_loop_setup()
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return

        self._run_cleanup = True
        loop = asyncio.get_running_loop()
        self._cleanup_task = loop.create_task(self._cleanup())

    def create_task_id(self) -> int:
        return self._snowflake_generator.generate()

    def bundle(
        self,
        call: Callable[..., Awaitable[T]] | Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> functools.partial[Any]:
        return functools.partial(
            call,
            *args,
            **kwargs,
        )

    def run(
        self,
        call: Callable[..., Awaitable[T]] | Callable[..., T],
        *args: Any,
        alias: str | None = None,
        run_id: int | None = None,
        timeout: str | int | float | None = None,
        schedule: str | int | float | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: Literal["NEVER", "ALWAYS"] | int = "NEVER",
        keep: int | None = None,
        max_age: str | int | float | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
        **kwargs: Any,
    ) -> Run:
        if self._is_shutdown:
            raise RuntimeError(
                "Cannot submit new tasks after TaskRunner has been shut down."
            )

        if isinstance(timeout, str):
            timeout = TimeParser(timeout).time

        if repeat != "NEVER" and schedule is None:
            raise ValueError(
                "A 'schedule' interval must be provided when 'repeat' is not 'NEVER'."
            )

        if self._cleanup_task is None or self._cleanup_task.done():
            self.start_cleanup()

        command_name = alias if alias is not None else _get_callable_name(call)

        task = self.tasks.get(command_name)
        if task is None and call is not None:
            task = Task(
                self._snowflake_generator,
                command_name,
                call,
                self._executor,
                self._executor_semaphore,
                schedule=schedule,
                trigger=trigger,
                repeat=repeat,
                timeout=timeout,
                keep=keep,
                max_age=max_age,
                keep_policy=keep_policy,
                active_executor_futures=self._active_executor_futures,
            )
            self.tasks[command_name] = task
        elif task is not None and call is not None:
            task.call = call

        if task is None:
            raise ValueError("A callable must be provided to run().")

        if task.repeat == "NEVER":
            return task.run(
                *args,
                **kwargs,
                run_id=run_id,
                timeout=timeout,
            )

        if task.schedule is not None:
            scheduled_run = task.run_schedule(
                *args,
                **kwargs,
                run_id=run_id,
                timeout=timeout,
            )
            assert scheduled_run is not None
            return scheduled_run

        raise ValueError(
            "A 'schedule' interval must be provided when 'repeat' is not 'NEVER'."
        )

    def command(
        self,
        command: str,
        *args: Any,
        alias: str | None = None,
        env: dict[str, Any] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool = False,
        run_id: int | None = None,
        timeout: str | int | float | None = None,
        schedule: str | int | float | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: Literal["NEVER", "ALWAYS"] | int = "NEVER",
        keep: int | None = None,
        max_age: str | int | float | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
    ) -> Run:
        if self._is_shutdown:
            raise RuntimeError(
                "Cannot submit new tasks after TaskRunner has been shut down."
            )

        if isinstance(timeout, str):
            timeout = TimeParser(timeout).time

        if repeat != "NEVER" and schedule is None:
            raise ValueError(
                "A 'schedule' interval must be provided when 'repeat' is not 'NEVER'."
            )

        if self._cleanup_task is None or self._cleanup_task.done():
            self.start_cleanup()

        command_name = alias if alias is not None else command

        quoted_args: tuple[Any, ...] = args
        if shell:
            quoted_args = tuple(shlex.quote(str(arg)) for arg in args)

        task = self.tasks.get(command_name)
        if task is None:
            task = Task(
                self._snowflake_generator,
                command_name,
                command,
                self._executor,
                self._executor_semaphore,
                schedule=schedule,
                trigger=trigger,
                repeat=repeat,
                timeout=timeout,
                keep=keep,
                max_age=max_age,
                keep_policy=keep_policy,
                task_type=TaskType.SHELL,
                active_executor_futures=self._active_executor_futures,
            )
            self.tasks[command_name] = task
        else:
            task.call = command

        if task.repeat == "NEVER":
            return task.run_shell(
                *quoted_args,
                env=env,
                cwd=cwd,
                shell=shell,
                run_id=run_id,
                timeout=timeout,
                poll_interval=self._cleanup_interval,
            )

        if task.schedule is not None:
            scheduled_run = task.run_shell_schedule(
                *quoted_args,
                env=env,
                cwd=cwd,
                shell=shell,
                run_id=run_id,
                timeout=timeout,
                poll_interval=self._cleanup_interval,
            )
            assert scheduled_run is not None
            return scheduled_run

        raise ValueError(
            "A 'schedule' interval must be provided when 'repeat' is not 'NEVER'."
        )

    async def wait_all(self, tokens: list[str]) -> list[ShellProcess | TaskRun]:
        return list(
            await asyncio.gather(
                *(self.wait(token) for token in tokens),
            )
        )

    async def wait(self, token: str) -> ShellProcess | TaskRun:
        task_name, run_id = self._parse_token(token)
        return await self.tasks[task_name].wait(run_id)

    async def get_task_update(self, token: str) -> ShellProcess | TaskRun:
        task_name, run_id = self._parse_token(token)
        return await self.tasks[task_name].get_run_update(run_id)

    def stop_schedules(
        self,
        task_name: str,
    ) -> None:
        task = self.tasks.get(task_name)
        if task:
            task.stop_schedules()

    def get_task_status(self, task_name: str) -> RunStatus | None:
        if task := self.tasks.get(task_name):
            return task.status
        return None

    def get_run_status(self, token: str) -> RunStatus | None:
        task_name, run_id = self._parse_token(token)

        if task := self.tasks.get(task_name):
            return task.get_run_status(run_id)
        return None

    async def complete(self, token: str) -> ShellProcess | TaskRun | None:
        task_name, run_id = self._parse_token(token)

        if task := self.tasks.get(task_name):
            return await task.complete(run_id)
        return None

    async def cancel(self, token: str) -> None:
        task_name, run_id = self._parse_token(token)

        task = self.tasks.get(task_name)
        if task:
            await task.cancel(run_id)

    async def cancel_schedule(
        self,
        token: str,
    ) -> None:
        task_name, run_id = self._parse_token(token)

        task = self.tasks.get(task_name)
        if task:
            await task.cancel_schedule(run_id)

    async def stop(self) -> None:
        for task in list(self.tasks.values()):
            await task.shutdown()

    async def shutdown(self) -> None:
        self._is_shutdown = True
        self._run_cleanup = False

        if self._cleanup_task is not None:
            cleanup_task = self._cleanup_task
            self._cleanup_task = None
            if not cleanup_task.done():
                cleanup_task.cancel()
            try:
                await cleanup_task
            except (asyncio.CancelledError, Exception):
                pass

        for task in list(self.tasks.values()):
            await task.shutdown()

        self._unregister_signal_handlers()

        if self._executor is not None:
            executor = self._executor
            self._executor = None
            has_running_futures = any(
                not fut.done() for fut in list(self._active_executor_futures)
            )
            try:
                if has_running_futures:
                    executor.shutdown(wait=False, cancel_futures=True)
                else:
                    await asyncio.to_thread(
                        executor.shutdown,
                        wait=True,
                        cancel_futures=True,
                    )
            except Exception:
                pass

    def abort(self) -> None:
        self._is_shutdown = True
        self._run_cleanup = False

        if self._cleanup_task is not None:
            cleanup_task = self._cleanup_task
            self._cleanup_task = None
            if not cleanup_task.done():
                cleanup_task.cancel()

        for task in list(self.tasks.values()):
            task.abort()

        self._unregister_signal_handlers()

        if self._executor is not None:
            executor = self._executor
            self._executor = None
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    async def _cleanup(self) -> None:
        try:
            while self._run_cleanup:
                await asyncio.sleep(self._cleanup_interval)
                if not self._run_cleanup:
                    break
                await self._cleanup_scheduled_tasks()
        except asyncio.CancelledError:
            pass

    async def _cleanup_scheduled_tasks(self) -> None:
        for task in list(self.tasks.values()):
            try:
                await task.cleanup()
            except Exception:
                pass
