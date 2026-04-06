"""SWIM認証・API実行クライアント

curl_cffi を使用してChrome TLSフィンガープリントを再現し、
リアルなブラウザヘッダーを送信する。
"""
import asyncio
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

from curl_cffi.requests import AsyncSession, BrowserType

logger = logging.getLogger(__name__)

SWIM_LOGIN_URL = "https://top.swim.mlit.go.jp/swim/api/login"
SWIM_SESSION_CHECK_URL = "https://web.swim.mlit.go.jp/service/api/accounts/summary"
SWIM_PORTAL_URL = "https://web.swim.mlit.go.jp"
SWIM_TOP_URL = "https://top.swim.mlit.go.jp"


def _resolve_cookie_file(override: str = "") -> str:
    """環境に応じたCookie保存先パスを決定する。

    優先順位:
      1. override（明示指定、環境変数 COOKIE_FILE 等）
      2. Docker環境（/app 配下で実行中）→ /app/data/.swim_cookies.json
      3. PyInstaller exe（frozen）→ exe と同じディレクトリの data/
      4. それ以外（VPS systemd等）→ ワーキングディレクトリの data/
    """
    if override:
        return override

    cookie_name = ".swim_cookies.json"

    # Docker: /app 配下で実行中
    if Path("/app").is_dir() and str(Path.cwd()).startswith("/app"):
        return f"/app/data/{cookie_name}"

    # PyInstaller exe: exe と同じディレクトリ
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).parent / "data" / cookie_name)

    # VPS / ローカル開発: ワーキングディレクトリ
    return str(Path.cwd() / "data" / cookie_name)

# API種別ごとのReferer（ポータルの実際の画面URLを再現）
_REFERER_MAP = {
    "/f2dnrq/": f"{SWIM_PORTAL_URL}/f2dnrq/browse/FUV201",       # NOTAM/空港一覧
    "/f2aspr/": f"{SWIM_PORTAL_URL}/f2aspr/browse/flv850s001",    # 空域プロファイル（全f2aspr APIの入口）
}

# SPA初期化リクエスト — 実ブラウザがブラウズ画面を開く際に自動で読み込むリソース
# (method, path, body, header_type) のリスト。
# header_type: "jquery" / "angular" / "angular_res" / "luciadria"
#   jquery      → X-Requested-With: XMLHttpRequest, Accept: json+js (jQuery $.ajax)
#   jquery_text → X-Requested-With: XMLHttpRequest, Accept: text/plain (LuciadRIA/ATCMAP)
#   angular     → Origin付きPOST, Accept: json (Angular HttpClient)
#   angular_res → Accept: */* (Angular リソースバンドル)
#   luciadria   → Accept: application/javascript, application/json (地図ライブラリ)
# 順序・body は Playwright キャプチャ (2026-04-02/04-06) で実測。

_BASIC_BODY = {
    "msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}},
    "ctrlInfo": {},
    "ctrlHeader": {},
}

_SPA_INIT_REQUESTS: dict[str, list[tuple[str, str, dict | None, str]]] = {
    "f2aspr": [
        ("POST", "LuciadRIALicense", None, "jquery_text"),
        ("GET",  "js/lib/WebGIS/ATCMAP.settings", None, "jquery_text"),
        ("GET",  "settings/auto_filter.json", None, "jquery"),
        ("POST", "web/FLV901/LGV300", {**_BASIC_BODY}, "angular"),
        ("POST", "web/FLV811/LGV231", {**_BASIC_BODY, "profileType": 0, "lang": "ja"}, "angular"),
        ("GET",  "settings/map_disp.json", None, "jquery"),
        ("GET",  "web/resource/message", None, "angular_res"),
        ("GET",  "web/resource/webfw", None, "angular_res"),
        ("GET",  "web/resource/user", None, "angular_res"),
        # ブラウズ画面GETはここで挿入（resource/userの後、_ensure_browse_pageで処理）
        ("POST", "web/FLV802/LGV205", {**_BASIC_BODY, "profileType": 0}, "angular"),
        ("POST", "web/FLV934/LGV387", {**_BASIC_BODY}, "angular"),
        ("GET",  "settings/velocity.json", None, "jquery"),
        ("GET",  "settings/default_view.json", None, "jquery"),
        ("GET",  "settings/default_font.json", None, "jquery"),
        ("GET",  "settings/default_dire_dist_position.json", None, "jquery"),
        ("GET",  "settings/shape_datablock_setting.json", None, "jquery"),
        ("GET",  "settings/default_color.json", None, "jquery"),
        ("GET",  "settings/map_disp.json", None, "jquery"),
        ("GET",  "settings/menu.json", None, "jquery"),
        ("GET",  "settings/commonMenuSetting.json", None, "jquery"),
        ("GET",  "settings/toolbarSetting.json", None, "jquery"),
        ("GET",  "settings/blink_info.json", None, "jquery"),
        ("GET",  "settings/groupLayer.json", None, "jquery"),
    ],
    "f2dnrq": [
        ("POST", "LuciadRIALicense", None, "jquery_text"),
        ("GET",  "js/lib/WebGIS/ATCMAP.settings", None, "jquery_text"),
        ("GET",  "settings/auto_filter.json", None, "jquery"),
        ("POST", "web/FUV201/USV005", {**_BASIC_BODY}, "angular"),
        ("GET",  "settings/map_disp.json", None, "jquery"),
        ("GET",  "web/resource/message", None, "angular_res"),
        ("GET",  "web/resource/webfw", None, "angular_res"),
        ("GET",  "web/resource/user", None, "angular_res"),
        # ブラウズ画面GETはここで挿入（resource/userの後）
        ("GET",  "settings/velocity.json", None, "jquery"),
        ("GET",  "settings/default_view.json", None, "jquery"),
        ("GET",  "settings/default_font.json", None, "jquery"),
        ("GET",  "settings/default_dire_dist_position.json", None, "jquery"),
        ("GET",  "settings/shape_datablock_setting.json", None, "jquery"),
        ("GET",  "settings/default_color.json", None, "jquery"),
        ("GET",  "settings/map_disp.json", None, "jquery"),
        ("GET",  "settings/menu.json", None, "jquery"),
        ("GET",  "settings/commonMenuSetting.json", None, "jquery"),
        ("GET",  "settings/toolbarSetting.json", None, "jquery"),
        ("GET",  "settings/blink_info.json", None, "jquery"),
        ("GET",  "settings/groupLayer.json", None, "jquery"),
        ("POST", "web/FUV201/USV005", {**_BASIC_BODY}, "angular"),
        ("GET",  "js/lib/WebGIS/layer/UTM0.json", None, "luciadria"),
        ("GET",  "js/lib/WebGIS/layer/UTM1.json", None, "luciadria"),
        ("GET",  "js/lib/WebGIS/layer/UTM2.json", None, "luciadria"),
        ("GET",  "js/lib/WebGIS/layer/UTM3.json", None, "luciadria"),
    ],
}


def _get_referer(url: str) -> str:
    """URLに応じたRefererを返す"""
    for prefix, referer in _REFERER_MAP.items():
        if prefix in url:
            return referer
    return f"{SWIM_PORTAL_URL}/"


# User-Agent, Sec-Ch-Ua, Sec-Ch-Ua-Platform はcurl_cffiのchrome136デフォルトに任せる
# （TLSフィンガープリントとの一貫性を維持するため）
# セッションデフォルトヘッダー（XHR/ナビゲーション共通）
# Origin と X-Requested-With はXHR固有のため、セッションデフォルトには含めない。
# ナビゲーションGETとのマージ時に残留してbot判定されるリスクを回避する。
_SESSION_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# POST/PUT時に追加するヘッダー（Chromeの標準動作: GETではOriginを送信しない）
_POST_EXTRA_HEADERS = {
    "Origin": SWIM_PORTAL_URL,
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

    def __init__(self, username: str, password: str, cookie_file: str = "") -> None:
        self._username = username
        self._password = password
        self._cookie_file = _resolve_cookie_file(cookie_file)
        self._session: AsyncSession | None = None
        self._is_ready = False
        self._relogin_lock = asyncio.Lock()
        # 応答速度ベーススロットリング
        self._last_response_time: float = 0.0
        self._slow_threshold: float = 10.0  # 10秒以上で「遅い」判定
        self._extra_delay: float = 0.0  # 追加遅延（秒）
        # 訪問済みブラウズ画面（セッション中に1回GETしたURL）
        self._visited_pages: set[str] = set()
        logger.info("Cookie保存先: %s", self._cookie_file)

    def _save_cookies(self) -> None:
        """セッションCookieをファイルに保存"""
        if self._session is None:
            return
        try:
            cookies = {}
            for name, value in self._session.cookies.items():
                cookies[name] = value
            os.makedirs(os.path.dirname(self._cookie_file), exist_ok=True)
            with open(self._cookie_file, "w") as f:
                json.dump(cookies, f)
            logger.info("Cookie保存: %d個 → %s", len(cookies), self._cookie_file)
        except Exception as e:
            logger.warning("Cookie保存失敗: %s", e)

    def _load_cookies(self) -> dict | None:
        """保存済みCookieを読み込む"""
        try:
            if not os.path.exists(self._cookie_file):
                logger.debug("Cookieファイルなし: %s", self._cookie_file)
                return None
            with open(self._cookie_file) as f:
                cookies = json.load(f)
            if cookies:
                logger.info("保存済みCookie読み込み: %d個 (%s)", len(cookies), self._cookie_file)
                return cookies
        except Exception as e:
            logger.warning("Cookie読み込み失敗: %s", e)
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
                headers=_SESSION_HEADERS,
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
            except Exception as e:
                logger.debug("Cookie復元後のナビゲーション失敗: %s", e)
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
                await tmp.get(f"{SWIM_TOP_URL}/swim/", headers=_NAV_HEADERS)
                await asyncio.sleep(random.uniform(1.0, 3.0))

                # 2. ログインPOST（SPA内のXHR）
                resp = await tmp.post(
                    SWIM_LOGIN_URL,
                    json={"userId": self._username, "password": self._password},
                    headers={
                        **_SESSION_HEADERS,
                        "Origin": SWIM_TOP_URL,
                        "Referer": f"{SWIM_TOP_URL}/swim/login",
                    },
                )

                if resp.status_code != 200:
                    raise SwimAuthError(f"ログインAPI失敗 (status={resp.status_code})")

                try:
                    data = resp.json()
                    status_code = data.get("statusCode", -1)
                    if status_code != 0:
                        msg = data.get("message", "unknown")
                        raise SwimAuthError(f"ログインAPIエラー (statusCode={status_code}, message={msg})")
                except (ValueError, KeyError):
                    pass

                if not resp.cookies:
                    raise SwimAuthError("ログイン後にCookieを取得できませんでした")

                # 3. web.swim への遷移（ログインレスポンスの redirectUrl に従う）
                redirect_url = f"{SWIM_PORTAL_URL}/service/portal?lang=ja"
                try:
                    redirect_url = data.get("datas", {}).get("redirectUrl", redirect_url)
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await tmp.get(redirect_url, headers={
                    **_NAV_HEADERS,
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
            headers=_SESSION_HEADERS,
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
                ref = browse_url
                # ヘッダーパターン（2026-04-06 Playwrightキャプチャ実測）
                _H = {
                    "jquery": lambda m: {
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": ref,
                        **({"Origin": SWIM_PORTAL_URL} if m == "POST" else {}),
                    },
                    "jquery_text": lambda m: {
                        "Accept": "text/plain, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": ref,
                        **({"Origin": SWIM_PORTAL_URL} if m == "POST" else {}),
                    },
                    "angular": lambda m: {
                        **_POST_EXTRA_HEADERS,
                        "Referer": ref,
                    },
                    "angular_res": lambda _: {
                        "Accept": "*/*",
                        "Referer": ref,
                    },
                    "luciadria": lambda _: {
                        "Accept": "application/javascript, application/json",
                        "Referer": ref,
                    },
                }
                browse_count = 0
                # フェーズブレイク: 実ブラウザではJSパース/実行で特定箇所に長めの間隔が入る
                _PHASE_BREAK_AFTER = {"ATCMAP.settings", "web/resource/user", "settings/groupLayer.json"}
                for method, path, body, htype in _SPA_INIT_REQUESTS[service_prefix]:
                    try:
                        url = f"{base}/{path}"
                        h = _H[htype](method)
                        if method == "POST":
                            if body is not None:
                                await self._session.post(url, json=body, headers=h)
                            else:
                                await self._session.post(url, headers=h)
                        else:
                            await self._session.get(url, headers=h)
                        # フェーズブレイク or 通常の短い間隔
                        if any(marker in path for marker in _PHASE_BREAK_AFTER):
                            await asyncio.sleep(random.uniform(0.3, 1.0))
                        else:
                            await asyncio.sleep(random.uniform(0.02, 0.15))
                    except Exception:
                        pass

                    # resource/user の後にブラウズ画面の追加GETが来る（実測パターン）
                    # jQuery $.ajax 経由: X-Requested-With + json/js Accept
                    if path == "web/resource/user" and browse_count == 0:
                        browse_jquery_h = _H["jquery"]("GET")
                        for _ in range(3):
                            try:
                                await self._session.get(browse_url, headers=browse_jquery_h)
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

        extra_headers = {**_POST_EXTRA_HEADERS, "Referer": _get_referer(url)}

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
                await self._relogin(force=True)
                return await self.execute_api(url, body, _retried=True)
            raise SwimAuthError(f"API {resp.status_code}エラー (body={resp.text[:500]})")

        if resp.status_code != 200:
            raise SwimAuthError(f"APIエラー (status={resp.status_code})")

        # レスポンス処理時間シミュレーション（ブラウザのDOM更新・レンダリング）
        # 実ブラウザはレスポンスサイズやJS処理量で変動するため、ばらつきを持たせる
        await asyncio.sleep(random.expovariate(3.0) + 0.05)  # 中央値~0.38秒、稀に1-2秒
        return resp.json()

    async def fetch_public_get(self, url: str, params: dict | None = None,
                               headers: dict | None = None) -> dict:
        """認証不要の公開GET API を実行する（メンテナンス情報API等）。

        SwimClientの認証済みセッションを使わず、毎回一時セッションを生成する。
        top.swim.mlit.go.jp/swim/api/informations 等の公開APIで使用。
        """
        merged_headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Referer": f"{SWIM_TOP_URL}/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if headers:
            merged_headers.update(headers)

        async with AsyncSession(impersonate=_BROWSER_TYPE, timeout=60.0) as client:
            resp = await client.get(url, params=params or {}, headers=merged_headers)
            if resp.status_code != 200:
                raise SwimAuthError(f"公開GET APIエラー (status={resp.status_code})")
            return resp.json()

    async def _relogin(self, *, force: bool = False) -> None:
        """再ログイン（ロック付き）

        force=True: セッションチェックをスキップして強制再ログイン（403起因時）
        """
        async with self._relogin_lock:
            if not force and self._is_ready and self._session is not None:
                try:
                    check = await self._session.get(SWIM_SESSION_CHECK_URL)
                    if check.status_code == 200:
                        return
                except Exception:
                    pass
            self._is_ready = False
            if force:
                # 403起因: 保存済みCookieも期限切れの可能性が高いので削除
                try:
                    if os.path.exists(self._cookie_file):
                        os.remove(self._cookie_file)
                        logger.debug("保存済みCookie削除（強制再ログイン）")
                except OSError:
                    pass
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
