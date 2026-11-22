//! Mutation job ordering and history-based watchdog budgets.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::{SourceLocator, absolute_path, glob_path};

#[cfg(test)]
mod tests;

/// Execution backend for one scheduled mutation job.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MutationJobKind {
    /// Run the mutation against pytest items from the already collected session.
    Collected,
    /// Start an isolated pytest process with the mutation applied before collection.
    Portable,
}

/// Isolation used by each mutation's selected pytest tests.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MutationTestIsolation {
    /// One pytest process executes the selected tests in collection order.
    Serial,
    /// Isolated pytest file groups share the mutation command's worker pool.
    File,
}

/// How a mutation's watchdog uses its configured timeout.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum MutationTimeoutPolicy {
    /// Reduce the configured ceiling using clean selected-test durations.
    #[default]
    Adaptive,
    /// Use the configured timeout regardless of selected-test history.
    Fixed,
}

/// Whether the shared file-worker pool can lease a slot to a new supervisor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MutationWorkerLease {
    /// A lease may be free; admission still has to acquire it.
    Available,
    /// Every lease is held by a collecting supervisor or an executing test.
    Exhausted,
}

/// Fresh pytest supervisors that may run at once in file isolation.
///
/// Each supervisor retains a fully collected pytest session. It collects while
/// holding one execution lease and hands that lease to its first file group,
/// so collection counts against the requested worker capacity. This bound
/// limits retained sessions; the free leases limit concurrent collection.
const FILE_ISOLATION_PORTABLE_SUPERVISORS: usize = 3;

/// Bound fresh pytest supervisors without reducing the executing-test budget.
///
/// File groups already share the requested worker capacity, and a collecting
/// supervisor holds one of the same leases. Bound retained pytest sessions
/// while allowing their collection to overlap test execution within that
/// capacity. When possible, leave one outer scheduling position for collected
/// jobs so waiting portable supervisors cannot block body mutations. Serial
/// execution retains mutation parallelism.
#[must_use]
pub fn mutation_portable_worker_limit(capacity: usize, isolation: MutationTestIsolation) -> usize {
    match isolation {
        MutationTestIsolation::Serial => capacity,
        MutationTestIsolation::File => capacity
            .saturating_sub(1)
            .max(1)
            .min(capacity)
            .min(FILE_ISOLATION_PORTABLE_SUPERVISORS),
    }
}

/// Select the first pending job that fits the shared execution budget.
///
/// A waiting portable supervisor must not block a collected mutation that can
/// run beside the active supervisor's file groups. In file isolation, a new
/// supervisor also needs a free worker lease for its collection, so
/// `MutationWorkerLease::Exhausted` admits only collected jobs. The effective
/// supervisor bound is therefore the smaller of the retained-session bound and
/// the free leases.
#[must_use]
pub fn next_mutation_job(
    pending: &[MutationJobKind],
    active: &[MutationJobKind],
    capacity: usize,
    isolation: MutationTestIsolation,
    lease: MutationWorkerLease,
) -> Option<usize> {
    if active.len() >= capacity {
        return None;
    }
    let portable = active
        .iter()
        .filter(|kind| **kind == MutationJobKind::Portable)
        .count();
    let collected = active.len() - portable;
    let (portable_limit, collected_limit) = match isolation {
        MutationTestIsolation::Serial => (capacity, capacity),
        MutationTestIsolation::File => (
            match lease {
                MutationWorkerLease::Available => {
                    mutation_portable_worker_limit(capacity, isolation)
                }
                MutationWorkerLease::Exhausted => 0,
            },
            collected_mutation_worker_limit(capacity, portable),
        ),
    };
    pending.iter().position(|kind| match kind {
        MutationJobKind::Portable => portable < portable_limit,
        MutationJobKind::Collected => collected < collected_limit,
    })
}

/// Return mutation job indices in portable-first order, preserving each kind's input order.
#[must_use]
pub fn order_mutation_jobs(kinds: &[MutationJobKind]) -> Vec<usize> {
    let mut order = Vec::with_capacity(kinds.len());
    order.extend(
        kinds
            .iter()
            .enumerate()
            .filter_map(|(index, kind)| (*kind == MutationJobKind::Portable).then_some(index)),
    );
    order.extend(
        kinds
            .iter()
            .enumerate()
            .filter_map(|(index, kind)| (*kind == MutationJobKind::Collected).then_some(index)),
    );
    order
}

/// Limit collected mutation jobs while portable jobs need inner file workers.
///
/// Portable jobs can consume the shared worker pool with pytest file groups.
/// While any are active, leave at least one third of the requested slots available
/// to those groups. When none are active, collected jobs may use full capacity.
#[must_use]
pub fn collected_mutation_worker_limit(capacity: usize, active_portable: usize) -> usize {
    if active_portable == 0 {
        capacity
    } else {
        capacity - capacity.div_ceil(3)
    }
}

fn nodeid_file(nodeid: &str) -> &str {
    nodeid.split_once("::").map_or(nodeid, |(file, _)| file)
}

/// Group pytest nodeid indices by file, moving preferred nodeid files first.
///
/// The preferred IDs only affect group ordering. Every input index appears
/// exactly once, and item order within each file remains collection order.
#[must_use]
pub fn plan_mutation_test_groups(
    nodeids: &[String],
    preferred_nodeids: &[String],
) -> Vec<Vec<usize>> {
    let mut group_by_file = HashMap::new();
    let mut groups: Vec<Vec<usize>> = Vec::new();
    for (index, nodeid) in nodeids.iter().enumerate() {
        let file = nodeid_file(nodeid);
        let group_index = if let Some(index) = group_by_file.get(file) {
            *index
        } else {
            let index = groups.len();
            groups.push(Vec::new());
            group_by_file.insert(file.to_owned(), index);
            index
        };
        groups[group_index].push(index);
    }

    let known_nodeids: HashSet<&str> = nodeids.iter().map(String::as_str).collect();
    let mut preferred_groups = Vec::with_capacity(groups.len());
    let mut seen_groups = vec![false; groups.len()];
    for nodeid in preferred_nodeids {
        if !known_nodeids.contains(nodeid.as_str()) {
            continue;
        }
        let Some(&group_index) = group_by_file.get(nodeid_file(nodeid)) else {
            continue;
        };
        if !seen_groups[group_index] {
            seen_groups[group_index] = true;
            preferred_groups.push(group_index);
        }
    }
    let mut remaining: Vec<_> = seen_groups
        .iter()
        .enumerate()
        .filter_map(|(index, seen)| (!seen).then_some(index))
        .collect();
    // Cold collection has no duration history. Item count is a cheap cost
    // estimate that avoids leaving the largest file until other workers idle.
    remaining.sort_by_key(|&index| std::cmp::Reverse(groups[index].len()));
    preferred_groups.extend(remaining);
    preferred_groups
        .into_iter()
        .map(|index| std::mem::take(&mut groups[index]))
        .collect()
}

/// Collect context tests intersecting an owner's source range for screening.
///
/// These contexts may prioritize work, but do not determine the full-suite
/// execution scope for definition-time mutations.
#[must_use]
pub fn mutation_test_context_group(
    path: &str,
    source: &str,
    start: u32,
    end: u32,
    locations: &BTreeMap<String, BTreeSet<String>>,
) -> Vec<String> {
    let start = start as usize;
    let end = end as usize;
    if start >= end || end > source.len() {
        return Vec::new();
    }
    let locator = SourceLocator::new(source);
    let first_line = locator.line(start);
    let last_line = locator.line(end - 1);
    let path = glob_path(&absolute_path(Path::new(path)));
    let mut contexts = BTreeSet::new();
    for line in first_line..=last_line {
        if let Some(tests) = locations.get(&format!("{path}:{line}")) {
            contexts.extend(tests.iter().map(|test| format!("{test}|run")));
        }
    }
    contexts.into_iter().collect()
}

/// Apply the configured watchdog policy to selected tests' clean history.
#[must_use]
pub fn mutation_timeout(
    tests: &[String],
    durations: &BTreeMap<String, f64>,
    ceiling: f64,
    policy: MutationTimeoutPolicy,
) -> f64 {
    if policy == MutationTimeoutPolicy::Fixed {
        return ceiling;
    }
    let expected_ms: f64 = tests
        .iter()
        .map(|test| durations.get(test).copied().unwrap_or_default())
        .sum();
    if expected_ms <= 0.0 {
        ceiling
    } else {
        ceiling.min((expected_ms / 1000.0 * 10.0 + 0.5).max(1.0))
    }
}

/// Allow clean collection and baseline runs to outlast a mutant's watchdog.
///
/// # Errors
/// Rejects non-finite or non-positive mutation timeouts.
pub fn mutation_baseline_timeout(mutation_timeout: f64) -> Result<f64, &'static str> {
    const CLEAN_BASELINE_SECONDS: f64 = 300.0;
    if !mutation_timeout.is_finite() || mutation_timeout <= 0.0 {
        return Err("mutation timeout must be finite and positive");
    }
    Ok(mutation_timeout.max(CLEAN_BASELINE_SECONDS))
}
