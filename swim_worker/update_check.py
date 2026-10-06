"""Redis を通さない最新版の確認 (Redis の認証に失敗した時の自動更新用)

普段の最新版の通知は Coordinator が Redis に置いた値で行う (管理者の一時停止・段階配布が効く)。
Redis の認証に失敗した Worker (パスワードの作り直し・入力ミスなど) はその値を読めないので、
GitHub の releases/latest のリダイレクト先から最新版を知り、自動更新を止めないようにする。
GitHub API は使わない (回数制限を避ける)。
"""
import logging
import re
import urllib.request

LATEST_RELEASE_URL = "https://github.com/Meku-30/swim-worker/releases/latest"
_TAG_RE = re.compile(r"/releases/tag/v?(\d+(?:\.\d+)*)$")

logger = logging.getLogger(__name__)


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.lstrip("v").split(".") if x.isdigit())


def fetch_latest_release_version(timeout: float = 10.0) -> str | None:
    """GitHub の最新リリースのバージョン ("1.2.0") を返す。取れなければ None"""
    try:
        req = urllib.request.Request(LATEST_RELEASE_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
    except Exception as e:
        logger.warning("GitHub の最新版の確認に失敗: %s", e)
        return None
    m = _TAG_RE.search(final_url)
    if not m:
        logger.warning("GitHub の最新版の URL を解釈できない: %s", final_url)
        return None
    return m.group(1)


def check_update_without_redis(current_version: str, notify) -> str | None:
    """GitHub の最新版が current より新しければ notify(version) を呼び、その版を返す"""
    latest = fetch_latest_release_version()
    if latest and version_tuple(latest) > version_tuple(current_version):
        logger.warning("Redis に入れないため GitHub で最新版を確認: v%s → v%s", current_version, latest)
        notify(latest)
        return latest
    return None
