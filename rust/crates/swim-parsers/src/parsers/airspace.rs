//! SIGMET・空港の気象状態 (Python の `parsers/airspace.py`)
use serde_json::{Map, Value};

use super::common::compact_utc;
use super::py::{self, get, iter};
use super::{diagnostics, ParseError, Record};
use crate::pyjson;

const JOB_TYPE: &str = "collect_airspace_data";
const KNOWN_KEYS: &[&str] = &["airportWeatherConditionResult", "sigmetList"];

fn dt(v: &Value) -> Result<Value, ParseError> {
    Ok(compact_utc(v)?.map_or(Value::Null, Value::String))
}

pub fn parse(raw_data: &Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE, raw_data, KNOWN_KEYS, &[]);
    let mut records = Vec::new();
    for cond in iter(&py::or_empty_list(get(raw_data, "airportWeatherConditionResult")?))? {
        let cond = cond.as_ref();
        records.push(py::record([
            ("icao_code", get(cond, "aerodromeCode")?.clone()),
            ("type", Value::String("CONDITION".into())),
            ("raw_text", Value::String(pyjson::dumps_unicode(cond))),
            ("observed_at", dt(get(cond, "timeOfObservation")?)?),
        ]));
    }
    for sig in iter(&py::or_empty_list(get(raw_data, "sigmetList")?))? {
        let info = py::or_empty_dict(get(&sig, "imdbSigmet")?);
        let body = get(&info, "bodyDataInformation")?;
        records.push(py::record([
            ("icao_code", get(&info, "location")?.clone()),
            ("type", Value::String("SIGMET".into())),
            (
                "raw_text",
                if py::truthy(body) {
                    body.clone()
                } else {
                    Value::String(pyjson::dumps_unicode(&info))
                },
            ),
            ("observed_at", dt(get(&info, "timeOfObservation")?)?),
        ]));
    }
    Ok(records)
}
