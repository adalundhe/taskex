import time
from typing import Any, Dict, Literal, Optional, Tuple

from pydantic import (
    BaseModel,
    Field,
    StrictBytes,
    StrictFloat,
    StrictInt,
    StrictStr,
)

from .checkpoint import Checkpoint
from .run_status import RunStatus
from .task_type import TaskType

CommandType = Literal["shell", "subprocess"]


class ShellProcess(BaseModel):
    run_id: StrictInt
    task_name: StrictStr
    process_id: StrictInt | None = None
    command: StrictStr
    status: RunStatus
    args: Tuple[str, ...] | None = None
    return_code: StrictInt | None = None
    env: Dict[str, Any] | None = None
    working_directory: StrictStr | None = None
    command_type: CommandType = "subprocess"
    error: StrictStr | StrictBytes | None = None
    trace: StrictStr | StrictBytes | None = None
    start: StrictInt | StrictFloat = Field(default_factory=time.monotonic)
    end: StrictInt | StrictFloat | None = None
    elapsed: StrictInt | StrictFloat = 0
    paused_duration: StrictInt | StrictFloat = 0.0
    result: StrictStr | StrictBytes | None = None
    checkpoint: Optional[Checkpoint] = None
    task_type: TaskType = TaskType.SHELL

    def complete(self) -> bool:
        return self.status in (
            RunStatus.COMPLETE,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        )
