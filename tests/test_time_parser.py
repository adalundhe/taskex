import pytest

from taskex.util import TimeParser


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0s", 0.0),
        ("1s", 1.0),
        ("0.5s", 0.5),
        ("5", 5.0),
        ("2.5", 2.5),
        ("100ms", 0.1),
        ("500MS", 0.5),
        ("2m", 120.0),
        ("1h", 3600.0),
        ("1d", 86400.0),
        ("1w", 604800.0),
        ("1h30m", 5400.0),
        ("1h 30m 15s", 5415.0),
        ("1m 500ms", 60.5),
        ("10s 5s", 15.0),
        ("  2m  30s  ", 150.0),
    ],
)
def test_time_parser_valid_durations(raw: str, expected: float) -> None:
    assert TimeParser(raw).time == pytest.approx(expected)


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "   ",
        "abc",
        "10x",
        "-5s",
        "1m 2q",
    ],
)
def test_time_parser_rejects_invalid_durations(invalid: str) -> None:
    with pytest.raises(ValueError):
        TimeParser(invalid)
