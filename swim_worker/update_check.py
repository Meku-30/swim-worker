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
_TAG_RE = re.compile(r"/releases/tag/(v?[0-9]+\.[0-9]+\.[0-9]+)\Z")
# リリースのバージョンは "X.Y.Z" だけを受け付ける (先頭の "v" 1 文字は可)。
# Redis・GitHub から来る文字列をそのまま比較・URL 組み立てに使わないため。
# \d は全角数字にも一致し、$ は末尾の改行の前でも一致するので使わない
VERSION_RE = re.compile(r"v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})")

logger = logging.getLogger(__name__)


def parse_version(version) -> tuple[int, int, int] | None:
    """"1.2.3" / "v1.2.3" を (1, 2, 3) に。それ以外 (rc 付き・桁数違い・空白入り等) は None"""
    if not isinstance(version, str):
        return None
    m = VERSION_RE.fullmatch(version)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def version_tuple(version: str) -> tuple[int, ...]:
    """比較用のタプル。不正な文字列は () (どの版より古い扱い)"""
    return parse_version(version) or ()


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
    if parse_version(m.group(1)) is None:
        return None
    return m.group(1).lstrip("v")


def check_update_without_redis(current_version: str, notify) -> str | None:
    """GitHub の最新版が current より新しければ notify(version) を呼び、その版を返す"""
    latest = fetch_latest_release_version()
    if latest and parse_version(latest) and version_tuple(latest) > version_tuple(current_version):
        logger.warning("Redis に入れないため GitHub で最新版を確認: v%s → v%s", current_version, latest)
        notify(latest)
        return latest
    return None
