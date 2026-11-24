import asyncio
import concurrent.futures
import inspect
import pathlib
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    TypeVar,
)

from .checkpoint import CheckpointStore
from .models import Checkpoint, RunStatus, ShellProcess, TaskRun, TaskType
from .run import TERMINAL_STATUSES, Run, _maybe_await
from .snowflake import SnowflakeGenerator
from .util import TimeParser

T = TypeVar("T")


class Task(Generic[T]):
    def __init__(
        self,
        snowflake_generator: SnowflakeGenerator,
        name: str,
        task: Callable[..., T] | str,
        executor: ProcessPoolExecutor | ThreadPoolExecutor | None,
        semaphore: asyncio.Semaphore,
        *args: Any,
        schedule: int | float | str | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: Literal["NEVER", "ALWAYS"] | int = "NEVER",
        timeout: int | float | str | None = None,
        keep: int | None = None,
        max_age: int | float | str | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
        task_type: TaskType = TaskType.CALLABLE,
        active_executor_futures: set[concurrent.futures.Future[Any]] | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        if isinstance(repeat, bool) or (isinstance(repeat, int) and repeat <= 0):
            raise ValueError(
                "'repeat' must be a positive integer, 'NEVER', or 'ALWAYS'."
            )

        if keep is not None and keep < 0:
            raise ValueError("'keep' must be non-negative.")

        self._snowflake_generator = snowflake_generator
        self.task_id = snowflake_generator.generate()
        self.name: str = name
        self.args = args
        self.trigger: Literal["MANUAL", "ON_START"] = trigger
        self.repeat: Literal["NEVER", "ALWAYS"] | int = repeat

        self.schedule: int | float | None = None
        if isinstance(schedule, str):
            self.schedule = TimeParser(schedule).time
        elif isinstance(schedule, (int, float)):
            self.schedule = float(schedule)

        self.timeout: int | float | None = None
        if isinstance(timeout, str):
            self.timeout = TimeParser(timeout).time
        elif isinstance(timeout, (int, float)):
            self.timeout = float(timeout)

        self.keep: int = 10 if keep is None else keep

        self.max_age: float | None = None
        if isinstance(max_age, str):
            self.max_age = TimeParser(max_age).time
        elif isinstance(max_age, (int, float)):
            self.max_age = float(max_age)

        self.keep_policy = keep_policy

        self.call = task
        self.task_type = task_type

        self._runs: Dict[int, Run] = {}
        self._schedules: Dict[int, asyncio.Task[Any]] = {}
        self._schedule_running_statuses: Dict[int, bool] = {}
        self._schedule_stop_events: Dict[int, asyncio.Event] = {}
        self._schedule_resume_events: Dict[int, asyncio.Event] = {}
        self._schedule_paused_statuses: Dict[int, bool] = {}
        self._schedule_current_runs: Dict[int, Run] = {}

        self._sem = asyncio.Semaphore(max(1, self.keep))
        self._executor = executor
        self._executor_semaphore = semaphore
        self._active_executor_futures = active_executor_futures
        self._checkpoint_store = checkpoint_store

    @property
    def status(self) -> RunStatus:
        if run := self.latest():
            return run.status

        return RunStatus.IDLE

    async def get_run_update(self, run_id: int) -> ShellProcess | TaskRun:
        return await self._runs[run_id].get_run_update()

    def get_run_status(self, run_id: int) -> RunStatus | None:
        if run := self._runs.get(run_id):
            return run.status
        return None

    def latest(self) -> Run | None:
        if self._runs:
            latest_run_id = max(self._runs)
            return self._runs[latest_run_id]
        return None

    async def wait(self, run_id: int) -> ShellProcess | TaskRun:
        run = self._runs[run_id]
        return await run.wait()

    async def update(self, run_id: int, status: RunStatus) -> None:
        if run := self._runs.get(run_id):
            run.update_status(status)

    async def complete(self, run_id: int) -> ShellProcess | TaskRun | None:
        if run := self._runs.get(run_id):
            return await run.complete()
        return None

    async def cancel(self, run_id: int) -> None:
        if run := self._runs.get(run_id):
            await run.cancel()

    async def pause(self, run_id: int) -> bool:
        if run := self._runs.get(run_id):
            return await run.pause()
        return False

    async def checkpoint(
        self,
        run_id: int,
        state: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        run = self._runs[run_id]
        return await run.checkpoint(state, metadata=metadata)

    async def get_checkpoint(self, run_id: int) -> Checkpoint | None:
        if run := self._runs.get(run_id):
            return await run.get_checkpoint()
        if self._checkpoint_store is not None:
            return await _maybe_await(
                self._checkpoint_store.get_latest(self.name, run_id)
            )
        return None

    async def list_checkpoints(self, run_id: int) -> list[Checkpoint]:
        if run := self._runs.get(run_id):
            return await run.list_checkpoints()
        if self._checkpoint_store is not None:
            return await _maybe_await(
                self._checkpoint_store.list_checkpoints(self.name, run_id)
            )
        return []

    async def clear_checkpoints(self, run_id: int) -> None:
        if self._checkpoint_store is not None:
            await _maybe_await(
                self._checkpoint_store.delete(self.name, run_id)
            )
        if run := self._runs.get(run_id):
            run._latest_checkpoint = None

    async def resume(
        self,
        run_id: int,
        *args: Any,
        call: Callable[..., Any] | str | None = None,
        env: Dict[str, str] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool | None = None,
        timeout: int | float | None = None,
        poll_interval: int | float | None = None,
        **kwargs: Any,
    ) -> Run:
        existing = self._runs.get(run_id)
        if existing is not None and existing.paused and existing.task_running:
            await existing.resume()
            return existing

        cp = await self.get_checkpoint(run_id)

        if existing is not None and existing.task_type == TaskType.SHELL:
            effective_call = call if call is not None else existing.call
            effective_args = args if args else (existing._args or ())
            effective_env = env if env is not None else existing._env
            effective_cwd = cwd if cwd is not None else existing._working_directory
            effective_shell = shell if shell is not None else existing._shell
            effective_timeout = (
                timeout if timeout is not None else existing.timeout
            )
            effective_poll = (
                poll_interval
                if poll_interval is not None
                else existing._poll_interval
            )
            return self.run_shell(
                *effective_args,
                call=effective_call,
                env=effective_env,
                cwd=effective_cwd,
                shell=effective_shell,
                timeout=effective_timeout,
                poll_interval=effective_poll,
                initial_checkpoint=cp,
            )

        if self.task_type == TaskType.SHELL:
            effective_call = call if call is not None else self.call
            return self.run_shell(
                *args,
                call=effective_call,
                env=env,
                cwd=cwd,
                shell=bool(shell) if shell is not None else False,
                timeout=timeout,
                poll_interval=poll_interval if poll_interval is not None else 0.5,
                initial_checkpoint=cp,
            )

        effective_call = (
            call
            if call is not None
            else (existing.call if existing is not None else self.call)
        )
        if inspect.iscoroutine(effective_call):
            raise ValueError(
                "Cannot restart a finished raw coroutine object; pass a callable "
                "function or provide 'call=' to resume()."
            )

        if existing is not None:
            effective_args = args if args else existing._call_args
            effective_kwargs = dict(existing._call_kwargs)
            effective_kwargs.update(kwargs)
            effective_timeout = (
                timeout if timeout is not None else existing.timeout
            )
        else:
            effective_args = args
            effective_kwargs = dict(kwargs)
            effective_timeout = timeout

        return self.run(
            *effective_args,
            call=effective_call,
            timeout=effective_timeout,
            initial_checkpoint=cp,
            **effective_kwargs,
        )

    def _find_schedule_id(self, run_id: int) -> int | None:
        if run_id in self._schedules:
            return run_id

        for schedule_id, current_run in self._schedule_current_runs.items():
            if current_run.run_id == run_id:
                return schedule_id

        return None

    async def pause_schedule(self, run_id: int) -> bool:
        schedule_id = self._find_schedule_id(run_id)
        if schedule_id is None:
            return False

        self._schedule_paused_statuses[schedule_id] = True
        if resume_event := self._schedule_resume_events.get(schedule_id):
            resume_event.clear()

        current_run = self._schedule_current_runs.get(schedule_id)
        if current_run is not None and current_run.status not in TERMINAL_STATUSES:
            await current_run.pause()

        return True

    async def resume_schedule(self, run_id: int) -> bool:
        schedule_id = self._find_schedule_id(run_id)
        if schedule_id is None:
            return False

        self._schedule_paused_statuses[schedule_id] = False

        current_run = self._schedule_current_runs.get(schedule_id)
        if current_run is not None and current_run.paused:
            await current_run.resume()

        if resume_event := self._schedule_resume_events.get(schedule_id):
            resume_event.set()

        return True

    def _clear_schedule_state(self, schedule_id: int) -> None:
        self._schedules.pop(schedule_id, None)
        self._schedule_running_statuses.pop(schedule_id, None)
        self._schedule_stop_events.pop(schedule_id, None)
        self._schedule_resume_events.pop(schedule_id, None)
        self._schedule_paused_statuses.pop(schedule_id, None)
        self._schedule_current_runs.pop(schedule_id, None)

    async def cancel_schedule(self, run_id: int) -> None:
        schedule_id = self._find_schedule_id(run_id)
        if schedule_id is None:
            return

        self._schedule_running_statuses[schedule_id] = False
        self._schedule_paused_statuses[schedule_id] = False
        if stop_event := self._schedule_stop_events.get(schedule_id):
            stop_event.set()
        if resume_event := self._schedule_resume_events.get(schedule_id):
            resume_event.set()

        current_run = self._schedule_current_runs.get(schedule_id)
        schedule_task = self._schedules.get(schedule_id)

        if schedule_task is not None and not schedule_task.done():
            schedule_task.cancel()
            try:
                await schedule_task
            except (asyncio.CancelledError, Exception):
                pass

        if current_run is not None and current_run.status not in TERMINAL_STATUSES:
            await current_run.cancel()

        if (
            (run := self._runs.get(run_id)) is not None
            and run is not current_run
            and run.status not in TERMINAL_STATUSES
        ):
            await run.cancel()

        self._clear_schedule_state(schedule_id)

    async def shutdown(self) -> None:
        self.stop_schedules()

        schedule_tasks = list(self._schedules.values())
        for schedule_task in schedule_tasks:
            if not schedule_task.done():
                schedule_task.cancel()

        if schedule_tasks:
            await asyncio.gather(*schedule_tasks, return_exceptions=True)

        self._schedules.clear()
        self._schedule_running_statuses.clear()
        self._schedule_stop_events.clear()
        self._schedule_resume_events.clear()
        self._schedule_paused_statuses.clear()
        self._schedule_current_runs.clear()

        active_runs = [
            run
            for run in list(self._runs.values())
            if run.status not in TERMINAL_STATUSES
        ]
        if active_runs:
            await asyncio.gather(
                *(run.cancel() for run in active_runs),
                return_exceptions=True,
            )

    def abort(self) -> None:
        self.stop_schedules()

        for schedule_task in list(self._schedules.values()):
            if not schedule_task.done():
                schedule_task.cancel()

        self._schedules.clear()
        self._schedule_running_statuses.clear()
        self._schedule_stop_events.clear()
        self._schedule_resume_events.clear()
        self._schedule_paused_statuses.clear()
        self._schedule_current_runs.clear()

        for run in list(self._runs.values()):
            run.abort()

    async def cleanup(self) -> None:
        match self.keep_policy:
            case "COUNT":
                await self._execute_count_policy()

            case "AGE":
                await self._execute_age_policy()

            case "COUNT_AND_AGE":
                await self._execute_age_policy()
                await self._execute_count_policy()

            case _:
                pass

    async def _execute_count_policy(self) -> None:
        if self.keep is None:
            return

        removed_runs: List[Run] = []
        if len(self._runs) > self.keep:
            run_ids = sorted(self._runs)
            excess = len(run_ids) - self.keep
            for run_id in run_ids[:excess]:
                removed_runs.append(self._runs.pop(run_id))

        active_removed = [
            run for run in removed_runs if run.status not in TERMINAL_STATUSES
        ]
        if active_removed:
            await asyncio.gather(
                *(run.cancel() for run in active_removed),
                return_exceptions=True,
            )

    async def _execute_age_policy(self) -> None:
        if self.max_age is None:
            return

        removed_runs: List[Run] = []
        current_time = time.monotonic()
        for run_id, run in list(self._runs.items()):
            if current_time - run.start > self.max_age:
                removed_runs.append(self._runs.pop(run_id))

        active_removed = [
            run for run in removed_runs if run.status not in TERMINAL_STATUSES
        ]
        if active_removed:
            await asyncio.gather(
                *(run.cancel() for run in active_removed),
                return_exceptions=True,
            )

    def _create_run(
        self,
        run_id: int,
        task_type: TaskType,
        timeout: int | float | None,
        call: Callable[..., Any] | str | None = None,
        initial_checkpoint: Checkpoint | None = None,
    ) -> Run:
        return Run(
            run_id,
            self.name,
            self.call if call is None else call,
            task_type,
            self._executor,
            self._executor_semaphore,
            timeout=timeout,
            active_executor_futures=self._active_executor_futures,
            checkpoint_store=self._checkpoint_store,
            snowflake_generator=self._snowflake_generator,
            initial_checkpoint=initial_checkpoint,
        )

    def run_shell(
        self,
        *args: Any,
        call: str | None = None,
        env: Dict[str, str] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool = False,
        run_id: Optional[int] = None,
        timeout: Optional[int | float] = None,
        poll_interval: int | float = 0.5,
        initial_checkpoint: Checkpoint | None = None,
    ) -> Run:
        if timeout is None:
            timeout = self.timeout

        if run_id is None:
            run_id = self._snowflake_generator.generate()

        if (existing := self._runs.get(run_id)) is not None and existing.task_running:
            existing.abort()

        run = self._create_run(
            run_id,
            TaskType.SHELL,
            timeout,
            call=call,
            initial_checkpoint=initial_checkpoint,
        )
        run.execute_shell(
            *args,
            env=env,
            cwd=cwd,
            shell=shell,
            timeout=timeout,
            poll_interval=poll_interval,
        )

        self._runs[run.run_id] = run
        return run

    def run(
        self,
        *args: Any,
        call: Callable[..., Any] | None = None,
        run_id: Optional[int] = None,
        timeout: Optional[int | float] = None,
        initial_checkpoint: Checkpoint | None = None,
        **kwargs: Any,
    ) -> Run:
        if timeout is None:
            timeout = self.timeout

        if run_id is None:
            run_id = self._snowflake_generator.generate()

        if (existing := self._runs.get(run_id)) is not None and existing.task_running:
            existing.abort()

        run = self._create_run(
            run_id,
            TaskType.CALLABLE,
            timeout,
            call=call,
            initial_checkpoint=initial_checkpoint,
        )
        run.execute(*args, **kwargs)

        self._runs[run.run_id] = run
        return run

    def stop_schedules(self) -> None:
        for schedule_id in list(self._schedule_running_statuses):
            self._schedule_running_statuses[schedule_id] = False
            self._schedule_paused_statuses[schedule_id] = False

        for stop_event in list(self._schedule_stop_events.values()):
            stop_event.set()

        for resume_event in list(self._schedule_resume_events.values()):
            resume_event.set()

    def run_schedule(
        self,
        *args: Any,
        run_id: Optional[int] = None,
        timeout: Optional[int | float] = None,
        initial_checkpoint: Checkpoint | None = None,
        **kwargs: Any,
    ) -> Run | None:
        if self.schedule is None:
            raise ValueError(
                "A 'schedule' interval must be set before calling run_schedule()."
            )

        if run_id is None:
            run_id = self._snowflake_generator.generate()

        if timeout is None:
            timeout = self.timeout

        existing_schedule = self._schedules.get(run_id)
        if (
            existing_schedule is None or existing_schedule.done()
        ) and not self._schedule_running_statuses.get(run_id, False):
            run = self._create_run(
                run_id,
                TaskType.CALLABLE,
                timeout,
                initial_checkpoint=initial_checkpoint,
            )
            stop_event = asyncio.Event()
            resume_event = asyncio.Event()
            resume_event.set()

            self._runs[run_id] = run
            self._schedule_running_statuses[run_id] = True
            self._schedule_paused_statuses[run_id] = False
            self._schedule_stop_events[run_id] = stop_event
            self._schedule_resume_events[run_id] = resume_event
            self._schedule_current_runs[run_id] = run

            loop = asyncio.get_running_loop()
            self._schedules[run_id] = loop.create_task(
                self._run_schedule(
                    run_id,
                    run,
                    stop_event,
                    resume_event,
                    *args,
                    **kwargs,
                )
            )
            return run

        return self._schedule_current_runs.get(run_id) or self.latest()

    def run_shell_schedule(
        self,
        *args: Any,
        env: Dict[str, str] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool = False,
        run_id: Optional[int] = None,
        timeout: Optional[int | float] = None,
        poll_interval: int | float = 0.5,
        initial_checkpoint: Checkpoint | None = None,
    ) -> Run | None:
        if self.schedule is None:
            raise ValueError(
                "A 'schedule' interval must be set before calling run_shell_schedule()."
            )

        if run_id is None:
            run_id = self._snowflake_generator.generate()

        if timeout is None:
            timeout = self.timeout

        existing_schedule = self._schedules.get(run_id)
        if (
            existing_schedule is None or existing_schedule.done()
        ) and not self._schedule_running_statuses.get(run_id, False):
            run = self._create_run(
                run_id,
                TaskType.SHELL,
                timeout,
                initial_checkpoint=initial_checkpoint,
            )
            stop_event = asyncio.Event()
            resume_event = asyncio.Event()
            resume_event.set()

            self._runs[run_id] = run
            self._schedule_running_statuses[run_id] = True
            self._schedule_paused_statuses[run_id] = False
            self._schedule_stop_events[run_id] = stop_event
            self._schedule_resume_events[run_id] = resume_event
            self._schedule_current_runs[run_id] = run

            loop = asyncio.get_running_loop()
            self._schedules[run_id] = loop.create_task(
                self._run_shell_schedule(
                    run_id,
                    run,
                    stop_event,
                    resume_event,
                    *args,
                    env=env,
                    cwd=cwd,
                    shell=shell,
                    poll_interval=poll_interval,
                )
            )
            return run

        return self._schedule_current_runs.get(run_id) or self.latest()

    async def _wait_if_schedule_paused(
        self,
        stop_event: asyncio.Event,
        resume_event: asyncio.Event,
    ) -> bool:
        if stop_event.is_set():
            return True
        if resume_event.is_set():
            return False

        loop = asyncio.get_running_loop()
        stop_waiter = loop.create_task(stop_event.wait())
        resume_waiter = loop.create_task(resume_event.wait())
        try:
            await asyncio.wait(
                {stop_waiter, resume_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in (stop_waiter, resume_waiter):
                if not waiter.done():
                    waiter.cancel()
                    try:
                        await waiter
                    except asyncio.CancelledError:
                        pass

        return stop_event.is_set()

    async def _sleep_or_stop(
        self,
        stop_event: asyncio.Event,
        resume_event: asyncio.Event,
    ) -> bool:
        assert self.schedule is not None
        remaining = float(self.schedule)

        while True:
            if await self._wait_if_schedule_paused(stop_event, resume_event):
                return True

            if remaining <= 0:
                return False

            started = time.monotonic()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
                return True
            except asyncio.TimeoutError:
                remaining -= time.monotonic() - started
                if not resume_event.is_set():
                    continue
                return False

    async def _run_schedule(
        self,
        schedule_id: int,
        run: Run,
        stop_event: asyncio.Event,
        resume_event: asyncio.Event,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        current_run = run
        run_timeout = run.timeout
        run_call = run.call

        try:
            if self.repeat == "ALWAYS":
                first = True
                while self._schedule_running_statuses.get(schedule_id, False):
                    if await self._wait_if_schedule_paused(
                        stop_event, resume_event
                    ):
                        break
                    if not self._schedule_running_statuses.get(
                        schedule_id, False
                    ):
                        break

                    if not first:
                        current_run = self._create_run(
                            self._snowflake_generator.generate(),
                            TaskType.CALLABLE,
                            run_timeout,
                            call=run_call,
                        )
                        self._runs[current_run.run_id] = current_run
                        self._schedule_current_runs[schedule_id] = current_run
                    first = False

                    current_run.execute(*args, **kwargs)

                    stopped = await self._sleep_or_stop(
                        stop_event, resume_event
                    )
                    if stopped or not self._schedule_running_statuses.get(
                        schedule_id, False
                    ):
                        break

            elif isinstance(self.repeat, int):
                for iteration in range(self.repeat):
                    if await self._wait_if_schedule_paused(
                        stop_event, resume_event
                    ):
                        break
                    if not self._schedule_running_statuses.get(
                        schedule_id, False
                    ):
                        break

                    if iteration > 0:
                        current_run = self._create_run(
                            self._snowflake_generator.generate(),
                            TaskType.CALLABLE,
                            run_timeout,
                            call=run_call,
                        )
                        self._runs[current_run.run_id] = current_run
                        self._schedule_current_runs[schedule_id] = current_run

                    current_run.execute(*args, **kwargs)

                    if iteration < self.repeat - 1:
                        stopped = await self._sleep_or_stop(
                            stop_event, resume_event
                        )
                        if stopped:
                            break

        except asyncio.CancelledError:
            if current_run.status not in TERMINAL_STATUSES:
                await current_run.cancel()
            raise

        finally:
            self._clear_schedule_state(schedule_id)

    async def _run_shell_schedule(
        self,
        schedule_id: int,
        run: Run,
        stop_event: asyncio.Event,
        resume_event: asyncio.Event,
        *args: Any,
        env: Dict[str, str] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool = False,
        poll_interval: int | float = 0.5,
    ) -> None:
        current_run = run
        run_timeout = run.timeout
        run_call = run.call

        try:
            if self.repeat == "ALWAYS":
                first = True
                while self._schedule_running_statuses.get(schedule_id, False):
                    if await self._wait_if_schedule_paused(
                        stop_event, resume_event
                    ):
                        break
                    if not self._schedule_running_statuses.get(
                        schedule_id, False
                    ):
                        break

                    if not first:
                        current_run = self._create_run(
                            self._snowflake_generator.generate(),
                            TaskType.SHELL,
                            run_timeout,
                            call=run_call,
                        )
                        self._runs[current_run.run_id] = current_run
                        self._schedule_current_runs[schedule_id] = current_run
                    first = False

                    current_run.execute_shell(
                        *args,
                        env=env,
                        cwd=cwd,
                        shell=shell,
                        timeout=run_timeout,
                        poll_interval=poll_interval,
                    )

                    stopped = await self._sleep_or_stop(
                        stop_event, resume_event
                    )
                    if stopped or not self._schedule_running_statuses.get(
                        schedule_id, False
                    ):
                        break

            elif isinstance(self.repeat, int):
                for iteration in range(self.repeat):
                    if await self._wait_if_schedule_paused(
                        stop_event, resume_event
                    ):
                        break
                    if not self._schedule_running_statuses.get(
                        schedule_id, False
                    ):
                        break

                    if iteration > 0:
                        current_run = self._create_run(
                            self._snowflake_generator.generate(),
                            TaskType.SHELL,
                            run_timeout,
                            call=run_call,
                        )
                        self._runs[current_run.run_id] = current_run
                        self._schedule_current_runs[schedule_id] = current_run

                    current_run.execute_shell(
                        *args,
                        env=env,
                        cwd=cwd,
                        shell=shell,
                        timeout=run_timeout,
                        poll_interval=poll_interval,
                    )

                    if iteration < self.repeat - 1:
                        stopped = await self._sleep_or_stop(
                            stop_event, resume_event
                        )
                        if stopped:
                            break

        except asyncio.CancelledError:
            if current_run.status not in TERMINAL_STATUSES:
                await current_run.cancel()
            raise

        finally:
            self._clear_schedule_state(schedule_id)

    async def _run(self, run: Run, *args: Any, **kwargs: Any) -> ShellProcess | TaskRun:
        run.update_status(RunStatus.PENDING)

        async with self._sem:
            run.update_status(RunStatus.RUNNING)
            run.execute(*args, **kwargs)
            return await run.wait()
