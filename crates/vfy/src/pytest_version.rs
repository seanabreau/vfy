//! Comparison of pytest version strings for compatibility checks.

use pep440_rs::{Version, VersionSpecifier};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyInt;

#[path = "pytest_version/comparison.rs"]
mod comparison;
pub(crate) use comparison::pytest_version_less_than;

#[derive(Clone, Copy)]
enum SupportedPlugin {
    Benchmark,
    Mock,
    Randomly,
    Timeout,
}

impl SupportedPlugin {
    fn contains(self, version: &Version) -> bool {
        if version.any_prerelease() {
            return false;
        }
        let (minimum, maximum) = match self {
            Self::Benchmark => (Version::new([5, 3]), Version::new([5, 4])),
            Self::Mock => (Version::new([3, 13]), Version::new([4])),
            Self::Randomly => return *version == Version::new([5, 0, 0]),
            Self::Timeout => (Version::new([2, 4]), Version::new([2, 5])),
        };
        VersionSpecifier::greater_than_equal_version(minimum).contains(version)
            && VersionSpecifier::less_than_version(maximum).contains(version)
    }
}

// Packaging accepts Python whitespace around the ASCII PEP 440 grammar.
fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

fn bounded_version(version: &str) -> String {
    // Every gate bound has components below 100. Saturation preserves these
    // comparisons while accepting Python integers larger than the crate's u64.
    let mut normalized = String::with_capacity(version.len());
    let mut characters = version.trim_matches(python_whitespace).chars().peekable();
    while let Some(character) = characters.next() {
        if !character.is_ascii_digit() {
            normalized.push(character);
            continue;
        }
        let mut number = u32::from(character) - u32::from('0');
        while let Some(next) = characters.peek().and_then(|next| next.to_digit(10)) {
            number = (number * 10 + next).min(100);
            let _ = characters.next();
        }
        normalized.push_str(&number.to_string());
    }
    normalized
}

fn validate_integer_limits(py: Python<'_>, version: &str) -> PyResult<()> {
    let (public, local) = version.split_once('+').unwrap_or((version, ""));
    let public_numbers = public.split(|character: char| !character.is_ascii_digit());
    let local_numbers = local
        .split(['.', '_', '-'])
        .filter(|part| part.bytes().all(|byte| byte.is_ascii_digit()));
    for number in public_numbers.chain(local_numbers) {
        // CPython's smallest nonzero limit is 640. Calling int for longer
        // tokens preserves its configured limit and exact ValueError message.
        if number.len() >= 640 {
            py.get_type::<PyInt>().call1((number,))?;
        }
    }
    Ok(())
}

/// Check a supported plugin release range without loading packaging.
///
/// Invalid PEP 440 versions return false; `CPython` integer digit limits propagate.
#[pyfunction]
pub(crate) fn pytest_plugin_version_supported(
    py: Python<'_>,
    plugin: &str,
    version: &str,
) -> PyResult<bool> {
    let plugin = match plugin {
        "benchmark" => SupportedPlugin::Benchmark,
        "mock" => SupportedPlugin::Mock,
        "randomly" => SupportedPlugin::Randomly,
        "timeout" => SupportedPlugin::Timeout,
        _ => {
            return Err(PyValueError::new_err(
                "expected plugin 'benchmark', 'mock', 'randomly', or 'timeout'",
            ));
        }
    };
    let normalized = bounded_version(version);
    let Ok(parsed) = normalized.parse::<Version>() else {
        return Ok(false);
    };
    validate_integer_limits(py, version.trim_matches(python_whitespace))?;
    Ok(plugin.contains(&parsed))
}

#[cfg(test)]
mod tests;
