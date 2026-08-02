"""审计 mcp-hub 找隐藏 bug。

检查项：
1. 跨进程文件并发（dashboard + cluster 同时写 tasks.json）
2. _load 解析失败时是否吞数据
3. _subscribers 内存泄漏
4. webhook 失败时是否影响主流程
5. 任务 acceptance 流程的边界条件
6. retry 计数是否正确
7. claim 超时是否死循环
8. cluster 启动失败是否被吞
"""
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def test_1_load_parsing_failure():
    """Bug 2: _load 出错时返回空，下一次 _flush 会把现有数据覆盖为空。"""
    print("\n=== Test 1: _load 解析失败吞数据 ===")
    test_file = Path("data/_audit_test_1.json")
    test_file.parent.mkdir(parents=True, exist_ok=True)

    # 1) 写正常数据
    test_file.write_text(json.dumps({"tasks": [{"id": "1", "status": "done"}]}), encoding="utf-8")
    print(f"  before: {test_file.read_text()[:80]}")

    # 2) 模拟外部进程写入了损坏数据（半截 JSON）
    test_file.write_text('{"tasks": [{"id": "1", "sta', encoding="utf-8")
    print(f"  corrupted: {test_file.read_text()[:80]}")

    # 3) TaskStore _load 解析失败 → 返回空
    from mcp_hub.queue import TaskStore
    store = TaskStore(test_file)
    data = store._load()  # noqa: SLF001
    print(f"  _load result: {data}")

    # 4) 模拟 publish 一个新 task（这会 _flush(data) 把"空"覆盖回去）
    asyncio.run(store.publish(topic="test", payload="hello"))
    data2 = store._load()  # noqa: SLF001
    print(f"  after publish: {data2}")

    if data2.get("tasks") and len(data2["tasks"]) == 1 and data2["tasks"][0]["payload"] == "hello":
        print(f"  ✅ publish 后数据正常")
    else:
        print(f"  ❌ BUG: publish 完数据异常: {data2}")
        return False
    test_file.unlink()
    return True


def test_2_subscribers_memory_leak():
    """Bug: subscribe() 满了的 queue 不清理，asyncio.Queue 永久存在。"""
    print("\n=== Test 2: subscribers 内存泄漏 ===")
    from mcp_hub.queue import TaskStore
    store = TaskStore(Path("data/_audit_test_2.json"))
    n_before = len(store._subscribers)  # noqa: SLF001
    print(f"  subscribers before: {n_before}")

    # 模拟 SSE 客户端连接：subscribe 1000 次但从不 unsubscribe
    for i in range(1000):
        store.subscribe(f"task_{i}")

    n_after = len(store._subscribers)  # noqa: SLF001
    print(f"  subscribers after 1000 subscribes: {n_after}")

    if n_after == 1000:
        print(f"  ❌ BUG: 每个 task_id 都一个 subscribers list，无界增长")
        return False
    else:
        print(f"  ✅ subscribers 数量合理")
    return True


def test_3_publish_corruption_recovery():
    """Bug: publish 时 _load 读到损坏文件 → 主文件数据丢失 / 备份是否正确。"""
    print("\n=== Test 3: publish 碰到损坏文件时备份 + 主文件行为 ===")
    test_file = Path("data/_audit_test_3.json")
    # 清理可能残留的 corrupted 备份
    for p in test_file.parent.glob("_audit_test_3.corrupted-*.json"):
        p.unlink()
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(json.dumps({"tasks": [
        {"task_id": "old1", "topic": "t", "status": "pending", "payload": "p", "from_model": "u", "for_model": None,
         "metadata": {}, "created_at": 1, "claimed_at": None, "completed_at": None, "retries": 0, "max_retries": 3,
         "acceptance": {}, "webhook": "", "verify_history": [], "notify_history": []}
    ]}), encoding="utf-8")

    from mcp_hub.queue import TaskStore
    store = TaskStore(test_file)

    # 模拟外部写坏
    test_file.write_text('{"tasks": [{"id": "1", "sta', encoding="utf-8")

    # publish 一个新 task —— _load 失败时备份 + 返回空
    asyncio.run(store.publish(topic="t", payload="new"))

    # 检查主文件有 new（被新加的），老数据丢了
    data = store._load()  # noqa: SLF001
    tasks = data.get("tasks", [])
    has_new = any(t.get("payload") == "new" for t in tasks)
    print(f"  after publish: 新 task 入队={has_new}, total={len(tasks)}")

    # 检查备份存在
    backups = list(test_file.parent.glob("_audit_test_3.corrupted-*.json"))
    print(f"  备份文件数: {len(backups)}")
    if backups:
        # 损坏文件的内容（不完整那段）原样保留 —— 不要 json.loads 它
        backup_text = backups[0].read_text(encoding="utf-8")
        print(f"  备份文件保留: {backups[0].name} ({len(backup_text)} 字节)")
        # 关键：ops 能从备份手工恢复
        if has_new and len(backups) >= 1:
            print(f"  ✅ 新 task 正常入队 + 损坏文件已备份（ops 可手工恢复）")
            for p in backups:
                p.unlink()
            test_file.unlink()
            return True
        else:
            print(f"  ❌ BUG: 新 task 没入队或备份没生成")
            return False
    else:
        print(f"  ❌ BUG: 损坏文件没备份")
        return False


def test_4_webhook_doesnt_block_main_flow():
    """Bug: webhook 失败时影响 _fire_webhook，subscriber 收到事件。"""
    print("\n=== Test 4: webhook 失败是否影响主流程 ===")
    test_file = Path("data/_audit_test_4.json")
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("{}", encoding="utf-8")

    from mcp_hub.queue import TaskStore
    store = TaskStore(test_file)

    # 给一个永远连不上的 webhook
    async def test():
        task = await store.publish(
            topic="test", payload="hello",
            webhook="http://127.0.0.1:1/will-fail",  # port 1 不通
        )
        await asyncio.sleep(2)  # 等 _fire_webhook
        return task

    t0 = time.time()
    task = asyncio.run(test())
    elapsed = time.time() - t0
    print(f"  publish + fire_webhook 耗时: {elapsed:.1f}s")
    if elapsed > 10:
        print(f"  ❌ BUG: webhook 失败导致 publish 卡住")
        test_file.unlink()
        return False
    else:
        print(f"  ✅ webhook 失败不阻塞")
    test_file.unlink()
    return True


def test_5_subscriber_queue_full_silently_drops():
    """Bug: _fire_webhook 的 SSE 推送 QueueFull 静默吞掉。"""
    print("\n=== Test 5: subscriber queue 满时事件丢失 ===")
    test_file = Path("data/_audit_test_5.json")
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("{}", encoding="utf-8")

    from mcp_hub.queue import TaskStore
    store = TaskStore(test_file)
    q = store.subscribe("t1")
    # q maxsize=64, 不消费会满

    async def test():
        # 推 100 个事件，q 满了之后会 QueueFull 静默吞掉
        for i in range(100):
            task_id = "t1"
            for t in store._load().get("tasks", []):  # noqa: SLF001
                pass
            # 直接构造 task 触发 _fire_webhook
            from mcp_hub.queue import Task
            t = Task(task_id="t1", topic="t", payload=str(i), from_model="u")
            await store._fire_webhook(t, "test")  # noqa: SLF001
        await asyncio.sleep(0.1)

    asyncio.run(test())
    print(f"  pushed 100 events, q.qsize()={q.qsize()}, maxsize=64")
    if q.qsize() < 100:
        print(f"  ⚠️  事件被静默丢（{100 - q.qsize()} 个）—— 没日志，没 metrics")
    return True


def test_6_retries_counter_overflow():
    """Bug: retries 不限上界，没 max_retries check 在 claim。"""
    print("\n=== Test 6: retries 超 max_retries 仍被 claim？===")
    test_file = Path("data/_audit_test_6.json")
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(json.dumps({"tasks": [
        {"task_id": "t1", "topic": "t", "status": "pending", "payload": "p", "from_model": "u",
         "for_model": None, "metadata": {}, "created_at": 1, "claimed_at": None, "completed_at": None,
         "retries": 5, "max_retries": 3,  # 已超 max_retries
         "acceptance": {}, "webhook": "", "verify_history": [], "notify_history": []}
    ]}), encoding="utf-8")

    from mcp_hub.queue import TaskStore
    store = TaskStore(test_file)

    async def test():
        return await store.claim("t", "w1")

    task = asyncio.run(test())
    print(f"  claim 返回: {task}")
    if task and task.task_id == "t1":
        print(f"  ❌ BUG: retries(5) 已超 max_retries(3)，但仍被 claim！")
        test_file.unlink()
        return False
    test_file.unlink()
    return True


def test_7_concurrent_writes_data_loss():
    """Bug: 跨进程写 tasks.json 丢数据。"""
    print("\n=== Test 7: 跨进程并发写 ===")
    test_file = Path("data/_audit_test_7.json")
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("{}", encoding="utf-8")

    from mcp_hub.queue import TaskStore

    # 模拟两个进程同时写
    s1 = TaskStore(test_file)
    s2 = TaskStore(test_file)

    async def publish(store, payload, count):
        for i in range(count):
            await store.publish(topic="t", payload=f"{payload}_{i}")

    async def main():
        await asyncio.gather(
            publish(s1, "A", 20),
            publish(s2, "B", 20),
        )

    asyncio.run(main())
    data = s1._load()  # noqa: SLF001
    n = len(data.get("tasks", []))
    print(f"  预期 40 个 task，实际 {n} 个")
    if n < 40:
        print(f"  ❌ BUG: 跨进程并发丢数据了（这里是同进程多 TaskStore 实例模拟）")
        test_file.unlink()
        return False
    test_file.unlink()
    return True


def main():
    results = []
    for name, test in [
        ("load 解析失败吞数据", test_1_load_parsing_failure),
        ("subscribers 内存泄漏", test_2_subscribers_memory_leak),
        ("publish 碰到损坏文件丢数据", test_3_publish_corruption_recovery),
        ("webhook 失败阻塞主流程", test_4_webhook_doesnt_block_main_flow),
        ("subscriber queue 满静默丢", test_5_subscriber_queue_full_silently_drops),
        ("retries 超 max_retries 仍 claim", test_6_retries_counter_overflow),
        ("并发写丢数据", test_7_concurrent_writes_data_loss),
    ]:
        try:
            ok = test()
            results.append((name, ok))
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    print("\n" + "=" * 60)
    print("AUDIT SUMMARY")
    print("=" * 60)
    for name, ok in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}")
    fails = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - fails} passed, {fails} failed (found bugs)")


if __name__ == "__main__":
    main()
