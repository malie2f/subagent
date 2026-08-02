"""截图 dashboard 的 cluster tab —— 验证多 pool 显示。"""
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

        # Cluster tab
        page.goto("http://127.0.0.1:8766/", wait_until="load", timeout=10000)
        page.wait_for_timeout(2000)
        page.click('button[data-tab="cluster"]')
        page.wait_for_timeout(3000)
        page.screenshot(path=str(out_dir / "10_cluster_multipool.png"), full_page=True)
        print("  saved 10_cluster_multipool.png")

        # 派活 tab —— 看模型卡片墙
        page.click('button[data-tab="dispatch"]')
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out_dir / "11_dispatch_with_codex.png"), full_page=True)
        print("  saved 11_dispatch_with_codex.png")

        browser.close()
        print("done")


if __name__ == "__main__":
    main()
