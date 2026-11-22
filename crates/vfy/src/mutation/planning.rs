//! Batched mutation selection and evidence ranking at the extension boundary.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;

use vfy_core::mutation::{
    FreshMutation, MutationInput, MutationJobKind, MutationPhase, MutationTestIsolation,
    MutationWorkerLease, collected_mutation_worker_limit as collected_worker_limit,
    mutation_location, mutation_portable_worker_limit as portable_worker_limit,
    next_mutation_job as next_job, order_mutation_jobs as order_jobs,
};

use super::MutationRecord;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MutationTestScope {
    Selected,
    FullSuite,
    Unreached,
}

fn parse_mutation_job_kind(value: &str) -> PyResult<MutationJobKind> {
    match value {
        "collected" => Ok(MutationJobKind::Collected),
        "portable" => Ok(MutationJobKind::Portable),
        _ => Err(PyValueError::new_err(format!(
            "invalid mutation job kind: {value}; expected 'collected' or 'portable'"
        ))),
    }
}

fn parse_mutation_isolation(value: &str) -> PyResult<MutationTestIsolation> {
    match value {
        "serial" => Ok(MutationTestIsolation::Serial),
        "file" => Ok(MutationTestIsolation::File),
        other => Err(PyValueError::new_err(format!(
            "invalid mutation test isolation {other:?}; expected 'serial' or 'file'"
        ))),
    }
}

fn parse_mutation_worker_lease(value: &str) -> PyResult<MutationWorkerLease> {
    match value {
        "available" => Ok(MutationWorkerLease::Available),
        "exhausted" => Ok(MutationWorkerLease::Exhausted),
        other => Err(PyValueError::new_err(format!(
            "invalid mutation worker lease {other:?}; expected 'available' or 'exhausted'"
        ))),
    }
}

/// Whether baseline coverage recorded any execution in the mutated file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FileExecution {
    Executed,
    NeverExecuted,
}

fn mutation_test_scope(
    phase: MutationPhase,
    execution: FileExecution,
    has_tests: bool,
) -> MutationTestScope {
    match (phase, execution, has_tests) {
        // Definitions run at import time, often outside any test context, so
        // only a file that no session ever executed proves them unreached.
        (MutationPhase::Definition, FileExecution::Executed, _) => MutationTestScope::FullSuite,
        (MutationPhase::Definition, FileExecution::NeverExecuted, _)
        | (MutationPhase::Body, _, false) => MutationTestScope::Unreached,
        (MutationPhase::Body, _, true) => MutationTestScope::Selected,
    }
}

fn planned_test_ids(
    phase: MutationPhase,
    execution: FileExecution,
    tests: Vec<(String, Option<f64>)>,
) -> (MutationTestScope, Vec<String>) {
    if phase == MutationPhase::Definition {
        return (mutation_test_scope(phase, execution, false), Vec::new());
    }
    let tests: Vec<String> = tests.into_iter().map(|(test, _)| test).collect();
    (
        mutation_test_scope(phase, execution, !tests.is_empty()),
        tests,
    )
}

fn context_group(
    path: &str,
    line: i32,
    locations: &BTreeMap<String, BTreeSet<String>>,
) -> Vec<String> {
    let path = vfy_core::glob_path(&vfy_core::absolute_path(Path::new(path)));
    locations
        .get(&format!("{path}:{line}"))
        .into_iter()
        .flatten()
        .map(|test| format!("{test}|run"))
        .collect()
}

fn rank_contexts<'a>(
    mutations: impl Iterator<Item = (&'a str, i32, MutationPhase)>,
    locations: &BTreeMap<String, BTreeSet<String>>,
    history_path: &str,
) -> Vec<Vec<(String, Option<f64>)>> {
    let groups = mutations
        .map(|(path, line, phase)| {
            if phase == MutationPhase::Definition {
                Vec::new()
            } else {
                context_group(path, line, locations)
            }
        })
        .collect();
    crate::selection::rank_test_contexts(groups, history_path)
}

impl MutationTestScope {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Selected => "selected",
            Self::FullSuite => "full_suite",
            Self::Unreached => "unreached",
        }
    }
}

fn parse_phase(phase: &str) -> PyResult<MutationPhase> {
    match phase {
        "definition" => Ok(MutationPhase::Definition),
        "body" => Ok(MutationPhase::Body),
        _ => Err(PyValueError::new_err(format!(
            "invalid mutation execution phase: {phase}"
        ))),
    }
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one complete evidence batch."
)]
pub(crate) fn rank_mutation_tests(
    mutations: Vec<(String, String, i32)>,
    locations: BTreeMap<String, BTreeSet<String>>,
    history_path: &str,
) -> BTreeMap<String, Vec<String>> {
    let ranked = rank_contexts(
        mutations
            .iter()
            .map(|(_, path, line)| (path.as_str(), *line, MutationPhase::Body)),
        &locations,
        history_path,
    );
    mutations
        .into_iter()
        .zip(ranked)
        .map(|((key, _, _), tests)| (key, tests.into_iter().map(|(test, _)| test).collect()))
        .collect()
}

#[pyfunction]
#[pyo3(signature = (mutations, locations, executed_paths, history_path, omitted_paths, *, collection_data=None, mutation_inputs=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one complete evidence batch."
)]
pub(crate) fn plan_mutation_tests(
    mutations: Vec<(String, String, i32, String)>,
    locations: BTreeMap<String, BTreeSet<String>>,
    executed_paths: BTreeSet<String>,
    history_path: &str,
    omitted_paths: Vec<String>,
    collection_data: Option<PyRef<'_, crate::data::CoverageData>>,
    mutation_inputs: Option<&str>,
) -> PyResult<BTreeMap<String, (String, Vec<String>)>> {
    let omitted = vfy_core::glob_set(&omitted_paths)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    let working_dir = std::env::current_dir().map_err(PyOSError::new_err)?;
    let mutations = mutations
        .into_iter()
        .map(|(key, path, line, phase)| Ok((key, path, line, parse_phase(&phase)?)))
        .collect::<PyResult<Vec<_>>>()?;
    let snapshot = collection_data.as_ref().map(|data| data.core_snapshot());
    let unisolated =
        collection_executed_mutations(&mutations, snapshot.as_deref(), mutation_inputs)?;
    let measured = snapshot.as_ref().map(|data| {
        data.measured_files()
            .into_iter()
            .map(|path| vfy_core::glob_path(&vfy_core::absolute_path(Path::new(&path))))
            .collect::<BTreeSet<_>>()
    });
    let ranked = rank_contexts(
        mutations
            .iter()
            .map(|(_, path, line, phase)| (path.as_str(), *line, *phase)),
        &locations,
        history_path,
    );
    let executed = vfy_core::mutation::ExecutedFiles::new(executed_paths);
    Ok(mutations
        .into_iter()
        .zip(ranked)
        .map(|((key, path, _, phase), tests)| {
            // Excluded or unmeasured files have no evidence proving a mutation unreached.
            let absolute = vfy_core::absolute_path(Path::new(&path));
            let absolute_path = vfy_core::glob_path(&absolute);
            let relative = absolute
                .strip_prefix(&working_dir)
                .ok()
                .map(vfy_core::glob_path);
            if measured
                .as_ref()
                .is_some_and(|paths| !paths.contains(&absolute_path))
                || omitted.as_ref().is_some_and(|patterns| {
                    patterns.is_match(&absolute_path)
                        || relative
                            .as_ref()
                            .is_some_and(|path| patterns.is_match(path))
                })
            {
                return (
                    key,
                    (MutationTestScope::FullSuite.as_str().to_owned(), Vec::new()),
                );
            }
            let execution = if executed.contains(&path) {
                FileExecution::Executed
            } else {
                FileExecution::NeverExecuted
            };
            let (scope, tests) =
                if execution == FileExecution::Executed && unisolated.contains(&key) {
                    (MutationTestScope::FullSuite, Vec::new())
                } else {
                    planned_test_ids(phase, execution, tests)
                };
            (key, (scope.as_str().to_owned(), tests))
        })
        .collect())
}

fn collection_executed_mutations(
    mutations: &[(String, String, i32, MutationPhase)],
    collection_data: Option<&vfy_core::data::CoverageData>,
    mutation_inputs: Option<&str>,
) -> PyResult<BTreeSet<String>> {
    let (data, encoded) = match (collection_data, mutation_inputs) {
        (None, None) => return Ok(BTreeSet::new()),
        (Some(data), Some(encoded)) => (data, encoded),
        _ => {
            return Err(PyValueError::new_err(
                "mutation collection data and inputs must be supplied together",
            ));
        }
    };
    let inputs: Vec<MutationInput> =
        serde_json::from_str(encoded).map_err(|error| PyValueError::new_err(error.to_string()))?;
    if inputs.len() != mutations.len() {
        return Err(PyValueError::new_err(
            "mutation collection inputs disagree with the planning catalog",
        ));
    }
    let inputs = inputs
        .into_iter()
        .zip(mutations)
        .map(|(input, (key, path, line, phase))| {
            let mutation = FreshMutation::new(input).map_err(PyValueError::new_err)?;
            let input = mutation.input();
            if input.id != *key
                || input.path != *path
                || input.line != *line
                || input.owner.phase != *phase
            {
                return Err(PyValueError::new_err(
                    "mutation collection inputs disagree with the planning catalog",
                ));
            }
            Ok(mutation)
        })
        .collect::<PyResult<Vec<_>>>()?;
    let unattributed = vfy_core::mutation::mutation_unattributed_lines(data);
    let mut by_path: BTreeMap<&str, Vec<&MutationInput>> = BTreeMap::new();
    for mutation in &inputs {
        let input = mutation.input();
        if input.owner.phase == MutationPhase::Body {
            by_path.entry(&input.path).or_default().push(input);
        }
    }
    let mut unisolated = BTreeSet::new();
    for (path, mutations) in by_path {
        let source = std::fs::read(path).map_err(PyOSError::new_err)?;
        for input in mutations {
            if !vfy_core::mutation::mutation_body_isolated(input, &source, &unattributed)
                .map_err(PyValueError::new_err)?
            {
                unisolated.insert(input.id.clone());
            }
        }
    }
    Ok(unisolated)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one batched owner/context request."
)]
pub(crate) fn mutation_test_preferences(
    mutations: Vec<(String, String, u32, u32)>,
    locations: BTreeMap<String, BTreeSet<String>>,
    history_path: &str,
) -> PyResult<BTreeMap<String, Vec<String>>> {
    let context_groups = mutations
        .iter()
        .map(|(_, path, start, end)| {
            let source = vfy_core::read_python_source(Path::new(path)).map_err(|error| {
                PyOSError::new_err(format!(
                    "failed to read mutation owner source {path}: {error}"
                ))
            })?;
            Ok(vfy_core::mutation::mutation_test_context_group(
                path, &source, *start, *end, &locations,
            ))
        })
        .collect::<PyResult<Vec<_>>>()?;
    let ranked = crate::selection::rank_test_contexts(context_groups, history_path);
    Ok(mutations
        .into_iter()
        .zip(ranked)
        .map(|((key, _, _, _), tests)| (key, tests.into_iter().map(|(test, _)| test).collect()))
        .collect())
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one complete grouping request."
)]
pub(crate) fn plan_mutation_test_groups(
    nodeids: Vec<String>,
    preferred_nodeids: Vec<String>,
) -> Vec<Vec<usize>> {
    vfy_core::mutation::plan_mutation_test_groups(&nodeids, &preferred_nodeids)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one complete scheduling batch."
)]
pub(crate) fn order_mutation_jobs(kinds: Vec<String>) -> PyResult<Vec<usize>> {
    let kinds = kinds
        .iter()
        .map(|kind| parse_mutation_job_kind(kind))
        .collect::<PyResult<Vec<_>>>()?;
    Ok(order_jobs(&kinds))
}

#[pyfunction]
pub(crate) fn collected_mutation_worker_limit(capacity: usize, active_portable: usize) -> usize {
    collected_worker_limit(capacity, active_portable)
}

#[pyfunction]
pub(crate) fn mutation_portable_worker_limit(capacity: usize, isolation: &str) -> PyResult<usize> {
    Ok(portable_worker_limit(
        capacity,
        parse_mutation_isolation(isolation)?,
    ))
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one scheduler request."
)]
pub(crate) fn next_mutation_job(
    pending: Vec<String>,
    active: Vec<String>,
    capacity: usize,
    isolation: &str,
    lease: &str,
) -> PyResult<Option<usize>> {
    let pending = pending
        .iter()
        .map(|kind| parse_mutation_job_kind(kind))
        .collect::<PyResult<Vec<_>>>()?;
    let active = active
        .iter()
        .map(|kind| parse_mutation_job_kind(kind))
        .collect::<PyResult<Vec<_>>>()?;
    Ok(next_job(
        &pending,
        &active,
        capacity,
        parse_mutation_isolation(isolation)?,
        parse_mutation_worker_lease(lease)?,
    ))
}

#[cfg(test)]
mod tests;

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts catalog options once."
)]
pub(crate) fn select_mutation_catalog(
    paths: Vec<String>,
    target: Option<&str>,
    operators: Vec<String>,
    limit: usize,
    keys: Option<&str>,
) -> PyResult<Vec<MutationRecord>> {
    let location = mutation_location(target).map_err(PyValueError::new_err)?;
    let keys = keys
        .filter(|keys| !keys.is_empty())
        .map(|keys| {
            serde_json::from_str::<BTreeSet<String>>(keys)
                .map_err(|_| PyValueError::new_err("VFY_MUTATION_KEYS must be a JSON string array"))
        })
        .transpose()?;
    let paths = paths
        .into_iter()
        .filter(|path| {
            let path = Path::new(path);
            path.is_file() && path.extension().is_some_and(|extension| extension == "py")
        })
        .collect();
    let mut records = super::analyze_mutation_files(paths)?;
    records.retain(|record| {
        location.as_ref().is_none_or(|(path, line)| {
            vfy_core::absolute_path(Path::new(&record.1)) == *path && record.9 == *line
        }) && (operators.is_empty()
            || operators.contains(&record.3)
            || operators.contains(&record.4))
            && keys.as_ref().is_none_or(|keys| keys.contains(&record.0))
    });
    if limit > 0 {
        records.truncate(limit);
    }
    if records.is_empty() {
        return Err(PyValueError::new_err(
            "no supported mutations were found in the requested source",
        ));
    }
    Ok(records)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the owned list of resolved source roots."
)]
pub(crate) fn select_mutation_sources(
    roots: Vec<String>,
    target: Option<&str>,
    all_sources: bool,
    base: Option<&str>,
) -> PyResult<(Vec<String>, bool, Option<String>)> {
    use vfy_core::mutation::MutationScope;
    if base.is_some() && (all_sources || target.is_some()) {
        return Err(PyValueError::new_err(
            "--base cannot be combined with --all or PATH:LINE",
        ));
    }
    if let Some((path, _)) = mutation_location(target).map_err(PyValueError::new_err)? {
        return Ok((vec![path.to_string_lossy().into_owned()], false, None));
    }
    let scope = if let Some(base) = base {
        MutationScope::GitBase(base)
    } else if all_sources {
        MutationScope::All
    } else {
        MutationScope::WorkingTree
    };
    let selected = vfy_core::mutation::select_mutation_sources(&roots, scope)
        .map_err(PyValueError::new_err)?;
    let scope = selected
        .scope
        .as_ref()
        .map(serde_json::to_string)
        .transpose()
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok((selected.paths, selected.fallback, scope))
}
