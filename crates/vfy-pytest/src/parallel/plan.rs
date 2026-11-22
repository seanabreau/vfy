//! Parallel plan schema, validation, and worker partitioning.

use super::{
    JsonSizeCounter, MAX_NODEID_BYTES, MAX_PARALLEL_NODEIDS, MAX_PARALLEL_WORKERS, MAX_PLAN_BYTES,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};
use std::path::Path;

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum ParallelScheduling {
    #[default]
    File,
    Scope,
    Cases,
    Striped,
    Demand,
}

impl ParallelScheduling {
    pub(super) fn from_environment() -> PyResult<Self> {
        match std::env::var("VFY_NATIVE_SCHEDULING") {
            Err(std::env::VarError::NotPresent) => Ok(Self::File),
            Ok(value) if value == "file" => Ok(Self::File),
            Ok(value) if value == "scope" => Ok(Self::Scope),
            Ok(value) if value == "cases" => Ok(Self::Cases),
            Ok(value) if value == "striped" => Ok(Self::Striped),
            Ok(value) if value == "demand" => Ok(Self::Demand),
            _ => Err(PyValueError::new_err(
                "VFY_NATIVE_SCHEDULING must be file, scope, cases, striped or demand",
            )),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct ParallelPlan {
    #[serde(default)]
    pub(super) scheduling: ParallelScheduling,
    pub(super) nodeids: Vec<String>,
    pub(super) assignments: Vec<Vec<usize>>,
    pub(super) maxfail: usize,
    pub(super) initial_failures: usize,
}

// Contiguous ranges preserve collection order within each worker.
// Broad fixture scopes may be replicated across workers.
pub(super) fn partition_by_cases(cases: usize, workers: usize) -> Vec<Vec<usize>> {
    let width = workers.max(1).min(cases.max(1));
    let base = cases / width;
    let remainder = cases % width;
    let mut start = 0;
    (0..width)
        .map(|worker| {
            let end = start + base + usize::from(worker < remainder);
            let assignment = (start..end).collect();
            start = end;
            assignment
        })
        .collect()
}

pub(super) fn partition_by_stripes(cases: usize, workers: usize) -> Vec<Vec<usize>> {
    let width = workers.max(1).min(cases.max(1));
    (0..width)
        .map(|worker| (worker..cases).step_by(width).collect())
        .collect()
}

/// Assign collected cases to workers for a static schedule.
///
/// Grouped schedules read recorded durations from `VFY_TEST_HISTORY` when it
/// names a readable history; otherwise every case weighs the same.
pub(super) fn partition(
    scheduling: ParallelScheduling,
    nodeids: &[String],
    workers: usize,
) -> Vec<Vec<usize>> {
    match scheduling {
        ParallelScheduling::File | ParallelScheduling::Scope => partition_by_scope(
            nodeids,
            workers,
            ScopeGrouping::for_scheduling(scheduling),
            &CaseWeights::from_environment(nodeids),
        ),
        ParallelScheduling::Cases => partition_by_cases(nodeids.len(), workers),
        ParallelScheduling::Striped => partition_by_stripes(nodeids.len(), workers),
        ParallelScheduling::Demand => vec![Vec::new(); workers.max(1)],
    }
}

/// The unit that a grouped schedule keeps in one worker.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ScopeGrouping {
    /// Every case from one file.
    File,
    /// Methods of one class, or the module-level functions of one file,
    /// matching pytest-xdist's `loadscope` grouping.
    Class,
}

impl ScopeGrouping {
    pub(super) fn for_scheduling(scheduling: ParallelScheduling) -> Self {
        if scheduling == ParallelScheduling::Scope {
            Self::Class
        } else {
            Self::File
        }
    }

    pub(super) fn key(self, nodeid: &str) -> &str {
        match self {
            Self::File => nodeid.split_once("::").map_or(nodeid, |(file, _)| file),
            Self::Class => {
                // Parameter IDs may contain `::`; only the name before them
                // separates the test from its owning class or module. Paths
                // may contain `[`, so the search starts after the file.
                let path_end = nodeid.find("::").map_or(nodeid.len(), |index| index + 2);
                let name_end = nodeid[path_end..]
                    .find('[')
                    .map_or(nodeid.len(), |offset| path_end + offset);
                nodeid[..name_end]
                    .rfind("::")
                    .map_or(nodeid, |separator| &nodeid[..separator])
            }
        }
    }
}

pub(super) struct CaseWeights(pub(super) Vec<f64>);

/// Fixed per-case cost added to duration weights. It keeps zero-duration groups
/// from stacking on one worker.
const CASE_OVERHEAD_MS: f64 = 0.1;

impl CaseWeights {
    pub(super) fn uniform(cases: usize) -> Self {
        Self(vec![1.0; cases])
    }

    pub(super) fn from_environment(nodeids: &[String]) -> Self {
        match std::env::var_os("VFY_TEST_HISTORY") {
            Some(path) => Self::from_history(
                nodeids,
                &vfy_core::read_test_history(Path::new(&path)).durations,
            ),
            None => Self::uniform(nodeids.len()),
        }
    }

    /// Weigh cases by their last recorded duration in milliseconds.
    ///
    /// Cases without a record use the upper middle recorded duration of this plan.
    /// Without any record, every case weighs the same.
    pub(super) fn from_history(nodeids: &[String], durations: &BTreeMap<String, f64>) -> Self {
        let recorded: Vec<Option<f64>> = nodeids
            .iter()
            .map(|nodeid| durations.get(nodeid).copied())
            .collect();
        let mut known: Vec<f64> = recorded.iter().flatten().copied().collect();
        if known.is_empty() {
            return Self::uniform(nodeids.len());
        }
        known.sort_unstable_by(f64::total_cmp);
        let median = known[known.len() / 2];
        Self(
            recorded
                .into_iter()
                .map(|duration| duration.unwrap_or(median) + CASE_OVERHEAD_MS)
                .collect(),
        )
    }
}

pub(super) fn group_count(nodeids: &[String], grouping: ScopeGrouping) -> usize {
    nodeids
        .iter()
        .map(|nodeid| grouping.key(nodeid))
        .collect::<std::collections::HashSet<_>>()
        .len()
}

pub(super) fn partition_by_scope(
    nodeids: &[String],
    workers: usize,
    grouping: ScopeGrouping,
    weights: &CaseWeights,
) -> Vec<Vec<usize>> {
    let mut groups: Vec<(f64, Vec<usize>)> = Vec::new();
    let mut positions = HashMap::new();
    for (index, nodeid) in nodeids.iter().enumerate() {
        let group_index = *positions.entry(grouping.key(nodeid)).or_insert_with(|| {
            groups.push((0.0, Vec::new()));
            groups.len() - 1
        });
        let group = &mut groups[group_index];
        group.0 += weights.0[index];
        group.1.push(index);
    }

    // Assign the most expensive groups first so a late large group does not
    // become a straggler. Stable sorting keeps collection order on ties.
    groups.sort_by(|left, right| right.0.total_cmp(&left.0));
    let mut assignments = vec![Vec::new(); workers.max(1).min(groups.len().max(1))];
    let mut loads = vec![0.0_f64; assignments.len()];
    for (weight, indices) in groups {
        let worker = loads
            .iter()
            .enumerate()
            .min_by(|(left_worker, left), (right_worker, right)| {
                left.total_cmp(right).then(left_worker.cmp(right_worker))
            })
            .map_or(0, |(worker, _)| worker);
        loads[worker] += weight;
        assignments[worker].extend(indices);
    }
    for assignment in &mut assignments {
        assignment.sort_unstable();
    }
    assignments
}

pub(super) fn validate_plan(plan: &ParallelPlan) -> Result<(), String> {
    if plan.nodeids.len() > MAX_PARALLEL_NODEIDS
        || plan
            .nodeids
            .iter()
            .any(|nodeid| nodeid.is_empty() || nodeid.len() > MAX_NODEID_BYTES)
    {
        return Err("native parallel plan exceeds the supported manifest size".to_owned());
    }
    if plan.assignments.is_empty() || plan.assignments.len() > MAX_PARALLEL_WORKERS {
        return Err("native parallel plan has an invalid worker count".to_owned());
    }
    if plan.scheduling == ParallelScheduling::Demand {
        return if plan.assignments.iter().all(Vec::is_empty) {
            Ok(())
        } else {
            Err("native demand plan contains static assignments".to_owned())
        };
    }
    let mut seen = vec![false; plan.nodeids.len()];
    let mut file_workers = HashMap::new();
    for (worker, assignment) in plan.assignments.iter().enumerate() {
        if !assignment.windows(2).all(|pair| pair[0] < pair[1]) {
            return Err(format!(
                "native parallel worker {worker} assignment is not in collection order"
            ));
        }
        for index in assignment {
            let Some(assigned) = seen.get_mut(*index) else {
                return Err(format!(
                    "native parallel assignment index {index} is out of range"
                ));
            };
            if *assigned {
                return Err(format!(
                    "native parallel assignment repeats collection index {index}"
                ));
            }
            *assigned = true;
            if matches!(
                plan.scheduling,
                ParallelScheduling::File | ParallelScheduling::Scope
            ) {
                let grouping = ScopeGrouping::for_scheduling(plan.scheduling);
                let scope = grouping.key(&plan.nodeids[*index]);
                if file_workers
                    .insert(scope, worker)
                    .is_some_and(|owner| owner != worker)
                {
                    return Err(match grouping {
                        ScopeGrouping::File => {
                            format!("native parallel plan splits file {scope:?} across workers")
                        }
                        ScopeGrouping::Class => {
                            format!("native parallel plan splits scope {scope:?} across workers")
                        }
                    });
                }
            }
        }
    }
    if seen.iter().any(|assigned| !assigned) {
        return Err("native parallel plan omits collected tests".to_owned());
    }
    Ok(())
}

pub(super) fn validate_plan_size(plan: &ParallelPlan) -> Result<(), String> {
    let mut counter = JsonSizeCounter { bytes: 0 };
    serde_json::to_writer(&mut counter, &plan)
        .map_err(|error| format!("failed to size native parallel plan: {error}"))?;
    if counter.bytes > MAX_PLAN_BYTES {
        return Err("native parallel collection exceeds the supported manifest size".to_owned());
    }
    Ok(())
}

pub(super) fn first_mismatch(expected: &[String], actual: &[String]) -> usize {
    expected
        .iter()
        .zip(actual)
        .position(|(left, right)| left != right)
        .unwrap_or(expected.len().min(actual.len()))
}
