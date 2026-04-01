#!/usr/bin/env python3
"""PKG気象API深掘り調査 — 他API対照テスト + スタックトレース解析

目的:
- 他のAPIが正常動作するか確認（PKG固有の問題か全体的なサーバー問題か）
- PKG APIの完全なエラーレスポンスを解析
- ブラウズ画面403の原因調査
- f2aspr系のSPAアプリの初期化が必要か確認

使い方:
    python3 scripts/test_pkg_deep.py --user 'ID' --password 'PASS'
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

from swim_worker.auth import (
    SwimClient, SwimAuthError, SWIM_PORTAL_URL,
    _XHR_HEADERS, _NAV_HEADERS, _BROWSER_TYPE,
    SWIM_SESSION_CHECK_URL,
)
from curl_cffi.requests import AsyncSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SWIM_BASE = SWIM_PORTAL_URL

# ブラウズ画面URLs
BROWSE_PKG = f"{SWIM_BASE}/f2aspr/browse/flv904s001"
BROWSE_FLIGHT = f"{SWIM_BASE}/f2aspr/browse/flv800s001"
BROWSE_PIREP = f"{SWIM_BASE}/f2aspr/browse/flv850s001"
BROWSE_NOTAM = f"{SWIM_BASE}/f2dnrq/browse/FUV201"

# API URLs
URL_PKG = f"{SWIM_BASE}/f2aspr/web/FLV904/LGV312"
URL_FLIGHT_LIST = f"{SWIM_BASE}/f2aspr/web/FLV803/LGV210"
URL_PIREP = f"{SWIM_BASE}/f2aspr/web/FLV920/LGV358"
URL_NOTAM = f"{SWIM_BASE}/f2dnrq/web/FUV201/USV001"
URL_AIRPORTS = f"{SWIM_BASE}/f2dnrq/web/FUV201/USV005"


async def main():
    parser = argparse.ArgumentParser(description="PKG気象API深掘り調査")
    parser.add_argument("--user", default=os.environ.get("SWIM_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("SWIM_PASSWORD", ""))
    args = parser.parse_args()

    if not args.user or not args.password:
        print("使い方: python3 scripts/test_pkg_deep.py --user 'ID' --password 'PASS'")
        sys.exit(1)

    from datetime import datetime, timezone, timedelta
    jst = datetime.now(timezone(timedelta(hours=9)))
    print(f"PKG気象API深掘り調査 — {jst.strftime('%Y-%m-%d %H:%M JST')}")

    # === ログイン ===
    print(f"\n{'=' * 70}")
    print("Phase 1: ログイン")
    print("=" * 70)

    client = SwimClient(username=args.user, password=args.password)
    if os.path.exists(_auth_module.COOKIE_FILE):
        os.remove(_auth_module.COOKIE_FILE)

    try:
        await client.login()
        print("  ログイン成功")
    except SwimAuthError as e:
        print(f"  ログイン失敗: {e}")
        return

    session = client._session
    assert session is not None

    # === 対照テスト: フライト一覧API ===
    print(f"\n{'=' * 70}")
    print("Phase 2: 対照テスト — フライト一覧API (LGV210)")
    print("=" * 70)

    flight_body = {
        "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
        "ctrlInfo": {},
        "ctrlHeader": {},
        "flightInformationSearchConditionsDTO": {
            "airportCode": "RJTT",
            "startDate": "",
        },
    }
    try:
        resp = await session.post(
            URL_FLIGHT_LIST,
            json=flight_body,
            headers={"Referer": BROWSE_FLIGHT, **_XHR_HEADERS},
        )
        print(f"  Status: {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            print(f"  ERROR: {data.get('clazzName')}: {data.get('errMsg', '?')[:100]}")
        else:
            ret = data.get("ret", {})
            dto = ret.get("flightInformationSearchResultsDTO", {})
            arr = len(dto.get("arrivingFlights", []))
            dep = len(dto.get("departingFlights", []))
            print(f"  SUCCESS! 到着{arr}便, 出発{dep}便")
    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    await asyncio.sleep(2.0)

    # === 対照テスト: 空港一覧API ===
    print(f"\n{'=' * 70}")
    print("Phase 3: 対照テスト — 空港一覧API (USV005)")
    print("=" * 70)
    try:
        resp = await session.post(
            URL_AIRPORTS,
            json={},
            headers={"Referer": BROWSE_NOTAM, **_XHR_HEADERS},
        )
        print(f"  Status: {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            print(f"  ERROR: {data.get('clazzName')}: {data.get('errMsg', '?')[:100]}")
        else:
            print(f"  SUCCESS! keys: {list(data.keys())[:5]}")
    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    await asyncio.sleep(2.0)

    # === 対照テスト: PIREP API ===
    print(f"\n{'=' * 70}")
    print("Phase 4: 対照テスト — PIREP API (LGV358)")
    print("=" * 70)
    pirep_body = {
        "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
        "airepRequest": {},
    }
    try:
        resp = await session.post(
            URL_PIREP,
            json=pirep_body,
            headers={"Referer": BROWSE_PIREP, **_XHR_HEADERS},
        )
        print(f"  Status: {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            print(f"  ERROR: {data.get('clazzName')}: {data.get('errMsg', '?')[:100]}")
        else:
            print(f"  SUCCESS! keys: {list(data.keys())[:5]}")
            if "ret" in data:
                print(f"  ret keys: {list(data['ret'].keys())}")
    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    await asyncio.sleep(2.0)

    # === NOTAM API ===
    print(f"\n{'=' * 70}")
    print("Phase 5: 対照テスト — NOTAM API (USV001)")
    print("=" * 70)
    notam_body = {
        "searchFlg": 0, "numberOfDisplay": 0, "scope": "", "type": "",
        "notamCode": "", "fromDate": "", "toDate": "", "freeText": "",
        "location": "RJJJ", "scopeAerodrome": True, "scopeEnroute": True, "scopeWarning": True,
        "lower": "", "upper": "", "keyword": "", "keywordAndOr": "",
        "displayValidAll": True, "validDatetimeFromDate": "", "validDatetimeFromTime": "",
        "validDatetimeToDate": "", "validDatetimeToTime": "",
    }
    try:
        resp = await session.post(
            URL_NOTAM,
            json=notam_body,
            headers={"Referer": BROWSE_NOTAM, **_XHR_HEADERS},
        )
        print(f"  Status: {resp.status_code}")
        data = resp.json()
        if data.get("error"):
            print(f"  ERROR: {data.get('clazzName')}: {data.get('errMsg', '?')[:100]}")
        else:
            print(f"  SUCCESS! keys: {list(data.keys())[:5]}")
    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    await asyncio.sleep(2.0)

    # === PKGのエラーレスポンスを完全ダンプ ===
    print(f"\n{'=' * 70}")
    print("Phase 6: PKG API完全エラーレスポンスダンプ")
    print("=" * 70)

    pkg_body = {
        "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
        "airportWeatherSearchConditionsDTO": {"targetAirportList": ["RJTT"]},
    }
    try:
        resp = await session.post(
            URL_PKG,
            json=pkg_body,
            headers={"Referer": BROWSE_PKG, **_XHR_HEADERS},
        )
        print(f"  Status: {resp.status_code}")
        data = resp.json()
        # エラースタックトレースを解析
        exc = data.get("exception", {})
        if exc:
            st = exc.get("stackTrace", [])
            print(f"\n  スタックトレース ({len(st)}フレーム):")
            for i, frame in enumerate(st[:15]):
                cls = frame.get("className", "?")
                method = frame.get("methodName", "?")
                file = frame.get("fileName", "?")
                line = frame.get("lineNumber", "?")
                print(f"    [{i:2d}] {cls}.{method}({file}:{line})")

            # NullPointerExceptionの原因を推測
            # 行番号266 in FLV904001GetPkgAerodromeTypeFlowLogicBean.java
            print(f"\n  エラーメッセージ: {data.get('errMsg', '?')}")

            # causeチェーン
            cause = exc.get("cause")
            depth = 0
            while cause and depth < 5:
                print(f"\n  cause[{depth}]: {cause.get('message', '?')}")
                cause_st = cause.get("stackTrace", [])
                for f in cause_st[:3]:
                    print(f"    {f.get('className', '?')}.{f.get('methodName', '?')}:{f.get('lineNumber', '?')}")
                cause = cause.get("cause")
                depth += 1

        # ctrlHeaderの中身
        ret = data.get("ret", {})
        ctrl = ret.get("ctrlHeader", {})
        if ctrl:
            print(f"\n  ctrlHeader:")
            for k, v in ctrl.items():
                print(f"    {k}: {v}")

        # 完全なエラーメッセージ
        print(f"\n  errMsg: {data.get('errMsg')}")
        print(f"  clazzName: {data.get('clazzName')}")

    except Exception as e:
        print(f"  リクエスト失敗: {e}")

    await asyncio.sleep(2.0)

    # === f2asprルートページのJavaScriptを調査 ===
    print(f"\n{'=' * 70}")
    print("Phase 7: f2aspr SPAのエントリポイント調査")
    print("=" * 70)

    # f2asprルートにアクセス
    for url in [
        f"{SWIM_BASE}/f2aspr/",
        f"{SWIM_BASE}/f2aspr/browse/",
        f"{SWIM_BASE}/f2aspr/browse/flv904s001",
    ]:
        print(f"\n  GET {url}")
        try:
            resp = await session.get(url, headers={
                **_NAV_HEADERS,
                "Referer": f"{SWIM_BASE}/",
                "Sec-Fetch-Site": "same-origin",
            })
            print(f"    Status: {resp.status_code}")
            print(f"    Content-Type: {resp.headers.get('content-type', '?')}")
            content_len = len(resp.text)
            print(f"    Content-Length: {content_len}")

            if resp.status_code == 200:
                html = resp.text
                # SPAの場合、JSバンドルやAPIコールが含まれる
                js_files = re.findall(r'src=["\']([^"\']*\.js[^"\']*)["\']', html)
                if js_files:
                    print(f"    JS files: {js_files[:5]}")

                # APIベースURLやエンドポイント定義
                api_refs = re.findall(r'(LGV\d+|FLV\d+|USV\d+)', html)
                if api_refs:
                    print(f"    API refs in HTML: {set(api_refs)}")

                # form要素
                forms = re.findall(r'<form[^>]*action=["\']([^"\']*)["\'][^>]*>', html)
                if forms:
                    print(f"    Forms: {forms}")

                # 先頭300文字
                print(f"    HTML preview: {html[:300]}")
            elif resp.status_code == 403:
                print(f"    403 Forbidden — HTML preview: {resp.text[:300]}")

        except Exception as e:
            print(f"    エラー: {e}")

        await asyncio.sleep(1.0)

    # === ブラウズ画面の正しいアクセス方法を試す ===
    print(f"\n{'=' * 70}")
    print("Phase 8: ブラウズ画面 — XHR Acceptヘッダーで試す")
    print("=" * 70)

    # SWIMはSPAなので、ブラウズ画面へのアクセスはXHRかもしれない
    for label, headers in [
        ("XHR (application/json)", {
            **_XHR_HEADERS,
            "Referer": f"{SWIM_BASE}/",
        }),
        ("ナビゲーション (Sec-Fetch-User除外)", {
            k: v for k, v in _NAV_HEADERS.items() if k != "Sec-Fetch-User"
        } | {
            "Referer": f"{SWIM_BASE}/",
            "Sec-Fetch-Site": "same-origin",
        }),
        ("ナビゲーション (cross-site)", {
            **_NAV_HEADERS,
            "Sec-Fetch-Site": "cross-site",
        }),
    ]:
        print(f"\n  --- {label} ---")
        try:
            resp = await session.get(BROWSE_PKG, headers=headers)
            print(f"    Status: {resp.status_code}")
            print(f"    Content-Type: {resp.headers.get('content-type', '?')}")
            if resp.status_code == 200:
                print(f"    Body preview: {resp.text[:200]}")
            elif resp.status_code == 403:
                print(f"    403 — ブロックされた")
        except Exception as e:
            print(f"    エラー: {e}")
        await asyncio.sleep(1.0)

    # === PKGをフライトAPIのbody構造で試す ===
    print(f"\n{'=' * 70}")
    print("Phase 9: PKG APIを他APIのbody構造パターンで試す")
    print("=" * 70)

    # フライトAPIが成功した構造をベースに
    patterns = [
        (
            "パターンI: フライトAPI成功body構造ベース",
            {
                "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
                "ctrlInfo": {},
                "ctrlHeader": {},
                "airportWeatherSearchConditionsDTO": {
                    "targetAirportList": ["RJTT"],
                    "airportCode": "RJTT",
                },
            },
        ),
        (
            "パターンJ: 空body",
            {},
        ),
        (
            "パターンK: nullのairportWeather",
            {
                "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
                "ctrlInfo": {},
                "ctrlHeader": {},
                "airportWeatherSearchConditionsDTO": None,
            },
        ),
        (
            "パターンL: RJTT以外の空港 (RJAA)",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {"targetAirportList": ["RJAA"]},
            },
        ),
        (
            "パターンM: 複数空港",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {"targetAirportList": ["RJTT", "RJAA"]},
            },
        ),
        (
            "パターンN: targetAirportListがnull",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {"targetAirportList": None},
            },
        ),
        (
            "パターンO: targetAirportList空配列",
            {
                "msgHeader": {"msgSendDateTime": "", "transactionId": ""},
                "airportWeatherSearchConditionsDTO": {"targetAirportList": []},
            },
        ),
    ]

    for label, body in patterns:
        print(f"\n  --- {label} ---")
        print(f"  Body: {json.dumps(body, ensure_ascii=False)[:200]}")
        try:
            resp = await session.post(
                URL_PKG,
                json=body,
                headers={"Referer": BROWSE_PKG, **_XHR_HEADERS},
            )
            print(f"  Status: {resp.status_code}")
            data = resp.json()
            if data.get("error"):
                err_msg = data.get("errMsg", "?")[:100]
                cls = data.get("clazzName", "?")
                # 行番号が変わるか確認
                exc = data.get("exception", {})
                st = exc.get("stackTrace", [])
                line = st[0].get("lineNumber", "?") if st else "?"
                print(f"  ERROR: {cls}: {err_msg} (line={line})")
            elif "ret" in data:
                print(f"  SUCCESS! ret keys: {list(data['ret'].keys())}")
            else:
                print(f"  JSON keys: {list(data.keys())}")
        except Exception as e:
            print(f"  リクエスト失敗: {e}")
        await asyncio.sleep(1.5)

    # === セッションチェック ===
    print(f"\n{'=' * 70}")
    print("Phase 10: セッション有効性確認")
    print("=" * 70)
    try:
        resp = await session.get(SWIM_SESSION_CHECK_URL)
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"  アカウント情報: {json.dumps(data, ensure_ascii=False)[:500]}")
    except Exception as e:
        print(f"  エラー: {e}")

    # === クリーンアップ ===
    await client.close()
    if os.path.exists(_auth_module.COOKIE_FILE):
        os.remove(_auth_module.COOKIE_FILE)

    print(f"\n{'=' * 70}")
    print("完了")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
