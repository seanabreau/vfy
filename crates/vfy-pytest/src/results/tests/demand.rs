use super::{ExecutionOutcome, sample_record, worker_case, worker_result_json};
use crate::results::{NativeRunResult, merge_demand_parallel_results, native_run_result};
use vfy_core::test_execution::compute_run_counts;
use vfy_core::test_plan::TestId;

fn global_worker(nodeids: &[&str], executed: &[usize]) -> NativeRunResult {
    let mut record = sample_record();
    record.engine = "vfy-independent-v1".to_owned();
    record.collected = nodeids.len();
    record.exitstatus = Some(0);
    record.cases = executed
        .iter()
        .map(|index| {
            let mut case = worker_case(nodeids[*index], 1.0, ExecutionOutcome::Passed);
            case.set_test_id(TestId::from_index(*index));
            case
        })
        .collect();
    record.counts = compute_run_counts(&record.cases, &record.collection_reports);
    record.observations = Some(
        serde_json::from_value(serde_json::json!({
            "configuration": {"ini_path": null, "ini_sha256": null, "values": {},
                "strict_config": false, "strict_markers": false},
            "case_markers": nodeids.iter().enumerate().map(|(index, nodeid)| {
                serde_json::json!({"nodeid": nodeid, "names": [format!("marker-{index}")]})
            }).collect::<Vec<_>>(),
            "plugins": [], "execution_topology": {"requested_workers": 2,
                "active_workers": 1, "worker_pids": [123]}
        }))
        .expect("full independent collection observations"),
    );
    let locations = record
        .cases
        .iter()
        .map(|case| {
            (
                case.nodeid.clone(),
                (
                    "actual/source.py".to_owned(),
                    Some(37),
                    "original".to_owned(),
                ),
            )
        })
        .collect();
    native_run_result(record, Some(locations))
}

#[test]
fn independent_claim_projection_retains_global_ids_and_only_owned_marker_rows() {
    let mut result = global_worker(&["same", "one", "two", "three", "same"], &[0, 2]);
    result
        .project_independent_claims(&[0, 2, 4])
        .expect("owned lookahead marker remains without inventing an executed case");
    assert_eq!(result.record().collected, 3);
    assert_eq!(result.record().cases.len(), 2);
    assert_eq!(result.record().counts.passed, 2);
    assert_eq!(
        result
            .record()
            .cases
            .iter()
            .map(vfy_core::test_execution::CaseResult::test_id)
            .collect::<Vec<_>>(),
        vec![Some(TestId::from_index(0)), Some(TestId::from_index(2))]
    );
    let markers = &result.record().observations.as_ref().unwrap().case_markers;
    assert_eq!(
        markers
            .iter()
            .map(|marker| marker.names[0].as_str())
            .collect::<Vec<_>>(),
        ["marker-0", "marker-2", "marker-4"]
    );
    assert_eq!(markers[0].nodeid, markers[2].nodeid);
    assert_eq!(result.report_location(1).unwrap().0, "actual/source.py");
}

#[test]
fn independent_claim_projection_rejects_unowned_duplicate_or_misordered_claims_atomically() {
    pyo3::Python::initialize();
    for claimed in [vec![0, 0], vec![0, 5], vec![2, 0], vec![4, 2]] {
        let mut result = global_worker(&["same", "one", "two", "three", "same"], &[0, 2]);
        let before = result.transport_json().unwrap();
        assert!(result.project_independent_claims(&claimed).is_err());
        assert_eq!(result.transport_json().unwrap(), before);
    }
}

#[test]
fn demand_unclaimed_tail_requires_observed_failures_even_when_every_claim_completed() {
    pyo3::Python::initialize();
    let nodeids = ["zero", "one", "unclaimed"].map(str::to_owned);
    for maxfail in [1, 2] {
        let document = worker_result_json(
            vec![
                worker_case(&nodeids[0], 1.0, ExecutionOutcome::Passed),
                worker_case(&nodeids[1], 1.0, ExecutionOutcome::Passed),
            ],
            2,
        );
        let error = merge_demand_parallel_results(
            std::iter::once(Ok(document)),
            &[std::iter::once(0..2).collect()],
            &nodeids,
            &[Some(0)],
            maxfail,
            0,
            2.0,
        )
        .err()
        .expect("merely enabling maxfail cannot authorize an unclaimed tail");
        assert!(error.to_string().contains("before reaching maxfail"));
    }
}
