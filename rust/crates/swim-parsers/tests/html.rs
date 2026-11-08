//! html の扱いが Python 3.12 の html.parser と同じか (見本は合成で、本番のデータではない。
//! 元は swim-coordinator の tools/golden/html_cases.py が作った tests/golden/html.json)
use serde_json::{json, Map, Value};
use swim_parsers::parsers::{airport, html};
use swim_parsers::pyjson;

#[test]
fn html_matches_python() {
    let golden: Value = serde_json::from_str(include_str!("golden/html.json")).unwrap();
    let mut failures = Vec::new();
    for case in golden["unescape"].as_array().unwrap() {
        let (input, want) = (case[0].as_str().unwrap(), case[1].as_str().unwrap());
        let got = html::unescape(input);
        if got != want {
            failures.push(format!("unescape {input:?}: want {want:?} got {got:?}"));
        }
    }
    for case in golden["tables"].as_array().unwrap() {
        let input = case[0].as_str().unwrap();
        let got = serde_json::to_value(html::tables(input)).unwrap();
        if pyjson::dumps(&got) != pyjson::dumps(&case[1]) {
            failures.push(format!("tables {input:?}:\n  want {}\n  got  {got}", case[1]));
        }
        let data: Map<String, Value> = json!({"html": input}).as_object().unwrap().clone();
        let recs = airport::parse_aip(&data).unwrap();
        let got = Value::Array(recs.into_iter().map(Value::Object).collect());
        if pyjson::dumps(&got) != pyjson::dumps(&case[2]) {
            failures.push(format!("aip {input:?}:\n  want {}\n  got  {got}", case[2]));
        }
    }
    assert!(failures.is_empty(), "{} 件ずれ:\n{}", failures.len(), failures.join("\n"));
}
