//! Worker の Python の解析 (swim_worker.parsers) と同じ結果か。見本は tools/golden/dump_parse_cases.py が作る
//! (rust/tests/golden/parse/、作った例だけ)
use std::path::PathBuf;

use serde_json::Value;
use swim_parsers::parsers::parse_for_job_type;
use swim_parsers::pyjson;

const JOB_TYPES: &[&str] = &[
    "collect_notams", "collect_pireps", "collect_pkg_weather", "collect_airspace_data",
    "collect_airport_profiles", "collect_flight_foids", "collect_flight_details", "collect_aip", "collect_airports",
];

#[test]
fn worker_parse_matches_python() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/parse");
    let mut failures = Vec::new();
    let mut n = 0;
    for jt in JOB_TYPES {
        let text = std::fs::read_to_string(dir.join(format!("{jt}.json"))).unwrap_or_else(|_| panic!("見本がない: {jt}"));
        let rows: Vec<Value> = serde_json::from_str(&text).unwrap();
        for (i, row) in rows.iter().enumerate() {
            n += 1;
            let data = row["data"].as_object().cloned().unwrap();
            let params = row.get("params").filter(|p| !p.is_null());
            let got = parse_for_job_type(jt, data, params);
            match (got, row.get("parsed"), row.get("error")) {
                (Ok(recs), Some(want), _) => {
                    let got = Value::Array(recs.into_iter().map(Value::Object).collect());
                    if pyjson::dumps(&got) != pyjson::dumps(want) {
                        failures.push(format!("{jt}[{i}]: want {want}\n  got {got}"));
                    }
                }
                (Err(e), _, Some(want)) if e.0 == want.as_str().unwrap() => {}
                (got, _, _) => failures.push(format!("{jt}[{i}]: {got:?} / 見本 {row}")),
            }
        }
    }
    assert!(n >= 25, "見本が少ない: {n}");
    assert!(failures.is_empty(), "{} / {n} 件ずれ:\n{}", failures.len(), failures.join("\n"));
}
