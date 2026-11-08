//! SWIM の raw の応答を記録にする (Python の `coordinator/parsers/`)。
//! 入力は Worker から届いた信用できない JSON。Python の parser が例外を出す入力 (型の違う値など) では、
//! その例外の名前を `ParseError` で返す (normalize_result が `raw の結果を parse できない ({0})` で弾く)。
use serde_json::{Map, Value};

pub mod airport;
pub mod airspace;
pub mod common;
pub mod diagnostics;
pub mod flight;
pub mod html;
pub mod notam;
pub mod pirep;
mod py;
pub mod weather;

/// 1 件の結果 (キーの順番を保つ)
pub type Record = serde_json::Map<String, serde_json::Value>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ParseError(pub &'static str);

/// Coordinator が raw の結果を parse する job_type (Python の `_PARSERS` の順)
pub const PARSED_JOB_TYPES: &[&str] = &[
    "collect_notams",
    "collect_pireps",
    "collect_pkg_weather",
    "collect_airspace_data",
    "collect_airport_profiles",
    "collect_flight_foids",
    "collect_flight_details",
    "collect_aip",
    "collect_airports",
];

/// `{"error": ..., "ret": {...}}` の ret の中身を返す (ret が dict のときだけ)
pub fn unwrap_ret(mut data: Map<String, Value>) -> Map<String, Value> {
    match data.get_mut("ret") {
        Some(Value::Object(ret)) => std::mem::take(ret),
        _ => data,
    }
}

/// collect_flight_foids の対象空港 (依頼の body.flightInformationSearchConditionsDTO.airportCode)。
/// 文字列でなければ None (依頼は Coordinator が作るので文字列か無いかのどちらか)
pub fn extract_queried_airport(params: Option<&Value>) -> Option<String> {
    queried_airport(params)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
}

fn queried_airport(params: Option<&Value>) -> Result<Value, ParseError> {
    let Some(params) = params.filter(|p| py::truthy(p)) else {
        return Ok(Value::Null);
    };
    let body = py::or_empty_dict(py::get(params, "body")?);
    let conds = py::or_empty_dict(py::get(&body, "flightInformationSearchConditionsDTO")?);
    Ok(py::get(&conds, "airportCode")?.clone())
}

pub fn parse_for_job_type(
    job_type: &str,
    data: Map<String, Value>,
    params: Option<&Value>,
) -> Result<Vec<Record>, ParseError> {
    if !PARSED_JOB_TYPES.contains(&job_type) {
        return Err(ParseError("KeyError"));
    }
    let mut data = unwrap_ret(data);
    // Worker と同じく、依頼の "_" 始まりのキー (collect_airport_profiles の _icao_code など) を足す (あれば上書きしない)
    if let Some(p) = params.filter(|p| py::truthy(p)) {
        let Value::Object(p) = p else {
            return Err(ParseError("AttributeError"));
        };
        for (k, v) in p {
            if k.starts_with('_') && !data.contains_key(k) {
                data.insert(k.clone(), v.clone());
            }
        }
    }
    match job_type {
        "collect_notams" => notam::parse(data),
        "collect_pireps" => pirep::parse(data),
        "collect_pkg_weather" => weather::parse_pkg(&data),
        "collect_airspace_data" => airspace::parse(&data),
        "collect_airport_profiles" => airport::parse_detail(&data),
        "collect_flight_foids" => flight::parse_foids_value(&data, queried_airport(params)?),
        "collect_flight_details" => flight::parse_details(&data),
        "collect_aip" => airport::parse_aip(&data),
        "collect_airports" => airport::parse_list(&data),
        _ => unreachable!(),
    }
}
