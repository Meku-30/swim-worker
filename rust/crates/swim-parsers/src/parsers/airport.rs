//! 空港の一覧・詳細・AIP (Python の `parsers/airport.py`)
use serde_json::{Map, Value};

use super::common::{isoformat, strptime};
use super::py::{self, get, iter};
use super::{diagnostics, html, ParseError, Record};

const JOB_TYPE_LIST: &str = "collect_airports";
const JOB_TYPE_DETAIL: &str = "collect_airport_profiles";
const JOB_TYPE_AIP: &str = "collect_aip";
const KNOWN_LIST_KEYS: &[&str] = &["locationInfoList"];
const KNOWN_DETAIL_KEYS: &[&str] = &[
    "_icao_code",
    "ret",
    "serviceStartTime",
    "serviceEndTime",
    "sunriseTime",
    "sunsetTime",
    "departureDelayTime",
    "arrivalDelayTime",
    "airportUsage",
    "airportUsageEn",
    "runwayNoDep",
    "runwayNoLdg",
    "approach",
];
const KNOWN_AIP_KEYS: &[&str] = &["html"];

pub fn parse_list(raw_data: &Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE_LIST, raw_data, KNOWN_LIST_KEYS, &[]);
    let mut records = Vec::new();
    for item in iter(&py::or_empty_list(get(raw_data, "locationInfoList")?))? {
        let id = get(&item, "id")?;
        let icao = if py::truthy(id) { py::strip_value(id)? } else { "" };
        if icao.is_empty() {
            continue;
        }
        let name = py::get_or(&item, &["nameJp", "nameEn"])?;
        let name = if py::truthy(name) {
            name.clone()
        } else {
            Value::String(icao.to_string())
        };
        records.push(py::record([
            ("icao_code", Value::String(icao.to_string())),
            ("name", name),
        ]));
    }
    Ok(records)
}

pub fn parse_detail(raw_data: &Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE_DETAIL, raw_data, KNOWN_DETAIL_KEYS, &[]);
    // raw_data.get("_icao_code", "")・raw_data.get("ret", raw_data)
    let icao = raw_data
        .get("_icao_code")
        .cloned()
        .unwrap_or_else(|| Value::String(String::new()));
    let ret: &dyn py::AsDict = match raw_data.get("ret") {
        Some(v) => v,
        None => raw_data,
    };
    let g = |k: &str| get(ret, k).cloned();
    let list = |k: &str| get(ret, k).map(|v| py::or_empty_list(v).into_owned());
    Ok(vec![py::record([
        ("icao_code", icao),
        ("service_start_time", g("serviceStartTime")?),
        ("service_end_time", g("serviceEndTime")?),
        ("sunrise_time", g("sunriseTime")?),
        ("sunset_time", g("sunsetTime")?),
        ("departure_delay", g("departureDelayTime")?),
        ("arrival_delay", g("arrivalDelayTime")?),
        ("airport_usage", list("airportUsage")?),
        ("airport_usage_en", list("airportUsageEn")?),
        ("runway_dep", list("runwayNoDep")?),
        ("runway_ldg", list("runwayNoLdg")?),
        ("approach", list("approach")?),
    ])])
}

/// AIP の HTML (`{"html": "<html>..."}`) から改訂の記録を作る
pub fn parse_aip(raw_data: &Map<String, Value>) -> Result<Vec<Record>, ParseError> {
    diagnostics::check_unknown_keys(JOB_TYPE_AIP, raw_data, KNOWN_AIP_KEYS, &[]);
    let Some(h) = raw_data.get("html").filter(|v| py::truthy(v)) else {
        return Ok(vec![]);
    };
    // HTMLParser.feed は str 以外で TypeError
    let Value::String(h) = h else {
        return Err(py::TYPE_ERROR);
    };
    Ok(parse_aip_html(h))
}

fn parse_aip_html(text: &str) -> Vec<Record> {
    let mut results = Vec::new();
    for table in html::tables(text) {
        if table.len() < 2 {
            continue;
        }
        let header: Vec<String> = table[0]
            .iter()
            .map(|h| py::strip(&h.to_lowercase()).to_string())
            .collect();
        let date_col = find_column(&header, &["effective", "date", "eff", "airac"]);
        let section_col = find_column(&header, &["section", "amendment", "amdt"]);
        let desc_col = find_column(&header, &["description", "reason", "subject", "remarks"]);
        for row in &table[1..] {
            if !row.iter().any(|c| !py::strip(c).is_empty()) {
                continue;
            }
            let cell = |i: Option<usize>| i.and_then(|i| row.get(i)).map(|c| py::strip(c).to_string());
            let mut effective = cell(date_col).and_then(|c| parse_date(&c));
            let mut section = cell(section_col).unwrap_or_default();
            let mut description = cell(desc_col).unwrap_or_default();
            // フォールバック
            if date_col.is_none() && !row.is_empty() {
                if let Some(d) = parse_date(py::strip(&row[0])) {
                    effective = Some(d);
                }
            }
            if section_col.is_none() && row.len() >= 2 && section.is_empty() {
                section = py::strip(&row[1]).to_string();
            }
            if desc_col.is_none() && row.len() >= 3 && description.is_empty() {
                description = py::strip(&row[2]).to_string();
            }
            results.push(py::record([
                ("effective_date", effective.map_or(Value::Null, Value::String)),
                ("section", Value::String(section)),
                ("description", Value::String(description)),
            ]));
        }
    }
    results
}

fn find_column(headers: &[String], keywords: &[&str]) -> Option<usize> {
    headers.iter().position(|h| keywords.iter().any(|k| h.contains(k)))
}

fn parse_date(value: &str) -> Option<String> {
    if value.is_empty() {
        return None;
    }
    const FORMATS: &[&str] = &["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"];
    FORMATS
        .iter()
        .find_map(|f| strptime(py::strip(value), f))
        .map(|p| isoformat(p.dt, 0))
}

/// AIP のセクションから ICAO を取る ("AD 2 RJTT" → "RJTT")。RJ・RO で始まるものだけ
pub fn extract_icao_from_section(section: &str) -> String {
    // re.search(r"\b([A-Z]{4})\b") の最初の 1 つ (\b は Python の \w の境目)
    let c: Vec<char> = section.chars().collect();
    for i in 0..c.len().saturating_sub(3) {
        let four = &c[i..i + 4];
        if four.iter().all(char::is_ascii_uppercase)
            && (i == 0 || !py::isword(c[i - 1]))
            && c.get(i + 4).is_none_or(|n| !py::isword(*n))
        {
            let s: String = four.iter().collect();
            return if s.starts_with("RJ") || s.starts_with("RO") {
                s
            } else {
                String::new()
            };
        }
    }
    String::new()
}
