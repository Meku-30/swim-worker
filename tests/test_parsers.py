"""Worker 側 parsers パッケージの入口テスト。

parse_for_job_type が task_params から queried_airport を抽出して
flight.parse_foids に渡すことを検証する (flight_foids parse 化の安全網)。
"""
import pytest

from swim_worker import parsers


class TestParseForJobType:
    def test_flight_foids_passes_queried_airport(self):
        raw = {"flightInformationSearchResultsDTO": {
            "arrivingFlights": [{"foid": "f001", "dep_AD": "RJAA"}]}}
        task_params = {"body": {
            "flightInformationSearchConditionsDTO": {"airportCode": "RJTT"}}}
        records = parsers.parse_for_job_type(
            "collect_flight_foids", raw, task_params=task_params)
        assert len(records) == 1
        assert records[0]["dest_ad"] == "RJTT"
        assert records[0]["dep_ad"] == "RJAA"

    def test_flight_foids_without_task_params_tolerates_none(self):
        """task_params=None でも例外にならない (queried_airport=None として処理)"""
        raw = {"flightInformationSearchResultsDTO": {
            "arrivingFlights": [{"foid": "f001", "dep_AD": "RJAA"}]}}
        records = parsers.parse_for_job_type("collect_flight_foids", raw)
        assert records[0]["dest_ad"] is None

    def test_non_flight_foids_ignores_task_params(self):
        """他 job_type では task_params は無視される"""
        raw = {"weatherDTO": {}}
        # task_params を渡しても 1 引数 parser がそのまま呼ばれる
        records = parsers.parse_for_job_type(
            "collect_pkg_weather", raw, task_params={"body": {}})
        assert records == []

    def test_ret_wrapper_is_unwrapped(self):
        """SWIM の {"ret": {...}} ラッパーは parser 前に剥がされる"""
        raw = {"ret": {"flightInformationSearchResultsDTO": {
            "arrivingFlights": [{"foid": "f001"}]}}}
        records = parsers.parse_for_job_type(
            "collect_flight_foids", raw,
            task_params={"body": {"flightInformationSearchConditionsDTO": {"airportCode": "RJTT"}}})
        assert records[0]["foid"] == "f001"

    def test_unknown_job_type_raises(self):
        with pytest.raises(KeyError):
            parsers.parse_for_job_type("unknown_job", {})


def test_parse_for_job_type_merges_underscore_task_params_into_data():
    """Coordinator の raw 経路と同様、task params の _ 始まりキー (例 _icao_code) を data に加える"""
    from swim_worker import parsers
    captured = {}

    def fake_parser(data):
        captured.update(data)
        return []

    original = parsers._PARSERS["collect_airport_profiles"]
    parsers._PARSERS["collect_airport_profiles"] = fake_parser
    try:
        parsers.parse_for_job_type("collect_airport_profiles", {"ret": {"x": 1}},
                                   task_params={"url": "u", "_icao_code": "RJTT"})
    finally:
        parsers._PARSERS["collect_airport_profiles"] = original
    assert captured == {"x": 1, "_icao_code": "RJTT"}


def test_parse_for_job_type_does_not_override_existing_keys():
    from swim_worker import parsers
    captured = {}
    original = parsers._PARSERS["collect_notams"]
    parsers._PARSERS["collect_notams"] = lambda d: captured.update(d) or []
    try:
        parsers.parse_for_job_type("collect_notams", {"_icao_code": "KEEP"},
                                   task_params={"_icao_code": "OTHER"})
    finally:
        parsers._PARSERS["collect_notams"] = original
    assert captured["_icao_code"] == "KEEP"
