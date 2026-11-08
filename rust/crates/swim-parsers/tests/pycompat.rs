use chrono::{FixedOffset, NaiveDate, TimeZone, Utc};
use serde_json::json;

use swim_parsers::pycompat::*;

#[test]
fn isoformat_omits_zero_micros() {
    let t = Utc.with_ymd_and_hms(2026, 11, 5, 13, 6, 15).unwrap();
    assert_eq!(isoformat_utc(&t), "2026-11-05T13:06:15+00:00");
    let t1 = t + chrono::Duration::microseconds(1);
    assert_eq!(isoformat_utc(&t1), "2026-11-05T13:06:15.000001+00:00");
    let t2 = t + chrono::Duration::microseconds(255656);
    assert_eq!(isoformat_utc(&t2), "2026-11-05T13:06:15.255656+00:00");
    let jst = FixedOffset::east_opt(9 * 3600).unwrap();
    assert_eq!(isoformat(&t.with_timezone(&jst)), "2026-11-05T22:06:15+09:00");
    // Python: timezone(timedelta(hours=-5, minutes=-30, seconds=-7))
    let odd = FixedOffset::west_opt(5 * 3600 + 30 * 60 + 7).unwrap();
    assert_eq!(
        isoformat(&odd.with_ymd_and_hms(2026, 11, 5, 13, 6, 15).unwrap()),
        "2026-11-05T13:06:15-05:30:07"
    );
}

#[test]
fn fromisoformat_keeps_offset() {
    let d = NaiveDate::from_ymd_opt(2026, 11, 5).unwrap();
    let p = fromisoformat("2026-11-05T01:02:03.5+05:30").unwrap();
    assert_eq!(p.naive, d.and_hms_micro_opt(1, 2, 3, 500_000).unwrap());
    assert_eq!(p.offset, Some(FixedOffset::east_opt(5 * 3600 + 30 * 60).unwrap()));
    let p = fromisoformat("2026-11-05T01:02:03-00:30").unwrap();
    assert_eq!(p.offset, Some(FixedOffset::west_opt(30 * 60).unwrap()));
    let p = fromisoformat("2026-11-05T01:02:03Z").unwrap();
    assert_eq!(p.offset, Some(FixedOffset::east_opt(0).unwrap()));
    let p = fromisoformat("2026-11-05").unwrap();
    assert_eq!(p.naive, d.and_hms_opt(0, 0, 0).unwrap());
    assert_eq!(p.offset, None);
    assert!(fromisoformat("2026-13-05").is_none());
    assert!(fromisoformat("2026-11-05T01:02:03+24:00").is_none());
}

#[test]
fn repr_str_quotes() {
    assert_eq!(repr_str("a"), "'a'");
    assert_eq!(repr_str("it's"), "\"it's\"");
    assert_eq!(repr_str("a\"b'c"), "'a\"b\\'c'");
    assert_eq!(repr_str("\n"), "'\\n'");
    assert_eq!(repr_str("日本"), "'日本'");
    assert_eq!(repr_str("\0"), "'\\x00'");
    assert_eq!(repr_str("😀"), "'😀'");
    assert_eq!(repr_str("\x7f\u{ad}\u{2028}\t\\"), "'\\x7f\\xad\\u2028\\t\\\\'");
    assert_eq!(repr_str("\u{e0001}"), "'\\U000e0001'");
}

// Python の ascii() は ASCII でない文字を全部エスケープする
#[test]
fn ascii_escapes() {
    assert_eq!(ascii("日本"), "'\\u65e5\\u672c'");
    assert_eq!(ascii("😀"), "'\\U0001f600'");
    assert_eq!(ascii("é"), "'\\xe9'");
    assert_eq!(ascii("a\n'"), "\"a\\n'\"");
}

#[test]
fn isprintable_matches_python() {
    let text = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/tables/isprintable.json")).unwrap();
    let ranges: Vec<[u32; 2]> = serde_json::from_str(&text).unwrap();
    let mut expect = vec![true; 0x110000];
    for [a, b] in ranges {
        for c in a..=b {
            expect[c as usize] = false;
        }
    }
    let mut checked = 0;
    for cp in 0..0x110000u32 {
        if let Some(c) = char::from_u32(cp) {
            assert_eq!(isprintable(c), expect[cp as usize], "U+{cp:04X}");
            checked += 1;
        }
    }
    assert_eq!(checked, 0x110000 - 0x800);
}

#[test]
fn printable_replaces_and_cuts() {
    assert_eq!(printable("a\nb\tc日本", 5), "a b c");
    assert_eq!(printable("日本語", 2), "日本");
    assert_eq!(printable("", 3), "");
}

#[test]
fn char_len_counts_code_points() {
    assert_eq!(char_len("abc"), 3);
    assert_eq!(char_len("日本"), 2);
    assert_eq!(char_len("😀"), 1);
    assert_eq!(char_len("e\u{301}"), 2);
}

#[test]
fn py_int_reads_fullwidth() {
    assert_eq!(py_int("１０"), Some(10));
    assert_eq!(py_int("1 0"), None);
    assert_eq!(py_int(" 42 "), Some(42));
    assert_eq!(py_int("\u{3000}-7\n"), Some(-7));
    assert_eq!(py_int("+010"), Some(10));
    assert_eq!(py_int("1_000"), Some(1000));
    assert_eq!(py_int("\u{0663}\u{0664}"), Some(34)); // アラビア・インド数字
    assert_eq!(py_int("𝟏𝟐"), Some(12)); // 数学用の太字の数字
    for bad in [
        "",
        " ",
        "_1",
        "1_",
        "1__0",
        "1.0",
        "0x10",
        "+-1",
        "²",
        "99999999999999999999",
    ] {
        assert_eq!(py_int(bad), None, "{bad:?}");
    }
}

#[test]
fn py_type_name_like_python() {
    assert_eq!(py_type_name(&json!(null)), "NoneType");
    assert_eq!(py_type_name(&json!(true)), "bool");
    assert_eq!(py_type_name(&json!(1)), "int");
    assert_eq!(py_type_name(&serde_json::from_str("1.0").unwrap()), "float");
    assert_eq!(py_type_name(&serde_json::from_str("1e5").unwrap()), "float");
    assert_eq!(py_type_name(&json!("s")), "str");
    assert_eq!(py_type_name(&json!([])), "list");
    assert_eq!(py_type_name(&json!({})), "dict");
}
