//! Input manifests for reusing a passing mutation baseline.

use std::path::Path;

use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Deserialize)]
struct BaselineInput {
    tests: Vec<String>,
    pytest_args: Vec<String>,
    suite_state: Vec<(String, String)>,
    data_file: Option<String>,
    data_hash: Option<String>,
}

fn manifest(request: &str) -> Result<Value, String> {
    let input: BaselineInput = serde_json::from_str(request).map_err(|error| error.to_string())?;
    let mut value = json!({
        "schema": "vfy.mutation-baseline",
        "tests": input.tests,
        "pytest_args": input.pytest_args,
        "suite_state": input.suite_state,
    });
    if let Some(path) = input.data_file {
        value["data_file"] = json!(crate::absolute_path(Path::new(&path)).to_string_lossy());
        value["data_hash"] = json!(input.data_hash.ok_or("baseline data hash is missing")?);
        value["green"] = json!(true);
    }
    Ok(value)
}

/// Check whether the stored baseline manifest matches the current inputs.
///
/// # Errors
/// Rejects malformed requests or a data-file entry without a hash. Missing,
/// unreadable, or malformed stored manifests return `false`.
pub fn mutation_baseline_valid(path: &Path, request: &str) -> Result<bool, String> {
    let expected = manifest(request)?;
    let stored = std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    Ok(stored.as_ref() == Some(&expected))
}

/// Parse the input manifest and atomically replace the baseline file.
///
/// # Errors
/// Returns an input or filesystem error without replacing an existing baseline.
pub fn write_mutation_baseline(path: &Path, request: &str) -> Result<(), String> {
    let value = manifest(request)?;
    let mut content = crate::python_json(&value, false).map_err(|error| error.to_string())?;
    content.push('\n');
    crate::atomic_write(path, content.as_bytes()).map_err(|error| error.to_string())
}
