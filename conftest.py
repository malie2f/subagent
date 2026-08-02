"""pytest 全局配置。"""

import pytest


def pytest_collection_modifyitems(config, items):
    """自动给所有 async test 加 asyncio 标记。"""
    for item in items:
        if item.get_closest_marker("asyncio") is None and "async" in item.keywords:
            pass  # pytest-asyncio 1.4+ 用 mode=auto，但我们显式标记更稳
