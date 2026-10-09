"""parsers 共通のヘルパー (DB 依存なし。Worker にも同じ内容で配布する)"""
from datetime import datetime, timezone


def parse_compact_utc(s: str | None) -> str | None:
    """SWIM の YYYYMMDDhhmm (UTC) を ISO 8601 文字列で返す (JSON-safe)。読めなければ None"""
    if not s or len(s) < 12:
        return None
    try:
        return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]),
                        int(s[8:10]), int(s[10:12]), tzinfo=timezone.utc).isoformat()
    except (ValueError, IndexError, TypeError):
        return None
