"""SWIM認証・API実行クライアント

curl_cffi を使用してChrome TLSフィンガープリントを再現し、
リアルなブラウザヘッダーを送信する。
"""
import asyncio
import json
import logging
import os
import random
import time

from curl_cffi.requests import AsyncSession, BrowserType

logger = logging.getLogger(__name__)

SWIM_LOGIN_URL = "https://top.swim.mlit.go.jp/swim/webapi/login"
SWIM_SESSION_CHECK_URL = "https://web.swim.mlit.go.jp/service/api/accounts/summary"
SWIM_PORTAL_URL = "https://web.swim.mlit.go.jp"
SWIM_TOP_URL = "https://top.swim.mlit.go.jp"

COOKIE_FILE = "/app/data/.swim_cookies.json"

# API種別ごとのReferer（ポータルの実際の画面URLを再現）
_REFERER_MAP = {
    "/f2dnrq/": f"{SWIM_PORTAL_URL}/f2dnrq/browse/FUV201",       # NOTAM/空港一覧
    "/f2aspr/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv850s001",    # 空域プロファイル（全f2aspr APIの入口）
}

# SPA初期化リクエスト — 実ブラウザがブラウズ画面を開く際に自動で読み込むリソース
# (method, path, body) のリスト。bodyはPOSTのみ使用、GETはNone。
# 順序はPlaywrightキャプチャに基づく（非同期JSにより実行順は毎回変動する）。
# body は 2026-04-02 のキャプチャで実測。

_BASIC_BODY = {
    "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
    "ctrlInfo": {},
    "ctrlHeader": {},
}

_SPA_INIT_REQUESTS: dict[str, list[tuple[str, str, dict | None]]] = {
    "f2aspr": [
        ("POST", "LuciadRIALicense", None),  # bodyなし（ライセンス検証）
        ("GET",  "js/lib/WebGIS/ATCMAP.settings", None),
        ("GET",  "settings/auto_filter.json", None),
        ("POST", "web/FLV901/LGV300", {**_BASIC_BODY}),
        ("POST", "web/FLV811/LGV231", {**_BASIC_BODY, "profileType": 0, "lang": "ja"}),
        ("GET",  "settings/map_disp.json", None),
        ("GET",  "web/resource/message", None),
        ("GET",  "web/resource/webfw", None),
        ("GET",  "web/resource/user", None),
        # ブラウズ画面GETはここで挿入（resource/userの後、_ensure_browse_pageで処理）
        ("POST", "web/FLV802/LGV205", {**_BASIC_BODY, "profileType": 0}),
        ("POST", "web/FLV934/LGV387", {**_BASIC_BODY}),
        ("GET",  "settings/velocity.json", None),
        ("GET",  "settings/default_view.json", None),
        ("GET",  "settings/default_font.json", None),
        ("GET",  "settings/default_dire_dist_position.json", None),
        ("GET",  "settings/shape_datablock_setting.json", None),
        ("GET",  "settings/default_color.json", None),
        ("GET",  "settings/map_disp.json", None),
        ("GET",  "settings/menu.json", None),
        ("GET",  "settings/commonMenuSetting.json", None),
        ("GET",  "settings/toolbarSetting.json", None),
        ("GET",  "settings/blink_info.json", None),
        ("GET",  "settings/groupLayer.json", None),
    ],
    "f2dnrq": [
        ("POST", "LuciadRIALicense", None),  # bodyなし
        ("GET",  "js/lib/WebGIS/ATCMAP.settings", None),
        ("GET",  "settings/auto_filter.json", None),
        ("POST", "web/FUV201/USV005", {**_BASIC_BODY}),
        ("GET",  "settings/map_disp.json", None),
        ("GET",  "web/resource/message", None),
        ("GET",  "web/resource/webfw", None),
        ("GET",  "web/resource/user", None),
        # ブラウズ画面GETはここで挿入（resource/userの後）
        ("GET",  "settings/velocity.json", None),
        ("GET",  "settings/default_view.json", None),
        ("GET",  "settings/default_font.json", None),
        ("GET",  "settings/default_dire_dist_position.json", None),
        ("GET",  "settings/shape_datablock_setting.json", None),
        ("GET",  "settings/default_color.json", None),
        ("GET",  "settings/map_disp.json", None),
        ("GET",  "settings/menu.json", None),
        ("GET",  "settings/commonMenuSetting.json", None),
        ("GET",  "settings/toolbarSetting.json", None),
        ("GET",  "settings/blink_info.json", None),
        ("GET",  "settings/groupLayer.json", None),
        ("POST", "web/FUV201/USV005", {**_BASIC_BODY}),
        ("GET",  "js/lib/WebGIS/layer/UTM0.json", None),
        ("GET",  "js/lib/WebGIS/layer/UTM1.json", None),
        ("GET",  "js/lib/WebGIS/layer/UTM2.json", None),
        ("GET",  "js/lib/WebGIS/layer/UTM3.json", None),
    ],
}


def _get_referer(url: str) -> str:
    """URLに応じたRefererを返す"""
    for prefix, referer in _REFERER_MAP.items():
        if prefix in url:
            return referer
    return f"{SWIM_PORTAL_URL}/"


# XHR固有のヘッダーのみオーバーライド
# User-Agent, Sec-Ch-Ua, Sec-Ch-Ua-Platform はcurl_cffiのchrome136デフォルトに任せる
# （TLSフィンガープリントとの一貫性を維持するため）
_XHR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Origin": SWIM_PORTAL_URL,
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# ページナビゲーション用ヘッダー（ログインページの初回読み込み）
_NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


_BROWSER_TYPE = BrowserType.chrome136


class SwimAuthError(Exception):
    """SWIM認証エラー"""


class SwimClient:
    """SWIM APIクライアント（Worker用）"""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._session: AsyncSession | None = None
        self._is_ready = False
        self._relogin_lock = asyncio.Lock()
        # 応答速度ベーススロットリング
        self._last_response_time: float = 0.0
        self._slow_threshold: float = 10.0  # 10秒以上で「遅い」判定
        self._extra_delay: float = 0.0  # 追加遅延（秒）
        # 訪問済みブラウズ画面（セッション中に1回GETしたURL）
        self._visited_pages: set[str] = set()

    def _save_cookies(self) -> None:
        """セッションCookieをファイルに保存"""
        if self._session is None:
            return
        try:
            cookies = {}
            for name, value in self._session.cookies.items():
                cookies[name] = value
            os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
            with open(COOKIE_FILE, "w") as f:
                json.dump(cookies, f)
            logger.debug("Cookie保存: %d個", len(cookies))
        except Exception as e:
            logger.debug("Cookie保存失敗: %s", e)

    def _load_cookies(self) -> dict | None:
        """保存済みCookieを読み込む"""
        try:
            if not os.path.exists(COOKIE_FILE):
                return None
            with open(COOKIE_FILE) as f:
                cookies = json.load(f)
            if cookies:
                logger.info("保存済みCookie読み込み: %d個", len(cookies))
                return cookies
        except Exception as e:
            logger.debug("Cookie読み込み失敗: %s", e)
        return None

    async def login(self) -> None:
        """SWIMにログインしてセッションCookieを取得する"""
        # まず保存済みCookieを試す
        saved = self._load_cookies()
        if saved:
            if self._session is not None:
                await self._session.close()
            self._session = AsyncSession(
                impersonate=_BROWSER_TYPE,
                headers=_XHR_HEADERS,
                timeout=60.0,
            )
            for name, value in saved.items():
                self._session.cookies.set(name, value, domain="mlit.go.jp")
            # web.swim へのナビゲーションを再現（ブラウザ再開を模倣）
            try:
                await self._session.get(f"{SWIM_PORTAL_URL}/", headers={
                    **_NAV_HEADERS,
                    "Sec-Fetch-Site": "none",
                })
                await asyncio.sleep(random.uniform(0.5, 1.0))
            except Exception:
                pass
            # セッション有効性チェック
            try:
                check = await self._session.get(SWIM_SESSION_CHECK_URL)
                if check.status_code == 200:
                    self._is_ready = True
                    self._visited_pages.clear()
                    logger.info("保存済みCookieでセッション復元成功")
                    return
            except Exception:
                pass
            logger.info("保存済みCookie失効、再ログイン")

        logger.info("SWIMポータルにログイン開始")
        all_cookies: dict[str, str] = {}
        try:
            async with AsyncSession(impersonate=_BROWSER_TYPE, timeout=60.0) as tmp:
                # 1. ポータルページ読み込み（URL直接入力を再現）
                await tmp.get(f"{SWIM_TOP_URL}/", headers=_NAV_HEADERS)
                await asyncio.sleep(random.uniform(1.0, 3.0))

                # 2. ログインPOST（SPA内のXHR）
                resp = await tmp.post(
                    SWIM_LOGIN_URL,
                    json={"id": self._username, "password": self._password},
                    headers={
                        **_XHR_HEADERS,
                        "Origin": SWIM_TOP_URL,
                        "Referer": f"{SWIM_TOP_URL}/",
                    },
                )

                if resp.status_code != 200:
                    raise SwimAuthError(f"ログインAPI失敗 (status={resp.status_code})")

                try:
                    data = resp.json()
                    error_code = data.get("error_info", {}).get("error_code", -1)
                    if error_code != 0:
                        raise SwimAuthError(f"ログインAPIエラー (error_code={error_code})")
                except (ValueError, KeyError):
                    pass

                if not resp.cookies:
                    raise SwimAuthError("ログイン後にCookieを取得できませんでした")

                # 3. web.swim への遷移を再現（ログイン後のSPAリダイレクト）
                # Sec-Fetch-User は除外（JS起動の遷移では Chrome が付与しない）
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await tmp.get(f"{SWIM_PORTAL_URL}/", headers={
                    k: v for k, v in _NAV_HEADERS.items() if k != "Sec-Fetch-User"
                } | {
                    "Referer": f"{SWIM_TOP_URL}/",
                    "Sec-Fetch-Site": "same-site",
                })
                await asyncio.sleep(random.uniform(0.5, 1.5))

                # tmpセッションの全Cookie（ページGET + ログイン + web.swim遷移）を移す
                for name, value in tmp.cookies.items():
                    all_cookies[name] = value

        except SwimAuthError:
            raise
        except Exception as e:
            raise SwimAuthError(f"ログインAPI呼び出しエラー: {e}") from e

        if self._session is not None:
            await self._session.close()

        self._session = AsyncSession(
            impersonate=_BROWSER_TYPE,
            headers=_XHR_HEADERS,
            timeout=60.0,
        )
        # Cookie domain は mlit.go.jp（実測で確認済み、省略すると403）
        for name, value in all_cookies.items():
            self._session.cookies.set(name, value, domain="mlit.go.jp")

        self._is_ready = True
        self._visited_pages.clear()
        self._save_cookies()
        logger.info("SWIMポータルにログイン成功")

    async def _ensure_browse_page(self, api_url: str) -> None:
        """API URLに対応するサービスのSPA初期化+ブラウズ画面遷移を再現する。

        実ブラウザでは、サービスページを開くと以下の順序でリクエストが発生する:
        1. ブラウズ画面の初回GET（HTMLシェル読み込み）
        2. SPA初期化前半（ライセンスPOST、設定ファイルGET、リソースバンドルGET）
        3. ブラウズ画面の追加GET（SPAルーティング、計3-4回）
        4. SPA初期化後半（追加設定ファイル、初期データAPI）
        セッション中に各サービス1回だけ実行する。
        """
        browse_url = _get_referer(api_url)
        if browse_url in self._visited_pages:
            return
        if self._session is None:
            return

        # サービスプレフィックスを特定
        service_prefix = None
        for prefix in _SPA_INIT_REQUESTS:
            if f"/{prefix}/" in api_url:
                service_prefix = prefix
                break

        nav_headers = {
            k: v for k, v in _NAV_HEADERS.items() if k != "Sec-Fetch-User"
        } | {
            "Referer": f"{SWIM_PORTAL_URL}/",
            "Sec-Fetch-Site": "same-origin",
        }

        try:
            # 1. ブラウズ画面の初回GET
            logger.debug("ブラウズ画面ナビゲーション: %s", browse_url)
            await self._session.get(browse_url, headers=nav_headers)
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # 2. SPA初期化リクエスト（実測順序に従う）
            if service_prefix and service_prefix in _SPA_INIT_REQUESTS:
                base = f"{SWIM_PORTAL_URL}/{service_prefix}"
                ref_header = {"Referer": browse_url}
                browse_count = 0
                for method, path, body in _SPA_INIT_REQUESTS[service_prefix]:
                    try:
                        url = f"{base}/{path}"
                        if method == "POST":
                            if body is not None:
                                await self._session.post(url, json=body, headers=ref_header)
                            else:
                                await self._session.post(url, headers=ref_header)
                        else:
                            await self._session.get(url, headers=ref_header)
                        await asyncio.sleep(random.uniform(0.02, 0.15))
                    except Exception:
                        pass

                    # resource/user の後にブラウズ画面の追加GETが来る（実測パターン）
                    if path == "web/resource/user" and browse_count == 0:
                        for _ in range(3):
                            try:
                                await self._session.get(browse_url, headers=nav_headers)
                                await asyncio.sleep(random.uniform(0.02, 0.1))
                            except Exception:
                                pass
                        browse_count += 1

            self._visited_pages.add(browse_url)
            await asyncio.sleep(random.uniform(1.0, 3.0))
        except Exception as e:
            logger.warning("ブラウズ画面ナビゲーション失敗: %s", e)

    async def execute_api(self, url: str, body: dict, *, _retried: bool = False) -> dict:
        """SWIM APIを実行する。403/HTTPエラー時は1回リトライする。"""
        if not self._is_ready or self._session is None:
            await self.login()
        assert self._session is not None

        # 応答速度ベースの追加遅延
        if self._extra_delay > 0:
            logger.debug("応答速度ベース追加遅延: %.1f秒", self._extra_delay)
            await asyncio.sleep(self._extra_delay)

        # 対応するブラウズ画面を未訪問なら先にナビゲーション
        await self._ensure_browse_page(url)

        extra_headers = {"Referer": _get_referer(url)}

        start = time.monotonic()
        try:
            resp = await self._session.post(url, json=body, headers=extra_headers)
        except Exception as e:
            if not _retried:
                delay = random.uniform(5, 15)
                logger.warning("API HTTPエラー、%.0f秒待機後にリトライ: %s", delay, e)
                await asyncio.sleep(delay)
                await self._relogin()
                return await self.execute_api(url, body, _retried=True)
            raise SwimAuthError(f"APIエラー: {e}") from e
        elapsed = time.monotonic() - start
        self._last_response_time = elapsed

        # 応答が遅い場合、追加遅延を増やす（サーバー負荷軽減）
        if elapsed > self._slow_threshold:
            self._extra_delay = min(self._extra_delay + 2.0, 15.0)
            logger.info("SWIM応答遅延検知 (%.1f秒)、追加遅延→%.1f秒", elapsed, self._extra_delay)
        elif self._extra_delay > 0:
            self._extra_delay = max(self._extra_delay - 0.5, 0.0)

        if resp.status_code in (401, 403):
            if not _retried:
                delay = random.uniform(5, 15)
                logger.warning("API %dエラー、%.0f秒待機後に再ログイン+リトライ", resp.status_code, delay)
                await asyncio.sleep(delay)
                await self._relogin()
                return await self.execute_api(url, body, _retried=True)
            raise SwimAuthError(f"API {resp.status_code}エラー (body={resp.text[:500]})")

        if resp.status_code != 200:
            raise SwimAuthError(f"APIエラー (status={resp.status_code})")

        # レスポンス処理時間シミュレーション（ブラウザのDOM更新・レンダリング）
        # 実ブラウザはレスポンスサイズやJS処理量で変動するため、ばらつきを持たせる
        await asyncio.sleep(random.expovariate(3.0) + 0.05)  # 中央値~0.38秒、稀に1-2秒
        return resp.json()

    async def _relogin(self) -> None:
        """再ログイン（ロック付き）"""
        async with self._relogin_lock:
            if self._is_ready and self._session is not None:
                try:
                    check = await self._session.get(SWIM_SESSION_CHECK_URL)
                    if check.status_code == 200:
                        return
                except Exception:
                    pass
            self._is_ready = False
            await self.login()

    async def close(self) -> None:
        """リソース解放"""
        if self._session is not None:
            self._save_cookies()
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self._is_ready = False
