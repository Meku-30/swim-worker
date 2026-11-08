//! SWIM の応答の解析。Worker が parsed で送る結果と、Coordinator が raw を解析した結果が
//! 同じになるよう、両方がこのクレートを使う (元は swim-coordinator の rust/src/ 、コミット 87a3761)。
pub mod parsers;
pub mod pycompat;
pub mod pyjson;
