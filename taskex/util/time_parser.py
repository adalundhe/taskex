import re
from collections import defaultdict
from datetime import timedelta

_FULL_PATTERN = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?\s*(?:ms|[smhdw])?\s*)+$",
    flags=re.IGNORECASE,
)
_TOKEN_PATTERN = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>ms|[smhdw])?",
    flags=re.IGNORECASE,
)


class TimeParser:
    UNITS = {
        "ms": "milliseconds",
        "s": "seconds",
        "m": "minutes",
        "h": "hours",
        "d": "days",
        "w": "weeks",
    }

    def __init__(self, time_amount: str) -> None:
        if not isinstance(time_amount, str) or not _FULL_PATTERN.match(time_amount):
            raise ValueError(f"Invalid duration string: {time_amount!r}")

        totals: dict[str, float] = defaultdict(float)
        for match in _TOKEN_PATTERN.finditer(time_amount):
            unit_key = (match.group("unit") or "s").lower()
            unit_name = self.UNITS[unit_key]
            totals[unit_name] += float(match.group("val"))

        self.time: float = float(timedelta(**totals).total_seconds())
