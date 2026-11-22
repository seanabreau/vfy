use super::{
    MutationJobKind, MutationTestIsolation, MutationTimeoutPolicy, MutationWorkerLease,
    collected_mutation_worker_limit, mutation_portable_worker_limit, mutation_test_context_group,
    mutation_timeout, next_mutation_job, order_mutation_jobs, plan_mutation_test_groups,
};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

const AVAILABLE: MutationWorkerLease = MutationWorkerLease::Available;

#[test]
fn clean_baseline_budget_does_not_change_the_mutation_watchdog() {
    use super::mutation_baseline_timeout;

    for timeout in [0.01, 1.0, 30.0, 600.0] {
        let baseline = mutation_baseline_timeout(timeout).expect("positive finite timeout");
        assert!(baseline >= 300.0 && baseline >= timeout);
        assert!(
            (mutation_timeout(&[], &BTreeMap::new(), timeout, MutationTimeoutPolicy::Fixed)
                - timeout)
                .abs()
                < f64::EPSILON
        );
    }
    for invalid in [0.0, -1.0, f64::INFINITY, f64::NAN] {
        assert!(mutation_baseline_timeout(invalid).is_err());
    }
}

#[test]
fn watchdog_uses_history_without_exceeding_the_explicit_ceiling() {
    let durations = BTreeMap::from([("test".to_owned(), 250.0)]);
    for (test, ceiling, expected) in [
        ("test", 10.0, 3.0),
        ("test", 2.0, 2.0),
        ("unknown", 10.0, 10.0),
    ] {
        assert!(
            (mutation_timeout(
                &[test.to_owned()],
                &durations,
                ceiling,
                MutationTimeoutPolicy::Adaptive,
            ) - expected)
                .abs()
                < f64::EPSILON
        );
        assert!(
            (mutation_timeout(
                &[test.to_owned()],
                &durations,
                ceiling,
                MutationTimeoutPolicy::Fixed,
            ) - ceiling)
                .abs()
                < f64::EPSILON
        );
    }
}

#[test]
fn portable_mutation_jobs_are_ordered_first_stably() {
    assert_eq!(
        order_mutation_jobs(&[
            MutationJobKind::Collected,
            MutationJobKind::Portable,
            MutationJobKind::Collected,
            MutationJobKind::Portable,
        ]),
        vec![1, 3, 0, 2]
    );
    assert!(order_mutation_jobs(&[]).is_empty());
}

#[test]
fn portable_mutations_reserve_file_worker_capacity() {
    for (capacity, expected) in [(0, 0), (1, 0), (2, 1), (3, 2), (4, 2), (5, 3)] {
        assert_eq!(collected_mutation_worker_limit(capacity, 1), expected);
        assert_eq!(collected_mutation_worker_limit(capacity, 2), expected);
    }
    assert_eq!(collected_mutation_worker_limit(5, 0), 5);
}

#[test]
fn file_groups_bound_portable_supervisors_without_changing_serial_capacity() {
    for (capacity, file_limit) in [(0, 0), (1, 1), (2, 1), (3, 2), (4, 3), (10, 3), (1024, 3)] {
        assert_eq!(
            mutation_portable_worker_limit(capacity, MutationTestIsolation::Serial),
            capacity
        );
        assert_eq!(
            mutation_portable_worker_limit(capacity, MutationTestIsolation::File),
            file_limit
        );
    }
}

#[test]
fn collected_mutations_can_pass_a_waiting_portable_supervisor() {
    let pending = [MutationJobKind::Portable, MutationJobKind::Collected];
    let active = [MutationJobKind::Portable, MutationJobKind::Portable];
    assert_eq!(
        next_mutation_job(&pending, &active, 3, MutationTestIsolation::File, AVAILABLE),
        Some(1)
    );
    assert_eq!(
        next_mutation_job(
            &pending,
            &active,
            3,
            MutationTestIsolation::Serial,
            AVAILABLE
        ),
        Some(0)
    );
    assert_eq!(
        next_mutation_job(&pending, &active, 1, MutationTestIsolation::File, AVAILABLE),
        None
    );
    assert_eq!(
        next_mutation_job(&pending, &[], 0, MutationTestIsolation::File, AVAILABLE),
        None
    );
    assert_eq!(
        next_mutation_job(&[], &[], 2, MutationTestIsolation::File, AVAILABLE),
        None
    );
}

#[test]
fn file_scheduler_overlaps_portable_collection_without_filling_outer_capacity() {
    let pending = [MutationJobKind::Portable];
    for (capacity, admitted) in [(0, 0), (1, 1), (2, 1), (3, 2), (4, 3), (10, 3)] {
        let mut active = Vec::new();
        for _ in 0..admitted {
            assert_eq!(
                next_mutation_job(
                    &pending,
                    &active,
                    capacity,
                    MutationTestIsolation::File,
                    AVAILABLE
                ),
                Some(0),
                "capacity {capacity} must admit the next portable supervisor"
            );
            active.push(MutationJobKind::Portable);
        }
        assert_eq!(
            next_mutation_job(
                &pending,
                &active,
                capacity,
                MutationTestIsolation::File,
                AVAILABLE
            ),
            None,
            "capacity {capacity} must bound retained portable supervisors"
        );
        if capacity >= 2 {
            assert_eq!(
                next_mutation_job(
                    &[MutationJobKind::Portable, MutationJobKind::Collected],
                    &active,
                    capacity,
                    MutationTestIsolation::File,
                    AVAILABLE,
                ),
                Some(1),
                "capacity {capacity} must leave an outer position for collected work"
            );
        }
    }
}

#[test]
fn file_groups_retain_reserved_slots_after_collecting_jobs_are_launched() {
    let pending = [MutationJobKind::Portable, MutationJobKind::Collected];
    let active = [
        MutationJobKind::Portable,
        MutationJobKind::Portable,
        MutationJobKind::Portable,
        MutationJobKind::Collected,
        MutationJobKind::Collected,
    ];
    assert_eq!(
        next_mutation_job(&pending, &active, 5, MutationTestIsolation::File, AVAILABLE),
        None
    );
    assert_eq!(
        next_mutation_job(&pending, &active, 6, MutationTestIsolation::File, AVAILABLE),
        Some(1)
    );
    let mut full = active.to_vec();
    full.push(MutationJobKind::Collected);
    assert_eq!(
        next_mutation_job(&pending, &full, 6, MutationTestIsolation::File, AVAILABLE),
        None
    );
    assert_eq!(
        next_mutation_job(&pending, &full, 7, MutationTestIsolation::File, AVAILABLE),
        Some(1)
    );
    full.extend([MutationJobKind::Collected; 3]);
    assert_eq!(
        next_mutation_job(&pending, &full, 10, MutationTestIsolation::File, AVAILABLE),
        None
    );
}

#[test]
fn exhausted_worker_leases_admit_collected_jobs_but_no_new_supervisor() {
    let exhausted = MutationWorkerLease::Exhausted;
    for capacity in [1, 2, 3, 4, 10] {
        let pending = [MutationJobKind::Portable, MutationJobKind::Collected];
        assert_eq!(
            next_mutation_job(
                &pending,
                &[],
                capacity,
                MutationTestIsolation::File,
                exhausted
            ),
            Some(1),
            "capacity {capacity} must pass a supervisor that cannot lease a worker"
        );
        assert_eq!(
            next_mutation_job(
                &[MutationJobKind::Portable],
                &[],
                capacity,
                MutationTestIsolation::File,
                exhausted,
            ),
            None,
            "capacity {capacity} must not start collection without a worker lease"
        );
        assert_eq!(
            next_mutation_job(
                &pending,
                &[],
                capacity,
                MutationTestIsolation::Serial,
                exhausted
            ),
            Some(0),
            "serial isolation has no shared worker leases"
        );
    }
}

/// One fresh supervisor in a model of the shared file-worker lease pool.
struct ModelSupervisor {
    collecting: bool,
    remaining_groups: usize,
    running_groups: usize,
}

/// Leased processes and whether a collection overlapped test execution.
struct LeaseModel {
    capacity: usize,
    free: usize,
    supervisors: Vec<ModelSupervisor>,
    collected: usize,
    overlapped: bool,
}

impl LeaseModel {
    fn active(&self) -> Vec<MutationJobKind> {
        let mut active = vec![MutationJobKind::Portable; self.supervisors.len()];
        active.extend(vec![MutationJobKind::Collected; self.collected]);
        active
    }

    /// Admit jobs as the controller does: a supervisor and a collected job each
    /// take one free lease before they start.
    fn admit(&mut self, pending: &mut Vec<MutationJobKind>, groups: usize) {
        while !pending.is_empty() {
            let mut index = next_mutation_job(
                pending,
                &self.active(),
                self.capacity,
                MutationTestIsolation::File,
                AVAILABLE,
            );
            if index.is_some_and(|index| pending[index] == MutationJobKind::Portable)
                && self.free == 0
            {
                index = next_mutation_job(
                    pending,
                    &self.active(),
                    self.capacity,
                    MutationTestIsolation::File,
                    MutationWorkerLease::Exhausted,
                );
            }
            let Some(index) = index else { break };
            if self.free == 0 {
                break;
            }
            self.free -= 1;
            match pending.remove(index) {
                MutationJobKind::Portable => self.supervisors.push(ModelSupervisor {
                    collecting: true,
                    remaining_groups: groups,
                    running_groups: 0,
                }),
                MutationJobKind::Collected => self.collected += 1,
            }
        }
    }

    fn assert_within_budget(&mut self) {
        let collecting = self
            .supervisors
            .iter()
            .filter(|supervisor| supervisor.collecting)
            .count();
        let executing = self.collected
            + self
                .supervisors
                .iter()
                .map(|supervisor| supervisor.running_groups)
                .sum::<usize>();
        assert!(
            collecting + executing <= self.capacity,
            "capacity {}: {collecting} collecting and {executing} executing processes",
            self.capacity
        );
        assert_eq!(collecting + executing + self.free, self.capacity);
        self.overlapped |= collecting > 0 && executing > 0;
    }

    /// Finish collections and running work, then start further file groups.
    fn advance(&mut self) {
        self.free += std::mem::take(&mut self.collected);
        for supervisor in &mut self.supervisors {
            if supervisor.collecting {
                // The collection lease becomes the first file group's lease.
                supervisor.collecting = false;
                supervisor.running_groups = 1;
                supervisor.remaining_groups -= 1;
            } else {
                self.free += std::mem::take(&mut supervisor.running_groups);
            }
        }
        self.supervisors.retain(|supervisor| {
            supervisor.collecting
                || supervisor.running_groups > 0
                || supervisor.remaining_groups > 0
        });
        for supervisor in &mut self.supervisors {
            while self.free > 0 && supervisor.remaining_groups > 0 && !supervisor.collecting {
                self.free -= 1;
                supervisor.running_groups += 1;
                supervisor.remaining_groups -= 1;
            }
        }
    }
}

#[test]
fn collecting_supervisors_and_executing_tests_share_the_worker_budget() {
    for capacity in [1, 2, 3, 4, 10] {
        let mut pending = [MutationJobKind::Portable; 6].to_vec();
        pending.extend([MutationJobKind::Collected; 6]);
        let mut model = LeaseModel {
            capacity,
            free: capacity,
            supervisors: Vec::new(),
            collected: 0,
            overlapped: false,
        };
        let mut steps = 0;
        while !pending.is_empty() || !model.supervisors.is_empty() || model.collected > 0 {
            model.admit(&mut pending, 3);
            model.assert_within_budget();
            model.advance();
            model.assert_within_budget();
            steps += 1;
            assert!(steps < 100, "capacity {capacity} must keep making progress");
        }
        assert_eq!(model.free, capacity);
        assert_eq!(
            model.overlapped,
            capacity > 1,
            "capacity {capacity} overlaps collection with execution only within its budget"
        );
    }
}

#[test]
fn mutation_test_groups_partition_items_and_prefer_files_stably() {
    let nodeids = [
        "tests/test_alpha.py::test_first".to_owned(),
        "tests/test_beta.py::test_second".to_owned(),
        "tests/test_alpha.py::TestGroup::test_third".to_owned(),
        "tests/test_gamma.py::test_fourth".to_owned(),
        "tests/test_beta.py::test_fifth".to_owned(),
    ];
    let preferred = [
        "tests/missing.py::test_unknown".to_owned(),
        "tests/test_gamma.py::test_fourth".to_owned(),
        "tests/test_alpha.py::TestGroup::test_third".to_owned(),
        "tests/test_gamma.py::test_fourth".to_owned(),
        "tests/test_beta.py::test_not_collected".to_owned(),
    ];

    let groups = plan_mutation_test_groups(&nodeids, &preferred);
    assert_eq!(groups, vec![vec![3], vec![0, 2], vec![1, 4]]);
    let mut partition: Vec<_> = groups.into_iter().flatten().collect();
    partition.sort_unstable();
    assert_eq!(partition, (0..nodeids.len()).collect::<Vec<_>>());
}

#[test]
fn mutation_test_groups_prioritize_larger_files_and_keep_ties_stable() {
    let nodeids = [
        "tests/test_alpha.py::test_first".to_owned(),
        "tests/test_beta.py::test_second".to_owned(),
        "tests/test_beta.py::test_third".to_owned(),
        "tests/test_gamma.py::test_fourth".to_owned(),
        "tests/test_gamma.py::test_fifth".to_owned(),
    ];

    assert_eq!(
        plan_mutation_test_groups(&nodeids, &["tests/test_beta.py::not_collected".to_owned()]),
        vec![vec![1, 2], vec![3, 4], vec![0]]
    );
    assert!(plan_mutation_test_groups(&[], &[]).is_empty());
}

#[test]
fn owner_context_preferences_union_only_lines_in_the_source_range() {
    let path = "src/module.py";
    let source = "def descend_b(n=1):\n    return n - 1\n\ndef other():\n    pass\n";
    let normalized = crate::glob_path(&crate::absolute_path(Path::new(path)));
    let locations = BTreeMap::from([
        (
            format!("{normalized}:1"),
            BTreeSet::from(["tests/test_defaults.py::test_default".to_owned()]),
        ),
        (
            format!("{normalized}:2"),
            BTreeSet::from(["tests/test_body.py::test_body".to_owned()]),
        ),
        (
            format!("{normalized}:5"),
            BTreeSet::from(["tests/test_other.py::test_other".to_owned()]),
        ),
    ]);
    let end = u32::try_from("def descend_b(n=1):\n    return n - 1\n".len())
        .expect("test source length fits in u32");

    assert_eq!(
        mutation_test_context_group(path, source, 0, end, &locations),
        vec![
            "tests/test_body.py::test_body|run".to_owned(),
            "tests/test_defaults.py::test_default|run".to_owned(),
        ]
    );
}
