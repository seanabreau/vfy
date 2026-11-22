//! Mutation aggregation and human-readable rendering.

use std::collections::BTreeMap;
use std::fmt::Write;

use serde_json::{Value, json};

use super::cache::{MutationResult, MutationStatus};

fn status_name(status: MutationStatus) -> &'static str {
    match status {
        MutationStatus::Protected => "protected",
        MutationStatus::Survived => "survived",
        MutationStatus::Unreached => "unreached",
        MutationStatus::Invalid => "invalid",
        MutationStatus::Error => "error",
        MutationStatus::Timeout => "timeout",
        MutationStatus::Unstable => "unstable",
        MutationStatus::Killed => "killed",
    }
}

fn summary(results: &[MutationResult]) -> BTreeMap<&'static str, usize> {
    let mut counts = BTreeMap::new();
    for result in results {
        *counts.entry(status_name(result.status)).or_default() += 1;
    }
    counts
}

/// Parse mutation results and metrics into the persisted report schema.
///
/// # Errors
/// Returns an error for malformed result, metric, or optional scope JSON.
pub fn build_mutation_report(
    fingerprint: &str,
    results: &str,
    metrics: &str,
    scope: Option<&str>,
) -> Result<String, String> {
    let results: Vec<MutationResult> =
        serde_json::from_str(results).map_err(|error| error.to_string())?;
    let metrics: BTreeMap<String, Value> =
        serde_json::from_str(metrics).map_err(|error| error.to_string())?;
    let mut value = json!({"schema": "vfy.mutation", "fingerprint": fingerprint, "summary": summary(&results), "mutations": results});
    if let Some(scope) = scope {
        let scope: super::MutationScopeEvidence =
            serde_json::from_str(scope).map_err(|error| error.to_string())?;
        value["scope"] = serde_json::to_value(scope).map_err(|error| error.to_string())?;
    }
    if !metrics.is_empty() {
        value["metrics"] = json!(metrics);
    }
    serde_json::to_string(&value).map_err(|error| error.to_string())
}

/// Render a report and return its exit status.
///
/// Errors, invalid mutations, timeouts, or unstable results return status 1.
/// With `fail_on_survivor`, surviving mutations return status 2; otherwise 0.
///
/// # Errors
/// Returns an error if the report is malformed or the output format is unknown.
pub fn render_mutation_report(
    report: &str,
    output_format: &str,
    pretty: bool,
    fail_on_survivor: bool,
    quoted: impl Fn(&str) -> Result<String, String>,
) -> Result<(String, i32), String> {
    let mut value: Value = serde_json::from_str(report).map_err(|error| error.to_string())?;
    let results: Vec<MutationResult> =
        serde_json::from_value(value["mutations"].clone()).map_err(|error| error.to_string())?;
    let counts = summary(&results);
    let exit_code = if ["error", "invalid", "timeout", "unstable"]
        .iter()
        .any(|name| counts.contains_key(name))
    {
        1
    } else if fail_on_survivor && counts.contains_key("survived") {
        2
    } else {
        0
    };
    let content = match output_format {
        "json" => {
            value["summary"] = json!(counts);
            value.sort_all_objects();
            format!(
                "{}\n",
                crate::python_json(&value, pretty).map_err(|error| error.to_string())?
            )
        }
        "text" => {
            let mut summary = counts
                .iter()
                .map(|(name, count)| format!("{name}={count}"))
                .collect::<Vec<_>>()
                .join(" ");
            if summary.is_empty() {
                summary.push_str("no mutations");
            }
            let mut output = format!("Counterfactual protection: {summary}\n");
            if let Some(scope) = value.get("scope") {
                let scope: super::MutationScopeEvidence =
                    serde_json::from_value(scope.clone()).map_err(|error| error.to_string())?;
                let _ = writeln!(
                    output,
                    "Scope: {} (merge base {}); {} changed Python file(s)",
                    scope.requested_base,
                    scope.merge_base_commit,
                    scope.selected_files.len()
                );
            }
            if let Some(metrics) = value
                .get("metrics")
                .and_then(Value::as_object)
                .filter(|metrics| !metrics.is_empty())
            {
                let elapsed = metrics
                    .get("elapsed_ms")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0)
                    / 1000.0;
                let mutations = metrics
                    .get("mutations")
                    .cloned()
                    .unwrap_or_else(|| json!(results.len()));
                let hits = metrics
                    .get("cache_hits")
                    .cloned()
                    .unwrap_or_else(|| json!(0));
                let fallback = metrics
                    .get("portable_fallbacks")
                    .cloned()
                    .unwrap_or_else(|| json!(0));
                let _ = writeln!(
                    output,
                    "Completed {mutations} mutations in {elapsed:.2}s (cache hits={hits}, portable fallback={fallback})."
                );
            }
            for result in results
                .iter()
                .filter(|result| !matches!(result.status, MutationStatus::Protected))
            {
                let mutation = &result.mutation;
                let status = status_name(result.status);
                let suffix = result
                    .killer
                    .as_ref()
                    .filter(|killer| !killer.is_empty())
                    .map_or_else(String::new, |killer| format!("; killer={killer}"));
                let _ = writeln!(
                    output,
                    "{status:10} {}:{}:{} {}: diff={} -> {}{suffix}",
                    mutation.path,
                    mutation.line,
                    mutation.column,
                    mutation.operator,
                    quoted(&mutation.original)?,
                    quoted(&mutation.replacement)?
                );
            }
            output
        }
        _ => return Err(format!("unknown mutation report format: {output_format}")),
    };
    Ok((content, exit_code))
}
