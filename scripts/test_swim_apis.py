#!/usr/bin/env python3
"""SWIM全APIテスト — 偽装動作確認 + レスポンス構造ダンプ

全APIを1リクエストずつ叩いて、偽装が通るか・レスポンス構造が想定通りかを確認する。
PKG成功時の weatherDTO の位置（トップレベル vs ret 内）も検証する。

使い方:
    python3 scripts/test_swim_apis.py --user 'ID' --password 'PASS'
    python3 scripts/test_swim_apis.py  # 環境変数 SWIM_USERNAME/SWIM_PASSWORD
"""
import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swim_worker.auth as _auth_module
_auth_module.COOKIE_FILE = "/tmp/.swim_test_cookies.json"

from swim_worker.auth import SwimClient, SwimAuthError, SWIM_PORTAL_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SWIM_BASE = SWIM_PORTAL_URL
URL_PKG = f"{SWIM_BASE}/f2aspr/web/FLV904/LGV312"
URL_PIREP = f"{SWIM_BASE}/f2aspr/web/FLV920/LGV358"
URL_NOTAM = f"{SWIM_BASE}/f2dnrq/web/FUV201/USV001"
URL_AIRPORTS = f"{SWIM_BASE}/f2dnrq/web/FUV201/USV005"
URL_FLIGHT_LIST = f"{SWIM_BASE}/f2aspr/web/FLV803/LGV210"
URL_FLIGHT_DETAIL = f"{SWIM_BASE}/f2aspr/web/FLV911/LGV340"

_BASIC_BODY = {
    "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
    "ctrlInfo": {},
    "ctrlHeader": {},
}

_PIREP_LAYERS = [
    "PIREP_SMTH", "PIREP_LGTM", "PIREP_LGT", "PIREP_LGTP",
    "PIREP_MOD", "PIREP_MODP", "PIREP_SEV", "PIREP_EXT",
    "PIREP_ARS", "Volcanic_Ash", "WS", "CLOUD", "ICE", "TS",
]

results: dict[str, str] = {}


def unwrap_ret(data: dict) -> dict:
    """SWIM APIレスポンスの ret ラッパーを剥がす"""
    if isinstance(data, dict) and "ret" in data and isinstance(data["ret"], dict):
        return data["ret"]
    return data


def find_key(d, target, path=""):
    """dict内から再帰的にキーを探す"""
    if isinstance(d, dict):
        for k, v in d.items():
            if k == target:
                return f"{path}.{k}", v
            r = find_key(v, target, f"{path}.{k}")
            if r:
                return r
    return None


async def call_api(client: SwimClient, label: str, url: str, body: dict) -> dict | None:
    """API呼び出し共通処理"""
    print(f"\n{'=' * 60}")
    print(f"{label}")
    print("=" * 60)
    try:
        start = time.monotonic()
        data = await client.execute_api(url, body)
        elapsed = time.monotonic() - start

        err = data.get("error")
        if err:
            msg = data.get("errMsg", "unknown")
            cls = data.get("clazzName", "")
            print(f"  [SERVER ERROR] {cls}: {msg}")
            results[label] = f"SERVER ERROR ({cls})"
            return None

        print(f"  [OK] {elapsed:.1f}秒")
        results[label] = "OK"
        return data
    except SwimAuthError as e:
        print(f"  [FAIL] {e}")
        results[label] = f"FAIL ({e})"
        return None


async def test_login(client: SwimClient):
    """ログインテスト（新規 + Cookie復元）"""
    # 新規ログイン
    print("=" * 60)
    print("1. ログイン（3段階フロー）")
    print("=" * 60)
    if os.path.exists(_auth_module.COOKIE_FILE):
        os.remove(_auth_module.COOKIE_FILE)
    try:
        start = time.monotonic()
        await client.login()
        elapsed = time.monotonic() - start
        print(f"  [OK] 新規ログイン成功 ({elapsed:.1f}秒)")
        results["ログイン"] = "OK"
    except SwimAuthError as e:
        print(f"  [FAIL] {e}")
        results["ログイン"] = f"FAIL"
        return False

    # Cookie復元
    print(f"\n{'=' * 60}")
    print("2. Cookie復元")
    print("=" * 60)
    client2 = SwimClient(username=client._username, password=client._password)
    try:
        start = time.monotonic()
        await client2.login()
        elapsed = time.monotonic() - start
        print(f"  [OK] Cookie復元成功 ({elapsed:.1f}秒)")
        results["Cookie復元"] = "OK"
        await client2.close()
    except SwimAuthError as e:
        print(f"  [FAIL] {e}")
        results["Cookie復元"] = "FAIL"
        await client2.close()

    return True


async def test_pkg(client: SwimClient):
    """PKG気象テスト — 構造を詳細ダンプ"""
    now = datetime.now(timezone(timedelta(hours=0)))
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)
    data = await call_api(client, "3. PKG気象 (LGV312, RJTT)", URL_PKG, {
        **_BASIC_BODY,
        "condition": {
            "startDate": start.strftime("%Y%m%d%H%M"),
            "endDate": end.strftime("%Y%m%d%H%M"),
            "airportCodeList": ["RJTT"],
        },
    })
    if not data:
        return

    data = unwrap_ret(data)
    # weatherDTO の位置を特定
    r = find_key(data, "weatherDTO")
    if r:
        path, dto = r
        print(f"\n  weatherDTO の位置: {path}")
        if path == ".weatherDTO":
            print(f"  → トップレベル（パーサー想定通り）")
        elif path == ".ret.weatherDTO":
            print(f"  → ret内（パーサー修正が必要）")
        else:
            print(f"  → 想定外の位置")

        # 中身ダンプ
        for key in ["metarSpeciInfoList", "tafInfoList", "atisInfoList", "useRunwayList"]:
            items = dto.get(key, [])
            print(f"\n  {key}: {len(items)}件")
            if items:
                sample = items[0]
                loc = sample.get("location", "?")
                body_data = sample.get("body_DATA", sample.get("plain_DATA", ""))
                print(f"    [0] location={loc}")
                print(f"    [0] body: {body_data[:200]}")
    else:
        print(f"\n  weatherDTO が見つかりません")
        print(f"  トップレベルキー: {list(data.keys())}")
        ret = data.get("ret", {})
        if ret:
            print(f"  ret キー: {list(ret.keys())}")


async def test_pirep(client: SwimClient):
    """PIREPテスト"""
    data = await call_api(client, "4. PIREP (LGV358)", URL_PIREP, {
        **_BASIC_BODY, "layerNameList": _PIREP_LAYERS,
    })
    if not data:
        return

    data = unwrap_ret(data)
    # airepDTO の位置を特定
    r = find_key(data, "airepDTO")
    if r:
        path, dto = r
        print(f"  airepDTO の位置: {path}")
        aireps = dto.get("airepInfoList", [])
        print(f"  件数: {len(aireps)}")
        if aireps:
            sample = aireps[0]
            print(f"  [0] keys: {list(sample.keys())[:10]}")
            print(f"  [0] body: {sample.get('body_DATA', '')[:200]}")
    else:
        # 別のキー名を探す
        for candidate in ["airepInfoList", "airep", "pirepList"]:
            r = find_key(data, candidate)
            if r:
                path, items = r
                print(f"  {candidate} の位置: {path}, {len(items) if isinstance(items, list) else '?'}件")
                break
        else:
            print(f"  PIREP関連キーが見つかりません")
            print(f"  トップレベルキー: {list(data.keys())}")
            ret = data.get("ret", {})
            if ret:
                print(f"  ret キー: {list(ret.keys())}")


async def test_notam(client: SwimClient):
    """NOTAMテスト (RJJJ)"""
    data = await call_api(client, "5. NOTAM (USV001, RJJJ)", URL_NOTAM, {
        **_BASIC_BODY,
        "nof": "", "fir": "", "location": "RJJJ",
        "notamCode": "", "series": "", "notamNr": "", "uuid": "",
        "scopeAerodrome": "0", "scopeEnroute": "0", "scopeWarning": "0",
        "lower": "", "upper": "", "keyword": "", "keywordAndOr": "0",
        "displayValidAll": 0,
        "validDatetimeFromDate": "", "validDatetimeFromTime": "",
        "validDatetimeToDate": "", "validDatetimeToTime": "",
        "numberOfDisplay": 0, "searchFlg": 0,
    })
    if not data:
        return

    data = unwrap_ret(data)
    # notamList を探す
    for candidate in ["notamList", "notamInfoList", "notam_list"]:
        r = find_key(data, candidate)
        if r:
            path, items = r
            count = len(items) if isinstance(items, list) else "?"
            print(f"  {candidate} の位置: {path}, {count}件")
            if isinstance(items, list) and items:
                sample = items[0]
                print(f"  [0] keys: {list(sample.keys())[:10]}")
                for tk in ["notam_TEXT", "notamText", "body", "text", "freeText", "raw"]:
                    if tk in sample:
                        print(f"  [0] {tk}: {str(sample[tk])[:200]}")
                        break
            break
    else:
        print(f"  NOTAM関連キーが見つかりません")
        print(f"  トップレベルキー: {list(data.keys())}")
        ret = data.get("ret", {})
        if ret:
            print(f"  ret キー: {list(ret.keys())}")


async def test_airports(client: SwimClient):
    """空港一覧テスト"""
    data = await call_api(client, "6. 空港一覧 (USV005)", URL_AIRPORTS, {**_BASIC_BODY})
    if not data:
        return

    data = unwrap_ret(data)
    for candidate in ["aerodromeList", "aerodrome_list", "airports"]:
        r = find_key(data, candidate)
        if r:
            path, items = r
            count = len(items) if isinstance(items, list) else "?"
            print(f"  {candidate} の位置: {path}, {count}件")
            if isinstance(items, list) and items:
                print(f"  [0]: {json.dumps(items[0], ensure_ascii=False)[:200]}")
            break
    else:
        print(f"  空港リスト関連キーが見つかりません")
        print(f"  トップレベルキー: {list(data.keys())}")
        ret = data.get("ret", {})
        if ret:
            print(f"  ret キー: {list(ret.keys())}")


async def test_flight(client: SwimClient):
    """フライト一覧 + 詳細テスト"""
    data = await call_api(client, "7. 便一覧 (LGV210, RJTT)", URL_FLIGHT_LIST, {
        **_BASIC_BODY,
        "flightInformationSearchConditionsDTO": {
            "airportCode": "RJTT",
            "startDate": "",
        },
    })
    if not data:
        return

    data = unwrap_ret(data)
    r = find_key(data, "flightInformationSearchResultsDTO")
    if not r:
        print(f"  flightInformationSearchResultsDTO が見つかりません")
        print(f"  ret キー: {list(data.get('ret', {}).keys())}")
        return

    path, dto = r
    print(f"  DTO の位置: {path}")
    arriving = dto.get("arrivingFlights", [])
    departing = dto.get("departingFlights", [])
    print(f"  到着: {len(arriving)}便, 出発: {len(departing)}便")

    sample = (arriving or departing or [None])[0]
    if not sample:
        return

    foid = sample.get("foid", "")
    code = sample.get("flt_INFOCODE", "?")
    dep = sample.get("dep_AD", "?")
    dest = sample.get("dest_AD", "?")
    print(f"  サンプル: {code} {dep}->{dest} foid={foid}")

    if not foid:
        return

    # 便詳細
    data = await call_api(client, f"8. 便詳細 (LGV340, {code})", URL_FLIGHT_DETAIL, {
        **_BASIC_BODY,
        "flightDetailsSearchConditionsDTO": {
            "foid": foid,
            "departureAerodrome": "",
            "destinationAerodrome": "",
        },
    })
    if not data:
        return

    data = unwrap_ret(data)
    r = find_key(data, "flightDetailsSearchResultsDTO")
    if not r:
        print(f"  flightDetailsSearchResultsDTO が見つかりません")
        return

    path, dto = r
    print(f"  DTO の位置: {path}")
    detail = dto.get("flightDetails", {})
    if detail:
        fields = ["flt_INFOCODE", "typ", "dep_AD", "dest_AD", "reg", "rte",
                   "eobt", "atd", "ata", "sch_DEP_RWY_NR", "sch_ARR_RWY_NR",
                   "dep_RWY_NR", "arr_RWY_NR", "depspotnr", "arrspotnr"]
        for f in fields:
            v = detail.get(f)
            if v:
                print(f"    {f}: {str(v)[:120]}")


async def main():
    parser = argparse.ArgumentParser(description="SWIM全APIテスト")
    parser.add_argument("--user", default=os.environ.get("SWIM_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("SWIM_PASSWORD", ""))
    args = parser.parse_args()

    if not args.user or not args.password:
        print("使い方: python3 scripts/test_swim_apis.py --user 'ID' --password 'PASS'")
        sys.exit(1)

    jst = datetime.now(timezone(timedelta(hours=9)))
    print(f"SWIM全APIテスト — {jst.strftime('%Y-%m-%d %H:%M JST')}")

    client = SwimClient(username=args.user, password=args.password)
    try:
        ok = await test_login(client)
        if not ok:
            return

        await test_pkg(client)
        await test_pirep(client)
        await test_notam(client)
        await test_airports(client)
        await test_flight(client)

    except Exception as e:
        print(f"\n[UNEXPECTED ERROR] {e}")
    finally:
        await client.close()
        if os.path.exists(_auth_module.COOKIE_FILE):
            os.remove(_auth_module.COOKIE_FILE)

    # サマリー
    print(f"\n{'=' * 60}")
    print("テスト結果サマリー")
    print("=" * 60)
    for name, status in results.items():
        mark = "OK" if status == "OK" else "NG"
        print(f"  [{mark:>2}] {name:30s} {status}")

    ok_count = sum(1 for s in results.values() if s == "OK")
    print(f"\n  {ok_count}/{len(results)} 成功")


if __name__ == "__main__":
    asyncio.run(main())
