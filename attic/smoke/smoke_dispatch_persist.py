"""验证 dispatch form 不被轮询清空 + 白名单失焦自动保存。"""
import sys
import time
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
        page.goto("http://127.0.0.1:8766/", wait_until="load", timeout=10000)
        page.wait_for_timeout(2000)

        # 1) 进 dispatch tab
        page.click('button[data-tab="dispatch"]')
        page.wait_for_timeout(3000)
        page.screenshot(path=str(out_dir / "07_dispatch_initial.png"), full_page=True)
        print("  saved 07_dispatch_initial.png")

        # 2) 改 quick dispatch 的 runtime + model select
        # 加 console log 看实际改了
        page.on("console", lambda msg: print(f"  [console.{msg.type}] {msg.text}"))

        page.select_option('#quick-runtime', 'opencode')
        page.wait_for_timeout(300)
        # model 选项变化后选第一个具体的 model
        opts = page.eval_on_selector_all('#quick-model option', 'els => els.map(e => e.value).filter(v => v)')
        print(f"  opencode model options: {opts[:5]}")
        if opts and opts[0]:
            page.select_option('#quick-model', opts[0])
            page.wait_for_timeout(200)

        # 3) 在 payload textarea 里输入文字
        page.fill('#quick-payload', '测试自动刷新会不会清掉我的输入')
        page.wait_for_timeout(200)

        # 抓当前 model select 的值
        model_before = page.eval_on_selector('#quick-model', 'el => el.value')
        runtime_before = page.eval_on_selector('#quick-runtime', 'el => el.value')
        payload_before = page.eval_on_selector('#quick-payload', 'el => el.value')
        print(f"  改完后: runtime={runtime_before}, model={model_before}")
        print(f"  payload: {payload_before[:50]}")

        # 4) 等 5 秒（超过 2 次轮询），看 select 还保不保
        # 加 mutation observer 监听 model 变化
        page.evaluate("""
            const sel = document.getElementById('quick-model');
            const log = (msg) => console.log('[mutation] ' + msg);
            const obs = new MutationObserver((muts) => {
                muts.forEach(m => {
                    log('model innerHTML changed, new options: ' + Array.from(sel.options).map(o => o.value).join(','));
                    log('  current value: ' + sel.value);
                });
            });
            obs.observe(sel, {childList: true, subtree: true, attributes: true});
        """)
        page.wait_for_timeout(5000)
        model_after = page.eval_on_selector('#quick-model', 'el => el.value')
        runtime_after = page.eval_on_selector('#quick-runtime', 'el => el.value')
        payload_after = page.eval_on_selector('#quick-payload', 'el => el.value')
        print(f"  5秒后: runtime={runtime_after}, model={model_after}")
        print(f"  payload: {payload_after[:50]}")

        # 5) 验证：3 个值都保持原样
        if model_before == model_after and runtime_before == runtime_after and payload_before == payload_after:
            print(f"  ✅ form 内容保持不变")
        else:
            print(f"  ❌ form 内容被改了")
            print(f"     runtime 改了: {runtime_before} -> {runtime_after}")
            print(f"     model 改了: {model_before} -> {model_after}")
            print(f"     payload 改了: {payload_before[:30]} -> {payload_after[:30]}")
        page.screenshot(path=str(out_dir / "08_dispatch_after_5s.png"), full_page=True)
        print("  saved 08_dispatch_after_5s.png")

        # 6) 测试白名单失焦自动保存
        # 先改白名单
        page.fill('#pinned-models', 'opencode-go/deepseek-v4-flash\nopencode-go/kimi-k3')
        page.wait_for_timeout(200)
        # 点别处触发 blur
        page.click('#quick-payload')
        page.wait_for_timeout(2000)  # 等自动保存完成

        # 7) 验证 server 端真存了
        resp = page.request.get("http://127.0.0.1:8766/api/dashboard/pinned-models")
        data = resp.json()
        pinned = data.get("pinned_models", [])
        print(f"  server 白名单: {pinned}")
        from_dash = data.get("from_dashboard", [])
        if from_dash and "opencode-go/deepseek-v4-flash" in from_dash:
            print(f"  ✅ 失焦自动保存到 server 成功")
        else:
            print(f"  ❌ 失焦自动保存失败（server from_dashboard: {from_dash}）")

        # 8) 看 "重置表单" 按钮能用 —— 应该重新拉 models，不应该清空用户已输入的 payload
        page.click('#dispatch-refresh-form')
        page.wait_for_timeout(2000)
        payload_after_reset = page.eval_on_selector('#quick-payload', 'el => el.value')
        # 重置表单只重拉 model 列表，不动用户输入（避免误删）
        if payload_after_reset == payload_after:
            print(f"  ✅ 重置表单保留用户输入")
        else:
            print(f"  ❌ 重置表单清掉了用户输入（不应该）")
        page.screenshot(path=str(out_dir / "09_dispatch_after_reset.png"), full_page=True)
        print("  saved 09_dispatch_after_reset.png")

        browser.close()
        print(f"\n所有截图: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
