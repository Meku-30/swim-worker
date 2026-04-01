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
    "/f2aspr/web/FLV904/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv904s001",  # PKG気象
    "/f2aspr/web/FLV803/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv800s001",  # 便一覧
    "/f2aspr/web/FLV911/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv800s001",  # 便詳細（同じ画面）
    "/f2aspr/web/FLV806/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv904s001",  # 空港プロファイル
    "/f2aspr/web/FLV920/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv850s001",  # PIREP
    "/f2aspr/web/FLV914/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv850s001",  # 空域気象
    "/f2aspr/web/FLV918/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv850s001",  # SIGMET
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
                timeout=30.0,
            )
            for name, value in saved.items():
                self._session.cookies.set(name, value, domain="mlit.go.jp")
            # セッション有効性チェック
            try:
                check = await self._session.get(SWIM_SESSION_CHECK_URL)
                if check.status_code == 200:
                    self._is_ready = True
                    logger.info("保存済みCookieでセッション復元成功")
                    return
            except Exception:
                pass
            logger.info("保存済みCookie失効、再ログイン")

        logger.info("SWIMポータルにログイン開始")
        all_cookies: dict[str, str] = {}
        try:
            async with AsyncSession(impersonate=_BROWSER_TYPE, timeout=30.0) as tmp:
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
            timeout=30.0,
        )
        # Cookie domain は mlit.go.jp（実測で確認済み、省略すると403）
        for name, value in all_cookies.items():
            self._session.cookies.set(name, value, domain="mlit.go.jp")

        self._is_ready = True
        self._save_cookies()
        logger.info("SWIMポータルにログイン成功")

    async def execute_api(self, url: str, body: dict, *, _retried: bool = False) -> dict:
        """SWIM APIを実行する。403/HTTPエラー時は1回リトライする。"""
        if not self._is_ready or self._session is None:
            await self.login()
        assert self._session is not None

        # 応答速度ベースの追加遅延
        if self._extra_delay > 0:
            logger.debug("応答速度ベース追加遅延: %.1f秒", self._extra_delay)
            await asyncio.sleep(self._extra_delay)

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

        if resp.status_code == 403:
            if not _retried:
                delay = random.uniform(5, 15)
                logger.warning("API 403エラー、%.0f秒待機後にリトライ", delay)
                await asyncio.sleep(delay)
                await self._relogin()
                return await self.execute_api(url, body, _retried=True)
            raise SwimAuthError(f"API 403エラー (body={resp.text[:500]})")

        if resp.status_code != 200:
            raise SwimAuthError(f"APIエラー (status={resp.status_code})")

        # レスポンス処理時間シミュレーション（ブラウザのDOM更新・レンダリング）
        await asyncio.sleep(random.uniform(0.1, 0.5))
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
