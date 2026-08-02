"""截图 dashboard 几个 tab 给用户看。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    from playwright.sync_api import sync_playwright

    out_dir = Path("data/_screenshots")
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        # 1) Overview
        page.goto("http://127.0.0.1:8766/", wait_until="load", timeout=10000)
        page.wait_for_timeout(3000)
        page.screenshot(path=str(out_dir / "01_overview.png"), full_page=True)
        print(f"  saved 01_overview.png")

        # 2) 派活 tab
        page.click('button[data-tab="dispatch"]')
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out_dir / "02_dispatch.png"), full_page=True)
        print(f"  saved 02_dispatch.png")

        # 3) 任务 tab（找 verifying 任务）
        page.click('button[data-tab="tasks"]')
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out_dir / "03_tasks.png"), full_page=True)
        print(f"  saved 03_tasks.png")

        # 4) 子 Agent tab
        page.click('button[data-tab="subagents"]')
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out_dir / "04_subagents.png"), full_page=True)
        print(f"  saved 04_subagents.png")

        # 5) 点一个 kimi-transcript-1784366869 看 transcript 详情
        # 先用 kimi 的 task_id
        # 通过 API 找有 transcript 的 task
        resp = page.request.get("http://127.0.0.1:8766/api/subagents")
        data = resp.json()
        for s in data.get("subagents", []):
            tid = s["task_id"]
            trans_resp = page.request.get(f"http://127.0.0.1:8766/api/subagents/{tid}/transcript")
            trans_data = trans_resp.json()
            if trans_data.get("ok") and trans_data.get("event_count", 0) > 1:
                # 点这个
                print(f"  点击 task_id={tid} (有 {trans_data['event_count']} 个事件)")
                # 找 subagent-item 并 click
                items = page.query_selector_all(".subagent-item")
                for it in items:
                    if it.get_attribute("data-tid") == tid:
                        it.click()
                        break
                page.wait_for_timeout(2000)
                page.screenshot(path=str(out_dir / "05_subagent_with_transcript.png"), full_page=True)
                print(f"  saved 05_subagent_with_transcript.png")
                break

        # 6) 回到任务 tab，点一个 verifying 的看 verify banner
        page.click('button[data-tab="tasks"]')
        page.wait_for_timeout(1000)
        # 找 verifying 的 task
        verifying_items = page.query_selector_all(".task-item.task-verify")
        if verifying_items:
            verifying_items[0].click()
            page.wait_for_timeout(2000)
            page.screenshot(path=str(out_dir / "06_task_verifying.png"), full_page=True)
            print(f"  saved 06_task_verifying.png")
        else:
            # 没有 verifying 的，那就看任意一个 task 的 transcript
            items = page.query_selector_all(".task-item")
            if items:
                items[0].click()
                page.wait_for_timeout(2000)
                page.screenshot(path=str(out_dir / "06_task_detail.png"), full_page=True)
                print(f"  saved 06_task_detail.png (no verifying, showing any)")

        browser.close()
        print(f"\n所有截图保存在: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
