//! 見本との比べ方 (ネットには出ない)
use swim_http::compare::{Diff, Step, compare_step};

fn step(headers: &[(&str, &str)]) -> Step {
    Step {
        method: "POST".into(),
        host: "web.swim.mlit.go.jp".into(),
        path: "/x".into(),
        headers: headers.iter().map(|(n, v)| (n.to_string(), v.to_string())).collect(),
        body: None,
        tls: swim_http::compare::Tls { ja4: "j".into(), peetprint_hash: "p".into() },
        http2: swim_http::compare::Http2 { akamai_fingerprint: "a".into() },
    }
}

const BASE: &[(&str, &str)] = &[
    (":method", "POST"),
    (":authority", "<authority>"),
    (":scheme", "https"),
    (":path", "/api/all"),
    ("cookie", "<cookie>"),
    ("accept", "application/json"),
    ("accept-language", "ja"),
    ("sec-fetch-user", "?1"),
];

#[test]
fn compare_ignores_values_but_not_order() {
    let g = step(BASE);
    let mut a = BASE.to_vec();
    a[1] = (":authority", "tls.peet.ws");
    a[4] = ("cookie", "SESSION=abc");
    assert!(compare_step(&g, &step(&a)).is_empty());

    let mut swapped = BASE.to_vec();
    swapped.swap(5, 6);
    let d = compare_step(&g, &step(&swapped));
    assert_eq!(d.iter().filter(|d| matches!(d, Diff::Order { .. })).count(), 1, "{d:?}");

    let missing: Vec<_> = BASE.iter().copied().filter(|(n, _)| *n != "sec-fetch-user").collect();
    let d = compare_step(&g, &step(&missing));
    assert_eq!(d.iter().filter(|d| matches!(d, Diff::Order { .. })).count(), 1, "{d:?}");
}

#[test]
fn compare_reports_value_diff() {
    let g = step(BASE);
    let mut a = BASE.to_vec();
    a[6] = ("accept-language", "en-US");
    let d = compare_step(&g, &step(&a));
    assert_eq!(d.len(), 1, "{d:?}");
    assert!(matches!(&d[0], Diff::Value { name, .. } if name == "accept-language"));
}

#[test]
fn compare_reports_tls_and_http2() {
    let g = step(BASE);
    let mut a = step(BASE);
    a.tls.ja4 = "other".into();
    a.http2.akamai_fingerprint = "other".into();
    let d = compare_step(&g, &a);
    assert!(d.iter().any(|d| matches!(d, Diff::Tls { .. })));
    assert!(d.iter().any(|d| matches!(d, Diff::Http2 { .. })));
}
