import time

import pytest
from pydantic import ValidationError

from taskex import Env, RunStatus, ShellProcess, TaskRun


def test_env_defaults_and_validation() -> None:
    env = Env()
    assert env.MERCURY_SYNC_TASK_RUNNER_MAX_THREADS >= 1
    assert "MERCURY_SYNC_MAX_RUNNING_WORKFLOWS" in Env.types_map()
    assert "MERCURY_SYNC_MAX_PENDING_WORKFLOWS" in Env.types_map()

    with pytest.raises(ValidationError):
        Env(MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=0)


def test_model_start_default_factory_is_per_instance() -> None:
    tr1 = TaskRun(run_id=1, task_name="t", status=RunStatus.CREATED)
    sp1 = ShellProcess(run_id=1, task_name="t", command="echo", status=RunStatus.CREATED)
    time.sleep(0.01)
    tr2 = TaskRun(run_id=2, task_name="t", status=RunStatus.CREATED)
    sp2 = ShellProcess(run_id=2, task_name="t", command="echo", status=RunStatus.CREATED)

    assert tr2.start > tr1.start
    assert sp2.start > sp1.start


def test_shell_process_allows_none_process_id() -> None:
    sp = ShellProcess(
        run_id=1,
        task_name="missing",
        process_id=None,
        command="no_such_cmd",
        status=RunStatus.FAILED,
        error="not found",
    )
    assert sp.process_id is None
    assert sp.complete() is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.CREATED, False),
        (RunStatus.PENDING, False),
        (RunStatus.IDLE, False),
        (RunStatus.RUNNING, False),
        (RunStatus.PAUSED, False),
        (RunStatus.COMPLETE, True),
        (RunStatus.FAILED, True),
        (RunStatus.CANCELLED, True),
    ],
)
def test_complete_helper_statuses(status: RunStatus, expected: bool) -> None:
    tr = TaskRun(run_id=1, task_name="t", status=status)
    sp = ShellProcess(run_id=1, task_name="t", command="ls", status=status)
    assert tr.complete() is expected
    assert sp.complete() is expected
