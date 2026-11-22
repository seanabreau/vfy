use super::FreshProcessReaper;

#[test]
fn failed_second_owner_cannot_restore_the_first_owners_process_setting() {
    pyo3::Python::initialize();
    pyo3::Python::attach(|_py| {
        let previous =
            rustix::process::child_subreaper().expect("read the original process setting");
        let owner = FreshProcessReaper::acquire().expect("the first owner is exclusive");
        assert!(FreshProcessReaper::acquire().is_err());
        assert!(
            rustix::process::child_subreaper()
                .expect("the first owner remains active")
                .is_some()
        );
        owner
            .finish()
            .expect("restore while retaining exclusive ownership");
        assert_eq!(
            rustix::process::child_subreaper().expect("read restored setting"),
            previous
        );
        FreshProcessReaper::acquire()
            .expect("a new owner can acquire after cleanup")
            .finish()
            .expect("restore the subsequent owner");
        assert_eq!(
            rustix::process::child_subreaper().expect("final process setting"),
            previous
        );
    });
}

#[test]
fn startup_failure_requires_a_mutation_and_its_owned_completion() {
    use crate::independent::{Disposition, FailureOrigin, LifecycleStage};
    use crate::parallel::independent::{ValidatedFailure, attach_failure};
    use crate::parallel::{NativeParallelSession, ParallelSupervisor, native_parallel_error};

    pyo3::Python::initialize();
    pyo3::Python::attach(|py| {
        let fixture = cache_directory();
        let input = mutation(fixture.path()).mutation;
        let encoded = serde_json::to_string(&input).expect("canonical source mutation");
        for (payload, authenticated) in
            [(None, false), (None, true), (Some(encoded.as_str()), false)]
        {
            let directory = tempfile::tempdir().expect("private supervisor directory");
            let session = NativeParallelSession::fresh_mutation_session(
                directory.path().to_string_lossy().into_owned(),
                30.0,
                payload,
                Vec::new(),
                None,
            )
            .expect("owned fresh session");
            let mut supervisor = ParallelSupervisor::prepare(&session).expect("owned supervisor");
            let error = native_parallel_error(py, "conftest_loading".to_owned(), Some(4));
            if authenticated {
                attach_failure(
                    py,
                    &error,
                    ValidatedFailure {
                        stage: LifecycleStage::ConftestLoading,
                        disposition: Disposition::Return(4),
                        origin: FailureOrigin::Usage,
                    },
                )
                .expect("owned baseline failure");
            }
            assert!(supervisor.failed_fresh_result(py, error).is_err());
        }
    });
}

use super::cache::{FreshInputs, fingerprints, read, write};
use crate::parallel::independent::{ConsumedArguments, InvocationPurpose};
use crate::results::{NativeRunResult, native_run_result};
use pyo3::prelude::*;
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use vfy_core::mutation::{
    ExecutionOwner, MutationIdentity, MutationInput, MutationResult, MutationStatus,
};
use vfy_core::test_execution::{
    CaseMarkerObservation, CaseResult, ConfigurationObservation, ExecutionOutcome, ExecutionPhase,
    ExecutionTopology, PhasePayload, PhaseResult, RunObservations, RunRecord, compute_run_counts,
};

const SUBJECT: &str = "def value():\n    return 1\n";

fn cache_directory() -> tempfile::TempDir {
    let directory = tempfile::tempdir().expect("real source fixture directory");
    for (path, source) in [
        ("subject.py", SUBJECT),
        ("test_subject.py", "def test_value(): pass\n"),
        ("pytest.ini", "[pytest]\n"),
    ] {
        std::fs::write(directory.path().join(path), source).expect("real source fixture");
    }
    std::fs::create_dir(directory.path().join(".vfy")).expect("real cache parent");
    directory
}

fn mutation(directory: &Path) -> MutationResult {
    let path = directory.join("subject.py").to_string_lossy().into_owned();
    let source_hash = vfy_core::mutation::source_hash(SUBJECT);
    let start = u32::try_from(SUBJECT.find('1').expect("actual mutation expression"))
        .expect("fixture offset fits the mutation schema");
    let identity = MutationIdentity {
        path: &path,
        digest: &source_hash,
        kind: "integer",
        operator: "integer_plus_one",
        start,
        end: start + 1,
        replacement: "2",
    };
    MutationResult {
        mutation: MutationInput {
            id: identity.key(),
            path,
            source_hash,
            kind: "integer".to_owned(),
            operator: "integer_plus_one".to_owned(),
            original: "1".to_owned(),
            replacement: "2".to_owned(),
            start,
            end: start + 1,
            line: 2,
            column: 11,
            owner: ExecutionOwner::default(),
        },
        status: MutationStatus::Survived,
        tests: Vec::new(),
        killer: None,
        duration_ms: 1.0,
        returncode: Some(0),
        detail: String::new(),
        phase: None,
    }
}

fn request(mutation: &MutationResult) -> String {
    serde_json::json!({
        "version":"test", "implementation":"cpython", "python_version":"3.12.0",
        "mutations":[mutation.mutation], "tests":["test_subject.py::test_value"],
        "pytest_args":["-q"], "timeout":"1.0",
        "evidence":{(mutation.mutation.id.clone()):["test_subject.py::test_value"]},
        "scopes":{(mutation.mutation.id.clone()):"selected"},
        "suite_state":[["<pytest-execution-identity>","declared-existing-inputs"]]
    })
    .to_string()
}

fn result(directory: &Path, nodeids: &[String], execute: bool) -> NativeRunResult {
    let cases = if execute {
        nodeids
            .iter()
            .map(|nodeid| {
                CaseResult::new(
                    nodeid.clone(),
                    1.0,
                    vec![PhaseResult::new(
                        ExecutionOutcome::Passed,
                        ExecutionPhase::Call,
                        1.0,
                        PhasePayload::default(),
                    )],
                )
            })
            .collect()
    } else {
        Vec::new()
    };
    native_run_result(
        RunRecord {
            internal_errors: Vec::new(),
            exitstatus: Some(0),
            engine: "vfy-independent-v1".to_owned(),
            execution_identity: None,
            collected: nodeids.len(),
            counts: compute_run_counts(&cases, &[]),
            duration_ms: 1.0,
            warnings: Vec::new(),
            cases,
            collection_reports: Vec::new(),
            deselected_nodeids: Vec::new(),
            observations: Some(RunObservations {
                configuration: ConfigurationObservation {
                    ini_path: Some(directory.join("pytest.ini").to_string_lossy().into_owned()),
                    ini_sha256: Some(vfy_core::mutation::source_hash("[pytest]\n")),
                    values: BTreeMap::new(),
                    strict_config: false,
                    strict_markers: false,
                },
                coordinator_configuration: None,
                case_markers: nodeids
                    .iter()
                    .map(|nodeid| CaseMarkerObservation {
                        nodeid: nodeid.clone(),
                        names: Vec::new(),
                    })
                    .collect(),
                plugins: Vec::new(),
                execution_topology: ExecutionTopology {
                    requested_workers: 1,
                    active_workers: 1,
                    worker_pids: vec![std::process::id()],
                },
            }),
        },
        None,
    )
}

fn inputs(directory: &Path, purpose: InvocationPurpose) -> FreshInputs {
    FreshInputs::capture(
        std::env::current_exe()
            .expect("real test executable")
            .to_str()
            .expect("test executable text"),
        directory,
        &["-q".to_owned()],
        &HashMap::new(),
        purpose,
        std::time::Duration::from_secs(1),
        None,
    )
    .expect("capture real fixture inputs")
}

fn consumed(purpose: InvocationPurpose) -> ConsumedArguments {
    ConsumedArguments::Stable {
        arguments: vec![
            "-p".to_owned(),
            "vfy.independent.coverage_support".to_owned(),
            "-q".to_owned(),
            if purpose == InvocationPurpose::FreshCollection {
                "--collect-only"
            } else {
                "-x"
            }
            .to_owned(),
        ],
        parser: vfy_core::pytest_config::PytestParserBehavior::Current,
    }
}

fn completed(
    directory: &Path,
    nodeids: &[String],
    mutation: Option<MutationResult>,
) -> NativeRunResult {
    let purpose = if mutation.is_some() {
        InvocationPurpose::FreshSerial
    } else {
        InvocationPurpose::FreshCollection
    };
    let inputs = inputs(directory, purpose);
    let mut result = result(directory, nodeids, mutation.is_some());
    let evidence = inputs
        .complete(
            nodeids,
            &result,
            Some(&consumed(purpose)),
            purpose,
            mutation,
            0,
        )
        .expect("complete the policy fixture after source stability checks");
    result.retain_fresh_evidence(evidence);
    result
}

#[test]
fn fresh_cache_requires_one_clean_lookup_and_actual_eligible_result_handles() {
    Python::initialize();
    Python::attach(|py| {
        let directory = cache_directory();
        let nodeids = vec!["test_subject.py::test_value".to_owned()];
        let mutation = mutation(directory.path());
        let request = request(&mutation);
        let clean = Py::new(py, completed(directory.path(), &nodeids, None))
            .unwrap()
            .into_bound(py);
        let path = directory.path().join(".vfy/cache.json");
        let path = path.to_str().unwrap();
        let initial = read(&clean.borrow(), path, &request).expect("actual fresh cache lookup");
        assert_eq!(initial, "{}");
        assert!(read(&clean.borrow(), path, &request).is_err());
        let actual = completed(directory.path(), &nodeids, Some(mutation.clone()));
        write(&clean, path, &request, vec![Py::new(py, actual).unwrap()])
            .expect("actual canonical update");
        let original = std::fs::read(path).unwrap();
        let next = completed(directory.path(), &nodeids, None);
        let cached: serde_json::Value =
            serde_json::from_str(&read(&next, path, &request).unwrap()).unwrap();
        assert_eq!(cached[&mutation.mutation.id]["status"], "survived");
        assert_eq!(
            cached[&mutation.mutation.id]["tests"],
            serde_json::json!([])
        );
        let mut changed = mutation;
        changed.duration_ms = 2.0;
        let changed = Py::new(py, completed(directory.path(), &nodeids, Some(changed))).unwrap();
        let forged = Py::new(py, result(directory.path(), &nodeids, true)).unwrap();
        assert!(write(&clean, path, &request, vec![changed, forged]).is_err());
        assert_eq!(std::fs::read(path).unwrap(), original);
    });
}

#[test]
fn actual_changed_mutant_collection_is_not_rewritten_as_the_clean_scope() {
    Python::initialize();
    Python::attach(|py| {
        let directory = cache_directory();
        let nodeids = vec!["test_subject.py::test_value".to_owned()];
        let mutation = mutation(directory.path());
        let request = request(&mutation);
        let clean = Py::new(py, completed(directory.path(), &nodeids, None))
            .unwrap()
            .into_bound(py);
        let changed = vec!["test_subject.py::test_value[actual-changed-id]".to_owned()];
        let actual = completed(directory.path(), &changed, Some(mutation.clone()));
        let path = directory.path().join(".vfy/cache.json");
        write(
            &clean,
            path.to_str().unwrap(),
            &request,
            vec![Py::new(py, actual).unwrap()],
        )
        .unwrap();
        assert!(!path.exists());
        let clean = Py::new(py, completed(directory.path(), &nodeids, None))
            .unwrap()
            .into_bound(py);
        let mut changed_timeout: serde_json::Value = serde_json::from_str(&request).unwrap();
        changed_timeout["timeout"] = serde_json::json!("2.0");
        let actual = completed(directory.path(), &nodeids, Some(mutation.clone()));
        write(
            &clean,
            path.to_str().unwrap(),
            &changed_timeout.to_string(),
            vec![Py::new(py, actual).unwrap()],
        )
        .unwrap();
        assert!(
            !path.exists(),
            "a different actual mutant deadline cannot authorize this key"
        );
        let virtual_patch = vfy_core::mutation::FreshMutation::new(mutation.mutation)
            .unwrap()
            .patch(SUBJECT.as_bytes())
            .expect("the exact declared mutation is allowed");
        assert_ne!(virtual_patch, SUBJECT.as_bytes());
        assert_eq!(
            std::fs::read(directory.path().join("subject.py")).unwrap(),
            SUBJECT.as_bytes()
        );
    });
}

#[test]
fn unavailable_changed_and_duplicate_collection_inputs_deny_only_cache_eligibility() {
    Python::initialize();
    let directory = cache_directory();
    let nodeids = vec!["test_subject.py::test_value".to_owned()];
    let mutation = mutation(directory.path());
    let request = request(&mutation);
    let unbound = result(directory.path(), &nodeids, false);
    assert!(
        inputs(directory.path(), InvocationPurpose::FreshCollection)
            .complete(
                &nodeids,
                &unbound,
                None,
                InvocationPurpose::FreshCollection,
                None,
                0
            )
            .is_err()
    );
    for consumed in [
        ConsumedArguments::Unavailable,
        ConsumedArguments::Stable {
            arguments: vec!["-q".to_owned()],
            parser: vfy_core::pytest_config::PytestParserBehavior::Current,
        },
    ] {
        let mut result = result(directory.path(), &nodeids, false);
        let evidence = inputs(directory.path(), InvocationPurpose::FreshCollection)
            .complete(
                &nodeids,
                &result,
                Some(&consumed),
                InvocationPurpose::FreshCollection,
                None,
                0,
            )
            .unwrap();
        result.retain_fresh_evidence(evidence);
        assert!(fingerprints(&result, &request).unwrap().is_none());
        assert_eq!(result.record().collected, 1);
    }
    let duplicated = vec![nodeids[0].clone(), nodeids[0].clone()];
    let duplicate = completed(directory.path(), &duplicated, None);
    assert!(fingerprints(&duplicate, &request).unwrap().is_none());
    assert_eq!(duplicate.record().collected, 2);
    assert_eq!(
        duplicate
            .record()
            .observations
            .as_ref()
            .unwrap()
            .case_markers
            .len(),
        2
    );
    let clean = completed(directory.path(), &nodeids, None);
    assert!(fingerprints(&clean, &request).unwrap().is_some());
    std::fs::write(
        directory.path().join("test_subject.py"),
        "def test_value(): assert True\n",
    )
    .unwrap();
    assert!(fingerprints(&clean, &request).unwrap().is_none());
}

#[test]
fn foreign_worker_parser_mode_is_part_of_the_fresh_cache_fingerprint() {
    use vfy_core::pytest_config::PytestParserBehavior;

    Python::initialize();
    Python::attach(|_py| {
        let directory = cache_directory();
        let nodeids = vec!["test_subject.py::test_value".to_owned()];
        let request = request(&mutation(directory.path()));
        let fingerprint = |parser| {
            // `None` captures both possible discovery modes for a foreign executable.
            let inputs = inputs(directory.path(), InvocationPurpose::FreshCollection);
            let mut result = result(directory.path(), &nodeids, false);
            let mut witness = consumed(InvocationPurpose::FreshCollection);
            if let ConsumedArguments::Stable { parser: actual, .. } = &mut witness {
                *actual = parser;
            }
            let evidence = inputs
                .complete(
                    &nodeids,
                    &result,
                    Some(&witness),
                    InvocationPurpose::FreshCollection,
                    None,
                    0,
                )
                .expect("complete authenticated foreign worker result");
            result.retain_fresh_evidence(evidence);
            fingerprints(&result, &request)
                .expect("derive cache fingerprint")
                .expect("worker mode identifies captured configuration")
                .0
        };
        let legacy = fingerprint(PytestParserBehavior::Legacy);
        let fixed_final = fingerprint(PytestParserBehavior::FixedFinal);
        let current = fingerprint(PytestParserBehavior::Current);
        assert_ne!(legacy, fixed_final);
        assert_ne!(fixed_final, current);
        assert_ne!(legacy, current);
    });
}
