//! tests/test_parsers.py を移したもの。
#![allow(non_snake_case)]

use serde_json::{json, Map, Value};
use swim_parsers::parsers::diagnostics::{Diag, MAX_SAMPLES_PER_JOB_TYPE, MAX_SAMPLE_BYTES};
use swim_parsers::parsers::{airport, airspace, flight, notam, pirep, weather};

fn raw(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}

fn s<'a>(r: &'a Map<String, Value>, k: &str) -> &'a str {
    r[k].as_str()
        .unwrap_or_else(|| panic!("{k} が文字列でない: {:?}", r[k]))
}

// === TestNotamParser ===

#[test]
fn notam_parser__parse_standard_format() {
    let data = json!({"notamList": [{"number": "A0001/26", "location": "RJTT", "contents": "test body",
        "startDate": "2026/03/01 00:00", "endDate": "2026/04/01 00:00", "notamCode": "QMRLC"}]});
    let records = notam::parse(raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["notam_id"], "A0001/26");
    assert_eq!(records[0]["icao_code"], "RJTT");
}

#[test]
fn notam_parser__parse_location_info_format() {
    let data = json!({"locationInfoList": [{"notamList": [{"notamId": "B0002", "icaoCode": "RJAA", "body": "test"}]}]});
    assert_eq!(notam::parse(raw(data)).unwrap().len(), 1);
}

#[test]
fn notam_parser__skip_no_id() {
    let data = json!({"notamList": [{"location": "RJTT", "contents": "no id"}]});
    assert_eq!(notam::parse(raw(data)).unwrap().len(), 0);
}

// === TestPirepParser ===

#[test]
fn pirep_parser__parse_turbulence() {
    let data = json!({"turbulencePirepList": [{"imdbTurbulenceInformation": {
        "pirepControlNumber": "TC001", "bodyData": "PIREP body",
        "turbulenceStrength": "MOD", "timeOfObservation": "202603011200"},
        "imdbTurbulenceInformationPosition": [{"latitude": "35.0", "longitude": "139.0"}]}]});
    let records = pirep::parse(raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["control_number"], "TC001");
    assert_eq!(records[0]["turbulence_strength"], "MOD");
}

#[test]
fn pirep_parser__parse_airep_special() {
    let data = json!({"airepSpecialList": [{"imdbAirepSpecial": {
        "pirepControlNumber": "SP001", "bodyDataInformation": "special",
        "turbulenceType": "SEV"}, "imdbAirepSpecialPosition": []}]});
    let records = pirep::parse(raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["turbulence_strength"], "SEV");
}

// === TestWeatherParser ===

// Python は環境変数で保存先を変える。Rust は同じ値の Diag を渡す (プロセスの環境変数は触らない)
#[test]
fn weather_parser__notam_dto_list_is_ignored_not_reported_as_unknown() {
    let tmp = tempfile::tempdir().unwrap();
    let diag = Diag {
        dir: Some(tmp.path().into()),
        max_samples: MAX_SAMPLES_PER_JOB_TYPE,
        max_bytes: MAX_SAMPLE_BYTES,
    };
    let data = json!({"weatherDTO": {"metarSpeciInfoList": [], "tafInfoList": [], "atisInfoList": [], "useRunwayList": []},
        "notamDTOList": [{"number": "I1/26", "coordinates": "3814N14055E"}]});
    weather::parse_pkg_with(&raw(data), &diag).unwrap();
    assert!(!tmp.path().join("collect_pkg_weather_unknown_samples").exists());
    // 確かめの道具が効いていること: 未知のキーなら保存される
    weather::parse_pkg_with(&raw(json!({"bogusKey": [1]})), &diag).unwrap();
    assert!(tmp.path().join("collect_pkg_weather_unknown_samples").exists());
}

#[test]
fn weather_parser__parse_metar() {
    let data = json!({"weatherDTO": {"metarSpeciInfoList": [
        {"location": "RJTT", "body_DATA": "METAR RJTT 010000Z...", "observed_DATE": "202603010000"}],
        "tafInfoList": [], "atisInfoList": [], "useRunwayList": []}});
    let records = weather::parse_pkg(&raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["_type"], "weather");
    assert_eq!(records[0]["type"], "METAR");
}

#[test]
fn weather_parser__parse_speci() {
    let data = json!({"weatherDTO": {"metarSpeciInfoList": [
        {"location": "RJTT", "body_DATA": "SPECI RJTT...", "observed_DATE": "202603010015"}],
        "tafInfoList": [], "atisInfoList": [], "useRunwayList": []}});
    assert_eq!(weather::parse_pkg(&raw(data)).unwrap()[0]["type"], "SPECI");
}

#[test]
fn weather_parser__parse_atis() {
    let data = json!({"weatherDTO": {"metarSpeciInfoList": [], "tafInfoList": [],
        "atisInfoList": [{"location": "RJTT", "body_DATA": "ATIS RJTT\nA\ninfo...", "observed_DATE": "202603010000"}],
        "useRunwayList": []}});
    let records = weather::parse_pkg(&raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["_type"], "atis");
    assert_eq!(records[0]["atis_letter"], "A");
}

// === TestFlightParser ===

#[test]
fn flight_parser__parse_foids_with_queried_airport() {
    let data = json!({"flightInformationSearchResultsDTO": {
        "arrivingFlights": [{"foid": "f001", "flt_INFOCODE": "JAL001",
            "typ": "B789", "dep_AD": "RJAA", "flt_RULES": "I", "opr": "JAL",
            "applicationeta": "202603011200", "ata": "202603011210"}],
        "departingFlights": [{"foid": "f002", "flt_INFOCODE": "ANA100",
            "typ": "A320", "dest_AD": "RJCC", "flt_RULES": "V", "opr": "ANA",
            "eobt": "202603011300"}]}});
    let records = flight::parse_foids(&raw(data), Some("RJTT")).unwrap();
    assert_eq!(records.len(), 2);
    let arr = records.iter().find(|r| r["foid"] == "f001").unwrap();
    let dep = records.iter().find(|r| r["foid"] == "f002").unwrap();
    // 到着便: dep_ad は response から、dest_ad は queried_airport
    assert_eq!(arr["dep_ad"], "RJAA");
    assert_eq!(arr["dest_ad"], "RJTT");
    assert_eq!(arr["flight_code"], "JAL001");
    assert_eq!(arr["aircraft_type"], "B789");
    assert_eq!(arr["flight_rules"], "I");
    assert_eq!(arr["operator"], "JAL");
    assert!(s(arr, "eta").starts_with("2026-03-01T12:00"));
    assert!(s(arr, "ata").starts_with("2026-03-01T12:10"));
    // 出発便: dep_ad は queried_airport、dest_ad は response から
    assert_eq!(dep["dep_ad"], "RJTT");
    assert_eq!(dep["dest_ad"], "RJCC");
    assert!(s(dep, "eobt").starts_with("2026-03-01T13:00"));
}

#[test]
fn flight_parser__parse_foids_without_queried_airport() {
    let data = json!({"flightInformationSearchResultsDTO": {
        "arrivingFlights": [{"foid": "f001", "dep_AD": "RJAA", "opr": "JAL"}]}});
    let records = flight::parse_foids(&raw(data), None).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["dep_ad"], "RJAA");
    assert_eq!(records[0]["dest_ad"], Value::Null);
}

#[test]
fn flight_parser__parse_foids_skips_empty_foid() {
    let data = json!({"flightInformationSearchResultsDTO": {
        "arrivingFlights": [{"foid": "f001"}, {"foid": null}, {"dep_AD": "RJAA"}]}});
    let records = flight::parse_foids(&raw(data), Some("RJTT")).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["foid"], "f001");
}

#[test]
fn flight_parser__parse_details() {
    let data = json!({"flightDetailsSearchResultsDTO": {"flightDetails": {
        "foid": "f001", "flt_INFOCODE": "JAL001", "typ": "B789", "reg": "JA001A",
        "dep_AD": "RJTT", "dest_AD": "RJAA", "rte": "route",
        "eobt": "202603011200", "eta": "202603011300"}}});
    let records = flight::parse_details(&raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["flight_code"], "JAL001");
    assert!(s(&records[0], "eobt").starts_with("2026-03-01T12:00"));
}

// === TestAirspaceParser ===

#[test]
fn airspace_parser__parse_sigmet() {
    let data = json!({"sigmetList": [{"imdbSigmet": {
        "location": "RJJJ", "bodyDataInformation": "SIGMET body",
        "timeOfObservation": "202603011200"}}]});
    let records = airspace::parse(&raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["type"], "SIGMET");
}

// === TestAirportParser ===

#[test]
fn airport_parser__parse_list() {
    let data = json!({"locationInfoList": [{"id": "RJTT", "nameJp": "RJTT(東京国際)"},
        {"id": "RJAA", "nameEn": "RJAA(Narita)"}]});
    let records = airport::parse_list(&raw(data)).unwrap();
    assert_eq!(records.len(), 2);
    assert_eq!(records[0]["icao_code"], "RJTT");
}

#[test]
fn airport_parser__parse_detail() {
    let data = json!({"_icao_code": "RJTT", "serviceStartTime": "0600", "serviceEndTime": "2300"});
    let records = airport::parse_detail(&raw(data)).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["service_start_time"], "0600");
}

#[test]
fn airport_parser__parse_aip_empty() {
    assert!(airport::parse_aip(&raw(json!({}))).unwrap().is_empty());
    assert!(airport::parse_aip(&raw(json!({"html": ""}))).unwrap().is_empty());
}

#[test]
fn airport_parser__parse_aip_html_table() {
    let html = r#"<html><body>
        <table>
            <tr><th>Effective Date</th><th>Section</th><th>Description</th></tr>
            <tr><td>2026-01-30</td><td>AD 2 RJTT</td><td>Runway update</td></tr>
            <tr><td>2026-02-15</td><td>ENR 1.1</td><td>Airway change</td></tr>
        </table>
        </body></html>"#;
    let records = airport::parse_aip(&raw(json!({"html": html}))).unwrap();
    assert_eq!(records.len(), 2);
    assert_eq!(records[0]["section"], "AD 2 RJTT");
    assert_eq!(records[0]["description"], "Runway update");
    assert!(s(&records[0], "effective_date").starts_with("2026-01-30"));
}

#[test]
fn airport_parser__parse_aip_no_table() {
    let html = "<html><body><p>No tables here</p></body></html>";
    assert!(airport::parse_aip(&raw(json!({"html": html}))).unwrap().is_empty());
}

// Python は coordinator.store._extract_icao_from_section (airport.py にも同じ関数がある)
#[test]
fn airport_parser__extract_icao_from_section() {
    assert_eq!(airport::extract_icao_from_section("AD 2 RJTT"), "RJTT");
    assert_eq!(airport::extract_icao_from_section("ENR 1.1"), "");
    assert_eq!(airport::extract_icao_from_section("AD 2 ROAH"), "ROAH");
}

// === Python のテストに無いもの (M2b の気象のジョブが使う) ===

fn some(v: &[&str]) -> std::collections::BTreeSet<Option<String>> {
    v.iter().map(|s| Some(s.to_string())).collect()
}

// 期待値は Python の weather.extract_atis_status (icao_code が None の ATIS も集合に入る)
#[test]
fn atis_status_and_routine_metar() {
    let data = json!({"weatherDTO": {
        "metarSpeciInfoList": [
            {"location": "RJTT", "body_DATA": "METAR RJTT", "observed_DATE": "202603010000"},
            {"location": "RJAA", "body_DATA": "SPECI RJAA", "observed_DATE": "202603010000"},
            {"location": null, "body_DATA": "METAR XXXX"}],
        "atisInfoList": [
            {"location": "RJTT", "body_DATA": "ATIS RJTT\nMS A\ninfo", "observed_DATE": "202603010000"},
            {"location": "RJAA", "body_DATA": "ATIS RJAA\nB\ninfo"},
            {"location": "RJCC", "body_DATA": "ATIS RJCC A\n CLOSE \n", "observed_DATE": "202603010000"},
            {"location": null, "body_DATA": "ATIS XXXX\nM C\ninfo", "observed_DATE": "202603010100"},
            {"location": null, "body_DATA": "ATIS XXXX\nCLOSE"}]}});
    let records = weather::parse_pkg(&raw(data)).unwrap();
    let st = weather::extract_atis_status(&records);
    let with_none = |v: &[&str]| {
        let mut s = some(v);
        s.insert(None);
        s
    };
    assert_eq!(st.atis_icaos, with_none(&["RJTT", "RJAA"]));
    assert_eq!(st.atis_close_icaos, with_none(&["RJCC"]));
    assert_eq!(st.atis_routine_icaos, with_none(&["RJTT"]));
    assert_eq!(
        st.atis_issued_at,
        [
            (Some("RJTT".to_string()), "2026-03-01T00:00:00+00:00".to_string()),
            (None, "2026-03-01T01:00:00+00:00".to_string())
        ]
        .into()
    );
    assert_eq!(
        weather::extract_routine_metar_airports(&records),
        ["RJTT".to_string()].into()
    );
    // icao_code のキーが無い記録は "" (r.get("icao_code", ""))
    let rec = raw(json!({"_type": "atis", "content": "ATIS RJTT\nA"}));
    assert_eq!(weather::extract_atis_status(&[rec]).atis_icaos, some(&[""]));
}
