//! Identity for a reusable successful test-run artifact.

use std::path::Path;

use serde_json::{Value, json};

fn manifest(coverage_path: &Path) -> Result<Value, String> {
    Ok(json!({
        "schema": "vfy.green-test-run",
        "coverage_hash": crate::file_digest(coverage_path).map_err(|error| error.to_string())?,
    }))
}

/// Check that a record still identifies the complete replay database.
///
/// # Errors
/// Returns an error when the current database cannot be read.
pub fn green_test_run_valid(record_path: &Path, coverage_path: &Path) -> Result<bool, String> {
    let expected = manifest(coverage_path)?;
    let stored = std::fs::read(record_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    Ok(stored.as_ref() == Some(&expected))
}

/// Atomically bind a successful test-run record to its replay database.
///
/// # Errors
/// Returns an input or filesystem error without replacing the existing record.
pub fn write_green_test_run(record_path: &Path, coverage_path: &Path) -> Result<(), String> {
    let value = manifest(coverage_path)?;
    let mut content = crate::python_json(&value, false).map_err(|error| error.to_string())?;
    content.push('\n');
    crate::atomic_write(record_path, content.as_bytes()).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests;
