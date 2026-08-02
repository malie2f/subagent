def binary_search(arr: list[int], target: int) -> int:
    """Return the index of *target* in a sorted ascending list *arr*, or -1 if not found."""
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def binary_search_leftmost(arr: list[int], target: int) -> int:
    """Return the index of the first occurrence of *target*, or -1 if not found."""
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    if lo < len(arr) and arr[lo] == target:
        return lo
    return -1


def binary_search_rightmost(arr: list[int], target: int) -> int:
    """Return the index of the last occurrence of *target*, or -1 if not found."""
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] <= target:
            lo = mid + 1
        else:
            hi = mid - 1
    if hi >= 0 and arr[hi] == target:
        return hi
    return -1


if __name__ == "__main__":
    tests = [
        ("empty", [], 5, -1),
        ("single_found", [5], 5, 0),
        ("single_not_found", [5], 3, -1),
        ("found_mid", [1, 3, 5, 7, 9], 5, 2),
        ("found_start", [1, 3, 5, 7, 9], 1, 0),
        ("found_end", [1, 3, 5, 7, 9], 9, 4),
        ("not_found", [1, 3, 5, 7, 9], 4, -1),
        ("below_range", [1, 3, 5, 7, 9], 0, -1),
        ("above_range", [1, 3, 5, 7, 9], 10, -1),
        ("negatives", [-5, 0, 10], -5, 0),
        ("two_elems", [10, 20], 10, 0),
        ("large_even", list(range(0, 1000000)), 777777, 777777),
    ]
    for name, arr, target, expected in tests:
        result = binary_search(arr, target)
        assert result == expected, f"[{name}] FAIL: arr[:3]=..., target={target}, expected={expected}, got={result}"

    dupe_tests = [
        ("leftmost_dup", [1, 2, 2, 2, 3], 2, 1),
        ("rightmost_dup", [1, 2, 2, 2, 3], 2, 3),
        ("leftmost_all_same", [2, 2, 2], 2, 0),
        ("rightmost_all_same", [2, 2, 2], 2, 2),
        ("leftmost_not_found", [1, 3, 5], 2, -1),
        ("rightmost_not_found", [1, 3, 5], 2, -1),
    ]
    for name, arr, target, expected in dupe_tests:
        if "leftmost" in name:
            result = binary_search_leftmost(arr, target)
        else:
            result = binary_search_rightmost(arr, target)
        assert result == expected, f"[{name}] FAIL: arr={arr}, target={target}, expected={expected}, got={result}"

    print("All tests passed.")
