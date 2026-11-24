import time
from typing import Any, Optional

from pydantic import (
    BaseModel,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
)

from .run_status import RunStatus
from .task_type import TaskType


class TaskRun(BaseModel):
    run_id: StrictInt
    task_name: StrictStr
    status: RunStatus
    error: Optional[StrictStr] = None
    trace: Optional[StrictStr] = None
    start: StrictInt | StrictFloat = Field(default_factory=time.monotonic)
    end: Optional[StrictInt | StrictFloat] = None
    elapsed: StrictInt | StrictFloat = 0
    result: Optional[Any] = None
    task_type: TaskType = TaskType.CALLABLE

    def complete(self) -> bool:
        return self.status in (
            RunStatus.COMPLETE,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        )
