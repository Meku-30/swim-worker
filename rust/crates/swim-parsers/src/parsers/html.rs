//! AIP の HTML の表を読む。Python 3.12.15 の `html.parser.HTMLParser` (convert_charrefs=True、
//! feed を 1 回だけで close しない) と `airport._TableParser` の組み合わせと同じ結果にする。
//! 文字の参照は `html.unescape` と同じ (表は tools/golden/html_cases.py が Python から作る)。
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use regex::{Captures, Regex};
use serde_json::Value;

use super::py::strip;

struct UnescapeTable {
    html5: HashMap<String, String>,
    invalid_charrefs: HashMap<u32, String>,
    invalid_codepoints: HashSet<u32>,
}

static TABLE: LazyLock<UnescapeTable> = LazyLock::new(|| {
    let v: Value = serde_json::from_str(include_str!("../tables/html_unescape.json")).expect("html_unescape.json");
    let strings = |k: &str| {
        v[k].as_object()
            .unwrap()
            .iter()
            .map(|(k, x)| (k.clone(), x.as_str().unwrap().to_string()))
    };
    UnescapeTable {
        html5: strings("html5").collect(),
        invalid_charrefs: strings("invalid_charrefs")
            .map(|(k, x)| (k.parse().unwrap(), x))
            .collect(),
        invalid_codepoints: v["invalid_codepoints"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n.as_u64().unwrap() as u32)
            .collect(),
    }
});

static CHARREF: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\x0C <&#;]{1,32};?)").unwrap());

/// `html.unescape`
pub fn unescape(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    CHARREF
        .replace_all(s, |c: &Captures| replace_charref(&c[1]))
        .into_owned()
}

fn replace_charref(s: &str) -> String {
    let t = &*TABLE;
    if let Some(num) = s.strip_prefix('#') {
        let (digits, radix) = match num.strip_prefix(['x', 'X']) {
            Some(h) => (h, 16),
            None => (num, 10),
        };
        // 桁が多すぎるものは 0x10FFFF より大きい扱い
        let n = u32::from_str_radix(digits.trim_end_matches(';'), radix).unwrap_or(u32::MAX);
        if let Some(r) = t.invalid_charrefs.get(&n) {
            return r.clone();
        }
        if (0xD800..=0xDFFF).contains(&n) || n > 0x10FFFF {
            return '\u{FFFD}'.to_string();
        }
        if t.invalid_codepoints.contains(&n) {
            return String::new();
        }
        return char::from_u32(n).map(String::from).unwrap_or_default();
    }
    if let Some(v) = t.html5.get(s) {
        return v.clone();
    }
    // 前から一番長く一致する名前 (2 文字以上)
    let idx: Vec<usize> = s.char_indices().map(|(i, _)| i).collect();
    for x in (2..idx.len()).rev() {
        if let Some(v) = t.html5.get(&s[..idx[x]]) {
            return format!("{v}{}", &s[idx[x]..]);
        }
    }
    format!("&{s}")
}

/// `_TableParser` で読んだ表 (表 → 行 → セルの文字)
pub fn tables(html: &str) -> Vec<Vec<Vec<String>>> {
    let mut t = TableParser::default();
    Tokenizer { raw: html, cdata: None }.run(&mut t);
    t.tables
}

#[derive(Default)]
struct TableParser {
    tables: Vec<Vec<Vec<String>>>,
    table: Option<Vec<Vec<String>>>,
    row: Option<Vec<String>>,
    cell: Option<String>,
    in_cell: bool,
}

impl TableParser {
    fn start(&mut self, tag: &str) {
        match tag {
            "table" => self.table = Some(vec![]),
            "tr" if self.table.is_some() => self.row = Some(vec![]),
            "td" | "th" if self.row.is_some() => {
                self.cell = Some(String::new());
                self.in_cell = true;
            }
            _ => {}
        }
    }

    fn end(&mut self, tag: &str) {
        match tag {
            "td" | "th" if self.in_cell => {
                let text = strip(self.cell.as_deref().unwrap_or("")).to_string();
                if let Some(row) = &mut self.row {
                    row.push(text);
                }
                self.cell = None;
                self.in_cell = false;
            }
            "tr" if self.row.is_some() => {
                let row = self.row.take().unwrap();
                if let Some(t) = &mut self.table {
                    t.push(row);
                }
            }
            "table" if self.table.is_some() => self.tables.push(self.table.take().unwrap()),
            _ => {}
        }
    }

    fn data(&mut self, s: &str) {
        if self.in_cell {
            if let Some(c) = &mut self.cell {
                c.push_str(s);
            }
        }
    }
}

const CDATA_CONTENT_ELEMENTS: &[&str] = &["script", "style", "xmp", "iframe", "noembed", "noframes"];
const RCDATA_CONTENT_ELEMENTS: &[&str] = &["textarea", "title"];

fn is_space(b: u8) -> bool {
    matches!(b, b'\t' | b'\n' | b'\r' | b'\x0c' | b' ')
}

struct Cdata {
    elem: String,
    escapable: bool,
}

struct Tokenizer<'a> {
    raw: &'a str,
    cdata: Option<Cdata>,
}

impl Tokenizer<'_> {
    fn at(&self, i: usize) -> Option<u8> {
        self.raw.as_bytes().get(i).copied()
    }

    fn find(&self, pat: &str, from: usize) -> Option<usize> {
        self.raw.get(from..).and_then(|r| r.find(pat)).map(|p| p + from)
    }

    fn starts(&self, pat: &str, i: usize) -> bool {
        self.raw.as_bytes()[i..].starts_with(pat.as_bytes())
    }

    fn letter(&self, i: usize) -> bool {
        self.at(i).is_some_and(|b| b.is_ascii_alphabetic())
    }

    // HTMLParser.goahead(end=False)。読み切れないところで止め、残りは捨てる (close しないので)
    fn run(&mut self, h: &mut TableParser) {
        let raw = self.raw;
        let n = raw.len();
        let mut i = 0;
        while i < n {
            let j = match &self.cdata {
                None => match self.find("<", i) {
                    Some(j) => j,
                    None => {
                        // 末尾近くの & が参照の途中かもしれなければ、続きを待つ (ここで終わり)
                        let from = raw.char_indices().rev().nth(33).map_or(0, |(p, _)| p).max(i);
                        if let Some(amp) = raw[from..].rfind('&').map(|p| p + from) {
                            if !raw.as_bytes()[amp..].iter().any(|&b| is_space(b) || b == b';') {
                                break;
                            }
                        }
                        n
                    }
                },
                Some(c) if c.elem == "plaintext" => n,
                Some(c) => match self.find_cdata_end(&c.elem, i) {
                    Some(j) => j,
                    None => break,
                },
            };
            if i < j {
                let text = &raw[i..j];
                if self.cdata.as_ref().is_none_or(|c| c.escapable) {
                    h.data(&unescape(text));
                } else {
                    h.data(text);
                }
            }
            i = j;
            if i == n {
                break;
            }
            let k = if self.letter(i + 1) {
                self.parse_starttag(i, h)
            } else if self.starts("</", i) {
                self.parse_endtag(i, h)
            } else if self.starts("<!--", i) {
                self.parse_comment(i)
            } else if self.starts("<?", i) {
                self.find(">", i + 2).map(|p| p + 1)
            } else if self.starts("<!", i) {
                self.parse_html_declaration(i)
            } else if i + 1 < n {
                h.data("<");
                Some(i + 1)
            } else {
                None
            };
            match k {
                Some(k) => i = k,
                None => break,
            }
        }
    }

    // `</elem(?=[\t\n\r\f />])` (IGNORECASE|ASCII)
    fn find_cdata_end(&self, elem: &str, from: usize) -> Option<usize> {
        let b = self.raw.as_bytes();
        let mut p = from;
        while let Some(q) = self.find("</", p) {
            let name = q + 2;
            if b.len() > name + elem.len()
                && b[name..name + elem.len()].eq_ignore_ascii_case(elem.as_bytes())
                && (is_space(b[name + elem.len()]) || matches!(b[name + elem.len()], b'/' | b'>'))
            {
                return Some(q);
            }
            p = q + 1;
        }
        None
    }

    // 属性の値 `[\t\n\r\f ]*=[\t\n\r\f ]*('[^']*'|"[^"]*"|(?!['"])[^>\t\n\r\f ]*)` の後ろ (無ければ q のまま)。
    // 引用符が閉じていなければ、= の後の空白を 1 つ返して空の値で一致する (正規表現の後戻りと同じ)
    fn value(&self, q: usize) -> usize {
        let n = self.raw.len();
        let mut r = q;
        while r < n && is_space(self.raw.as_bytes()[r]) {
            r += 1;
        }
        if self.at(r) != Some(b'=') {
            return q;
        }
        r += 1;
        let after_eq = r;
        while r < n && is_space(self.raw.as_bytes()[r]) {
            r += 1;
        }
        match self.at(r) {
            Some(quote @ (b'\'' | b'"')) => match self.raw.as_bytes()[r + 1..].iter().position(|&b| b == quote) {
                Some(e) => r + 1 + e + 1,
                None if r > after_eq => r - 1,
                None => q,
            },
            _ => {
                while r < n && !(is_space(self.raw.as_bytes()[r]) || self.raw.as_bytes()[r] == b'>') {
                    r += 1;
                }
                r
            }
        }
    }

    // 属性を 1 つ読む (先頭の 1 文字前が引用符か空白か / であること)。読めなければ None
    fn attribute(&self, p: usize) -> Option<usize> {
        let b = self.raw.as_bytes();
        if p == 0 || !(matches!(b[p - 1], b'\'' | b'"' | b'/') || is_space(b[p - 1])) {
            return None;
        }
        let first = *b.get(p)?;
        if is_space(first) || first == b'/' || first == b'>' {
            return None;
        }
        let mut q = p + 1;
        while q < b.len() && !(is_space(b[q]) || matches!(b[q], b'/' | b'=' | b'>')) {
            q += 1;
        }
        Some(self.value(q))
    }

    // `[\t\n\r\f /]*` (locatetagend) か `(?:[\t\n\r\f ]|/(?!>))*` (tagfind_tolerant・attrfind_tolerant)
    fn skip(&self, mut p: usize, slash_before_gt: bool) -> usize {
        let b = self.raw.as_bytes();
        while p < b.len() && (is_space(b[p]) || (b[p] == b'/' && (slash_before_gt || b.get(p + 1) != Some(&b'>')))) {
            p += 1;
        }
        p
    }

    fn tag_name_end(&self, p: usize) -> usize {
        let b = self.raw.as_bytes();
        let mut q = p + 1;
        while q < b.len() && !(is_space(b[q]) || b[q] == b'/' || b[q] == b'>') {
            q += 1;
        }
        q
    }

    // locatetagend.match(rawdata, p).end()
    fn locate_tag_end(&self, p: usize) -> usize {
        let mut q = self.skip(self.tag_name_end(p), true);
        while let Some(r) = self.attribute(q) {
            q = self.skip(r, true);
        }
        if self.at(q) == Some(b'>') {
            q += 1;
        }
        q
    }

    fn tag_name(&self, p: usize) -> String {
        self.raw[p..self.tag_name_end(p)].to_lowercase()
    }

    fn parse_starttag(&mut self, i: usize, h: &mut TableParser) -> Option<usize> {
        let endpos = self.locate_tag_end(i + 1);
        if self.raw.as_bytes()[endpos - 1] != b'>' {
            return None;
        }
        let tag = self.tag_name(i + 1);
        let mut k = self.skip(self.tag_name_end(i + 1), false);
        while k < endpos {
            match self.attribute(k) {
                Some(r) => k = self.skip(r, false),
                None => break,
            }
        }
        let end = strip(self.raw.get(k.min(endpos)..endpos).unwrap_or(""));
        if end != ">" && end != "/>" {
            h.data(&self.raw[i..endpos]);
            return Some(endpos);
        }
        h.start(&tag);
        if end == "/>" {
            h.end(&tag);
        } else if CDATA_CONTENT_ELEMENTS.contains(&tag.as_str()) || tag == "plaintext" {
            self.cdata = Some(Cdata {
                elem: tag,
                escapable: false,
            });
        } else if RCDATA_CONTENT_ELEMENTS.contains(&tag.as_str()) {
            self.cdata = Some(Cdata {
                elem: tag,
                escapable: true,
            });
        }
        Some(endpos)
    }

    fn parse_endtag(&mut self, i: usize, h: &mut TableParser) -> Option<usize> {
        self.find(">", i + 2)?;
        if !self.letter(i + 2) {
            if self.at(i + 2) == Some(b'>') {
                return Some(i + 3);
            }
            return self.find(">", i + 2).map(|p| p + 1);
        }
        let j = self.locate_tag_end(i + 2);
        if self.raw.as_bytes()[j - 1] != b'>' {
            return None;
        }
        h.end(&self.tag_name(i + 2));
        self.cdata = None;
        Some(j)
    }

    // `--!?>` を探し、無ければ `<!-->`・`<!--->`
    fn parse_comment(&self, i: usize) -> Option<usize> {
        let mut p = i + 4;
        while let Some(q) = self.find("--", p) {
            if self.starts("-->", q) {
                return Some(q + 3);
            }
            if self.starts("--!>", q) {
                return Some(q + 4);
            }
            p = q + 1;
        }
        if self.starts(">", i + 4) {
            Some(i + 5)
        } else if self.starts("->", i + 4) {
            Some(i + 6)
        } else {
            None
        }
    }

    fn parse_html_declaration(&self, i: usize) -> Option<usize> {
        if self.starts("<![CDATA[", i) {
            return self.find("]]>", i + 9).map(|j| j + 3);
        }
        let head: String = self.raw[i..].chars().take(9).collect();
        if head.to_lowercase() == "<!doctype" {
            return self.find(">", i + head.len()).map(|j| j + 1);
        }
        if self.starts("<![", i) {
            return self.find(">", i + 3).map(|j| j + 1);
        }
        self.find(">", i + 2).map(|j| j + 1)
    }
}
