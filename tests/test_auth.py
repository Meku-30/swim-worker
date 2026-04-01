"""SWIM認証テスト"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from swim_worker.auth import SwimClient, SwimAuthError


@pytest.mark.asyncio
class TestSwimClient:
    async def test_login_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"error_info": {"error_code": 0}}
        mock_response.cookies = {"MSMSI": "val1", "MSMAI": "val2"}

        with patch("swim_worker.auth.AsyncSession") as MockSession:
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

        with patch("swim_worker.auth.AsyncSession") as MockSession:
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
