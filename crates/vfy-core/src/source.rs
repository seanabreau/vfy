//! Python source decoding and line-column locations shared by analysis, semantic evidence, and rendering.

use std::io::{Error, ErrorKind};
use std::ops::Range;
use std::path::Path;

/// A borrowed source-text locator with precomputed physical line starts.
///
/// Lines are one-based and columns are zero-based UTF-8 byte columns, not
/// character or display columns. Offsets are clamped to the end of the source.
/// LF, CRLF, and lone CR each terminate a physical line.
/// Empty input therefore has an EOF position of `(1, 0)`. A newline starts a
/// new line, including a final newline, so the EOF after a terminal newline is
/// at column zero of the following empty line.
#[derive(Debug)]
pub struct SourceLocator<'a> {
    source: &'a str,
    starts: Vec<usize>,
}

impl<'a> SourceLocator<'a> {
    /// Precompute the byte offset of every physical line in `source`.
    #[must_use]
    pub fn new(source: &'a str) -> Self {
        let mut starts = vec![0];
        let bytes = source.as_bytes();
        starts.extend(bytes.iter().enumerate().filter_map(|(index, &byte)| {
            (byte == b'\n' || (byte == b'\r' && bytes.get(index + 1) != Some(&b'\n')))
                .then_some(index + 1)
        }));
        Self { source, starts }
    }

    /// Return the one-based line containing `offset`.
    #[must_use]
    pub fn line(&self, offset: usize) -> i32 {
        let offset = offset.min(self.source.len());
        let index = match self.starts.binary_search(&offset) {
            Ok(index) => index + 1,
            Err(index) => index,
        };
        i32::try_from(index).unwrap_or(i32::MAX)
    }

    /// Return the zero-based UTF-8 byte column at `offset`.
    #[must_use]
    pub fn byte_column(&self, offset: usize) -> i32 {
        let offset = offset.min(self.source.len());
        let line = self.line(offset);
        let start = self.line_start(line).unwrap_or(0);
        i32::try_from(offset.saturating_sub(start)).unwrap_or(i32::MAX)
    }

    /// Return a physical line without its trailing CR or LF characters.
    ///
    /// Invalid line numbers return an empty string. The empty line after a
    /// terminal newline is a valid line and also returns an empty string.
    #[must_use]
    pub fn line_text(&self, line: i32) -> &'a str {
        let Ok(index) = usize::try_from(line.saturating_sub(1)) else {
            return "";
        };
        let Some(&start) = self.starts.get(index) else {
            return "";
        };
        let end = self
            .starts
            .get(index + 1)
            .copied()
            .unwrap_or(self.source.len());
        self.source[start..end].trim_end_matches(['\r', '\n'])
    }

    /// Locate a byte range as `(line, column, end_line, end_column)`.
    ///
    /// The end offset is exclusive and is located directly, rather than by
    /// locating the preceding byte. Consequently a multiline range ending at
    /// a line start has an end column of zero on that line. Both offsets are
    /// clamped independently to EOF.
    #[must_use]
    pub fn range_position(&self, range: Range<usize>) -> (i32, i32, i32, i32) {
        (
            self.line(range.start),
            self.byte_column(range.start),
            self.line(range.end),
            self.byte_column(range.end),
        )
    }

    /// Return the byte offset of a one-based line, when it exists.
    #[must_use]
    pub fn line_start(&self, line: i32) -> Option<usize> {
        usize::try_from(line.saturating_sub(1))
            .ok()
            .and_then(|index| self.starts.get(index).copied())
    }
}

fn cookie(line: &[u8]) -> std::io::Result<Option<String>> {
    let text = std::str::from_utf8(line).map_err(|_| {
        Error::new(
            ErrorKind::InvalidData,
            "invalid or missing encoding declaration",
        )
    })?;
    let Some(comment) = text
        .trim_start_matches([' ', '\t', '\u{c}'])
        .strip_prefix('#')
    else {
        return Ok(None);
    };
    for (index, _) in comment.match_indices("coding") {
        let suffix = &comment[index + 6..];
        if let Some(label) = suffix.strip_prefix([':', '=']) {
            let label = label.trim_start_matches([' ', '\t']);
            let length = label
                .bytes()
                .take_while(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.')
                })
                .count();
            if length > 0 {
                return Ok(Some(label[..length].to_owned()));
            }
        }
    }
    Ok(None)
}

fn is_utf8(label: &str) -> bool {
    matches!(label, "utf-8" | "utf8" | "utf-8-sig") || label.starts_with("utf-8-")
}

fn decode(mut bytes: Vec<u8>) -> std::io::Result<String> {
    if bytes.contains(&b'\r') {
        let mut previous_cr = false;
        bytes.retain_mut(|byte| {
            if *byte == b'\r' {
                *byte = b'\n';
                previous_cr = true;
                true
            } else {
                let skip = *byte == b'\n' && previous_cr;
                previous_cr = false;
                !skip
            }
        });
    }
    let bom = bytes.starts_with(b"\xef\xbb\xbf");
    if bom {
        drop(bytes.drain(..3));
    }
    let mut lines = bytes.split_inclusive(|byte| *byte == b'\n');
    let first = lines.next().unwrap_or_default();
    let first_is_comment_or_blank = first
        .iter()
        .copied()
        .find(|byte| !matches!(byte, b' ' | b'\t' | b'\x0c' | b'\r' | b'\n'))
        .is_none_or(|byte| byte == b'#');
    let mut label = cookie(first)?;
    if label.is_none() && first_is_comment_or_blank {
        label = cookie(lines.next().unwrap_or_default())?;
    }
    let label = label
        .unwrap_or_else(|| "utf-8".to_owned())
        .to_ascii_lowercase()
        .replace('_', "-");
    if bom && !is_utf8(&label) {
        return Err(Error::new(
            ErrorKind::InvalidData,
            "encoding problem: utf-8",
        ));
    }
    if is_utf8(&label) {
        return Ok(match String::from_utf8(bytes) {
            Ok(source) => source,
            Err(error) => String::from_utf8_lossy(error.as_bytes()).into_owned(),
        });
    }
    if matches!(label.as_str(), "ascii" | "us-ascii" | "646" | "us") {
        return Ok(bytes
            .iter()
            .map(|byte| {
                if byte.is_ascii() {
                    char::from(*byte)
                } else {
                    '\u{fffd}'
                }
            })
            .collect());
    }
    // Python's Latin-1 decoder preserves C1 control characters.
    if matches!(
        label.as_str(),
        "latin-1"
            | "latin1"
            | "iso-8859-1"
            | "iso8859-1"
            | "iso-latin-1"
            | "l1"
            | "cp819"
            | "ibm819"
    ) {
        return Ok(bytes.iter().copied().map(char::from).collect());
    }
    if matches!(label.as_str(), "cp1252" | "windows-1252" | "1252") {
        // Undefined Windows-1252 bytes use the same replacement policy as
        // Coverage.py's source reader. C1 controls remain unchanged in Latin-1.
        const C1: [char; 32] = [
            '\u{20ac}', '\u{fffd}', '\u{201a}', '\u{192}', '\u{201e}', '\u{2026}', '\u{2020}',
            '\u{2021}', '\u{2c6}', '\u{2030}', '\u{160}', '\u{2039}', '\u{152}', '\u{fffd}',
            '\u{17d}', '\u{fffd}', '\u{fffd}', '\u{2018}', '\u{2019}', '\u{201c}', '\u{201d}',
            '\u{2022}', '\u{2013}', '\u{2014}', '\u{2dc}', '\u{2122}', '\u{161}', '\u{203a}',
            '\u{153}', '\u{fffd}', '\u{17e}', '\u{178}',
        ];
        return Ok(bytes
            .iter()
            .map(|&byte| match byte {
                0x80..=0x9f => C1[usize::from(byte - 0x80)],
                _ => char::from(byte),
            })
            .collect());
    }
    Err(Error::new(
        ErrorKind::InvalidData,
        format!("unknown or unsupported Python source encoding: {label}"),
    ))
}

/// Read Python source using its encoding declaration and universal newlines.
///
/// # Errors
///
/// Returns filesystem errors or invalid-data errors for invalid encoding declarations.
pub fn read_python_source(path: impl AsRef<Path>) -> std::io::Result<String> {
    decode(std::fs::read(path)?)
}

#[cfg(test)]
mod tests;
