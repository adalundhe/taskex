import threading
from time import time
from typing import Optional

from .constants import MAX_INSTANCE, MAX_SEQ
from .snowflake import Snowflake


class SnowflakeGenerator:
    def __init__(
        self,
        instance: int,
        *,
        seq: int = 0,
        epoch: int = 0,
        timestamp: Optional[int] = None,
    ):
        if not 0 <= instance <= MAX_INSTANCE:
            raise ValueError(f"instance must be between 0 and {MAX_INSTANCE}")

        if not 0 <= seq <= MAX_SEQ:
            raise ValueError(f"seq must be between 0 and {MAX_SEQ}")

        if epoch < 0:
            raise ValueError("epoch must be non-negative")

        self._epoch = epoch
        current = int(time() * 1000) - self._epoch
        self._ts = current if timestamp is None else timestamp
        self._inf = instance << 12
        self._seq = seq
        self._lock = threading.Lock()

    @classmethod
    def from_snowflake(cls, sf: Snowflake) -> "SnowflakeGenerator":
        return cls(sf.instance, seq=sf.seq, epoch=sf.epoch, timestamp=sf.timestamp)

    def __iter__(self) -> "SnowflakeGenerator":
        return self

    def __next__(self) -> int:
        return self.generate()

    def generate(self) -> int:
        with self._lock:
            current = int(time() * 1000) - self._epoch

            if current <= self._ts:
                if self._seq >= MAX_SEQ:
                    self._ts += 1
                    self._seq = 0
                else:
                    self._seq += 1
            else:
                self._ts = current
                self._seq = 0

            return self._ts << 22 | self._inf | self._seq
