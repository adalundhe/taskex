import time
from typing import Any, Dict

from pydantic import BaseModel, Field, StrictFloat, StrictInt, StrictStr

from .run_status import RunStatus


class Checkpoint(BaseModel):
    checkpoint_id: StrictInt
    run_id: StrictInt
    task_name: StrictStr
    sequence: StrictInt = Field(default=1, ge=1)
    state: Any = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: StrictFloat = Field(default_factory=time.time)
    status: RunStatus = RunStatus.RUNNING
