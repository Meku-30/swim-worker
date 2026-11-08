//! PKG の気象 (METAR・TAF・ATIS・RWY-INFO。Python の `parsers/weather.py`)
use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};

use super::common::compact_utc;
use super::diagnostics::Diag;
use super::py::{self, get, iter, S, W};
use super::{ParseError, Record};
use crate::pyjson;

const JOB_TYPE: &str = "collect_pkg_weather";
const KNOWN_TOP_KEYS: &[&str] = &["weatherDTO"];
const KNOWN_DTO_KEYS: &[&str] = &["metarSpeciInfoList", "tafInfoList", "atisInfoList", "useRunwayList"];
const IGNORED_TOP_KEYS: &[&str] = &[
    "msgHeader",
    "ctrlInfo",
    "ctrlHeader",
    "errorInfoDTO",
    "shapeDisplayList",
    "notamDTOList",
];

fn re(p: &str) -> Regex {
    Regex::new(&p.replace(r"\s", S).replace(r"\w", W)).unwrap()
}

static CLOSE_ATIS: LazyLock<Regex> = LazyLock::new(|| re(r"ATIS\s+\w{4}(?:\s+[A-Z])?\s*\n\s*CLOSE\s*$"));
static ATIS_LETTER: LazyLock<Regex> = LazyLock::new(|| re(r"ATIS\s+\w{4}\s+([A-Z])\s"));
static APCH: LazyLock<Regex> = LazyLock::new(|| re(r"\(APCH\)\s+(.+)"));
static RWY_LINE: LazyLock<Regex> = LazyLock::new(|| re(r"^(?:LDG|DEP|USING)\s+RWY"));
static LDG: LazyLock<Regex> = LazyLock::new(|| re(r"LDG\s+RWY\s+(.+?)(?:\n|$)"));
static DEP: LazyLock<Regex> = LazyLock::new(|| re(r"DEP\s+RWY\s+(.+?)(?:\n|$)"));
static USING: LazyLock<Regex> = LazyLock::new(|| re(r"USING\s+RWY\s+(.+?)(?:\n|$)"));

fn dt(v: &Value) -> Result<Value, ParseError> {
    Ok(compact_utc(v)?.map_or(Value::Null, Value::String))
}

fn one_letter(s: &str) -> bool {
    let mut c = s.chars();
    c.next().is_some_and(py::isalpha) && c.next().is_none()
}

fn extract_atis_letter(body: &str) -> Option<String> {
    let lines: Vec<&str> = py::strip(body).split('\n').collect();
    if lines.len() >= 2 {
        let second = py::strip(lines[1]);
        if one_letter(second) {
            return Some(second.to_string());
        }
        if second.starts_with("M ") || second.starts_with("MS ") {
            if let Some(p) = py::split_ws(second).find(|p| one_letter(p)) {
                return Some(p.to_string());
            }
        }
    }
    ATIS_LETTER.captures(body).map(|c| c[1].to_string())
}

fn group(r: &Regex, text: &str) -> Option<String> {
    r.captures(text).map(|c| py::strip(&c[1]).to_string())
}

fn parse_rwy_info(plain: &str) -> (Vec<String>, Option<String>, Option<String>, Option<String>) {
    let mut approach = Vec::new();
    let mut in_apch = false;
    for line in plain.split('\n') {
        if line.contains("(APCH)") {
            if let Some(c) = APCH.captures(line) {
                approach.push(py::strip(&c[1]).to_string());
                in_apch = true;
            }
        } else if in_apch {
            let stripped = py::strip(line);
            if stripped.is_empty() || RWY_LINE.is_match(stripped) {
                in_apch = false;
            } else {
                approach.push(stripped.to_string());
            }
        } else {
            in_apch = false;
        }
    }
    let ldg = group(&LDG, plain);
    let dep = group(&DEP, plain);
    let using = group(&USING, plain).or_else(|| ldg.clone());
    (approach, using, ldg, dep)
}

fn opt(s: Option<String>) -> Value {
    s.map_or(Value::Null, Value::String)
}

// `(item.get(key) or "").strip()`
fn stripped(item: &Value, key: &str) -> Result<String, ParseError> {
    let v = get(item, key)?;
    if !py::truthy(v) {
        return Ok(String::new());
    }
    Ok(py::strip_value(v)?.to_string())
}

/// PKG の気象の応答から weather・atis・runway_info の記録を作る (`_type` で区別)
pub fn parse_pkg(raw_data: &Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    parse_pkg_with(raw_data, &Diag::from_env())
}

/// 診断の保存先を渡して parse_pkg する (テストが環境変数を変えずに済むように)
pub fn parse_pkg_with(raw_data: &Map<String, Value>, diag: &Diag) -> Result<Vec<Record>, ParseError> {
    diag.check_unknown_keys(JOB_TYPE, raw_data, KNOWN_TOP_KEYS, IGNORED_TOP_KEYS);
    let dto = py::or_empty_dict(get(raw_data, "weatherDTO")?);
    diag.check_unknown_keys(JOB_TYPE, &dto, KNOWN_DTO_KEYS, &[]);
    let mut records = Vec::new();
    let s = |x: &str| Value::String(x.to_string());

    for item in iter(&py::or_empty_list(get(&dto, "metarSpeciInfoList")?))? {
        let body = stripped(&item, "body_DATA")?;
        if body.is_empty() {
            continue;
        }
        let kind = if body.starts_with("SPECI") { "SPECI" } else { "METAR" };
        records.push(py::record([
            ("_type", s("weather")),
            ("icao_code", get(&item, "location")?.clone()),
            ("type", s(kind)),
            ("raw_text", Value::String(body)),
            ("observed_at", dt(get(&item, "observed_DATE")?)?),
        ]));
    }
    for item in iter(&py::or_empty_list(get(&dto, "tafInfoList")?))? {
        let body = stripped(&item, "body_DATA")?;
        if body.is_empty() {
            continue;
        }
        records.push(py::record([
            ("_type", s("weather")),
            ("icao_code", get(&item, "location")?.clone()),
            ("type", s("TAF")),
            ("raw_text", Value::String(body)),
            ("observed_at", dt(get(&item, "observed_DATE")?)?),
        ]));
    }
    for item in iter(&py::or_empty_list(get(&dto, "atisInfoList")?))? {
        let body = stripped(&item, "body_DATA")?;
        if body.is_empty() {
            continue;
        }
        records.push(py::record([
            ("_type", s("atis")),
            ("icao_code", get(&item, "location")?.clone()),
            ("atis_letter", opt(extract_atis_letter(&body))),
            ("content", Value::String(body)),
            ("issued_at", dt(get(&item, "observed_DATE")?)?),
        ]));
    }
    for item in iter(&py::or_empty_list(get(&dto, "useRunwayList")?))? {
        let plain = stripped(&item, "plain_DATA")?;
        if plain.is_empty() {
            continue;
        }
        let (apch, rwy_use, ldg, dep) = parse_rwy_info(&plain);
        let apch = Value::Array(apch.into_iter().map(Value::String).collect());
        records.push(py::record([
            ("_type", s("runway_info")),
            ("icao_code", get(&item, "location")?.clone()),
            ("runway_number", get(&item, "runway_NUMBER")?.clone()),
            ("approach_types", Value::String(pyjson::dumps(&apch))),
            ("runway_in_use", opt(rwy_use)),
            ("ldg_rwy", opt(ldg)),
            ("dep_rwy", opt(dep)),
            ("plain_data", Value::String(plain)),
            ("observed_at", dt(get(&item, "observed_DATE")?)?),
        ]));
    }
    Ok(records)
}

/// extract_atis_status の結果。issued_at は記録の ISO の文字列のまま。
/// 空港は Python と同じく None (icao_code が null の記録) も入る。キーが無い記録は ""
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AtisStatus {
    pub atis_icaos: BTreeSet<Option<String>>,
    pub atis_close_icaos: BTreeSet<Option<String>>,
    pub atis_routine_icaos: BTreeSet<Option<String>>,
    pub atis_issued_at: BTreeMap<Option<String>, String>,
}

fn str_of<'a>(r: &'a Record, k: &str) -> Option<&'a str> {
    r.get(k).and_then(Value::as_str)
}

/// parse_pkg の記録から ATIS の状態を取る
pub fn extract_atis_status(records: &[Record]) -> AtisStatus {
    let mut st = AtisStatus::default();
    for r in records {
        if str_of(r, "_type") != Some("atis") {
            continue;
        }
        // r.get("icao_code", "")
        let icao = match r.get("icao_code") {
            None => Some(String::new()),
            Some(Value::Null) => None,
            Some(v) => Some(crate::pycompat::py_str(v)),
        };
        let content = str_of(r, "content").unwrap_or("");
        if CLOSE_ATIS.is_match(content) {
            st.atis_close_icaos.insert(icao);
            continue;
        }
        st.atis_icaos.insert(icao.clone());
        if let Some(issued) = str_of(r, "issued_at").filter(|s| !s.is_empty()) {
            st.atis_issued_at.insert(icao.clone(), issued.to_string());
        }
        let lines: Vec<&str> = py::strip(content).split('\n').collect();
        if lines.len() >= 2 && py::strip(lines[1]).starts_with('M') {
            st.atis_routine_icaos.insert(icao);
        }
    }
    st
}

/// 通常の METAR (SPECI でない) がある空港 (SPECI の状態を解くのに使う)
pub fn extract_routine_metar_airports(records: &[Record]) -> BTreeSet<String> {
    records
        .iter()
        .filter(|r| str_of(r, "_type") == Some("weather") && str_of(r, "type") == Some("METAR"))
        .filter_map(|r| str_of(r, "icao_code").filter(|s| !s.is_empty()).map(str::to_string))
        .collect()
}
