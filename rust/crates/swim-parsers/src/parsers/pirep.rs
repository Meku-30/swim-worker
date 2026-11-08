//! PIREP (Python の `parsers/pirep.py`)
use serde_json::{Map, Value};

use super::common::compact_utc;
use super::py::{self, get, into_iter};
use super::{diagnostics, ParseError, Record};
use crate::pyjson;

const JOB_TYPE: &str = "collect_pireps";
const TURBULENCE_KEY_CANDIDATES: &[&str] = &["turbulenceList", "turbulencePirepList", "pirepList"];
const IGNORED_KEYS: &[&str] = &[
    "msgHeader",
    "ctrlInfo",
    "ctrlHeader",
    "xmlBody",
    "errorInfoDTO",
    "shapeDisplayList",
];
const KNOWN_KEYS: &[&str] = &["turbulenceList", "turbulencePirepList", "pirepList", "airepSpecialList"];

fn dt(v: &Value) -> Result<Value, ParseError> {
    Ok(compact_utc(v)?.map_or(Value::Null, Value::String))
}

// `positions[0] if positions else {}`
fn first_position(positions: &Value) -> Result<Value, ParseError> {
    if !py::truthy(positions) {
        return Ok(Value::Object(Map::new()));
    }
    match positions {
        Value::Array(a) => Ok(a[0].clone()),
        Value::String(s) => Ok(Value::String(s.chars().next().unwrap().to_string())),
        Value::Object(_) => Err(ParseError("KeyError")),
        _ => Err(py::TYPE_ERROR),
    }
}

struct Kind {
    list: &'static [&'static str],
    info: &'static str,
    position: &'static str,
    body: &'static str,
    turbulence: &'static str,
    icing: Option<&'static str>,
}

const TURBULENCE: Kind = Kind {
    list: TURBULENCE_KEY_CANDIDATES,
    info: "imdbTurbulenceInformation",
    position: "imdbTurbulenceInformationPosition",
    body: "bodyData",
    turbulence: "turbulenceStrength",
    icing: Some("icingStrength"),
};

const SPECIAL: Kind = Kind {
    list: &["airepSpecialList"],
    info: "imdbAirepSpecial",
    position: "imdbAirepSpecialPosition",
    body: "bodyDataInformation",
    turbulence: "turbulenceType",
    icing: None,
};

// `raw.get(k1) or raw.get(k2) or ... or []` を取り出す (写さずに移す)
fn take_list(raw: &mut Map<String, Value>, keys: &[&str]) -> Value {
    match keys.iter().find(|k| raw.get(**k).is_some_and(py::truthy)) {
        Some(k) => raw.shift_remove(*k).unwrap(),
        None => Value::Array(vec![]),
    }
}

fn parse_kind(list: Value, k: &Kind, records: &mut Vec<Record>) -> Result<(), ParseError> {
    for item in into_iter(list)? {
        let info = py::or_empty_dict(get(&item, k.info)?);
        let positions = py::or_empty_list(get(&item, k.position)?);
        let cn = get(&info, "pirepControlNumber")?;
        if !py::truthy(cn) {
            continue;
        }
        let pos = first_position(&positions)?;
        let body = get(&info, k.body)?;
        let body = if py::truthy(body) {
            body.clone()
        } else {
            Value::String(pyjson::dumps_unicode(&info))
        };
        let mut rec = py::record([
            ("control_number", cn.clone()),
            ("body", body),
            ("turbulence_strength", get(&info, k.turbulence)?.clone()),
            (
                "icing_strength",
                match k.icing {
                    Some(key) => get(&info, key)?.clone(),
                    None => Value::Null,
                },
            ),
            ("latitude", get(&pos, "latitude")?.clone()),
            ("longitude", get(&pos, "longitude")?.clone()),
            ("altitude", get(&pos, "observationAltitude")?.clone()),
            ("altitude_indicator", get(&pos, "observationAltitudeIndicator")?.clone()),
            ("observed_at", dt(get(&info, "timeOfObservation")?)?),
            ("effective_end", dt(get(&info, "effectiveEndTime")?)?),
        ]);
        rec.insert("raw_data".into(), item);
        records.push(rec);
    }
    Ok(())
}

/// raw を所有権で受け取り、各 PIREP を写さずに記録の raw_data へ移す
pub fn parse(mut raw_data: Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE, &raw_data, KNOWN_KEYS, IGNORED_KEYS);
    diagnostics::check_key_collision(JOB_TYPE, &raw_data, TURBULENCE_KEY_CANDIDATES);
    let mut records = Vec::new();
    let turbulence = take_list(&mut raw_data, TURBULENCE.list);
    let special = take_list(&mut raw_data, SPECIAL.list);
    parse_kind(turbulence, &TURBULENCE, &mut records)?;
    parse_kind(special, &SPECIAL, &mut records)?;
    Ok(records)
}
