"""フライト詳細パーサー

DB 依存なし (Worker と同じ内容)。DB への保存は coordinator/store.py。
"""
import logging

from . import diagnostics
from .common import parse_compact_utc

logger = logging.getLogger(__name__)

_JOB_TYPE_FOIDS = "collect_flight_foids"
_JOB_TYPE_DETAILS = "collect_flight_details"
_KNOWN_FOIDS_KEYS = {"flightInformationSearchResultsDTO"}
_KNOWN_DETAILS_KEYS = {"flightDetailsSearchResultsDTO"}
_IGNORED_KEYS = {
    "msgHeader", "ctrlInfo", "ctrlHeader", "errorInfoDTO",  # プロトコル処理結果メタデータ
    "flightRemovalTriggerDTO",  # SWIM側のフライト除去タイマー設定 (静的値、フライトデータではない)
}

_parse_dt = parse_compact_utc


def parse_foids(raw_data: dict, queried_airport: str | None = None) -> list[dict]:
    """FLV803レスポンスからFlightDetail基本データを抽出 (list 即保存用)。

    queried_airport: このレスポンスの取得元空港ICAO。到着便ではdest_ad、出発便ではdep_adとなる。
    指定しない場合は該当側が None になる (enrich で補完される前提)。
    rte/reg/滑走路/ssta/sstd はNULLのまま（enrich で補完）。
    """
    diagnostics.check_unknown_keys(_JOB_TYPE_FOIDS, raw_data, _KNOWN_FOIDS_KEYS, _IGNORED_KEYS)
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


def parse_details(raw_data: dict) -> list[dict]:
    """FLV911レスポンスからFlightDetailレコードを生成"""
    diagnostics.check_unknown_keys(_JOB_TYPE_DETAILS, raw_data, _KNOWN_DETAILS_KEYS, _IGNORED_KEYS)
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
