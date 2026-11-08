"""Python の解析 (swim_worker.parsers.parse_for_job_type) の結果を見本にする。

入力はテストと同じ作った例だけ (本番の raw は入れない)。Rust の swim-parsers が同じ結果を出すかを
rust/crates/swim-parsers/tests/worker_parse.rs が確かめる。
使い方: python tools/golden/dump_parse_cases.py --out rust/tests/golden/parse
"""
import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from swim_worker import parsers  # noqa: E402

WX = {"metarSpeciInfoList": [], "tafInfoList": [], "atisInfoList": [], "useRunwayList": []}
FOIDS_PARAMS = {"body": {"flightInformationSearchConditionsDTO": {"airportCode": "RJTT"}}}

CASES = {
    "collect_notams": [
        ({"notamList": [{"number": "A0001/26", "location": "RJTT", "contents": "test body",
                         "startDate": "2026/03/01 00:00", "endDate": "2026/04/01 00:00", "notamCode": "QMRLC"}]}, None),
        ({"locationInfoList": [{"notamList": [{"notamId": "B0002", "icaoCode": "RJAA", "body": "test"}]}]}, None),
        ({"notamList": [{"location": "RJTT", "contents": "no id"}]}, None),
        ({"ret": {"notamList": []}}, None),
        ({}, None),
        ({"notamList": "not a list"}, None),
        ({"_icao_code": "KEEP"}, {"_icao_code": "OTHER"}),
    ],
    "collect_pireps": [
        ({"turbulencePirepList": [{"imdbTurbulenceInformation": {
            "pirepControlNumber": "TC001", "bodyData": "PIREP body",
            "turbulenceStrength": "MOD", "timeOfObservation": "202603011200"},
            "imdbTurbulenceInformationPosition": [{"latitude": "35.0", "longitude": "139.0"}]}]}, None),
        ({"airepSpecialList": [{"imdbAirepSpecial": {
            "pirepControlNumber": "SP001", "bodyDataInformation": "special",
            "turbulenceType": "SEV"}, "imdbAirepSpecialPosition": []}]}, None),
        ({"sigmetList": [{"imdbSigmet": {"location": "RJJJ", "bodyDataInformation": "SIGMET body",
                                         "timeOfObservation": "202603011200"}}]}, None),
        ({}, None),
    ],
    "collect_pkg_weather": [
        ({"weatherDTO": {**WX, "metarSpeciInfoList": [
            {"location": "RJTT", "body_DATA": "METAR RJTT 010000Z...", "observed_DATE": "202603010000"}]}}, None),
        ({"weatherDTO": {**WX, "metarSpeciInfoList": [
            {"location": "RJTT", "body_DATA": "SPECI RJTT...", "observed_DATE": "202603010015"}]}}, None),
        ({"weatherDTO": {**WX, "atisInfoList": [
            {"location": "RJTT", "body_DATA": "ATIS RJTT\nA\ninfo...", "observed_DATE": "202603010000"}]}}, None),
        ({"weatherDTO": WX, "notamDTOList": [{"number": "I1/26", "coordinates": "3814N14055E"}]}, None),
        ({"weatherDTO": {}}, {"body": {}}),
    ],
    "collect_airspace_data": [({}, None), ({"ret": {}}, None)],
    "collect_airport_profiles": [
        ({"serviceStartTime": "0600", "serviceEndTime": "2300"}, {"url": "u", "_icao_code": "RJTT"}),
        ({}, None),
    ],
    "collect_flight_foids": [
        ({"flightInformationSearchResultsDTO": {
            "arrivingFlights": [{"foid": "f001", "flt_INFOCODE": "JAL001", "typ": "B789", "dep_AD": "RJAA",
                                 "flt_RULES": "I", "opr": "JAL", "applicationeta": "202603011200", "ata": "202603011210"}],
            "departingFlights": [{"foid": "f002", "flt_INFOCODE": "ANA100", "typ": "A320", "dest_AD": "RJCC",
                                  "flt_RULES": "V", "opr": "ANA", "eobt": "202603011300"}]}}, FOIDS_PARAMS),
        ({"flightInformationSearchResultsDTO": {"arrivingFlights": [{"foid": "f001", "dep_AD": "RJAA"}]}}, None),
        ({"ret": {"flightInformationSearchResultsDTO": {
            "arrivingFlights": [{"foid": "f001"}, {"foid": None}, {"dep_AD": "RJAA"}]}}}, FOIDS_PARAMS),
    ],
    "collect_flight_details": [
        ({"flightDetailsSearchResultsDTO": {"flightDetails": {
            "foid": "f001", "flt_INFOCODE": "JAL001", "typ": "B789", "reg": "JA001A", "dep_AD": "RJTT",
            "dest_AD": "RJAA", "rte": "route", "eobt": "202603011200", "eta": "202603011300"}}}, None),
        ({}, None),
    ],
    "collect_aip": [({}, None), ({"html": ""}, None),
                    ({"html": "<table><tr><td>RWY</td><td>34L</td></tr></table>"}, None)],
    "collect_airports": [
        ({"locationInfoList": [{"id": "RJTT", "nameJp": "RJTT(東京国際)"}, {"id": "RJAA", "nameEn": "RJAA(Narita)"}]}, None),
        ({}, None),
    ],
}


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    assert set(CASES) == set(parsers._PARSERS), "Python 側の job_type と見本の job_type がずれた"
    for job_type, cases in CASES.items():
        rows = []
        for data, params in cases:
            row = {"data": copy.deepcopy(data), "params": params}
            try:
                row["parsed"] = parsers.parse_for_job_type(job_type, copy.deepcopy(data), task_params=params)
            except Exception as e:  # noqa: BLE001 — 例外の型名を見本にする
                row["error"] = type(e).__name__
            rows.append(row)
        (out / f"{job_type}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(job_type, len(rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    main(ap.parse_args().out)
