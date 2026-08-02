"""Unit tests for the custom LRU cache decorator."""

from __future__ import annotations

import pytest

from lru_cache_decorator import CacheInfo, lru_cache


def test_repeated_call_hits_cache() -> None:
    calls = 0

    @lru_cache(maxsize=2)
    def add(a: int, b: int) -> int:
        nonlocal calls
        calls += 1
        return a + b

    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert calls == 1
    assert add.cache_info() == CacheInfo(1, 1, 2, 1)


def test_access_refreshes_entry_before_lru_eviction() -> None:
    calls: list[int] = []

    @lru_cache(maxsize=2)
    def identity(value: int) -> int:
        calls.append(value)
        return value

    identity(1)
    identity(2)
    identity(1)  # 1 becomes the most recently used entry.
    identity(3)  # 2 is therefore evicted.

    assert identity(1) == 1  # Still cached.
    assert identity(2) == 2  # Recomputed after eviction.
    assert calls == [1, 2, 3, 2]


def test_keyword_order_does_not_change_cache_key() -> None:
    calls = 0

    @lru_cache(maxsize=4)
    def join(*, left: str, right: str) -> str:
        nonlocal calls
        calls += 1
        return left + right

    assert join(left="a", right="b") == "ab"
    assert join(right="b", left="a") == "ab"
    assert calls == 1


def test_positional_and_keyword_call_styles_are_distinct() -> None:
    calls = 0

    @lru_cache(maxsize=4)
    def add(a: int, b: int = 0) -> int:
        nonlocal calls
        calls += 1
        return a + b

    assert add(1, 2) == 3
    assert add(1, b=2) == 3
    assert calls == 2


def test_none_maxsize_creates_unbounded_cache() -> None:
    @lru_cache(maxsize=None)
    def square(value: int) -> int:
        return value * value

    for value in range(100):
        assert square(value) == value * value
    for value in range(100):
        assert square(value) == value * value

    assert square.cache_info() == CacheInfo(100, 100, None, 100)
    assert square.cache_parameters() == {"maxsize": None}


def test_cache_clear_removes_entries_and_resets_statistics() -> None:
    calls = 0

    @lru_cache(maxsize=2)
    def double(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    assert double(2) == 4
    assert double(2) == 4

    double.cache_clear()

    assert double.cache_info() == CacheInfo(0, 0, 2, 0)
    assert double(2) == 4
    assert calls == 2


def test_exceptions_are_not_cached() -> None:
    calls = 0

    @lru_cache(maxsize=2)
    def fail() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        fail()
    with pytest.raises(RuntimeError, match="boom"):
        fail()

    assert calls == 2
    assert fail.cache_info() == CacheInfo(0, 2, 2, 0)


@pytest.mark.parametrize("maxsize", [0, -1, True, False, 1.5, "2", object()])
def test_invalid_maxsize_is_rejected(maxsize: object) -> None:
    with pytest.raises(ValueError, match="positive integer or None"):
        lru_cache(maxsize=maxsize)  # type: ignore[arg-type]


def test_unhashable_arguments_raise_type_error() -> None:
    @lru_cache(maxsize=2)
    def length(value: list[int]) -> int:
        return len(value)

    with pytest.raises(TypeError):
        length([1, 2, 3])


def test_function_metadata_is_preserved() -> None:
    @lru_cache(maxsize=2)
    def documented(value: int) -> int:
        """Example docstring."""
        return value

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "Example docstring."
    assert documented.__wrapped__(3) == 3
