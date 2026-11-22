//! Pairwise PEP 440 ordering with arbitrary precision numeric components.

use std::ops::Range;

use pep440_rs::Version;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyType};

use super::{python_whitespace, validate_integer_limits};

pyo3::create_exception!(packaging.version, InvalidVersion, PyValueError);

fn invalid_version(value: &Bound<'_, PyAny>) -> PyResult<PyErr> {
    let message = format!("Invalid version: {}", value.repr()?);
    // Reuse an already loaded public exception without loading Packaging.
    let modules = value.py().import("sys")?.getattr("modules")?;
    if let Some(module) = modules.cast::<PyDict>()?.get_item("packaging.version")?
        && let Ok(exception) = module.getattr("InvalidVersion")?.cast_into::<PyType>()
    {
        return Ok(PyErr::from_type(exception, (message,)));
    }
    Ok(InvalidVersion::new_err(message))
}

fn numeric_spans(version: &str) -> Vec<Range<usize>> {
    let public_end = version.find('+').unwrap_or(version.len());
    let mut spans = Vec::new();
    let bytes = version.as_bytes();
    let mut offset = 0;
    while offset < public_end {
        if !bytes[offset].is_ascii_digit() {
            offset += 1;
            continue;
        }
        let start = offset;
        while offset < public_end && bytes[offset].is_ascii_digit() {
            offset += 1;
        }
        spans.push(start..offset);
    }
    if public_end != version.len() {
        let mut offset = public_end + 1;
        for part in version[offset..].split(['.', '_', '-']) {
            if !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()) {
                spans.push(offset..offset + part.len());
            }
            offset += part.len() + 1;
        }
    }
    spans
}

fn canonical_integer(number: &str) -> &str {
    let normalized = number.trim_start_matches('0');
    if normalized.is_empty() {
        "0"
    } else {
        normalized
    }
}

fn ranked_pair(left: &str, right: &str) -> [String; 2] {
    let originals = [left, right];
    let spans = originals.map(numeric_spans);
    let mut numbers = vec!["0"];
    for (version, spans) in originals.iter().zip(&spans) {
        numbers.extend(
            spans
                .iter()
                .map(|span| canonical_integer(&version[span.clone()])),
        );
    }
    numbers.sort_unstable_by(|left, right| left.len().cmp(&right.len()).then(left.cmp(right)));
    numbers.dedup();
    // Zero retains rank zero for implicit epoch/release/prerelease components.
    // A shared ordering preserves every numeric comparison in pep440_rs; local
    // alphanumeric segments remain untouched because they compare as strings.
    std::array::from_fn(|index| {
        let version = originals[index];
        let mut output = String::with_capacity(version.len());
        let mut start = 0;
        for span in &spans[index] {
            output.push_str(&version[start..span.start]);
            let number = canonical_integer(&version[span.clone()]);
            let rank = numbers
                .binary_search_by(|candidate| {
                    candidate
                        .len()
                        .cmp(&number.len())
                        .then(candidate.cmp(&number))
                })
                .expect("rank collected from the same component");
            output.push_str(&rank.to_string());
            start = span.end;
        }
        output.push_str(&version[start..]);
        output
    })
}

/// Compare optional dependency versions without importing Packaging.
#[pyfunction]
pub(crate) fn pytest_version_less_than(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let Ok(left_string) = left.cast::<PyString>() else {
        return Err(invalid_version(left)?);
    };
    let left_text = left_string.to_string_lossy();
    // Construction of the left Version precedes all validation of the right.
    let right_text = right
        .cast::<PyString>()
        .ok()
        .map(|value| value.to_string_lossy());
    let left_trimmed = left_text.trim_matches(python_whitespace);
    let right_trimmed = right_text
        .as_deref()
        .unwrap_or("")
        .trim_matches(python_whitespace);
    let [normalized_left, normalized_right] = ranked_pair(left_trimmed, right_trimmed);
    let Ok(parsed_left) = normalized_left.parse::<Version>() else {
        return Err(invalid_version(left)?);
    };
    validate_integer_limits(left.py(), left_trimmed)?;
    if right_text.is_none() {
        return Err(invalid_version(right)?);
    }
    let Ok(parsed_right) = normalized_right.parse::<Version>() else {
        return Err(invalid_version(right)?);
    };
    validate_integer_limits(right.py(), right_trimmed)?;
    Ok(parsed_left < parsed_right)
}
