"""一个不依赖 functools.lru_cache 的线程安全 LRU 缓存装饰器。"""

from collections import OrderedDict
from functools import wraps
from threading import RLock
from typing import Any, Callable, Hashable, TypeVar, cast


F = TypeVar("F", bound=Callable[..., Any])

# 使用独立标记分隔位置参数和关键字参数，避免缓存键发生歧义。
_KWARGS_MARKER = object()


def lru_cache(maxsize: int = 128) -> Callable[[F], F]:
    """创建一个线程安全的 LRU 缓存装饰器。

    Args:
        maxsize: 缓存可保存的最大条目数。设为 0 时禁用缓存。

    Returns:
        装饰器。被装饰的函数会额外拥有 ``cache_clear()`` 方法。

    Raises:
        TypeError: maxsize 不是整数时抛出。
        ValueError: maxsize 小于 0 时抛出。

    Notes:
        参数必须是可哈希的，因为参数组合会被用作字典键。
        为避免长时间计算阻塞其他缓存访问，执行原函数时不会持有锁；
        因此多个线程同时请求同一个尚未缓存的键时，可能会重复计算，
        但缓存结构本身始终是线程安全的。
    """
    if not isinstance(maxsize, int):
        raise TypeError("maxsize 必须是整数")
    if maxsize < 0:
        raise ValueError("maxsize 不能小于 0")

    def decorator(func: F) -> F:
        cache: "OrderedDict[Hashable, Any]" = OrderedDict()
        lock = RLock()
        generation = 0

        def make_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
            if not kwargs:
                key: Hashable = args
            else:
                # 关键字参数排序后，f(a=1, b=2) 和 f(b=2, a=1)
                # 会命中同一个缓存条目。
                key = args + (_KWARGS_MARKER,) + tuple(sorted(kwargs.items()))

            # 提前触发不可哈希参数的 TypeError，并避免在两次加锁时重复检查。
            hash(key)
            return key

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal generation

            if maxsize == 0:
                return func(*args, **kwargs)

            key = make_key(args, kwargs)

            with lock:
                try:
                    value = cache.pop(key)
                except KeyError:
                    current_generation = generation
                else:
                    # 最近访问的条目放到 OrderedDict 尾部。
                    cache[key] = value
                    return value

            # 不持锁执行原函数，避免耗时计算阻塞其他线程。
            result = func(*args, **kwargs)

            with lock:
                # 如果计算期间调用过 cache_clear()，不把旧计算结果重新放回缓存。
                if generation != current_generation:
                    return result

                # 另一个线程可能已经缓存了相同的键。这里用本线程的结果更新它，
                # 并将其标记为最近使用。
                cache.pop(key, None)
                cache[key] = result

                # OrderedDict 头部是最近最少使用的条目。
                if len(cache) > maxsize:
                    cache.popitem(last=False)

            return result

        def cache_clear() -> None:
            """清空函数缓存。"""
            nonlocal generation
            with lock:
                cache.clear()
                generation += 1

        wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        return cast(F, wrapper)

    return decorator


if __name__ == "__main__":
    calculation_count = 0

    @lru_cache(maxsize=2)
    def square(number: int) -> int:
        global calculation_count
        calculation_count += 1
        print(f"实际计算 square({number})")
        return number * number

    print(square(2))  # 实际计算，并缓存 2
    print(square(3))  # 实际计算，并缓存 3
    print(square(2))  # 命中缓存；2 成为最近使用项
    print(square(4))  # 超出容量，淘汰最近最少使用的 3
    print(square(3))  # 3 已被淘汰，因此重新计算
    print(f"实际计算次数: {calculation_count}")

    square.cache_clear()
    print("缓存已清空")
