//! Python の json.dumps (既定) と同じ文字列を書く。swim-api が同じ DB の JSON 列を読むので 1 文字もずらさない。
use serde_json::{Number, Value};
use std::fmt::Write;

pub fn dumps(v: &Value) -> String {
    let mut out = String::new();
    write_value(
        &mut out,
        v,
        Opts {
            ascii: true,
            indent: None,
        },
        0,
    );
    out
}

/// `json.dumps(v, ensure_ascii=False)`
pub fn dumps_unicode(v: &Value) -> String {
    let mut out = String::new();
    write_value(
        &mut out,
        v,
        Opts {
            ascii: false,
            indent: None,
        },
        0,
    );
    out
}

/// `json.dumps(v, ensure_ascii=False, indent=indent)`
pub fn dumps_unicode_indent(v: &Value, indent: usize) -> String {
    let mut out = String::new();
    write_value(
        &mut out,
        v,
        Opts {
            ascii: false,
            indent: Some(indent),
        },
        0,
    );
    out
}

#[derive(Clone, Copy)]
struct Opts {
    ascii: bool,
    indent: Option<usize>,
}

// indent があれば要素ごとに改行し、区切りは "," (Python の既定の separators)
fn write_items<T>(
    out: &mut String,
    items: impl ExactSizeIterator<Item = T>,
    o: Opts,
    level: usize,
    mut each: impl FnMut(&mut String, T),
) {
    let empty = items.len() == 0;
    for (i, x) in items.enumerate() {
        match o.indent {
            Some(n) => {
                if i > 0 {
                    out.push(',');
                }
                out.push('\n');
                out.extend(std::iter::repeat_n(' ', n * (level + 1)));
            }
            None if i > 0 => out.push_str(", "),
            None => {}
        }
        each(out, x);
    }
    if let (Some(n), false) = (o.indent, empty) {
        out.push('\n');
        out.extend(std::iter::repeat_n(' ', n * level));
    }
}

fn write_value(out: &mut String, v: &Value, o: Opts, level: usize) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => write_number(out, n),
        Value::String(s) => write_str(out, s, o.ascii),
        Value::Array(a) => {
            out.push('[');
            write_items(out, a.iter(), o, level, |out, x| write_value(out, x, o, level + 1));
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            write_items(out, m.iter(), o, level, |out, (k, x)| {
                write_str(out, k, o.ascii);
                out.push_str(": ");
                write_value(out, x, o, level + 1);
            });
            out.push('}');
        }
    }
}

// arbitrary_precision なので入力の表記のまま持っている。Python の json.loads と同じく
// `.` `e` `E` が無ければ int、あれば float として読み直して書く。
fn write_number(out: &mut String, n: &Number) {
    let text = n.to_string();
    if !text.contains(['.', 'e', 'E']) {
        out.push_str(if text == "-0" { "0" } else { &text });
        return;
    }
    let f: f64 = text.parse().expect("JSON の数値は f64 として読める");
    write_float_repr(out, f);
}

// float.__repr__ と同じ: 最短で元に戻る桁、10 進の指数が -4 以上 16 未満なら固定小数点。
fn write_float_repr(out: &mut String, f: f64) {
    if f.is_infinite() {
        out.push_str(if f > 0.0 { "Infinity" } else { "-Infinity" });
        return;
    }
    let (digits, exp) = shortest_digits(f.abs());
    if f.is_sign_negative() {
        out.push('-');
    }
    if (-4..16).contains(&exp) {
        if exp < 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', (-exp - 1) as usize));
            out.push_str(&digits);
        } else {
            let int_len = exp as usize + 1;
            if digits.len() > int_len {
                out.push_str(&digits[..int_len]);
                out.push('.');
                out.push_str(&digits[int_len..]);
            } else {
                out.push_str(&digits);
                out.extend(std::iter::repeat_n('0', int_len - digits.len()));
                out.push_str(".0");
            }
        }
    } else {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let sign = if exp < 0 { '-' } else { '+' };
        write!(out, "e{sign}{:02}", exp.abs()).unwrap();
    }
}

// 最短で元に戻る桁 (先頭と末尾に 0 なし。0 は "0") と、先頭の桁の 10 進の指数。
// std の {:e} は最短の候補が 2 つあるとき真の値に近い方を選ばないことがあり、Python とずれる
// (例 -1113178120592002.2 が ...3 になる)。ryu は近い方を選ぶので Python の repr と揃う。
fn shortest_digits(f: f64) -> (String, i32) {
    let mut buf = ryu::Buffer::new();
    let s = buf.format_finite(f);
    let (mant, e) = match s.split_once('e') {
        Some((m, e)) => (m, e.parse::<i32>().unwrap()),
        None => (s, 0),
    };
    let point = mant.find('.').unwrap_or(mant.len()) as i32;
    let all: String = mant.chars().filter(|c| *c != '.').collect();
    let lead = all.len() - all.trim_start_matches('0').len();
    let digits = all.trim_matches('0');
    if digits.is_empty() {
        return ("0".to_string(), 0);
    }
    (digits.to_string(), e + point - 1 - lead as i32)
}

// ensure_ascii=True: 印字できる ASCII (0x20〜0x7e) 以外は全部エスケープする。
// ensure_ascii=False: `"` `\\` と 0x20 未満だけエスケープする。
fn write_str(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            c if !ascii && c >= ' ' => out.push(c),
            _ => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    write!(out, "\\u{u:04x}").unwrap();
                }
            }
        }
    }
    out.push('"');
}
