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

        result = await client.execute_api("https://example.com/api", {"key": "val"})
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

        result = await client.execute_api("https://example.com/api", {})
        assert result == {"data": "ok"}
        client._relogin.assert_called_once()


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
