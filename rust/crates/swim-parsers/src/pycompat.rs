//! Python (3.12) と同じ結果を出すための文字・日時の扱い。

use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, Timelike, Utc, Weekday};
use serde_json::Value;

/// `str.isprintable()` が偽の範囲 (両端を含む)。tools/golden/py_text_cases.py が Python 3.12 から作る (rust/src/tables/)
const NON_PRINTABLE: &[[u32; 2]] = &include!("tables/isprintable.json");
/// `int()` が読む 10 進の数字 (Nd) の 0 の位置。0〜9 が続けて並ぶ
const DECIMAL_ZEROS: &[u32] = &include!("tables/decimal_zeros.json");

/// `datetime.isoformat()`。マイクロ秒が 0 なら省く
pub fn isoformat(dt: &DateTime<FixedOffset>) -> String {
    let mut s = dt.naive_local().format("%Y-%m-%dT%H:%M:%S").to_string();
    let us = dt.nanosecond() / 1000 % 1_000_000;
    if us != 0 {
        s.push_str(&format!(".{us:06}"));
    }
    let off = dt.offset().local_minus_utc();
    let (sign, a) = if off < 0 { ('-', -off) } else { ('+', off) };
    s.push_str(&format!("{sign}{:02}:{:02}", a / 3600, a / 60 % 60));
    if a % 60 != 0 {
        s.push_str(&format!(":{:02}", a % 60));
    }
    s
}

/// `datetime.now(timezone.utc).isoformat()` の形
pub fn isoformat_utc(dt: &DateTime<Utc>) -> String {
    isoformat(&dt.fixed_offset())
}

/// `str(datetime)` (UTC)。`isoformat(sep=" ")`
pub fn str_datetime_utc(dt: &DateTime<Utc>) -> String {
    isoformat_utc(dt).replacen('T', " ", 1)
}

/// `str(timedelta)` (`0:01:01`・`7 days, 0:00:00`・`-1 day, 23:55:00`)。マイクロ秒より下は切り捨て
pub fn str_timedelta(d: chrono::Duration) -> String {
    const DAY_US: i128 = 86_400_000_000;
    let total = i128::from(d.num_seconds()) * 1_000_000 + i128::from(d.subsec_nanos() / 1000);
    let days = total.div_euclid(DAY_US);
    let rest = total.rem_euclid(DAY_US);
    let (secs, us) = (rest / 1_000_000, rest % 1_000_000);
    let mut s = String::new();
    if days != 0 {
        let plural = if days.abs() != 1 { "s" } else { "" };
        s.push_str(&format!("{days} day{plural}, "));
    }
    s.push_str(&format!("{}:{:02}:{:02}", secs / 3600, secs / 60 % 60, secs % 60));
    if us != 0 {
        s.push_str(&format!(".{us:06}"));
    }
    s
}

/// `datetime.fromisoformat` の結果。offset が None ならタイムゾーンなし (naive)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PyDateTime {
    pub naive: NaiveDateTime,
    pub offset: Option<FixedOffset>,
}

/// `repr(s)` (`%r`・`!r`)
pub fn repr_str(s: &str) -> String {
    repr_impl(s, false)
}

/// `ascii(s)`
pub fn ascii(s: &str) -> String {
    repr_impl(s, true)
}

// CPython の unicode_repr と同じ
fn repr_impl(s: &str, ascii_only: bool) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            c if c == quote || c == '\\' => {
                out.push('\\');
                out.push(c);
            }
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c if (c as u32) < 0x7f => out.push(c),
            c if !ascii_only && isprintable(c) => out.push(c),
            c => {
                let n = c as u32;
                if n <= 0xff {
                    out.push_str(&format!("\\x{n:02x}"));
                } else if n <= 0xffff {
                    out.push_str(&format!("\\u{n:04x}"));
                } else {
                    out.push_str(&format!("\\U{n:08x}"));
                }
            }
        }
    }
    out.push(quote);
    out
}

/// `str.isprintable()` (1 文字)
pub fn isprintable(c: char) -> bool {
    let n = c as u32;
    NON_PRINTABLE
        .binary_search_by(|[a, b]| {
            if *b < n {
                std::cmp::Ordering::Less
            } else if *a > n {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_err()
}

/// dispatcher._printable: 先頭 limit 文字の、表示できない文字を空白にする
pub fn printable(s: &str, limit: usize) -> String {
    s.chars()
        .take(limit)
        .map(|c| if isprintable(c) { c } else { ' ' })
        .collect()
}

/// Python の `len(str)`
pub fn char_len(s: &str) -> usize {
    s.chars().count()
}

fn decimal_value(c: char) -> Option<u32> {
    let n = c as u32;
    let i = DECIMAL_ZEROS.partition_point(|&z| z <= n);
    let z = DECIMAL_ZEROS[i.checked_sub(1)?];
    (n - z < 10).then_some(n - z)
}

/// `str.isspace()`。Rust の White_Space に U+001C〜U+001F (区切りの制御文字) を足したもの
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// 正規表現の `\d` を Python 3.12 の `re` と同じ数字に揃える文字クラス (regex クレートの Unicode は Python より新しい)
pub fn py_digit_class() -> String {
    let ranges: String = DECIMAL_ZEROS
        .iter()
        .map(|&z| format!("\\x{{{z:X}}}-\\x{{{:X}}}", z + 9))
        .collect();
    format!("[{ranges}]")
}

/// `int(s)` (10 進)。全角などの Unicode の数字も読む。i64 に収まらなければ None
pub fn py_int(s: &str) -> Option<i64> {
    let t = s.trim_matches(py_isspace);
    let (neg, body) = match t.chars().next()? {
        '-' => (true, &t[1..]),
        '+' => (false, &t[1..]),
        _ => (false, t),
    };
    let mut v: i64 = 0;
    let mut prev_digit = false;
    let mut any = false;
    for c in body.chars() {
        if c == '_' {
            if !prev_digit {
                return None;
            }
            prev_digit = false;
            continue;
        }
        let d = decimal_value(c)? as i64;
        // 負の側で溜めると i64::MIN も読める
        v = v.checked_mul(10)?.checked_sub(d)?;
        prev_digit = true;
        any = true;
    }
    if !any || !prev_digit {
        return None;
    }
    if neg {
        Some(v)
    } else {
        v.checked_neg()
    }
}

/// `type(v).__name__` (json.loads した値)
pub fn py_type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.as_str().contains(['.', 'e', 'E']) => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `repr(v)` (json.loads した値)
pub fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => match n.as_str().parse::<f64>() {
            Ok(f) if f.is_infinite() && n.as_str().contains(['.', 'e', 'E']) => {
                if f > 0.0 { "inf" } else { "-inf" }.into()
            }
            _ => crate::pyjson::dumps(v),
        },
        Value::String(s) => repr_str(s),
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter()
                .map(|(k, x)| format!("{}: {}", repr_str(k), py_repr(x)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// `str(v)` (json.loads した値)
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        _ => py_repr(v),
    }
}

/// `bool(v)` (json.loads した値)
pub fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_str().parse::<f64>() != Ok(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

// 以下は CPython 3.12 Modules/_datetimemodule.c の datetime_fromisoformat をなぞる。
// C は NUL 終端の UTF-8 を読むので、範囲の外は 0 として読む (末尾や途中の NUL の扱いも揃う)。

/// Python 3.12 の `datetime.fromisoformat` (C 実装)。読めなければ None (Python の ValueError)。
/// タイムゾーンの秒未満は FixedOffset に入らないので捨てる
pub fn fromisoformat(s: &str) -> Option<PyDateTime> {
    if s.chars().count() < 7 {
        return None;
    }
    let b = s.as_bytes();
    let len = b.len();
    let sep = find_separator(b)?;
    let (year, month, day) = parse_date(b, sep)?;
    let (mut h, mut mi, mut sec, mut us, mut offset) = (0, 0, 0, 0, None);
    if len > sep {
        // 区切りは任意の 1 文字。C は UTF-8 の先頭バイトから文字の長さを見て飛ばす
        let lead = b[sep];
        let width = if lead & 0x80 == 0 {
            1
        } else {
            match lead & 0xf0 {
                0xe0 => 3,
                0xf0 => 4,
                _ => 2,
            }
        };
        let t = b.get(sep + width..).unwrap_or(&[]);
        (h, mi, sec, us, offset) = parse_time(t)?;
    }
    if !(1..=9999).contains(&year) || h > 23 || mi > 59 || sec > 59 {
        return None;
    }
    let date = NaiveDate::from_ymd_opt(year, month, day)?;
    let naive = date.and_hms_micro_opt(h, mi, sec, us)?;
    Some(PyDateTime { naive, offset })
}

fn at(b: &[u8], i: usize) -> u8 {
    b.get(i).copied().unwrap_or(0)
}

fn digits(b: &[u8], mut p: usize, n: usize) -> Option<(u32, usize)> {
    let mut v = 0u32;
    for _ in 0..n {
        let c = at(b, p).wrapping_sub(b'0');
        if c > 9 {
            return None;
        }
        v = v * 10 + c as u32;
        p += 1;
    }
    Some((v, p))
}

fn find_separator(b: &[u8]) -> Option<usize> {
    let len = b.len();
    if len == 7 {
        return Some(7);
    }
    if b[4] == b'-' {
        if b[5] == b'W' {
            if len > 8 && b[8] == b'-' {
                if len == 9 {
                    return None;
                }
                if len > 10 && b[10].is_ascii_digit() {
                    return Some(8);
                }
                return Some(10);
            }
            return Some(8);
        }
        return Some(10);
    }
    if b[4] == b'W' {
        let mut idx = 7;
        while idx < len && b[idx].is_ascii_digit() {
            idx += 1;
        }
        if idx < 9 {
            return Some(idx);
        }
        return Some(if idx % 2 == 0 { 7 } else { 8 });
    }
    Some(8)
}

fn parse_date(b: &[u8], len: usize) -> Option<(i32, u32, u32)> {
    let (year, mut p) = digits(b, 0, 4)?;
    let uses_sep = at(b, p) == b'-';
    if uses_sep {
        p += 1;
    }
    if at(b, p) == b'W' {
        p += 1;
        let (week, np) = digits(b, p, 2)?;
        p = np;
        let mut wday = 1;
        if p < len {
            if uses_sep {
                if at(b, p) != b'-' {
                    return None;
                }
                p += 1;
            }
            wday = digits(b, p, 1)?.0;
        }
        return iso_to_ymd(year, week, wday);
    }
    let (month, mut p) = digits(b, p, 2)?;
    if uses_sep {
        if at(b, p) != b'-' {
            return None;
        }
        p += 1;
    }
    let (day, _) = digits(b, p, 2)?;
    Some((year as i32, month, day))
}

fn iso_to_ymd(year: u32, week: u32, wday: u32) -> Option<(i32, u32, u32)> {
    if !(1..=9999).contains(&year) || !(1..=7).contains(&wday) {
        return None;
    }
    let wd = Weekday::try_from((wday - 1) as u8).ok()?;
    // chrono も Python と同じく 53 週目は 53 週ある年だけ受ける
    let d = NaiveDate::from_isoywd_opt(year as i32, week, wd)?;
    Some((d.year(), d.month(), d.day()))
}

type Time = (u32, u32, u32, u32, Option<FixedOffset>);

fn parse_time(t: &[u8]) -> Option<Time> {
    let end = t.len();
    let mut tz = 0;
    loop {
        if matches!(at(t, tz), b'Z' | b'+' | b'-') {
            break;
        }
        tz += 1;
        if tz >= end {
            break;
        }
    }
    let (h, mi, sec, us, rest) = parse_hh_mm_ss_ff(t, 0, tz)?;
    if tz == end {
        return if rest { None } else { Some((h, mi, sec, us, None)) };
    }
    if at(t, tz) == b'Z' {
        return if at(t, tz + 1) != 0 {
            None
        } else {
            Some((h, mi, sec, us, FixedOffset::east_opt(0)))
        };
    }
    let sign: i32 = if at(t, tz) == b'-' { -1 } else { 1 };
    let (th, tm, ts, tus, rest) = parse_hh_mm_ss_ff(t, tz + 1, end)?;
    if rest {
        return None;
    }
    // timezone() は ±24 時間未満だけ (分・秒は 60 以上でもそのまま足される)
    let total = (th as i64 * 3600 + tm as i64 * 60 + ts as i64) * 1_000_000 + tus as i64;
    if total >= 86_400_000_000 {
        return None;
    }
    let secs = (th * 3600 + tm * 60 + ts) as i32;
    Some((h, mi, sec, us, FixedOffset::east_opt(sign * secs)))
}

/// 戻り値の最後は「そこで文字列が終わっていない」(C の戻り値 1)
fn parse_hh_mm_ss_ff(t: &[u8], start: usize, end: usize) -> Option<(u32, u32, u32, u32, bool)> {
    let mut vals = [0u32; 3];
    let mut p = start;
    let mut has_sep = true;
    for i in 0..3 {
        let (v, np) = digits(t, p, 2)?;
        vals[i] = v;
        p = np;
        let c = at(t, p);
        p += 1;
        if i == 0 {
            has_sep = c == b':';
        }
        if p >= end {
            return Some((vals[0], vals[1], vals[2], 0, c != 0));
        } else if has_sep && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            break;
        } else if !has_sep {
            p -= 1;
        } else {
            return None;
        }
    }
    let to_parse = (end - p).min(6);
    let (mut us, mut p) = digits(t, p, to_parse)?;
    const CORRECTION: [u32; 5] = [100_000, 10_000, 1_000, 100, 10];
    if to_parse < 6 {
        us *= CORRECTION[to_parse - 1];
    }
    while at(t, p).is_ascii_digit() {
        p += 1;
    }
    Some((vals[0], vals[1], vals[2], us, at(t, p) != 0))
}
