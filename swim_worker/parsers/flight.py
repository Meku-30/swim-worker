"""フライト詳細パーサー

parse_foids/parse_details は DB 依存なし → Worker でも使える。
store_* のみ SQLAlchemy + models を関数内 import で使用する。
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _parse_dt(s: str | None) -> datetime | None:
    if not s or len(s) < 12:
        return None
    try:
        return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]), int(s[10:12]), tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def parse_foids(raw_data: dict) -> list[dict]:
    """FLV803レスポンスからfoid + 補完データを抽出"""
    result = raw_data.get("flightInformationSearchResultsDTO") or {}
    records = []
    for flight in (result.get("arrivingFlights") or []) + (result.get("departingFlights") or []):
        foid = flight.get("foid")
        if not foid:
            continue
        records.append({"foid": foid, "flt_RULES": flight.get("flt_RULES") or "",
            "opr": flight.get("opr") or ""})
    return records


def parse_foids_for_db(raw_data: dict, queried_airport: str) -> list[dict]:
    """FLV803レスポンスからFlightDetail基本データを抽出 (Phase1即保存用)。

    queried_airport: このレスポンスの取得元空港ICAO。到着便ではdest_ad、出発便ではdep_adとなる。
    rte/reg/滑走路/ssta/sstd はNULLのまま（Phase3で補完）。
    """
    result = raw_data.get("flightInformationSearchResultsDTO") or {}
    records = []

    for flight in result.get("arrivingFlights") or []:
        foid = flight.get("foid")
        if not foid:
            continue
        records.append({
            "foid": foid,
            "flight_code": flight.get("flt_INFOCODE"),
            "aircraft_type": flight.get("typ"),
            "dep_ad": flight.get("dep_AD"),
            "dest_ad": queried_airport,
            "flight_rules": flight.get("flt_RULES"),
            "operator": flight.get("opr"),
            "arr_spot": flight.get("arrspotnr"),
            "eta": _parse_dt(flight.get("applicationeta")),
            "ata": _parse_dt(flight.get("ata")),
        })

    for flight in result.get("departingFlights") or []:
        foid = flight.get("foid")
        if not foid:
            continue
        records.append({
            "foid": foid,
            "flight_code": flight.get("flt_INFOCODE"),
            "aircraft_type": flight.get("typ"),
            "dep_ad": queried_airport,
            "dest_ad": flight.get("dest_AD"),
            "flight_rules": flight.get("flt_RULES"),
            "operator": flight.get("opr"),
            "dep_spot": flight.get("depspotnr"),
            "eobt": _parse_dt(flight.get("eobt")),
            "atd": _parse_dt(flight.get("atd")),
        })

    return records


async def store_foids(session_factory, records: list[dict]) -> int:
    """foidリストは保存不要（coordinatorがfilter_new_foidsで使う）。件数を返す。"""
    return len(records) if records else 0


def parse_details(raw_data: dict) -> list[dict]:
    """FLV911レスポンスからFlightDetailレコードを生成"""
    result = raw_data.get("flightDetailsSearchResultsDTO") or {}
    fd = result.get("flightDetails") or {}
    if not fd:
        return []
    foid = fd.get("foid")
    if not foid:
        return []
    reg = fd.get("reg")
    if not reg:
        reg = fd.get("flt_INFOCODE")
    return [{
        "foid": foid, "flight_code": fd.get("flt_INFOCODE"),
        "aircraft_type": fd.get("typ"), "registration": reg,
        "dep_ad": fd.get("dep_AD"), "dest_ad": fd.get("dest_AD"),
        "route": fd.get("rte"),
        "flight_rules": fd.get("flt_RULES"), "operator": fd.get("opr"),
        "sch_dep_rwy": fd.get("sch_DEP_RWY_NR"), "sch_arr_rwy": fd.get("sch_ARR_RWY_NR"),
        "dep_rwy": fd.get("dep_RWY_NR"), "arr_rwy": fd.get("arr_RWY_NR"),
        "dep_spot": fd.get("depspotnr"), "arr_spot": fd.get("arrspotnr"),
        "eobt": _parse_dt(fd.get("eobt")), "eta": _parse_dt(fd.get("eta")),
        "atd": _parse_dt(fd.get("atd")), "ata": _parse_dt(fd.get("ata")),
        "ssta": _parse_dt(fd.get("ssta")), "sstd": _parse_dt(fd.get("sstd")),
        "dep_name_jp": fd.get("dep_AIRPORTNAMEJP"),
        "dest_name_jp": fd.get("dest_AIRPORTNAMEJP"),
    }]


async def store_details(session_factory, records: list[dict]) -> int:
    if not records:
        return 0
    from sqlalchemy import select
    from coordinator.db.models import FlightDetail
    foids = [r["foid"] for r in records]
    async with session_factory() as session:
        existing = await session.execute(
            select(FlightDetail.foid).where(FlightDetail.foid.in_(foids))
        )
        existing_foids = {row[0] for row in existing.all()}
    new_records = [r for r in records if r["foid"] not in existing_foids]
    if not new_records:
        return 0
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        for r in new_records:
            session.add(FlightDetail(**r, collected_at=now))
        await session.commit()
    return len(new_records)
