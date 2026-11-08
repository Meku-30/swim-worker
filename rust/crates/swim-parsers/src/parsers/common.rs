//! parser の日時の読み方 (Python の `parsers/common.py` と、`datetime.strptime` の使う書式だけ)。
use std::collections::HashMap;
use std::sync::LazyLock;

use chrono::{Datelike, NaiveDate, Timelike};
use regex::Regex;
use serde_json::Value;

use super::py::{self, S};
use super::ParseError;
use crate::pycompat::py_int;

/// SWIM の YYYYMMDDhhmm (UTC) を ISO 8601 の文字列にする。読めなければ None
pub fn parse_compact_utc(s: Option<&str>) -> Option<String> {
    s.and_then(compact_str)
}

/// 任意の値で Python と同じ: 数・True は `len()` の TypeError、12 個以上のキーの dict は `s[:4]` の KeyError
pub(crate) fn compact_utc(v: &Value) -> Result<Option<String>, ParseError> {
    if !py::truthy(v) {
        return Ok(None);
    }
    let len = match v {
        Value::String(s) => s.chars().count(),
        Value::Array(a) => a.len(),
        Value::Object(m) => m.len(),
        _ => return Err(py::TYPE_ERROR),
    };
    if len < 12 {
        return Ok(None);
    }
    match v {
        Value::String(s) => Ok(compact_str(s)),
        // int(list) は TypeError で None。3.12 の slice は hash できるので dict[slice] は KeyError
        Value::Object(_) => Err(ParseError("KeyError")),
        _ => Ok(None),
    }
}

fn compact_str(s: &str) -> Option<String> {
    let c: Vec<char> = s.chars().collect();
    if c.len() < 12 {
        return None;
    }
    let num = |a: usize, b: usize| py_int(&c[a..b].iter().collect::<String>());
    let (y, mo, d, h, mi) = (num(0, 4)?, num(4, 6)?, num(6, 8)?, num(8, 10)?, num(10, 12)?);
    let dt = datetime(y, mo, d, h, mi, 0)?;
    Some(isoformat(dt, 0))
}

/// `datetime(y, m, d, H, M, S)` が通る値か
pub(crate) fn datetime(y: i64, mo: i64, d: i64, h: i64, mi: i64, s: i64) -> Option<chrono::NaiveDateTime> {
    if !(1..=9999).contains(&y) || !(0..24).contains(&h) || !(0..60).contains(&mi) || !(0..60).contains(&s) {
        return None;
    }
    NaiveDate::from_ymd_opt(y as i32, u32::try_from(mo).ok()?, u32::try_from(d).ok()?)?
        .and_hms_opt(h as u32, mi as u32, s as u32)
}

/// `datetime.isoformat()` (offset はマイクロ秒単位。Python の timezone は秒未満も持てる)
pub(crate) fn isoformat(dt: chrono::NaiveDateTime, offset_us: i64) -> String {
    let mut out = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    );
    let (sign, a) = if offset_us < 0 {
        ('-', -offset_us)
    } else {
        ('+', offset_us)
    };
    let (secs, us) = (a / 1_000_000, a % 1_000_000);
    out.push_str(&format!("{sign}{:02}:{:02}", secs / 3600, secs / 60 % 60));
    if secs % 60 != 0 || us != 0 {
        out.push_str(&format!(":{:02}", secs % 60));
    }
    if us != 0 {
        out.push_str(&format!(".{us:06}"));
    }
    out
}

/// strptime が読んだ値。offset_us は %z があったときだけ
pub(crate) struct Parsed {
    pub dt: chrono::NaiveDateTime,
    pub offset_us: Option<i64>,
}

const MONTHS: [&str; 12] = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
];

// re.IGNORECASE の英字。Python は i に ı・İ、k に K、s に ſ も当てるが、月の名前は lower() して
// 表と比べるので、それらが当たっても読めない (ValueError) のは同じ。ここでは当てない
fn ignorecase(c: char) -> String {
    if c.is_ascii_alphabetic() {
        format!("[{}{}]", c.to_ascii_lowercase(), c.to_ascii_uppercase())
    } else {
        regex::escape(&c.to_string())
    }
}

fn names(list: &[&str]) -> String {
    // __seqToRE: 長い順 (同じ長さは元の順)
    let mut v: Vec<&str> = list.to_vec();
    v.sort_by_key(|s| std::cmp::Reverse(s.chars().count()));
    v.iter()
        .map(|n| n.chars().map(ignorecase).collect::<String>())
        .collect::<Vec<_>>()
        .join("|")
}

// _strptime.TimeRE の書式を regex にする (使う書式の指示子だけ)
fn compile(fmt: &str) -> Regex {
    let abbr: Vec<String> = MONTHS.iter().map(|m| m[..3].to_string()).collect();
    let abbr: Vec<&str> = abbr.iter().map(String::as_str).collect();
    let mut re = String::from("^");
    let mut chars = fmt.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '%' {
            let d = chars.next().expect("書式の % の後に指示子");
            re.push_str(&match d {
                'd' => r"(?P<d>3[0-1]|[1-2]\d|0[1-9]|[1-9]| [1-9])".to_string(),
                'H' => r"(?P<H>2[0-3]|[0-1]\d|\d)".to_string(),
                'm' => r"(?P<m>1[0-2]|0[1-9]|[1-9])".to_string(),
                'M' => r"(?P<M>[0-5]\d|\d)".to_string(),
                'S' => r"(?P<S>6[0-1]|[0-5]\d|\d)".to_string(),
                'y' => r"(?P<y>\d\d)".to_string(),
                'Y' => r"(?P<Y>\d\d\d\d)".to_string(),
                'z' => r"(?P<z>[+-]\d\d:?[0-5]\d(:?[0-5]\d(\.\d{1,6})?)?|Z)".to_string(),
                'b' => format!("(?P<b>{})", names(&abbr)),
                'B' => format!("(?P<B>{})", names(&MONTHS)),
                _ => panic!("使わない指示子 %{d}"),
            });
        } else if py::strip(&c.to_string()).is_empty() {
            while chars.peek().is_some_and(|n| py::strip(&n.to_string()).is_empty()) {
                chars.next();
            }
            re.push_str(S);
            re.push('+');
        } else {
            re.push_str(&ignorecase(c));
        }
    }
    Regex::new(&re).unwrap()
}

/// parser が使う書式 (NOTAM と AIP) の正規表現
pub(crate) const FORMATS: &[&str] = &[
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y%m%d%H%M",
    "%y%m%d%H%M",
    "%Y-%m-%d",
    "%d %b %Y",
    "%d %B %Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
];

static COMPILED: LazyLock<HashMap<&'static str, Regex>> =
    LazyLock::new(|| FORMATS.iter().map(|f| (*f, compile(f))).collect());

/// `datetime.strptime(s, fmt)`。ValueError なら None
pub(crate) fn strptime(s: &str, fmt: &str) -> Option<Parsed> {
    let re = COMPILED.get(fmt).expect("FORMATS に無い書式");
    let caps = re.captures(s)?;
    if caps.get(0)?.end() != s.len() {
        return None; // unconverted data remains
    }
    let int = |k: &str| caps.name(k).map(|m| py_int(m.as_str()));
    let month_of = |k: &str, abbr: bool| {
        caps.name(k).map(|m| {
            let low = m.as_str().to_lowercase();
            MONTHS
                .iter()
                .position(|n| if abbr { n[..3] == low } else { *n == low })
                .map(|i| i as i64 + 1)
        })
    };
    let mut year = 1900;
    if let Some(y) = int("y") {
        let y = y?;
        year = if y <= 68 { y + 2000 } else { y + 1900 };
    }
    if let Some(y) = int("Y") {
        year = y?;
    }
    let month = int("m")
        .or_else(|| month_of("B", false))
        .or_else(|| month_of("b", true))
        .unwrap_or(Some(1))?;
    let day = int("d").unwrap_or(Some(1))?;
    let hour = int("H").unwrap_or(Some(0))?;
    let minute = int("M").unwrap_or(Some(0))?;
    let second = int("S").unwrap_or(Some(0))?;
    let offset_us = match caps.name("z") {
        None => None,
        Some(z) => Some(offset_us(z.as_str())?),
    };
    let dt = datetime(year, month, day, hour, minute, second)?;
    Some(Parsed { dt, offset_us })
}

// _strptime の %z の読み方。timezone() は ±24 時間未満だけ
fn offset_us(z: &str) -> Option<i64> {
    if z == "Z" {
        return Some(0);
    }
    let mut c: Vec<char> = z.chars().collect();
    if c[3] == ':' {
        c.remove(3);
        if c.len() > 5 {
            if c[5] != ':' {
                return None; // Inconsistent use of :
            }
            c.remove(5);
        }
    }
    let part = |a: usize, b: usize| -> String { c[a.min(c.len())..b.min(c.len())].iter().collect() };
    let hours = py_int(&part(1, 3))?;
    let minutes = py_int(&part(3, 5))?;
    let sec_text = part(5, 7);
    let seconds = if sec_text.is_empty() { 0 } else { py_int(&sec_text)? };
    let frac_text = part(8, c.len());
    let frac = if frac_text.is_empty() {
        0
    } else {
        py_int(&format!("{frac_text:0<6}"))?
    };
    let mut total = (hours * 3600 + minutes * 60 + seconds) * 1_000_000 + frac;
    if c[0] == '-' {
        total = -total;
    }
    (total.abs() < 24 * 3600 * 1_000_000).then_some(total)
}
