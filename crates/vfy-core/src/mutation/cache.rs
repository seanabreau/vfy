//! Mutation cache validation and replacement.

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum OwnerKind {
    #[default]
    Module,
    Function,
}

/// Whether a mutation runs while defining an owner or executing its body.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum MutationPhase {
    Definition,
    #[default]
    Body,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(default)]
pub struct ExecutionOwner {
    pub kind: OwnerKind,
    pub qualname: String,
    pub start: u32,
    pub end: u32,
    /// Parsed first statement offset; `None` (module-level owners) never authorizes retained selection.
    pub body_start: Option<u32>,
    pub fast_eligible: bool,
    pub phase: MutationPhase,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct MutationInput {
    pub id: String,
    pub path: String,
    pub source_hash: String,
    pub kind: String,
    pub operator: String,
    pub original: String,
    pub replacement: String,
    pub start: u32,
    pub end: u32,
    pub line: i32,
    pub column: i32,
    #[serde(default)]
    pub owner: ExecutionOwner,
}

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum MutationStatus {
    Protected,
    Survived,
    Unreached,
    Invalid,
    Error,
    Timeout,
    Unstable,
    Killed,
}

impl MutationStatus {
    #[must_use]
    pub const fn cacheable(self) -> bool {
        matches!(self, Self::Protected | Self::Survived | Self::Unreached)
    }
}

#[derive(Clone, Deserialize, Serialize)]
pub struct MutationResult {
    #[serde(flatten)]
    pub mutation: MutationInput,
    pub status: MutationStatus,
    #[serde(default)]
    pub tests: Vec<String>,
    pub killer: Option<String>,
    #[serde(default)]
    pub duration_ms: f64,
    pub returncode: Option<i32>,
    #[serde(default)]
    pub detail: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<String>,
}

impl MutationResult {
    fn valid(&self) -> bool {
        self.duration_ms.is_finite()
            && self.duration_ms >= 0.0
            && self.mutation.owner.end >= self.mutation.owner.start
            && self.mutation.owner.body_start.is_none_or(|start| {
                self.mutation.owner.start <= start
                    && start < self.mutation.owner.end
                    && (self.mutation.owner.phase != MutationPhase::Body
                        || start <= self.mutation.start)
            })
    }
}

fn read_json(path: &Path) -> Result<Option<Value>, String> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(format!(
                "failed to read mutation cache {}: {error}",
                path.display()
            ));
        }
    };
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid mutation cache {}: {error}", path.display()))?;
    validate_cache(&value)
        .map_err(|error| format!("invalid mutation cache {}: {error}", path.display()))?;
    Ok(Some(value))
}

fn validate_result(value: &Value) -> Result<MutationResult, String> {
    let result: MutationResult =
        serde_json::from_value(value.clone()).map_err(|error| error.to_string())?;
    if !result.valid() {
        return Err("invalid mutation result duration or owner".to_owned());
    }
    Ok(result)
}

fn validate_cache(value: &Value) -> Result<(), String> {
    if value.get("schema").and_then(Value::as_str) != Some("vfy.mutation") {
        return Err("unsupported or missing schema".to_owned());
    }
    let entries = value.get("entries");
    let mutations = value.get("mutations");
    if entries.is_none() && mutations.is_none() {
        return Err("missing mutation entries".to_owned());
    }
    if let Some(entries) = entries {
        for (key, entry) in entries.as_object().ok_or("entries must be an object")? {
            entry
                .get("fingerprint")
                .and_then(Value::as_str)
                .ok_or("entry fingerprint must be a string")?;
            let result = validate_result(entry.get("result").unwrap_or(entry))?;
            if result.mutation.id != *key {
                return Err(format!("entry {key:?} has a different mutation id"));
            }
        }
    }
    if let Some(mutations) = mutations {
        value
            .get("fingerprint")
            .and_then(Value::as_str)
            .ok_or("fingerprint must be a string")?;
        for mutation in mutations.as_array().ok_or("mutations must be an array")? {
            validate_result(mutation)?;
        }
    }
    Ok(())
}

/// Return cacheable results whose saved fingerprints match the requested inputs.
///
/// A missing file or changed fingerprint is a miss. Unreadable, malformed, and
/// unsupported cache files are errors, so they cannot be silently replaced.
///
/// # Errors
/// Returns an error when an existing cache cannot be read or validated.
pub fn read_mutation_cache(
    path: &Path,
    fingerprint: &str,
    fingerprints: Option<&BTreeMap<String, String>>,
) -> Result<BTreeMap<String, MutationResult>, String> {
    let Some(value) = read_json(path)? else {
        return Ok(BTreeMap::new());
    };
    let candidates: Vec<&Value> = if let (Some(entries), Some(fingerprints)) = (
        value.get("entries").and_then(Value::as_object),
        fingerprints,
    ) {
        entries
            .iter()
            .filter_map(|(key, entry)| {
                let expected = fingerprints.get(key)?;
                (entry.get("fingerprint").and_then(Value::as_str) == Some(expected.as_str()))
                    .then(|| entry.get("result").unwrap_or(entry))
            })
            .collect()
    } else if value.get("fingerprint").and_then(Value::as_str) == Some(fingerprint) {
        value
            .get("mutations")
            .and_then(Value::as_array)
            .map_or_else(Vec::new, |items| items.iter().collect())
    } else {
        Vec::new()
    };
    let mut results = BTreeMap::new();
    for candidate in candidates {
        let result = validate_result(candidate)?;
        if result.status.cacheable() {
            results.insert(result.mutation.id.clone(), result);
        }
    }
    Ok(results)
}

/// Parse report results and atomically replace the cache file.
///
/// With per-mutation fingerprints, retain existing entries and update cacheable
/// results. Reject invalid durations or reversed owner ranges before writing.
///
/// # Errors
/// Returns an error if the report is malformed or the destination cannot be replaced.
pub fn write_mutation_cache(
    path: &Path,
    report: &str,
    fingerprints: Option<&BTreeMap<String, String>>,
) -> Result<(), String> {
    let mut value: Value = serde_json::from_str(report).map_err(|error| error.to_string())?;
    if value.get("schema").and_then(Value::as_str) != Some("vfy.mutation") {
        return Err("invalid mutation report schema".to_owned());
    }
    validate_cache(&value)?;
    let results = value
        .get("mutations")
        .and_then(Value::as_array)
        .ok_or_else(|| "mutation report must contain a mutation array".to_owned())?;
    let results = results
        .iter()
        .map(|item| {
            let result: MutationResult =
                serde_json::from_value(item.clone()).map_err(|error| error.to_string())?;
            if !result.valid() {
                return Err("invalid mutation result duration or owner".to_owned());
            }
            Ok(result)
        })
        .collect::<Result<Vec<_>, String>>()?;
    let existing = read_json(path)?;
    if let Some(fingerprints) = fingerprints {
        let mut updates = Vec::new();
        for result in results
            .into_iter()
            .filter(|result| result.status.cacheable())
        {
            let key = result.mutation.id.clone();
            let fingerprint = fingerprints
                .get(&key)
                .ok_or_else(|| format!("mutation cache entry {key} has no fingerprint"))?;
            updates.push((key, fingerprint, result));
        }
        let mut entries = existing
            .and_then(|value| value.get("entries").and_then(Value::as_object).cloned())
            .unwrap_or_default();
        for (key, fingerprint, result) in updates {
            entries.insert(key, json!({"fingerprint": fingerprint, "result": result}));
        }
        value["entries"] = Value::Object(entries);
    }
    value.sort_all_objects();
    let content = crate::python_json(&value, true).map_err(|error| error.to_string())?;
    crate::atomic_write(path, format!("{content}\n").as_bytes()).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests;
