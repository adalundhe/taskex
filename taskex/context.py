from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any, Optional

from .models import Checkpoint

if TYPE_CHECKING:
    from .run import Run


class RunContext:
    """Execution context exposed to running tasks via ContextVar helpers."""

    __slots__ = ("_run",)

    def __init__(self, run: Run) -> None:
        self._run = run

    @property
    def run_id(self) -> int:
        return self._run.run_id

    @property
    def task_name(self) -> str:
        return self._run.task_name

    @property
    def token(self) -> str:
        return self._run.token

    @property
    def is_paused(self) -> bool:
        return self._run.paused

    @property
    def is_cancelled(self) -> bool:
        return self._run.cancelled or self._run._cancel_requested

    def last_checkpoint(self) -> Optional[Checkpoint]:
        return self._run.latest_checkpoint

    async def checkpoint(
        self,
        state: Any,
        *,
        metadata: dict[str, Any] | None = None,
        check_pause: bool = True,
    ) -> Checkpoint:
        cp = await self._run.checkpoint(state, metadata=metadata)
        if check_pause:
            await self._run.pause_point()
        return cp

    def checkpoint_sync(
        self,
        state: Any,
        *,
        metadata: dict[str, Any] | None = None,
        check_pause: bool = True,
    ) -> Checkpoint:
        cp = self._run.checkpoint_sync(state, metadata=metadata)
        if check_pause:
            self._run.pause_point_sync()
        return cp

    async def pause_point(self) -> None:
        await self._run.pause_point()

    def pause_point_sync(self) -> None:
        self._run.pause_point_sync()


_CURRENT_RUN_CONTEXT: contextvars.ContextVar[Optional[RunContext]] = (
    contextvars.ContextVar("taskex_current_run_context", default=None)
)


def get_run_context() -> Optional[RunContext]:
    """Return the active RunContext for the current taskex run, if any."""
    return _CURRENT_RUN_CONTEXT.get()


def last_checkpoint() -> Optional[Checkpoint]:
    """Return the latest Checkpoint for the current run (or resumed checkpoint)."""
    ctx = _CURRENT_RUN_CONTEXT.get()
    if ctx is None:
        return None
    return ctx.last_checkpoint()


async def checkpoint(
    state: Any,
    *,
    metadata: dict[str, Any] | None = None,
    check_pause: bool = True,
) -> Checkpoint:
    """Save a checkpoint from inside an async taskex task."""
    ctx = _CURRENT_RUN_CONTEXT.get()
    if ctx is None:
        raise RuntimeError(
            "checkpoint() must be called from inside an active taskex Run."
        )
    return await ctx.checkpoint(state, metadata=metadata, check_pause=check_pause)


def checkpoint_sync(
    state: Any,
    *,
    metadata: dict[str, Any] | None = None,
    check_pause: bool = True,
) -> Checkpoint:
    """Save a checkpoint from inside a sync (thread-pool) taskex task."""
    ctx = _CURRENT_RUN_CONTEXT.get()
    if ctx is None:
        raise RuntimeError(
            "checkpoint_sync() must be called from inside an active taskex Run."
        )
    return ctx.checkpoint_sync(state, metadata=metadata, check_pause=check_pause)


async def pause_point() -> None:
    """Cooperative async pause point. Suspends if the current run is paused."""
    ctx = _CURRENT_RUN_CONTEXT.get()
    if ctx is not None:
        await ctx.pause_point()


def pause_point_sync() -> None:
    """Cooperative sync pause point for thread-pool tasks."""
    ctx = _CURRENT_RUN_CONTEXT.get()
    if ctx is not None:
        ctx.pause_point_sync()
