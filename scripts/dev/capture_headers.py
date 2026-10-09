"""Playwright でSWIMポータルのリクエストヘッダーをキャプチャする

ブラウズ画面GETとAPI POSTのヘッダーの違いを実測し、
Worker側のヘッダー設定が正しいか検証する。

使い方:
  pip install playwright
  playwright install chromium
  python scripts/dev/capture_headers.py
  (SWIM ID・パスワードは環境変数 SWIM_USERNAME / SWIM_PASSWORD か、無ければ入力を求める)
  出力 (header_capture_*.json) はセッション Cookie を含むので 0600 で書く。コミットしない
"""

import asyncio
import os
import sys
from datetime import datetime

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import swim_credentials, write_private_json  # noqa: E402

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           f"header_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="ja-JP",
    )
    page = await context.new_page()

    captured = []

    async def on_response(response):
        request = response.request
        url = request.url
        if "swim.mlit.go.jp" not in url:
            return
        resource = request.resource_type
        if resource in ("stylesheet", "image", "font", "media"):
            return

        headers = await request.all_headers()
        captured.append({
            "url": url,
            "method": request.method,
            "resource_type": resource,
            "status": response.status,
            "headers": headers,
        })

    page.on("response", on_response)

    username, password = swim_credentials()

    # === ログイン ===
    print("1. ログインページへアクセス...")
    await page.goto("https://top.swim.mlit.go.jp/swim/", wait_until="commit")
    await asyncio.sleep(3)
    await page.click('a:has-text("ログイン")')
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page.fill('input[type="email"]', username)
    await page.fill('input[type="password"]', password)
    await page.click('button:has-text("ログイン")')
    try:
        await page.wait_for_url("**/web.swim.mlit.go.jp/**", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(3)
    print(f"   ログイン完了 (キャプチャ: {len(captured)}件)")

    # === f2aspr ブラウズ画面 ===
    print("2. f2aspr ブラウズ画面へ遷移...")
    await page.goto(
        "https://web.swim.mlit.go.jp/f2aspr/browse/flv850s001",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    # SPA初期化完了を待つ（groupLayer.json がSPA初期化の最後のほう）
    try:
        await page.wait_for_response(
            lambda r: "groupLayer.json" in r.url, timeout=30000,
        )
        await asyncio.sleep(3)
    except Exception:
        print("   (groupLayer.json 待機タイムアウト、30秒追加待機)")
        await asyncio.sleep(30)
    print(f"   f2aspr完了 (キャプチャ: {len(captured)}件)")

    # === f2dnrq ブラウズ画面 ===
    print("3. f2dnrq ブラウズ画面へ遷移...")
    await page.goto(
        "https://web.swim.mlit.go.jp/f2dnrq/browse/FUV201",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    try:
        await page.wait_for_response(
            lambda r: "UTM3.json" in r.url, timeout=30000,
        )
        await asyncio.sleep(3)
    except Exception:
        print("   (UTM3.json 待機タイムアウト、30秒追加待機)")
        await asyncio.sleep(30)
    print(f"   f2dnrq完了 (キャプチャ: {len(captured)}件)")

    await browser.close()
    await pw.stop()

    # === 結果を保存 ===
    write_private_json(OUTPUT_FILE, captured)
    print(f"\n保存: {OUTPUT_FILE} ({len(captured)}件)")

    # === ヘッダー比較サマリー ===
    print("\n=== ヘッダー比較サマリー ===\n")

    check_headers = ["origin", "x-requested-with", "sec-fetch-dest", "sec-fetch-mode",
                     "sec-fetch-site", "sec-fetch-user", "upgrade-insecure-requests",
                     "accept", "referer", "cache-control", "pragma"]

    # documentナビゲーション vs XHR POST を比較
    nav_requests = [r for r in captured if r["resource_type"] == "document"]
    xhr_posts = [r for r in captured if r["method"] == "POST"
                 and ("web/" in r["url"] or "LuciadRIA" in r["url"])]

    print(f"Document Navigation: {len(nav_requests)}件")
    for req in nav_requests[:5]:
        print(f"  {req['method']} {req['url']}")
        for h in check_headers:
            val = req["headers"].get(h)
            if val:
                print(f"    {h}: {val}")
            else:
                print(f"    {h}: (なし)")
        print()

    print(f"XHR POST (API): {len(xhr_posts)}件")
    for req in xhr_posts[:3]:
        print(f"  {req['method']} {req['url']}")
        for h in check_headers:
            val = req["headers"].get(h)
            if val:
                print(f"    {h}: {val}")
            else:
                print(f"    {h}: (なし)")
        print()


if __name__ == "__main__":
    asyncio.run(main())
