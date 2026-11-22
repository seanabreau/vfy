use super::{
    CaseWeights, MAX_NODEID_BYTES, ParallelPlan, ParallelScheduling, ProgressRecord, ScopeGrouping,
    WorkerResultBundle, is_missing_process_group, partition_by_cases, partition_by_scope,
    partition_by_stripes, validate_plan, validate_plan_size, write_json_atomic,
};
use crate::results::{merge_parallel_results, native_run_result};
use std::collections::{BTreeMap, BTreeSet};
use vfy_core::test_execution::{
    CaseResult, Counts, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, RunRecord,
};

#[test]
fn progress_transport_preserves_fractional_durations_and_authoritative_report_history() {
    for builtin_child in [false, true] {
        let mut document = serde_json::json!({
            "nodeid": "test_progress.py::test_body",
            "duration_ms": 1.875,
            "phases": [
                {"outcome":"passed","phase":"setup","duration_ms":0.125,
                 "stdout":"","stderr":"","log":"","detail":""},
                {"outcome":"passed","phase":"call","duration_ms":1.5,
                 "stdout":"parent output\n","stderr":"","log":"","detail":""},
                {"outcome":"passed","phase":"teardown","duration_ms":0.25,
                 "stdout":"","stderr":"","log":"","detail":""}
            ],
            "subtests": [],
            "report_events": [
                {"kind":"phase","index":0},
                {"kind":"phase","index":1},
                {"kind":"phase","index":2}
            ]
        });
        if builtin_child {
            document["subtests"] = serde_json::json!([{
                "outcome":"failed", "context":"[child]", "exception":null,
                "detail":"child failure",
                "report": {
                    "context":{"msg":"child","kwargs":[["position","1"]]},
                    "duration_ms":0.375,"start":10.25,"stop":10.250_375,
                    "stdout":"child output\n","stderr":"","log":"",
                    "report_sections":null
                }
            }]);
            document["report_events"] = serde_json::json!([
                {"kind":"phase","index":0},
                {"kind":"subtest","index":0},
                {"kind":"phase","index":1},
                {"kind":"phase","index":2}
            ]);
        }
        let case: CaseResult = serde_json::from_value(document).expect("valid progress case");
        let expected = serde_json::to_vec(&ProgressRecord { case: case.clone() })
            .expect("transparent progress record wire");
        let wire = serde_json::to_vec(&case).expect("borrowed worker progress serialization");
        assert_eq!(
            wire, expected,
            "progress retains the existing case wire shape"
        );
        let restored: ProgressRecord =
            serde_json::from_slice(&wire).expect("supervisor reads fractional progress durations");
        let restored = restored.into_case();
        assert_eq!(restored.duration_ms.to_bits(), 1.875_f64.to_bits());
        assert_eq!(
            restored.phases[0].duration_ms.to_bits(),
            0.125_f64.to_bits()
        );
        assert_eq!(restored.subtests().len(), usize::from(builtin_child));
        assert_eq!(
            serde_json::to_vec(&restored).expect("restored progress case wire"),
            expected,
            "child metadata, capture, and chronological references survive transport"
        );
    }
}

#[test]
fn failed_worker_serialization_preserves_the_previous_result_and_removes_staging() {
    struct InvalidBundle;

    impl serde::Serialize for InvalidBundle {
        fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            use serde::ser::{Error, SerializeMap};

            let mut bundle = serializer.serialize_map(Some(2))?;
            bundle.serialize_entry("result", &"capture".repeat(16_384))?;
            Err(S::Error::custom("worker result serialization rejected"))
        }
    }

    let directory = tempfile::tempdir().expect("worker result directory");
    let result = directory.path().join("result.0.json");
    let previous = b"previous worker result\n";
    std::fs::write(&result, previous).expect("previous result is staged");

    let error = write_json_atomic(&result, &InvalidBundle).expect_err("invalid bundle rejected");

    assert!(
        error
            .to_string()
            .contains("worker result serialization rejected")
    );
    assert_eq!(
        std::fs::read(&result).expect("previous result survives"),
        previous
    );
    assert_eq!(
        std::fs::read_dir(directory.path())
            .expect("staging directory readable")
            .count(),
        1,
        "failed worker serialization leaves no partial staging file"
    );
}

fn partition_by_file(nodeids: &[String], workers: usize) -> Vec<Vec<usize>> {
    partition_by_scope(
        nodeids,
        workers,
        ScopeGrouping::File,
        &CaseWeights::uniform(nodeids.len()),
    )
}

#[test]
fn file_partition_keeps_repeated_files_together_and_collection_indices_ordered() {
    let nodeids = [
        "a/test_one.py::test_first",
        "b/test_two.py::test_second",
        "a/test_one.py::test_third",
        "c/test_three.py::test_fourth",
        "b/test_two.py::test_fifth",
    ]
    .map(str::to_owned);

    let assignments = partition_by_file(&nodeids, 2);

    assert_eq!(assignments.len(), 2);
    assert_eq!(assignments[0], [0, 2, 3]);
    assert_eq!(assignments[1], [1, 4]);
    for assignment in &assignments {
        assert!(assignment.windows(2).all(|pair| pair[0] < pair[1]));
    }
    let assigned = assignments
        .iter()
        .flatten()
        .copied()
        .collect::<BTreeSet<_>>();
    assert_eq!(assigned, BTreeSet::from([0, 1, 2, 3, 4]));
    for nodeid in [&nodeids[0], &nodeids[1]] {
        let file = nodeid
            .split_once("::")
            .expect("node ID fixture has a file separator")
            .0;
        let owners = assignments
            .iter()
            .enumerate()
            .filter(|(_, indices)| {
                indices
                    .iter()
                    .any(|index| nodeids[*index].starts_with(file))
            })
            .map(|(worker, _)| worker)
            .collect::<Vec<_>>();
        assert_eq!(owners.len(), 1);
    }
}

#[test]
fn file_partition_caps_workers_at_the_number_of_distinct_files() {
    let nodeids = ["test_one.py::test_first", "test_one.py::test_second"].map(str::to_owned);

    assert_eq!(partition_by_file(&nodeids, 8), vec![vec![0, 1]]);
    assert_eq!(partition_by_file(&[], 4), vec![Vec::<usize>::new()]);
}

#[test]
fn file_partition_balances_late_large_files_without_reordering_worker_cases() {
    let nodeids = [
        ("a.py", 2),
        ("b.py", 4),
        ("c.py", 1),
        ("d.py", 8),
        ("e.py", 3),
    ]
    .into_iter()
    .flat_map(|(file, cases)| (0..cases).map(move |case| format!("{file}::test_case_{case}")))
    .collect::<Vec<_>>();

    let assignments = partition_by_file(&nodeids, 2);

    assert_eq!(
        assignments,
        vec![(6..15).collect::<Vec<_>>(), (0..6).chain(15..18).collect()]
    );
    assert!(
        assignments
            .iter()
            .all(|assignment| assignment.windows(2).all(|pair| pair[0] < pair[1]))
    );
    let plan = ParallelPlan {
        scheduling: ParallelScheduling::File,
        nodeids,
        assignments,
        maxfail: 0,
        initial_failures: 0,
    };
    assert!(validate_plan(&plan).is_ok());
}

fn captured_three_phase_record(nodeid: &str, capture: &str) -> RunRecord {
    let phases: Vec<_> = [
        ExecutionPhase::Setup,
        ExecutionPhase::Call,
        ExecutionPhase::Teardown,
    ]
    .into_iter()
    .map(|phase| {
        PhaseResult::new(
            ExecutionOutcome::Passed,
            phase,
            0.5,
            PhasePayload {
                stdout: capture.to_owned(),
                ..PhasePayload::default()
            },
        )
    })
    .collect();
    RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 1,
        counts: Counts {
            passed: 1,
            ..Counts::default()
        },
        duration_ms: 1.5,
        warnings: Vec::new(),
        cases: vec![CaseResult::new(nodeid.to_owned(), 1.5, phases)],
    }
}

#[test]
fn nested_worker_transport_preserves_capture_and_locations_and_rejects_corruption() {
    let nodeid = "test_one.py::test_quotes".to_owned();
    let capture = "\"quoted\" \\ unicode λ\n".to_owned();
    let record = captured_three_phase_record(&nodeid, &capture);
    let result = native_run_result(
        record,
        Some(vec![(
            nodeid.clone(),
            ("test_one.py".to_owned(), Some(4), "test_quotes".to_owned()),
        )]),
    );
    let properties = serde_json::json!({"global_properties": [
        ["ordinary", "first"],
        ["second", "value"]
    ]});
    let bundle = WorkerResultBundle {
        result: result.transport(),
        junit_properties: serde_json::from_value(properties.clone())
            .expect("worker suite properties decode"),
        metadata: Vec::new(),
        collection_warnings: Vec::new(),
        finish_warnings: Vec::new(),
    };
    let json = serde_json::to_string(&bundle).expect("worker bundle serializes");
    let merged = merge_parallel_results(
        vec![json.clone()].into_iter().map(Ok),
        &[vec![0]],
        std::slice::from_ref(&nodeid),
        &[Some(0)],
        0,
        0,
        2.0,
    )
    .expect("nested worker result validates");
    assert_eq!(
        serde_json::to_value(merged.frozen_junit_properties()).unwrap(),
        properties
    );
    let merged: serde_json::Value =
        serde_json::from_str(&merged.transport_json().unwrap()).unwrap();
    assert_eq!(merged["cases"][0]["phases"][1]["stdout"], capture);
    assert_eq!(
        merged["report_locations"][0][1],
        serde_json::json!(["test_one.py", 4, "test_quotes"])
    );

    for corruption in ["counts", "locations", "type"] {
        let mut damaged: serde_json::Value = serde_json::from_str(&json).unwrap();
        match corruption {
            "counts" => damaged["result"]["counts"]["passed"] = serde_json::json!(2),
            "locations" => damaged["result"]["report_locations"][0][0] = serde_json::json!("wrong"),
            "type" => damaged["result"] = serde_json::json!("not a record"),
            _ => panic!("unknown test corruption"),
        }
        assert!(
            merge_parallel_results(
                vec![damaged.to_string()].into_iter().map(Ok),
                &[vec![0]],
                std::slice::from_ref(&nodeid),
                &[Some(0)],
                0,
                0,
                2.0,
            )
            .is_err(),
            "worker accepted {corruption} corruption"
        );
    }
}

#[test]
fn file_partition_sorts_indices_after_reuniting_interleaved_files() {
    let nodeids = [
        "a.py::test_one",
        "b.py::test_two",
        "b.py::test_three",
        "c.py::test_four",
        "d.py::test_five",
        "a.py::test_five",
    ]
    .map(str::to_owned);

    let assignments = partition_by_file(&nodeids, 2);

    assert_eq!(assignments, vec![vec![0, 3, 5], vec![1, 2, 4]]);
    assert!(
        assignments
            .iter()
            .all(|assignment| assignment.windows(2).all(|pair| pair[0] < pair[1]))
    );
}

#[test]
fn worker_plan_rejects_split_files_duplicate_indices_and_missing_indices() {
    let split_file = ParallelPlan {
        scheduling: ParallelScheduling::File,
        nodeids: ["a.py::test_one", "a.py::test_two"]
            .map(str::to_owned)
            .to_vec(),
        assignments: vec![vec![0], vec![1]],
        maxfail: 0,
        initial_failures: 0,
    };
    assert!(validate_plan(&split_file).is_err());

    let duplicate_index = ParallelPlan {
        scheduling: ParallelScheduling::File,
        nodeids: ["a.py::test_one", "b.py::test_two"]
            .map(str::to_owned)
            .to_vec(),
        assignments: vec![vec![0], vec![0, 1]],
        maxfail: 0,
        initial_failures: 0,
    };
    assert!(validate_plan(&duplicate_index).is_err());

    let missing_index = ParallelPlan {
        scheduling: ParallelScheduling::File,
        nodeids: ["a.py::test_one", "b.py::test_two"]
            .map(str::to_owned)
            .to_vec(),
        assignments: vec![vec![0], vec![]],
        maxfail: 0,
        initial_failures: 0,
    };
    assert!(validate_plan(&missing_index).is_err());
}

#[test]
fn worker_plan_rejects_oversized_serialized_manifests_before_writing_them() {
    let nodeid = "\0".repeat(MAX_NODEID_BYTES);
    let nodeids = vec![nodeid; 350];
    let assignments = vec![(0..nodeids.len()).collect()];
    let plan = ParallelPlan {
        scheduling: ParallelScheduling::File,
        nodeids,
        assignments,
        maxfail: 0,
        initial_failures: 0,
    };

    assert!(
        validate_plan_size(&plan)
            .expect_err("control characters expand beyond the plan size bound")
            .contains("supported manifest size")
    );
}

#[test]
fn process_group_cleanup_ignores_only_esrch_for_groups_without_members() {
    assert!(is_missing_process_group(rustix::io::Errno::SRCH));
    assert!(!is_missing_process_group(rustix::io::Errno::PERM));
    assert!(!is_missing_process_group(rustix::io::Errno::NOENT));
}

#[test]
fn case_partition_balances_one_large_file_without_changing_file_default() {
    let nodeids = (0..17)
        .map(|i| format!("a.py::test_case[{i}]"))
        .collect::<Vec<_>>();
    assert_eq!(partition_by_file(&nodeids, 4).len(), 1);
    let assignments = partition_by_cases(nodeids.len(), 4);
    assert_eq!(
        assignments.iter().map(Vec::len).collect::<Vec<_>>(),
        [5, 4, 4, 4]
    );
    let mut plan = ParallelPlan {
        scheduling: ParallelScheduling::Cases,
        nodeids,
        assignments,
        maxfail: 0,
        initial_failures: 0,
    };
    assert!(validate_plan(&plan).is_ok());
    plan.assignments[1][0] = 0;
    assert!(validate_plan(&plan).is_err());
    assert_eq!(partition_by_cases(0, 4), vec![Vec::<usize>::new()]);
    assert_eq!(partition_by_cases(2, 8), vec![vec![0], vec![1]]);
}

#[test]
fn case_plan_still_rejects_omissions_reordering_and_out_of_range_indices() {
    for assignments in [
        vec![vec![0], vec![2]],
        vec![vec![1, 0], vec![2]],
        vec![vec![0, 1], vec![3]],
    ] {
        let plan = ParallelPlan {
            scheduling: ParallelScheduling::Cases,
            nodeids: (0..3).map(|i| format!("a.py::test_case[{i}]")).collect(),
            assignments,
            maxfail: 0,
            initial_failures: 0,
        };
        assert!(validate_plan(&plan).is_err());
    }
}

#[test]
fn striped_plans_cover_collection_once_in_each_workers_order() {
    for cases in [0, 1, 2, 17, 62_435] {
        for workers in [1, 2, 4, 256] {
            let assignments = partition_by_stripes(cases, workers);
            let loads = assignments.iter().map(Vec::len).collect::<Vec<_>>();
            assert_eq!(assignments.len(), workers.min(cases.max(1)));
            assert!(loads.iter().max().unwrap() - loads.iter().min().unwrap() <= 1);
            let plan = ParallelPlan {
                scheduling: ParallelScheduling::Striped,
                nodeids: (0..cases)
                    .map(|i| format!("a.py::test_case[{i}]"))
                    .collect(),
                assignments,
                maxfail: 0,
                initial_failures: 0,
            };
            assert!(validate_plan(&plan).is_ok());
        }
    }
}

#[test]
fn case_partition_assigns_every_case_once_with_bounded_load_difference() {
    for cases in [0, 1, 2, 17, 62_435] {
        for workers in [1, 2, 4, 256] {
            let assignments = partition_by_cases(cases, workers);
            let loads = assignments.iter().map(Vec::len).collect::<Vec<_>>();
            assert_eq!(assignments.len(), workers.min(cases.max(1)));
            assert_eq!(
                assignments.iter().flatten().copied().collect::<Vec<_>>(),
                (0..cases).collect::<Vec<_>>()
            );
            let minimum = loads.iter().min().expect("plan has at least one worker");
            let maximum = loads.iter().max().expect("plan has at least one worker");
            assert!(maximum - minimum <= 1);
        }
    }
}

#[test]
fn fork_child_fallback_filters_parent_and_tolerates_disappeared_processes() {
    let proc_root = tempfile::tempdir().expect("synthetic proc directory");
    for (pid, status) in [(101, "Name:\tchild\nPPid:\t42\n"), (102, "PPid:\t7\n")] {
        let directory = proc_root.path().join(pid.to_string());
        std::fs::create_dir(&directory).expect("process directory");
        std::fs::write(directory.join("status"), status).expect("process status");
    }
    std::fs::create_dir(proc_root.path().join("103")).expect("disappeared process directory");
    std::fs::create_dir(proc_root.path().join("self")).expect("nonnumeric proc entry");
    let children = super::read_fork_children(proc_root.path(), 42)
        .expect("fallback scans surviving process statuses");
    assert_eq!(children, [101]);
}

#[test]
#[cfg(target_os = "linux")]
fn fork_child_fallback_tolerates_a_reaped_process_with_a_retained_proc_inode() {
    use std::os::fd::AsRawFd as _;
    use std::os::unix::fs::symlink;
    use std::process::{Command, Stdio};

    let mut child = Command::new("cat")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .expect("child remains alive until its input closes");
    let status = std::fs::File::open(format!("/proc/{}/status", child.id()))
        .expect("retain the live process status inode");
    drop(child.stdin.take());
    child.wait().expect("reap the exited process");

    let proc_root = tempfile::tempdir().expect("synthetic proc directory");
    for pid in [101, 102] {
        std::fs::create_dir(proc_root.path().join(pid.to_string())).expect("process directory");
    }
    std::fs::write(proc_root.path().join("101/status"), "PPid:\t42\n")
        .expect("surviving child status");
    let disappeared = proc_root.path().join("102/status");
    symlink(
        format!("/proc/self/fd/{}", status.as_raw_fd()),
        &disappeared,
    )
    .expect("pin the disappeared process status path");
    assert_eq!(
        std::fs::read_to_string(&disappeared)
            .expect_err("the retained proc inode has no live process")
            .raw_os_error(),
        Some(rustix::io::Errno::SRCH.raw_os_error()),
    );
    assert_eq!(
        super::read_fork_children(proc_root.path(), 42)
            .expect("an unrelated process exiting does not reject fork admission"),
        [101],
    );
}

#[test]
fn fork_child_fast_path_does_not_scan_unrelated_status_files() {
    let proc_root = tempfile::tempdir().expect("synthetic proc directory");
    let task = proc_root.path().join("self/task/42");
    std::fs::create_dir_all(&task).expect("task directory");
    std::fs::write(task.join("children"), "301 302\n").expect("kernel child inventory");
    std::fs::create_dir(proc_root.path().join("999")).expect("unrelated process directory");
    std::fs::write(proc_root.path().join("999/status"), "invalid\n")
        .expect("unrelated malformed fixture");
    assert_eq!(
        super::read_fork_children(proc_root.path(), 42).expect("task child inventory preferred"),
        [301, 302]
    );
}

#[test]
fn fork_child_fallback_rejects_invalid_or_unreadable_status() {
    for status in [Some("Name:\tchild\n"), Some("PPid:\tinvalid\n"), None] {
        let proc_root = tempfile::tempdir().expect("synthetic proc directory");
        let directory = proc_root.path().join("101");
        std::fs::create_dir(&directory).expect("process directory");
        if let Some(status) = status {
            std::fs::write(directory.join("status"), status).expect("invalid status fixture");
        } else {
            std::fs::create_dir(directory.join("status")).expect("unreadable status fixture");
        }
        assert!(super::read_fork_children(proc_root.path(), 42).is_err());
    }
}

#[test]
fn scope_keys_group_class_methods_and_module_functions() {
    let key = |nodeid| ScopeGrouping::Class.key(nodeid);
    assert_eq!(key("tests/test_a.py::test_function"), "tests/test_a.py");
    assert_eq!(
        key("tests/test_a.py::test_function[x::y]"),
        "tests/test_a.py"
    );
    assert_eq!(
        key("tests/test_a.py::test_function[a[b]]"),
        "tests/test_a.py"
    );
    assert_eq!(
        key("tests/test_a.py::TestCase::test_method"),
        "tests/test_a.py::TestCase"
    );
    assert_eq!(
        key("tests/test_a.py::Outer::Inner::test_method[1]"),
        "tests/test_a.py::Outer::Inner"
    );
    assert_eq!(
        key("tests/[v2]/test_a.py::TestCase::test_method[x]"),
        "tests/[v2]/test_a.py::TestCase"
    );
    assert_eq!(
        key("tests/[v2]/test_a.py::test_function"),
        "tests/[v2]/test_a.py"
    );
    assert_eq!(key("README.txt"), "README.txt");
    assert_eq!(
        ScopeGrouping::File.key("tests/test_a.py::TestCase::test_method"),
        "tests/test_a.py"
    );
}

#[test]
fn scope_partition_splits_one_file_by_class_and_keeps_each_class_whole() {
    let nodeids = [
        "test_unit.py::TestFirst::test_a",
        "test_unit.py::TestFirst::test_b",
        "test_unit.py::TestSecond::test_a",
        "test_unit.py::TestSecond::test_b",
        "test_unit.py::test_function[1]",
        "test_unit.py::test_function[2]",
    ]
    .map(str::to_owned);

    assert_eq!(
        partition_by_file(&nodeids, 3),
        vec![(0..6).collect::<Vec<_>>()]
    );
    let assignments = partition_by_scope(
        &nodeids,
        3,
        ScopeGrouping::Class,
        &CaseWeights::uniform(nodeids.len()),
    );

    assert_eq!(assignments, vec![vec![0, 1], vec![2, 3], vec![4, 5]]);
    let plan = ParallelPlan {
        scheduling: ParallelScheduling::Scope,
        nodeids: nodeids.to_vec(),
        assignments,
        maxfail: 0,
        initial_failures: 0,
    };
    assert!(validate_plan(&plan).is_ok());
    let split = ParallelPlan {
        assignments: vec![vec![0, 2, 3], vec![1, 4, 5]],
        ..plan
    };
    assert_eq!(
        validate_plan(&split),
        Err(
            "native parallel plan splits scope \"test_unit.py::TestFirst\" across workers"
                .to_owned()
        )
    );
}

#[test]
fn recorded_durations_balance_groups_and_fill_unrecorded_cases_with_the_median() {
    let nodeids = [
        "test_slow.py::test_slow",
        "test_fast.py::test_one",
        "test_fast.py::test_two",
        "test_fast.py::test_three",
        "test_new.py::test_unrecorded",
    ]
    .map(str::to_owned);
    let durations = BTreeMap::from([
        ("test_slow.py::test_slow".to_owned(), 900.0),
        ("test_fast.py::test_one".to_owned(), 1.0),
        ("test_fast.py::test_two".to_owned(), 2.0),
        ("test_fast.py::test_three".to_owned(), 3.0),
        ("removed.py::test_gone".to_owned(), 5_000.0),
    ]);

    let weights = CaseWeights::from_history(&nodeids, &durations);
    // The median of this plan's recorded cases is 3 ms; stale entries do not count.
    assert_eq!(weights.0, vec![900.1, 1.1, 2.1, 3.1, 3.1]);
    // Case counts would pair the slow file with another group.
    assert_eq!(
        partition_by_file(&nodeids, 2),
        vec![vec![1, 2, 3], vec![0, 4]]
    );
    assert_eq!(
        partition_by_scope(&nodeids, 2, ScopeGrouping::File, &weights),
        vec![vec![0], vec![1, 2, 3, 4]]
    );
    assert_eq!(
        CaseWeights::from_history(&nodeids, &BTreeMap::new()).0,
        vec![1.0; nodeids.len()]
    );
}

#[test]
fn zero_duration_groups_still_spread_across_workers() {
    let nodeids = ["a.py::test", "b.py::test", "c.py::test"].map(str::to_owned);
    let durations = nodeids
        .iter()
        .map(|nodeid| (nodeid.clone(), 0.0))
        .collect::<BTreeMap<_, _>>();

    let weights = CaseWeights::from_history(&nodeids, &durations);

    assert_eq!(
        partition_by_scope(&nodeids, 3, ScopeGrouping::File, &weights),
        vec![vec![0], vec![1], vec![2]]
    );
}
