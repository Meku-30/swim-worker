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
