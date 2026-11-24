import threading
import warnings

import pytest

from taskex.snowflake import SnowflakeGenerator
from taskex.snowflake.constants import MAX_INSTANCE, MAX_SEQ
from taskex.snowflake.snowflake import Snowflake


def test_snowflake_generator_never_returns_none_under_high_burst() -> None:
    gen = SnowflakeGenerator(instance=1)
    # Exceed MAX_SEQ (4095) in rapid succession
    ids = [gen.generate() for _ in range(10_000)]

    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def test_snowflake_generator_handles_backward_clock_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_time = [1_700_000_000.0]
    monkeypatch.setattr(
        "taskex.snowflake.snowflake_generator.time",
        lambda: fake_time[0],
    )

    gen = SnowflakeGenerator(instance=5)
    first = gen.generate()

    # Move wall clock backwards by 10 seconds
    fake_time[0] -= 10.0
    second = gen.generate()
    third = gen.generate()

    assert isinstance(first, int)
    assert isinstance(second, int)
    assert isinstance(third, int)
    assert first < second < third


def test_snowflake_from_snowflake_and_epoch_roundtrip() -> None:
    epoch = 1_600_000_000_000
    gen = SnowflakeGenerator(instance=42, epoch=epoch)
    first_id = gen.generate()
    parsed = Snowflake.parse(first_id, epoch=epoch)

    assert parsed.instance == 42
    assert parsed.epoch == epoch
    assert int(parsed) == first_id

    resumed = SnowflakeGenerator.from_snowflake(parsed)
    next_id = resumed.generate()
    assert next_id > first_id
    assert Snowflake.parse(next_id, epoch=epoch).instance == 42


def test_snowflake_iterator_protocol() -> None:
    gen = SnowflakeGenerator(instance=0)
    assert iter(gen) is gen
    first = next(gen)
    second = next(gen)
    assert isinstance(first, int)
    assert second > first


def test_snowflake_datetime_no_deprecation_warning() -> None:
    gen = SnowflakeGenerator(instance=0)
    sf = Snowflake.parse(gen.generate())

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dt = sf.datetime

    assert dt is not None
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_snowflake_validation() -> None:
    with pytest.raises(ValueError):
        SnowflakeGenerator(instance=-1)
    with pytest.raises(ValueError):
        SnowflakeGenerator(instance=MAX_INSTANCE + 1)
    with pytest.raises(ValueError):
        SnowflakeGenerator(instance=0, seq=MAX_SEQ + 1)
    with pytest.raises(ValueError):
        SnowflakeGenerator(instance=0, epoch=-1)


def test_snowflake_thread_safety() -> None:
    gen = SnowflakeGenerator(instance=7)
    results: list[list[int]] = [[] for _ in range(4)]

    def worker(slot: int) -> None:
        results[slot] = [gen.generate() for _ in range(500)]

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_ids = [item for sub in results for item in sub]
    assert len(all_ids) == 2000
    assert len(set(all_ids)) == 2000
