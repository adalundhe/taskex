from enum import Enum
from typing import Literal


class RunStatus(Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


RunStatusName = Literal[
    "CREATED",
    "PENDING",
    "IDLE",
    "RUNNING",
    "PAUSED",
    "COMPLETE",
    "CANCELLED",
    "FAILED",
]
