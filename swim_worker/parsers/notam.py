"""NOTAMパーサー

parse() は DB 依存なし → Worker でも使える (フルパース移行用)。
store() のみ SQLAlchemy + models を関数内 import で使用する。
"""
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_DT_FORMATS = ["%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y%m%d%H%M", "%y%m%d%H%M"]


def _parse_dt(s: str | None) -> datetime | None:
    if not s or s == "PERM":
        return None
    s = s.replace("EST", "").strip()
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse(raw_data: dict) -> list[dict]:
    """SWIM NOTAM API生JSONからレコードリストを生成"""
    records = []
    # Multiple response formats
    notam_list = raw_data.get("notamList") or []
    if not notam_list:
        for loc_info in (raw_data.get("locationInfoList") or []):
            notam_list.extend(loc_info.get("notamList") or [])

    for raw in notam_list:
        notam_id = raw.get("number") or raw.get("notamId") or raw.get("notamNo") or raw.get("id")
        if not notam_id:
            continue
        body = raw.get("contents") or raw.get("list") or raw.get("notamText") or raw.get("body") or raw.get("text")
        if not body:
            body = json.dumps(raw, ensure_ascii=False, default=str)
        records.append({
            "notam_id": str(notam_id),
            "icao_code": raw.get("location") or raw.get("icaoCode") or raw.get("locationId") or "",
            "body": body,
            "valid_from": _parse_dt(raw.get("startDate") or raw.get("validFrom") or raw.get("effectiveFrom")),
            "valid_to": _parse_dt(raw.get("endDate") or raw.get("validTo") or raw.get("effectiveTo")),
            "category": raw.get("notamCode") or raw.get("category") or raw.get("type"),
            "raw_data": raw,
        })
    return records


async def store(session_factory, records: list[dict]) -> int:
    if not records:
        return 0
    from sqlalchemy import select
    from coordinator.db.models import Notam
    ids = [r["notam_id"] for r in records]
    async with session_factory() as session:
        existing = await session.execute(
            select(Notam.notam_id).where(Notam.notam_id.in_(ids))
        )
        existing_ids = {row[0] for row in existing.all()}
    new_records = [r for r in records if r["notam_id"] not in existing_ids]
    if not new_records:
        return 0
    async with session_factory() as session:
        for r in new_records:
            session.add(Notam(
                notam_id=r["notam_id"], icao_code=r["icao_code"], body=r["body"],
                valid_from=r["valid_from"], valid_to=r["valid_to"],
                category=r["category"], raw_data=r["raw_data"],
                collected_at=datetime.now(timezone.utc),
            ))
        await session.commit()
    return len(new_records)
