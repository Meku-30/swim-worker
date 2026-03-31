"""SWIM認証・API実行クライアント

swim-api の src/scraper/browser.py を簡素化。
Worker用途に特化: ログイン + execute_api + 自動リトライ。
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

SWIM_LOGIN_URL = "https://top.swim.mlit.go.jp/swim/webapi/login"
SWIM_SESSION_CHECK_URL = "https://web.swim.mlit.go.jp/service/api/accounts/summary"


class SwimAuthError(Exception):
    """SWIM認証エラー"""


class SwimClient:
    """SWIM APIクライアント（Worker用）"""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._client: httpx.AsyncClient | None = None
        self._is_ready = False
        self._relogin_lock = asyncio.Lock()

    async def login(self) -> None:
        """SWIMにログインしてセッションCookieを取得する"""
        logger.info("SWIMポータルにログイン開始")
        try:
            async with httpx.AsyncClient(timeout=30.0) as tmp:
                resp = await tmp.post(
                    SWIM_LOGIN_URL,
                    json={"id": self._username, "password": self._password},
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

        cookies = httpx.Cookies()
        for name, value in resp.cookies.items():
            cookies.set(name, value, domain="mlit.go.jp")
        if not cookies:
            raise SwimAuthError("ログイン後にCookieを取得できませんでした")

        if self._client is not None:
            await self._client.aclose()

        self._client = httpx.AsyncClient(
            cookies=cookies,
            timeout=httpx.Timeout(60.0),
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self._is_ready = True
        logger.info("SWIMポータルにログイン成功")

    async def execute_api(self, url: str, body: dict, *, _retried: bool = False) -> dict:
        """SWIM APIを実行する。403/HTTPエラー時は1回リトライする。"""
        if not self._is_ready or self._client is None:
            await self.login()
        assert self._client is not None

        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
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
            if self._is_ready and self._client is not None:
                try:
                    check = await self._client.get(SWIM_SESSION_CHECK_URL)
                    if check.status_code == 200:
                        return
                except Exception:
                    pass
            self._is_ready = False
            await self.login()

    async def close(self) -> None:
        """リソース解放"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._is_ready = False
