from enum import Enum


class TaskStatus(Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
