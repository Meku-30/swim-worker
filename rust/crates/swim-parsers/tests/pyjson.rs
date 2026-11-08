use swim_parsers::pyjson::dumps;

// 見本は tools/golden/pyjson_cases.py が Python の json.dumps で作る。
#[test]
fn matches_python_dumps() {
    let cases: serde_json::Value = serde_json::from_str(include_str!("golden/pyjson.json")).unwrap();
    let mut failures = Vec::new();
    for case in cases.as_array().unwrap() {
        let input = case["input"].as_str().unwrap();
        let expected = case["expected"].as_str().unwrap();
        let v: serde_json::Value = serde_json::from_str(input).unwrap();
        let got = dumps(&v);
        if got != expected {
            failures.push(format!("{}: {input}\n  want {expected}\n  got  {got}", case["name"]));
        }
    }
    assert!(
        failures.is_empty(),
        "{} 件ずれ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
