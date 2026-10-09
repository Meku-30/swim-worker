"""SWIM認証テスト"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from swim_worker.auth import SwimClient, SwimAuthError


@pytest.mark.asyncio
class TestSwimClient:
    async def test_login_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"statusCode": 0, "datas": {}}
        mock_response.cookies = {"MSMSI": "val1", "MSMAI": "val2"}

        # 保存済み Cookie を強制的に無視し、新規ログインパスを確実に通す
        with patch("swim_worker.auth.AsyncSession") as MockSession, \
             patch.object(SwimClient, "_load_cookies", return_value=None):
            # tmpセッション（ログインフロー用、context manager）
            tmp_session = AsyncMock()
            tmp_session.post.return_value = mock_response
            tmp_session.get.return_value = MagicMock(status_code=200)
            tmp_session.cookies = {"MSMSI": "val1", "MSMAI": "val2"}
            tmp_session.__aenter__ = AsyncMock(return_value=tmp_session)
            tmp_session.__aexit__ = AsyncMock(return_value=False)

            # 永続セッション（API呼び出し用）
            persistent_session = AsyncMock()
            persistent_session.cookies = MagicMock()

            MockSession.side_effect = [tmp_session, persistent_session]

            client = SwimClient(username="user", password="pass")
            await client.login()
            assert client._is_ready

    async def test_login_failure_raises(self):
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("swim_worker.auth.AsyncSession") as MockSession, \
             patch.object(SwimClient, "_load_cookies", return_value=None):
            instance = AsyncMock()
            instance.post.return_value = mock_response
            instance.get.return_value = MagicMock(status_code=200)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockSession.return_value = instance

            client = SwimClient(username="user", password="pass")
            with pytest.raises(SwimAuthError):
                await client.login()

    async def test_execute_api_returns_json(self):
        client = SwimClient(username="user", password="pass")
        client._is_ready = True
        client._session = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": "test"}
        client._session.post.return_value = mock_resp

        result = await client.execute_api("https://web.swim.mlit.go.jp/service/api/test", {"key": "val"})
        assert result == {"data": "test"}

    async def test_execute_api_retries_on_403(self):
        client = SwimClient(username="user", password="pass")
        client._is_ready = True
        client._session = AsyncMock()
        client._relogin = AsyncMock()

        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"data": "ok"}
        client._session.post.side_effect = [resp_403, resp_200]

        result = await client.execute_api("https://web.swim.mlit.go.jp/service/api/test", {})
        assert result == {"data": "ok"}
        client._relogin.assert_called_once()

    async def test_execute_api_no_relogin_on_403_when_retry_disabled(self):
        client = SwimClient(username="user", password="pass")
        client._is_ready = True
        session = AsyncMock()
        session.post.return_value = MagicMock(status_code=403, text="forbidden")
        client._session = session
        client._relogin = AsyncMock()
        with patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(SwimAuthError, match="403"):
                await client.execute_api("https://web.swim.mlit.go.jp/service/api/test", {}, retry_on_auth_error=False)
        client._relogin.assert_not_called()
        assert session.post.await_count == 1

    async def test_execute_api_raises_unauthorized_subclass_on_403(self):
        from swim_worker.auth import SwimUnauthorizedError
        client = SwimClient(username="user", password="pass")
        client._is_ready = True
        session = AsyncMock()
        session.post.return_value = MagicMock(status_code=403, text="forbidden")
        client._session = session
        client._relogin = AsyncMock()
        with patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(SwimUnauthorizedError) as ei:
                await client.execute_api("https://web.swim.mlit.go.jp/service/api/test", {}, retry_on_auth_error=False)
        assert ei.value.status_code == 403
        assert isinstance(ei.value, SwimAuthError)

    async def test_execute_api_raises_unauthorized_after_retry_still_403(self):
        from swim_worker.auth import SwimUnauthorizedError
        client = SwimClient(username="user", password="pass")
        client._is_ready = True
        session = AsyncMock()
        session.post.return_value = MagicMock(status_code=401, text="nope")
        client._session = session
        client._relogin = AsyncMock()
        with patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(SwimUnauthorizedError):
                await client.execute_api("https://web.swim.mlit.go.jp/service/api/test", {})
        client._relogin.assert_awaited_once()


@pytest.mark.asyncio
class TestLoginBackoff:
    """ログイン失敗時の指数バックオフ (認証情報誤り時に SWIM を叩き続けない)"""

    @staticmethod
    def _failing_session_patch():
        mock_response = MagicMock()
        mock_response.status_code = 401
        instance = AsyncMock()
        instance.post.return_value = mock_response
        instance.get.return_value = MagicMock(status_code=200)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        return instance

    async def test_second_login_is_suppressed_without_hitting_swim(self):
        instance = self._failing_session_patch()
        with patch("swim_worker.auth.AsyncSession", return_value=instance), \
             patch.object(SwimClient, "_load_cookies", return_value=None), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            client = SwimClient(username="user", password="pass")
            with pytest.raises(SwimAuthError):
                await client.login()
            assert instance.post.await_count == 1
            # 抑制中: SWIM へリクエストせず即エラー
            with pytest.raises(SwimAuthError, match="抑制中"):
                await client.login()
            assert instance.post.await_count == 1

    async def test_backoff_grows_exponentially_and_is_capped(self):
        instance = self._failing_session_patch()
        now = {"t": 1000.0}
        with patch("swim_worker.auth.AsyncSession", return_value=instance), \
             patch.object(SwimClient, "_load_cookies", return_value=None), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()), \
             patch("swim_worker.auth.time.monotonic", side_effect=lambda: now["t"]):
            client = SwimClient(username="user", password="pass",
                                login_backoff_base=60.0, login_backoff_max=300.0)
            expected = [60.0, 120.0, 240.0, 300.0, 300.0]
            for i, exp in enumerate(expected):
                with pytest.raises(SwimAuthError):
                    await client.login()
                assert instance.post.await_count == i + 1
                assert client._login_blocked_until - now["t"] == pytest.approx(exp)
                now["t"] += exp  # 抑制期間を経過させて次の試行を許可

    async def test_success_resets_backoff(self):
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"statusCode": 0, "datas": {}}
        ok_response.cookies = {"MSMSI": "v"}
        tmp = AsyncMock()
        tmp.post.return_value = ok_response
        tmp.get.return_value = MagicMock(status_code=200)
        tmp.cookies = {"MSMSI": "v"}
        tmp.__aenter__ = AsyncMock(return_value=tmp)
        tmp.__aexit__ = AsyncMock(return_value=False)
        persistent = AsyncMock()
        persistent.cookies = MagicMock()
        with patch("swim_worker.auth.AsyncSession", side_effect=[tmp, persistent]), \
             patch.object(SwimClient, "_load_cookies", return_value=None), \
             patch.object(SwimClient, "_save_cookies"), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            client = SwimClient(username="user", password="pass")
            client._login_failures = 3
            client._login_blocked_until = 0.0
            await client.login()
            assert client._login_failures == 0
            assert client._login_blocked_until == 0.0


API = "https://web.swim.mlit.go.jp/f2aspr/web/FLV920/LGV358"


class TestUrlAllowlist:
    """W1-2: タスクの URL は https://*.swim.mlit.go.jp だけ"""

    def test_allowed(self):
        from swim_worker.auth import validate_swim_url
        for url in (API,
                    "https://top.swim.mlit.go.jp/swim/api/informations",
                    "https://web.swim.mlit.go.jp:443/f2dnrq/web/FUV201/USV001",
                    "https://WEB.SWIM.MLIT.GO.JP/x"):
            validate_swim_url(url)

    def test_rejected(self):
        from swim_worker.auth import validate_swim_url, DisallowedUrlError
        for url in ("http://web.swim.mlit.go.jp/x",
                    "https://example.com/api",
                    "https://web.swim.mlit.go.jp.example.com/x",
                    "https://evilswim.mlit.go.jp/x",
                    "https://swim.mlit.go.jp/x",
                    "https://mlit.go.jp/x",
                    "https://user@web.swim.mlit.go.jp/x",
                    "https://user:pw@web.swim.mlit.go.jp/x",
                    "https://web.swim.mlit.go.jp:8443/x",
                    "https://example.com\\@web.swim.mlit.go.jp/x",
                    "https://web.swim.mlit.go.jp./x",
                    "https://10.0.0.1/x",
                    "https:///x",
                    "file:///etc/passwd",
                    "", None, 123):
            with pytest.raises(DisallowedUrlError):
                validate_swim_url(url)


@pytest.mark.asyncio
class TestUrlAllowlistInClient:
    async def test_execute_api_rejects_before_any_request(self):
        from swim_worker.auth import DisallowedUrlError
        client = SwimClient(username="user", password="[REDACTED]")
        client.login = AsyncMock()
        client._session = AsyncMock()
        client._is_ready = True
        with pytest.raises(DisallowedUrlError):
            await client.execute_api("https://example.com/api", {})
        client.login.assert_not_called()
        client._session.post.assert_not_called()
        client._session.get.assert_not_called()

    async def test_fetch_public_get_rejects(self):
        from swim_worker.auth import DisallowedUrlError
        client = SwimClient(username="user", password="[REDACTED]")
        with patch("swim_worker.auth.AsyncSession") as MockSession:
            with pytest.raises(DisallowedUrlError):
                await client.fetch_public_get("https://example.com/informations")
            MockSession.assert_not_called()

    async def test_execute_api_rejects_redirect_outside(self):
        from swim_worker.auth import DisallowedUrlError
        client = SwimClient(username="user", password="[REDACTED]")
        client._is_ready = True
        client._session = AsyncMock()
        resp = MagicMock(status_code=200, url="https://example.com/landing")
        resp.json.return_value = {"x": 1}
        client._session.post.return_value = resp
        with patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(DisallowedUrlError):
                await client.execute_api(API, {})
        resp.json.assert_not_called()

    async def test_redirect_outside_is_distinguishable(self):
        """SWIM の外へのリダイレクト (メンテ中のお知らせページ等) は専用の例外 (DisallowedUrlError の
        サブクラス)。メッセージにはクエリを出さない"""
        from swim_worker.auth import RedirectOutsideError, DisallowedUrlError
        client = SwimClient(username="user", password="[REDACTED]")
        client._is_ready = True
        client._session = AsyncMock()
        resp = MagicMock(status_code=200, url="https://maint.example.jp/info?token=[REDACTED]")
        client._session.post.return_value = resp
        with patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(RedirectOutsideError) as ei:
                await client.execute_api(API, {})
        assert isinstance(ei.value, DisallowedUrlError)
        assert "maint.example.jp" in str(ei.value) and "secret" not in str(ei.value)
        assert "メンテナンス" in str(ei.value)

    async def test_fetch_public_get_rejects_redirect_outside(self):
        from swim_worker.auth import DisallowedUrlError
        client = SwimClient(username="user", password="[REDACTED]")
        tmp = AsyncMock()
        tmp.get.return_value = MagicMock(status_code=200, url="https://example.com/")
        tmp.__aenter__ = AsyncMock(return_value=tmp)
        tmp.__aexit__ = AsyncMock(return_value=False)
        with patch("swim_worker.auth.AsyncSession", return_value=tmp):
            with pytest.raises(DisallowedUrlError):
                await client.fetch_public_get("https://top.swim.mlit.go.jp/swim/api/informations")


def _login_session(json_value=None, json_exc=None):
    resp = MagicMock(status_code=200)
    if json_exc is not None:
        resp.json.side_effect = json_exc
    else:
        resp.json.return_value = json_value
    resp.cookies = {"MSMSI": "v"}
    tmp = AsyncMock()
    tmp.post.return_value = resp
    tmp.get.return_value = MagicMock(status_code=200)
    tmp.cookies = {"MSMSI": "v"}
    tmp.__aenter__ = AsyncMock(return_value=tmp)
    tmp.__aexit__ = AsyncMock(return_value=False)
    persistent = AsyncMock()
    persistent.cookies = MagicMock()
    return tmp, persistent


@pytest.mark.asyncio
class TestLoginDetails:
    async def test_redirect_url_outside_swim_is_not_followed(self):
        tmp, persistent = _login_session({"statusCode": 0, "datas": {"redirectUrl": "https://example.com/x"}})
        with patch("swim_worker.auth.AsyncSession", side_effect=[tmp, persistent]), \
             patch.object(SwimClient, "_load_cookies", return_value=None), \
             patch.object(SwimClient, "_save_cookies"), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            client = SwimClient(username="user", password="[REDACTED]")
            await client.login()
        urls = [c.args[0] for c in tmp.get.call_args_list]
        assert "https://example.com/x" not in urls
        assert "https://web.swim.mlit.go.jp/service/portal?lang=ja" in urls

    async def test_redirect_url_inside_swim_is_followed(self):
        target = "https://web.swim.mlit.go.jp/service/portal?lang=en"
        tmp, persistent = _login_session({"statusCode": 0, "datas": {"redirectUrl": target}})
        with patch("swim_worker.auth.AsyncSession", side_effect=[tmp, persistent]), \
             patch.object(SwimClient, "_load_cookies", return_value=None), \
             patch.object(SwimClient, "_save_cookies"), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            client = SwimClient(username="user", password="[REDACTED]")
            await client.login()
        assert target in [c.args[0] for c in tmp.get.call_args_list]

    async def test_cookies_are_set_secure(self):
        tmp, persistent = _login_session({"statusCode": 0, "datas": {}})
        with patch("swim_worker.auth.AsyncSession", side_effect=[tmp, persistent]), \
             patch.object(SwimClient, "_load_cookies", return_value=None), \
             patch.object(SwimClient, "_save_cookies"), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            client = SwimClient(username="user", password="[REDACTED]")
            await client.login()
        calls = persistent.cookies.set.call_args_list
        assert calls and all(c.kwargs.get("secure") is True for c in calls)

    async def test_non_json_login_response_does_not_crash(self):
        """W1-6: resp.json() が失敗しても NameError にならず既定の遷移先へ進む"""
        tmp, persistent = _login_session(json_exc=ValueError("not json"))
        with patch("swim_worker.auth.AsyncSession", side_effect=[tmp, persistent]), \
             patch.object(SwimClient, "_load_cookies", return_value=None), \
             patch.object(SwimClient, "_save_cookies"), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()), \
             patch("swim_worker.auth.logger") as log:
            client = SwimClient(username="user", password="[REDACTED]")
            await client.login()
        assert client._is_ready
        assert "https://web.swim.mlit.go.jp/service/portal?lang=ja" in [c.args[0] for c in tmp.get.call_args_list]
        assert not any("NameError" in str(c) for c in log.mock_calls)

    async def test_saved_cookies_are_tried_only_on_first_login(self):
        """W1-6: 保存済み Cookie の復元は起動後の最初のログインだけ (失効した Cookie を何度も試さない)"""
        saved = {"MSMSI": "old"}
        restore = AsyncMock()
        restore.get.return_value = MagicMock(status_code=401)
        restore.cookies = MagicMock()
        tmp1, p1 = _login_session({"statusCode": 0, "datas": {}})
        tmp2, p2 = _login_session({"statusCode": 0, "datas": {}})
        with patch("swim_worker.auth.AsyncSession", side_effect=[restore, tmp1, p1, tmp2, p2]), \
             patch.object(SwimClient, "_load_cookies", return_value=saved) as load, \
             patch.object(SwimClient, "_save_cookies"), \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            client = SwimClient(username="user", password="[REDACTED]")
            await client.login()
            await client.login()
        assert load.call_count == 1
        assert tmp1.post.await_count == 1 and tmp2.post.await_count == 1

    async def test_suppressed_login_does_not_touch_swim_even_with_saved_cookies(self):
        """W1-6: ログイン抑制中は保存済み Cookie の復元 (SWIM へのアクセス) もしない"""
        with patch("swim_worker.auth.AsyncSession") as MockSession, \
             patch.object(SwimClient, "_load_cookies", return_value={"MSMSI": "v"}) as load:
            client = SwimClient(username="user", password="[REDACTED]")
            client._login_failures = 2
            import time as _t
            client._login_blocked_until = _t.monotonic() + 100
            with pytest.raises(SwimAuthError, match="抑制中"):
                await client.login()
        MockSession.assert_not_called()
        load.assert_not_called()

    async def test_cookies_saved_periodically_after_api_success(self):
        """W1-6: 長く動いている間も Cookie を定期的に保存する (異常終了しても次回復元できる)"""
        now = {"t": 1000.0}
        client = SwimClient(username="user", password="[REDACTED]")
        client._is_ready = True
        client._session = AsyncMock()
        resp = MagicMock(status_code=200, url=API)
        resp.json.return_value = {}
        client._session.post.return_value = resp
        client._visited_pages.add("https://web.swim.mlit.go.jp/f2aspr/browse/flv850s001")
        with patch.object(SwimClient, "_save_cookies") as save, \
             patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()), \
             patch("swim_worker.auth.time.monotonic", side_effect=lambda: now["t"]):
            client._last_cookie_save = 1000.0
            await client.execute_api(API, {})
            assert save.call_count == 0
            now["t"] += client.COOKIE_SAVE_INTERVAL + 1
            await client.execute_api(API, {})
            assert save.call_count == 1
