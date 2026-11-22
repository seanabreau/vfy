use super::{ExecutionOutcome, worker_case, worker_result_json};
use crate::results::{
    NativeRunResult, merge_collect_only_parallel_results, merge_parallel_results,
};

fn observed_worker(nodeid: &str, execute: bool) -> String {
    let cases = if execute {
        vec![worker_case(nodeid, 1.0, ExecutionOutcome::Passed)]
    } else {
        Vec::new()
    };
    let mut document: serde_json::Value =
        serde_json::from_str(&worker_result_json(cases, 1)).unwrap();
    let record = &mut document["result"];
    record["engine"] = serde_json::json!("vfy-independent-v1");
    record["exitstatus"] = serde_json::json!(0);
    record["configuration"] = serde_json::json!({"ini_path": null, "ini_sha256": null,
        "values": {}, "strict_config": false, "strict_markers": false});
    record["case_markers"] = serde_json::json!([{"nodeid": nodeid, "names": []}]);
    record["plugins"] = serde_json::json!([]);
    record["execution_topology"] = serde_json::json!({"requested_workers": 1,
        "active_workers": 1, "worker_pids": [123]});
    document.to_string()
}

#[test]
fn explicit_collection_mode_preserves_ids_without_weakening_execute_omission_checks() {
    pyo3::Python::initialize();
    let nodeids = ["a.py::test_case".to_owned()];
    let worker = observed_worker(&nodeids[0], false);
    let rejected = merge_parallel_results(
        std::iter::once(Ok(worker.clone())),
        &[vec![0]],
        &nodeids,
        &[Some(0)],
        0,
        0,
        1.0,
    )
    .err()
    .expect("execute mode cannot omit assigned tests");
    assert!(
        rejected
            .to_string()
            .contains("omitted cases without maxfail")
    );
    let collected = merge_collect_only_parallel_results(
        std::iter::once(Ok(worker)),
        &[vec![0]],
        &nodeids,
        &[Some(0)],
        1.0,
    )
    .expect("explicit collect-only mode retains real collection");
    assert_eq!(collected.record().collected, 1);
    assert!(collected.record().cases.is_empty());
    assert_eq!(collected.record().counts.passed, 0);
    assert!(collected.fresh_evidence().is_none());
    assert!(crate::parallel::fresh_cache_fingerprints(&collected, "{}").is_err());
    let portable = NativeRunResult::from_independent_json(
        &collected
            .to_json()
            .expect("ordinary portable collection wire"),
    )
    .expect("ordinary collection transport remains usable");
    assert!(portable.fresh_evidence().is_none());
    assert!(crate::parallel::fresh_cache_fingerprints(&portable, "{}").is_err());
    assert_eq!(
        collected
            .record()
            .observations
            .as_ref()
            .unwrap()
            .case_markers[0]
            .nodeid,
        nodeids[0]
    );
}

#[test]
fn collection_mode_rejects_executed_cases_and_conflicting_collection_identity() {
    pyo3::Python::initialize();
    let nodeids = ["a.py::test_case".to_owned()];
    for (worker, expected) in [
        (
            observed_worker(&nodeids[0], true),
            "returned executed cases",
        ),
        (
            observed_worker("a.py::forged", false),
            "different collection identities",
        ),
    ] {
        let error = merge_collect_only_parallel_results(
            std::iter::once(Ok(worker)),
            &[vec![0]],
            &nodeids,
            &[Some(0)],
            1.0,
        )
        .err()
        .expect("invalid collection result must be rejected");
        assert!(error.to_string().contains(expected), "{error}");
    }
}

#[test]
fn worker_transport_rejects_coordinator_only_configuration() {
    pyo3::Python::initialize();
    let nodeids = ["a.py::test_case".to_owned()];
    for execute in [false, true] {
        let mut document: serde_json::Value =
            serde_json::from_str(&observed_worker(&nodeids[0], execute)).unwrap();
        document["result"]["coordinator_configuration"] =
            document["result"]["configuration"].clone();
        let worker = document.to_string();
        let result = if execute {
            merge_parallel_results(
                std::iter::once(Ok(worker)),
                &[vec![0]],
                &nodeids,
                &[Some(0)],
                0,
                0,
                1.0,
            )
        } else {
            merge_collect_only_parallel_results(
                std::iter::once(Ok(worker)),
                &[vec![0]],
                &nodeids,
                &[Some(0)],
                1.0,
            )
        };
        assert!(
            result
                .err()
                .expect("workers cannot impersonate a coordinator")
                .to_string()
                .contains("worker result contains coordinator configuration")
        );
    }
}
