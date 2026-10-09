"""Redis を通さない最新版の確認のテスト"""
from unittest.mock import MagicMock, patch

from swim_worker import update_check


def _fake_urlopen(final_url):
    resp = MagicMock()
    resp.geturl.return_value = final_url
    cm = MagicMock()
    cm.__enter__.return_value = resp
    return MagicMock(return_value=cm)


def test_fetch_latest_parses_redirect_tag():
    url = "https://github.com/Meku-30/swim-worker/releases/tag/v1.2.3"
    with patch("swim_worker.update_check.urllib.request.urlopen", _fake_urlopen(url)):
        assert update_check.fetch_latest_release_version() == "1.2.3"


def test_fetch_latest_returns_none_on_error_or_unexpected_url():
    with patch("swim_worker.update_check.urllib.request.urlopen", side_effect=OSError("down")):
        assert update_check.fetch_latest_release_version() is None
    with patch("swim_worker.update_check.urllib.request.urlopen",
               _fake_urlopen("https://github.com/Meku-30/swim-worker/releases")):
        assert update_check.fetch_latest_release_version() is None


def test_check_update_notifies_only_when_newer():
    notify = MagicMock()
    with patch.object(update_check, "fetch_latest_release_version", return_value="1.3.0"):
        assert update_check.check_update_without_redis("1.2.0", notify) == "1.3.0"
    notify.assert_called_once_with("1.3.0")

    notify.reset_mock()
    for latest in ("1.2.0", "1.1.9", None):
        with patch.object(update_check, "fetch_latest_release_version", return_value=latest):
            assert update_check.check_update_without_redis("1.2.0", notify) is None
    notify.assert_not_called()


def test_parse_version_is_strict():
    assert update_check.parse_version("1.2.3") == (1, 2, 3)
    assert update_check.parse_version("v1.2.3") == (1, 2, 3)
    for bad in ("1.2", "1.2.3.4", "v1.2.3-rc1", " 1.2.3", "1.2.3\n", "vv1.2.3", "", None, "a.b.c"):
        assert update_check.parse_version(bad) is None, repr(bad)


def test_fetch_latest_rejects_non_semver_tag():
    url = "https://github.com/Meku-30/swim-worker/releases/tag/v1.2"
    with patch("swim_worker.update_check.urllib.request.urlopen", _fake_urlopen(url)):
        assert update_check.fetch_latest_release_version() is None
