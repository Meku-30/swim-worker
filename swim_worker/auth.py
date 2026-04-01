"""SWIM認証・API実行クライアント

curl_cffi を使用してChrome TLSフィンガープリントを再現し、
リアルなブラウザヘッダーを送信する。
"""
import asyncio
import logging
import random

from curl_cffi.requests import AsyncSession, BrowserType

logger = logging.getLogger(__name__)

SWIM_LOGIN_URL = "https://top.swim.mlit.go.jp/swim/webapi/login"
SWIM_SESSION_CHECK_URL = "https://web.swim.mlit.go.jp/service/api/accounts/summary"
SWIM_PORTAL_URL = "https://web.swim.mlit.go.jp"

# Chrome風ヘッダー
_CHROME_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Ch-Ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
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

    async def login(self) -> None:
        """SWIMにログインしてセッションCookieを取得する"""
        logger.info("SWIMポータルにログイン開始")
        try:
            async with AsyncSession(impersonate=_BROWSER_TYPE) as tmp:
                resp = await tmp.post(
                    SWIM_LOGIN_URL,
                    json={"id": self._username, "password": self._password},
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                        "Content-Type": "application/json",
                        "Origin": "https://top.swim.mlit.go.jp",
                        "Referer": "https://top.swim.mlit.go.jp/",
                    },
                )
        except Exception as e:
            raise SwimAuthError(f"ログインAPI呼び出しエラー: {e}") from e

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

        if self._session is not None:
            await self._session.close()

        self._session = AsyncSession(
            impersonate=_BROWSER_TYPE,
            headers=_CHROME_HEADERS,
            timeout=60.0,
        )
        # ログインCookieを転写（domain=mlit.go.jp指定）
        for name, value in resp.cookies.items():
            self._session.cookies.set(name, value, domain="mlit.go.jp")

        self._is_ready = True
        logger.info("SWIMポータルにログイン成功")

    async def execute_api(self, url: str, body: dict, *, _retried: bool = False) -> dict:
        """SWIM APIを実行する。403/HTTPエラー時は1回リトライする。"""
        if not self._is_ready or self._session is None:
            await self.login()
        assert self._session is not None

        # Refererをリクエスト先に合わせて動的設定
        referer = SWIM_PORTAL_URL + "/"
        extra_headers = {"Referer": referer}

        try:
            resp = await self._session.post(url, json=body, headers=extra_headers)
        except Exception as e:
            if not _retried:
                logger.warning("API HTTPエラー、再ログインしてリトライ: %s", e)
                await self._relogin()
                return await self.execute_api(url, body, _retried=True)
            raise SwimAuthError(f"APIエラー: {e}") from e

        if resp.status_code == 403:
            if not _retried:
                logger.warning("API 403エラー、再ログインしてリトライ")
                await self._relogin()
                return await self.execute_api(url, body, _retried=True)
            raise SwimAuthError(f"API 403エラー (body={resp.text[:500]})")

        if resp.status_code != 200:
            raise SwimAuthError(f"APIエラー (status={resp.status_code})")

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
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        self._is_ready = False
