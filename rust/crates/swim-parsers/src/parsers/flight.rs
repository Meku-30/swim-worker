//! 便 (Python の `parsers/flight.py`)
use serde_json::{Map, Value};

use super::common::compact_utc;
use super::py::{self, get, iter};
use super::{diagnostics, ParseError, Record};

const JOB_TYPE_FOIDS: &str = "collect_flight_foids";
const JOB_TYPE_DETAILS: &str = "collect_flight_details";
const KNOWN_FOIDS_KEYS: &[&str] = &["flightInformationSearchResultsDTO"];
const KNOWN_DETAILS_KEYS: &[&str] = &["flightDetailsSearchResultsDTO"];
const IGNORED_KEYS: &[&str] = &[
    "msgHeader",
    "ctrlInfo",
    "ctrlHeader",
    "errorInfoDTO",
    "flightRemovalTriggerDTO",
];

fn dt(v: &Value) -> Result<Value, ParseError> {
    Ok(compact_utc(v)?.map_or(Value::Null, Value::String))
}

/// FLV803 の応答から便の基本の記録を作る。queried_airport は到着便の dest_ad・出発便の dep_ad
pub fn parse_foids(raw_data: &Map<String, Value>, queried_airport: Option<&str>) -> Result<Vec<Record>, ParseError> {
    parse_foids_value(
        raw_data,
        queried_airport.map_or(Value::Null, |s| Value::String(s.into())),
    )
}

// 依頼から取った値をそのまま入れる (Python と同じく文字列とは限らない)
pub(crate) fn parse_foids_value(raw_data: &Map<String, Value>, queried: Value) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE_FOIDS, raw_data, KNOWN_FOIDS_KEYS, IGNORED_KEYS);
    let result = py::or_empty_dict(get(raw_data, "flightInformationSearchResultsDTO")?);
    let mut records = Vec::new();
    for f in iter(&py::or_empty_list(get(&result, "arrivingFlights")?))? {
        let f = f.as_ref();
        let foid = get(f, "foid")?;
        if !py::truthy(foid) {
            continue;
        }
        records.push(py::record([
            ("foid", foid.clone()),
            ("flight_code", get(f, "flt_INFOCODE")?.clone()),
            ("aircraft_type", get(f, "typ")?.clone()),
            ("dep_ad", get(f, "dep_AD")?.clone()),
            ("dest_ad", queried.clone()),
            ("flight_rules", get(f, "flt_RULES")?.clone()),
            ("operator", get(f, "opr")?.clone()),
            ("arr_spot", get(f, "arrspotnr")?.clone()),
            ("eta", dt(get(f, "applicationeta")?)?),
            ("ata", dt(get(f, "ata")?)?),
        ]));
    }
    for f in iter(&py::or_empty_list(get(&result, "departingFlights")?))? {
        let f = f.as_ref();
        let foid = get(f, "foid")?;
        if !py::truthy(foid) {
            continue;
        }
        records.push(py::record([
            ("foid", foid.clone()),
            ("flight_code", get(f, "flt_INFOCODE")?.clone()),
            ("aircraft_type", get(f, "typ")?.clone()),
            ("dep_ad", queried.clone()),
            ("dest_ad", get(f, "dest_AD")?.clone()),
            ("flight_rules", get(f, "flt_RULES")?.clone()),
            ("operator", get(f, "opr")?.clone()),
            ("dep_spot", get(f, "depspotnr")?.clone()),
            ("eobt", dt(get(f, "eobt")?)?),
            ("atd", dt(get(f, "atd")?)?),
        ]));
    }
    Ok(records)
}

/// FLV911 の応答から便の詳しい記録を作る
pub fn parse_details(raw_data: &Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE_DETAILS, raw_data, KNOWN_DETAILS_KEYS, IGNORED_KEYS);
    let result = py::or_empty_dict(get(raw_data, "flightDetailsSearchResultsDTO")?);
    let fd = py::or_empty_dict(get(&result, "flightDetails")?);
    if !py::truthy(&fd) {
        return Ok(vec![]);
    }
    let foid = get(&fd, "foid")?;
    if !py::truthy(foid) {
        return Ok(vec![]);
    }
    let mut reg = get(&fd, "reg")?;
    if !py::truthy(reg) {
        reg = get(&fd, "flt_INFOCODE")?;
    }
    let g = |k: &str| get(&fd, k).cloned();
    Ok(vec![py::record([
        ("foid", foid.clone()),
        ("flight_code", g("flt_INFOCODE")?),
        ("aircraft_type", g("typ")?),
        ("registration", reg.clone()),
        ("dep_ad", g("dep_AD")?),
        ("dest_ad", g("dest_AD")?),
        ("route", g("rte")?),
        ("flight_rules", g("flt_RULES")?),
        ("operator", g("opr")?),
        ("sch_dep_rwy", g("sch_DEP_RWY_NR")?),
        ("sch_arr_rwy", g("sch_ARR_RWY_NR")?),
        ("dep_rwy", g("dep_RWY_NR")?),
        ("arr_rwy", g("arr_RWY_NR")?),
        ("dep_spot", g("depspotnr")?),
        ("arr_spot", g("arrspotnr")?),
        ("eobt", dt(&g("eobt")?)?),
        ("eta", dt(&g("eta")?)?),
        ("atd", dt(&g("atd")?)?),
        ("ata", dt(&g("ata")?)?),
        ("ssta", dt(&g("ssta")?)?),
        ("sstd", dt(&g("sstd")?)?),
        ("dep_name_jp", g("dep_AIRPORTNAMEJP")?),
        ("dest_name_jp", g("dest_AIRPORTNAMEJP")?),
    ])])
}
