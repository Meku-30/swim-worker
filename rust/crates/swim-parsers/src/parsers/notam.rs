//! NOTAM (Python の `parsers/notam.py`)
use serde_json::{Map, Value};

use super::common::{isoformat, strptime};
use super::py::{self, get_or, into_iter, take};
use super::{diagnostics, ParseError, Record};
use crate::pycompat::py_str;
use crate::pyjson;

const JOB_TYPE: &str = "collect_notams";
const KNOWN_KEYS: &[&str] = &["notamList", "locationInfoList"];
const IGNORED_KEYS: &[&str] = &["result", "ok", "maxFlg", "msgList"];
const DT_FORMATS: &[&str] = &[
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y%m%d%H%M",
    "%y%m%d%H%M",
];

/// 日時を ISO 8601 (タイムゾーンが無ければ UTC) にする。読めなければ None
fn parse_dt(v: &Value) -> Result<Value, ParseError> {
    if !py::truthy(v) || *v == "PERM" {
        return Ok(Value::Null);
    }
    let Value::String(s) = v else {
        return Err(py::ATTRIBUTE_ERROR);
    };
    let s = s.replace("EST", "");
    let s = py::strip(&s);
    for fmt in DT_FORMATS {
        if let Some(p) = strptime(s, fmt) {
            return Ok(Value::String(isoformat(p.dt, p.offset_us.unwrap_or(0))));
        }
    }
    Ok(Value::Null)
}

/// raw を所有権で受け取り、各 NOTAM を写さずに記録の raw_data へ移す
pub fn parse(mut raw_data: Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE, &raw_data, KNOWN_KEYS, IGNORED_KEYS);
    let first = raw_data.shift_remove("notamList").unwrap_or(Value::Null);
    let notam_list = if py::truthy(&first) {
        into_iter(first)?
    } else {
        let mut list = Vec::new();
        let locs = raw_data.shift_remove("locationInfoList").unwrap_or(Value::Null);
        let locs = if py::truthy(&locs) { locs } else { Value::Array(vec![]) };
        for mut loc in into_iter(locs)? {
            // list.extend(x) は x を回す
            let inner = take(&mut loc, "notamList")?;
            if py::truthy(&inner) {
                list.extend(into_iter(inner)?);
            }
        }
        list
    };
    let mut records = Vec::new();
    for item in notam_list {
        let notam_id = get_or(&item, &["number", "notamId", "notamNo", "id"])?;
        if !py::truthy(notam_id) {
            continue;
        }
        let notam_id = Value::String(py_str(notam_id));
        let mut body = get_or(&item, &["contents", "list", "notamText", "body", "text"])?.clone();
        if !py::truthy(&body) {
            body = Value::String(pyjson::dumps_unicode(&item));
        }
        let icao = get_or(&item, &["location", "icaoCode", "locationId"])?;
        let icao = if py::truthy(icao) {
            icao.clone()
        } else {
            Value::String(String::new())
        };
        let valid_from = parse_dt(get_or(&item, &["startDate", "validFrom", "effectiveFrom"])?)?;
        let valid_to = parse_dt(get_or(&item, &["endDate", "validTo", "effectiveTo"])?)?;
        let category = get_or(&item, &["notamCode", "category", "type"])?.clone();
        records.push(py::record([
            ("notam_id", notam_id),
            ("icao_code", icao),
            ("body", body),
            ("valid_from", valid_from),
            ("valid_to", valid_to),
            ("category", category),
            ("raw_data", item),
        ]));
    }
    Ok(records)
}
