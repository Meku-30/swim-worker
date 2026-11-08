//! parser の中の Python の書き方 (`d.get(k)`・`a or b`・`for x in v`・`s.strip()`) を、
//! 型の違う値で出る例外まで含めて同じにする。
use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};

pub(crate) use super::diagnostics::AsDict;
use super::ParseError;
use crate::pycompat::{py_isspace, py_truthy};

pub(crate) static NONE: Value = Value::Null;
pub(crate) const ATTRIBUTE_ERROR: ParseError = ParseError("AttributeError");
pub(crate) const TYPE_ERROR: ParseError = ParseError("TypeError");

pub(crate) fn truthy(v: &Value) -> bool {
    py_truthy(v)
}

/// `d.get(k)`。dict でなければ AttributeError
pub(crate) fn get<'a, D: AsDict + ?Sized>(d: &'a D, k: &str) -> Result<&'a Value, ParseError> {
    Ok(d.as_dict().ok_or(ATTRIBUTE_ERROR)?.get(k).unwrap_or(&NONE))
}

/// `d.get(k1) or d.get(k2) or ...` (全部偽なら最後の値)
pub(crate) fn get_or<'a, D: AsDict + ?Sized>(d: &'a D, keys: &[&str]) -> Result<&'a Value, ParseError> {
    let mut last = &NONE;
    for k in keys {
        last = get(d, k)?;
        if truthy(last) {
            break;
        }
    }
    Ok(last)
}

/// `x or {}`
pub(crate) fn or_empty_dict(v: &Value) -> Cow<'_, Value> {
    if truthy(v) {
        Cow::Borrowed(v)
    } else {
        Cow::Owned(Value::Object(Map::new()))
    }
}

/// `x or []`
pub(crate) fn or_empty_list(v: &Value) -> Cow<'_, Value> {
    if truthy(v) {
        Cow::Borrowed(v)
    } else {
        Cow::Owned(Value::Array(vec![]))
    }
}

/// `for x in v`: list は要素、dict はキー、str は 1 文字ずつ。数・真偽・None は TypeError
pub(crate) fn iter(v: &Value) -> Result<Vec<Cow<'_, Value>>, ParseError> {
    match v {
        Value::Array(a) => Ok(a.iter().map(Cow::Borrowed).collect()),
        Value::Object(m) => Ok(m.keys().map(|k| Cow::Owned(Value::String(k.clone()))).collect()),
        Value::String(s) => Ok(s.chars().map(|c| Cow::Owned(Value::String(c.to_string()))).collect()),
        _ => Err(TYPE_ERROR),
    }
}

/// `for x in v` で値を取り出す (list の要素は写さずに移す)
pub(crate) fn into_iter(v: Value) -> Result<Vec<Value>, ParseError> {
    match v {
        Value::Array(a) => Ok(a),
        other => Ok(iter(&other)?.into_iter().map(Cow::into_owned).collect()),
    }
}

/// `d.pop(k)` に当たる取り出し (dict でなければ AttributeError、無ければ None)
pub(crate) fn take(d: &mut Value, k: &str) -> Result<Value, ParseError> {
    match d {
        Value::Object(m) => Ok(m.shift_remove(k).unwrap_or(Value::Null)),
        _ => Err(ATTRIBUTE_ERROR),
    }
}

/// `str.strip()`
pub(crate) fn strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// `v.strip()`。str でなければ AttributeError
pub(crate) fn strip_value(v: &Value) -> Result<&str, ParseError> {
    match v {
        Value::String(s) => Ok(strip(s)),
        _ => Err(ATTRIBUTE_ERROR),
    }
}

/// `str.split()` (空白の並びで分け、空の要素は無い)
pub(crate) fn split_ws(s: &str) -> impl Iterator<Item = &str> {
    s.split(py_isspace).filter(|p| !p.is_empty())
}

static LETTER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\p{L}$").unwrap());
static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[\p{L}\p{N}_]$").unwrap());

/// `c.isalpha()` (Unicode の L*)
pub(crate) fn isalpha(c: char) -> bool {
    LETTER.is_match(c.encode_utf8(&mut [0; 4]))
}

/// re の `\w` (`str.isalnum()` か `_`)
pub(crate) fn isword(c: char) -> bool {
    WORD.is_match(c.encode_utf8(&mut [0; 4]))
}

/// Python の re の `\s` (str.isspace) を Rust の regex で書くときの文字の組
pub(crate) const S: &str = r"[\s\x1C-\x1F]";
/// Python の re の `\w`
pub(crate) const W: &str = r"[\p{L}\p{N}_]";

/// `{k: v, ...}` を順に作る
pub(crate) fn record<const N: usize>(fields: [(&str, Value); N]) -> Map<String, Value> {
    fields.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}
