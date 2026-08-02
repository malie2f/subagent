"""Unit tests for the lru_cache decorator."""

import pytest

from lru_cache_decorator import lru_cache


def test_basic_cache_hit():
    call_count = 0

    @lru_cache(maxsize=2)
    def add(a, b):
        nonlocal call_count
        call_count += 1
        return a + b

    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert call_count == 1

    info = add.cache_info()
    assert info.hits == 2
    assert info.misses == 1
    assert info.maxsize == 2
    assert info.currsize == 1


def test_lru_eviction_order():
    call_count = 0

    @lru_cache(maxsize=2)
    def identity(x):
        nonlocal call_count
        call_count += 1
        return x

    identity(1)  # cache: [1]
    identity(2)  # cache: [1, 2]
    identity(1)  # cache: [2, 1] (1 becomes most recently used)
    identity(3)  # cache: [1, 3] (2 is evicted)

    # 1 should still be cached; 2 should have been evicted.
    assert identity(1) == 1
    assert call_count == 3

    identity(2)  # 2 is recomputed and cached.
    assert call_count == 4


def test_keyword_arguments_are_different_keys():
    call_count = 0

    @lru_cache(maxsize=4)
    def func(a, b=0):
        nonlocal call_count
        call_count += 1
        return a + b

    assert func(1, b=2) == 3
    assert func(1, 2) == 3
    assert func(a=1, b=2) == 3
    assert func(1) == 1
    assert call_count == 4

    # Repeating the same calls should hit the cache.
    assert func(1, b=2) == 3
    assert func(1, 2) == 3
    assert func(a=1, b=2) == 3
    assert func(1) == 1
    assert call_count == 4


def test_invalid_maxsize_raises():
    invalid_values = [0, -1, -5, "128", 3.14, [], {}]
    for value in invalid_values:
        with pytest.raises(ValueError):
            lru_cache(maxsize=value)(lambda x: x)


def test_maxsize_none_unbounded():
    call_count = 0

    @lru_cache(maxsize=None)
    def square(x):
        nonlocal call_count
        call_count += 1
        return x * x

    for i in range(100):
        assert square(i) == i * i

    assert call_count == 100

    for i in range(100):
        assert square(i) == i * i

    assert call_count == 100

    info = square.cache_info()
    assert info.hits == 100
    assert info.misses == 100
    assert info.maxsize is None
    assert info.currsize == 100


def test_cache_clear():
    call_count = 0

    @lru_cache(maxsize=2)
    def double(x):
        nonlocal call_count
        call_count += 1
        return x * 2

    assert double(2) == 4
    assert double(2) == 4
    assert call_count == 1

    double.cache_clear()
    info = double.cache_info()
    assert info.hits == 0
    assert info.misses == 0
    assert info.currsize == 0

    assert double(2) == 4
    assert call_count == 2


def test_cache_info_stats():
    @lru_cache(maxsize=3)
    def identity(x):
        return x

    for i in range(3):
        identity(i)

    for i in range(3):
        identity(i)

    identity(3)  # miss, evicts key 0
    identity(0)  # miss
    identity(1)  # miss

    info = identity.cache_info()
    assert info.hits == 3
    assert info.misses == 6
    assert info.maxsize == 3
    assert info.currsize == 3


def test_wrapped_metadata_preserved():
    @lru_cache(maxsize=2)
    def my_function(x):
        """My docstring."""
        return x

    assert my_function.__name__ == "my_function"
    assert my_function.__doc__ == "My docstring."
