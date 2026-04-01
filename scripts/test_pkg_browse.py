#!/usr/bin/env python3
"""PKG気象API調査スクリプト — ブラウズ画面事前アクセスの影響を検証

目的:
- ブラウズ画面GETのレスポンスにCSRFトークンやセッション情報が含まれるか
- ブラウズ画面GETで追加Cookieが設定されるか
- PKG APIに必要なリクエストボディの正確なフォーマット

使い方:
    python3 scripts/test_pkg_browse.py --user 'ID' --password 'PASS'
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swim_worker.auth as _auth_module
_auth_module.COOKIE_FILE = "/tmp/.swim_test_cookies.json"

from swim_worker.auth import SwimClient, SwimAuthError, SWIM_PORTAL_URL, _XHR_HEADERS, _NAV_HEADERS, _BROWSER_TYPE
from curl_cffi.requests import AsyncSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SWIM_BASE = SWIM_PORTAL_URL
BROWSE_URL = f"{SWIM_BASE}/f2aspr/browse/flv904s001"
PKG_API_URL = f"{SWIM_BASE}/f2aspr/web/FLV904/LGV312"


def dump_cookies(session, label: str):
    """セッションのCookieを一覧表示"""
    print(f"\n  [{label}] Cookies:")
    for name, value in session.cookies.items():
        display_val = value[:60] + "..." if len(value) > 60 else value
        print(f"    {name} = {display_val}")
    if not session.cookies:
        print(f"    (なし)")


def diff_cookies(before: dict, after: dict) -> dict:
    """Cookie変化を検出"""
    added = {k: v for k, v in after.items() if k not in before}
    changed = {k: (before[k], v) for k, v in after.items() if k in before and before[k] != v}
    removed = {k: v for k, v in before.items() if k not in after}
    return {"added": added, "changed": changed, "removed": removed}


def get_cookies_dict(session) -> dict:
    return {name: value for name, value in session.cookies.items()}


async def main():
    parser = argparse.ArgumentParser(description="PKG気象ブラウズ画面調査")
    parser.add_argument("--user", default=os.environ.get("SWIM_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("SWIM_PASSWORD", ""))
    args = parser.parse_args()

    if not args.user or not args.password:
        print("使い方: python3 scripts/test_pkg_browse.py --user 'ID' --password 'PASS'")
        sys.exit(1)

    from datetime import datetime, timezone, timedelta
    jst = datetime.now(timezone(timedelta(hours=9)))
    print(f"PKG気象API調査 — {jst.strftime('%Y-%m-%d %H:%M JST')}")

    # === Phase 1: ログイン ===
    print(f"\n{'=' * 70}")
    print("Phase 1: ログイン")
    print("=" * 70)

    client = SwimClient(username=args.user, password=args.password)

    # 古いCookieファイルを削除して新規ログイン
    if os.path.exists(_auth_module.COOKIE_FILE):
        os.remove(_auth_module.COOKIE_FILE)

    try:
        await client.login()
        print("  ログイン成功")
    except SwimAuthError as e:
        print(f"  ログイン失敗: {e}")
        return
    finally:
        pass

    session = client._session
    assert session is not None

    dump_cookies(session, "ログイン後")
    cookies_after_login = get_cookies_dict(session)

    # === Phase 2: ブラウズ画面にGETアクセス（事前なし） ===
    # まずブラウズ画面アクセスなしでPKG APIを叩いてみる
    print(f"\n{'=' * 70}")
    print("Phase 2: ブラウズ画面アクセスなし → PKG API直接POST")
    print("=" * 70)

    body_original = {
        "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
        "airportWeatherSearchConditionsDTO": {"targetAirportList": ["RJTT"]},
    }
    print(f"  Body: {json.dumps(body_original, ensure_ascii=False)}")

    try:
        resp = await session.post(
            PKG_API_URL,
            json=body_original,
            headers={"Referer": BROWSE_URL},
        )
        print(f"  Status: {resp.status_code}")
        print(f"  Response headers:")
        for k, v in resp.headers.items():
            if k.lower() in ("content-type", "set-cookie", "x-csrf", "x-xsrf", "x-request-id"):
                print(f"    {k}: {v}")
        body_text = resp.text[:2000]
        print(f"  Response body (first 2000 chars):")
        print(f"    {body_text}")

        # JSON解析を試みる
        try:
            data = resp.json()
            print(f"\n  JSON解析OK。トップレベルキー: {list(data.keys())}")
            if data.get("error"):
                print(f"  エラー: {data.get('errMsg', '?')}")
                print(f"  クラス: {data.get('clazzName', '?')}")
        except Exception:
            print(f"  JSONではない")
    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    await asyncio.sleep(2.0)

    # === Phase 3: ブラウズ画面にGETアクセス ===
    print(f"\n{'=' * 70}")
    print("Phase 3: ブラウズ画面にGETアクセス")
    print(f"  URL: {BROWSE_URL}")
    print("=" * 70)

    cookies_before_browse = get_cookies_dict(session)

    try:
        browse_resp = await session.get(
            BROWSE_URL,
            headers={
                **_NAV_HEADERS,
                "Referer": f"{SWIM_BASE}/",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        print(f"  Status: {browse_resp.status_code}")
        print(f"\n  Response headers (全部):")
        for k, v in browse_resp.headers.items():
            print(f"    {k}: {v}")

        cookies_after_browse = get_cookies_dict(session)
        cookie_diff = diff_cookies(cookies_before_browse, cookies_after_browse)

        print(f"\n  Cookie変化:")
        if cookie_diff["added"]:
            print(f"    追加: {cookie_diff['added']}")
        if cookie_diff["changed"]:
            print(f"    変更: {list(cookie_diff['changed'].keys())}")
        if cookie_diff["removed"]:
            print(f"    削除: {list(cookie_diff['removed'].keys())}")
        if not any(cookie_diff.values()):
            print(f"    変化なし")

        dump_cookies(session, "ブラウズ後")

        # HTMLレスポンスを解析
        html = browse_resp.text
        print(f"\n  HTMLサイズ: {len(html)} bytes")

        # CSRFトークンを探す
        csrf_patterns = [
            r'name=["\']_csrf["\'].*?value=["\']([^"\']+)["\']',
            r'name=["\']csrf["\'].*?value=["\']([^"\']+)["\']',
            r'<meta\s+name=["\']_csrf["\'].*?content=["\']([^"\']+)["\']',
            r'<meta\s+name=["\']csrf-token["\'].*?content=["\']([^"\']+)["\']',
            r'csrfToken\s*[=:]\s*["\']([^"\']+)["\']',
            r'_csrf_token\s*[=:]\s*["\']([^"\']+)["\']',
            r'X-CSRF-TOKEN.*?["\']([^"\']+)["\']',
            r'XSRF-TOKEN.*?["\']([^"\']+)["\']',
        ]

        print(f"\n  CSRFトークン検索:")
        found_csrf = False
        for pat in csrf_patterns:
            matches = re.findall(pat, html, re.IGNORECASE)
            if matches:
                print(f"    パターン '{pat[:40]}...' → {matches}")
                found_csrf = True
        if not found_csrf:
            print(f"    CSRFトークンは見つかりません")

        # セッション/トークン系のJavaScript変数を探す
        print(f"\n  JS変数検索 (token/session/auth関連):")
        js_patterns = [
            r'(token|session|auth|nonce)\s*[=:]\s*["\']([^"\']{8,})["\']',
            r'window\.__([A-Z_]+)\s*=\s*["\']?([^"\';\s]+)',
        ]
        for pat in js_patterns:
            matches = re.findall(pat, html, re.IGNORECASE)
            if matches:
                for m in matches[:5]:
                    print(f"    {m}")

        # hiddenフィールドを探す
        hidden_matches = re.findall(r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.IGNORECASE)
        if hidden_matches:
            print(f"\n  Hidden input fields:")
            for hm in hidden_matches[:10]:
                print(f"    {hm[:200]}")

        # HTMLの先頭500文字
        print(f"\n  HTML先頭500文字:")
        print(f"    {html[:500]}")

        # scriptタグの内容を確認
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        if scripts:
            print(f"\n  Inline scripts: {len(scripts)}個")
            for i, s in enumerate(scripts[:5]):
                content = s.strip()[:300]
                if content:
                    print(f"    [{i}] {content}")

    except Exception as e:
        print(f"  ブラウズ画面GETエラー: {e}")
        import traceback
        traceback.print_exc()

    await asyncio.sleep(2.0)

    # === Phase 4: ブラウズ画面アクセス後にPKG APIを叩く ===
    print(f"\n{'=' * 70}")
    print("Phase 4: ブラウズ画面アクセス後 → PKG API POST (同じbody)")
    print("=" * 70)

    try:
        resp = await session.post(
            PKG_API_URL,
            json=body_original,
            headers={"Referer": BROWSE_URL},
        )
        print(f"  Status: {resp.status_code}")
        body_text = resp.text[:2000]
        print(f"  Response: {body_text}")

        try:
            data = resp.json()
            print(f"  JSON keys: {list(data.keys())}")
            if data.get("error"):
                print(f"  エラー: {data.get('errMsg', '?')}")
                print(f"  クラス: {data.get('clazzName', '?')}")
            elif "ret" in data:
                print(f"  ret keys: {list(data['ret'].keys())}")
                print(f"  SUCCESS!")
        except Exception:
            print(f"  JSONではない")
    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    await asyncio.sleep(2.0)

    # === Phase 5: 別のbodyフォーマットを試す ===
    print(f"\n{'=' * 70}")
    print("Phase 5: 別のbodyフォーマット試行")
    print("=" * 70)

    # 既存テストスクリプトの _BASIC_BODY 構造を使ってみる
    body_variants = [
        (
            "パターンA: _BASIC_BODY + airportWeatherSearchConditionsDTO",
            {
                "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
                "ctrlInfo": {},
                "ctrlHeader": {},
                "airportWeatherSearchConditionsDTO": {"targetAirportList": ["RJTT"]},
            },
        ),
        (
            "パターンB: 空のmsgHeader (全フィールドなし)",
            {
                "msgHeader": {},
                "airportWeatherSearchConditionsDTO": {"targetAirportList": ["RJTT"]},
            },
        ),
        (
            "パターンC: targetAirportList を文字列で",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {"targetAirportList": "RJTT"},
            },
        ),
        (
            "パターンD: weatherSearchConditions",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "weatherSearchConditions": {"targetAirportList": ["RJTT"]},
            },
        ),
        (
            "パターンE: targetAirport (単数)",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {"targetAirport": "RJTT"},
            },
        ),
        (
            "パターンF: airportCode形式",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {"airportCode": "RJTT"},
            },
        ),
        (
            "パターンG: 空のairportWeatherSearchConditionsDTO",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {},
            },
        ),
        (
            "パターンH: ctrlInfo + ctrlHeader付き (targetAirportList配列)",
            {
                "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
                "ctrlInfo": {},
                "ctrlHeader": {},
                "airportWeatherSearchConditionsDTO": {
                    "targetAirportList": ["RJTT"],
                },
            },
        ),
    ]

    for label, body in body_variants:
        print(f"\n  --- {label} ---")
        print(f"  Body: {json.dumps(body, ensure_ascii=False)[:200]}")
        try:
            resp = await session.post(
                PKG_API_URL,
                json=body,
                headers={"Referer": BROWSE_URL},
            )
            print(f"  Status: {resp.status_code}")
            resp_text = resp.text[:500]

            try:
                data = resp.json()
                if data.get("error"):
                    err_msg = data.get("errMsg", "?")[:100]
                    cls = data.get("clazzName", "?")
                    print(f"  ERROR: {cls}: {err_msg}")
                elif "ret" in data:
                    ret_keys = list(data["ret"].keys())
                    print(f"  SUCCESS! ret keys: {ret_keys}")
                    # weatherDTO の中身を表示
                    wd = data["ret"].get("weatherDTO", data.get("weatherDTO"))
                    if wd:
                        for key in ["metarSpeciInfoList", "tafInfoList", "atisInfoList", "useRunwayList"]:
                            items = wd.get(key, [])
                            print(f"    {key}: {len(items)}件")
                            if items:
                                sample = items[0]
                                body_data = sample.get("body_DATA", sample.get("plain_DATA", ""))
                                print(f"      [0]: {str(body_data)[:200]}")
                    else:
                        print(f"  (weatherDTO not found in ret)")
                        print(f"  ret全体: {json.dumps(data['ret'], ensure_ascii=False)[:500]}")
                else:
                    print(f"  JSON keys: {list(data.keys())}")
                    print(f"  Full: {json.dumps(data, ensure_ascii=False)[:500]}")
            except Exception:
                print(f"  Response (non-JSON): {resp_text}")
        except Exception as e:
            print(f"  リクエスト失敗: {e}")

        await asyncio.sleep(1.5)

    # === Phase 6: 全XHRヘッダー付きでリクエスト（ブラウザ完全再現） ===
    print(f"\n{'=' * 70}")
    print("Phase 6: 完全なXHRヘッダー付きPOST")
    print("=" * 70)

    full_headers = {
        **_XHR_HEADERS,
        "Referer": BROWSE_URL,
        "Content-Type": "application/json",
    }
    print(f"  Headers: {json.dumps(full_headers, indent=2)}")
    print(f"  Body: {json.dumps(body_original, ensure_ascii=False)}")

    try:
        resp = await session.post(
            PKG_API_URL,
            json=body_original,
            headers=full_headers,
        )
        print(f"  Status: {resp.status_code}")
        print(f"  Response: {resp.text[:1000]}")
    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    # === クリーンアップ ===
    await client.close()
    if os.path.exists(_auth_module.COOKIE_FILE):
        os.remove(_auth_module.COOKIE_FILE)

    print(f"\n{'=' * 70}")
    print("完了")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
