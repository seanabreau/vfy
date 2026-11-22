use super::*;

pub(super) fn issued(
    purpose: InvocationPurpose,
    width: usize,
) -> (tempfile::TempDir, InvocationBinding) {
    let directory = tempfile::tempdir().expect("actual native collection directory");
    let identity = match purpose {
        InvocationPurpose::Parallel => BootstrapIdentity::create(directory.path(), width),
        InvocationPurpose::FreshSerial => {
            BootstrapIdentity::create_fresh(directory.path(), width, None)
        }
        InvocationPurpose::FreshCollection => {
            BootstrapIdentity::create_fresh_collection(directory.path(), width)
        }
    }
    .expect("native-issued collection identity");
    write_json_atomic(&directory.path().join("bootstrap.json"), &width)
        .expect("actual bootstrap width");
    let binding = InvocationBinding {
        directory: directory.path().to_owned(),
        index: 0,
        identity,
        pid: std::process::id(),
        assignment: None,
        mutation: None,
        source_applied: false,
        consumed_arguments: None,
    };
    (directory, binding)
}

fn empty_collection(index: usize) -> super::super::WorkerCollection {
    super::super::WorkerCollection {
        index,
        nodeids: Vec::new(),
        reports: Vec::new(),
        warnings: Vec::new(),
        errors: 0,
        cache_snapshot: None,
        benchmark_fixture: false,
    }
}

#[test]
fn native_fresh_binding_admits_width_one_collection_without_changing_legacy_admission() {
    let (directory, binding) = issued(InvocationPurpose::FreshSerial, 1);
    let collection = empty_collection(0);
    assert!(
        NativeParallelWorker::publish_collection_snapshot(directory.path(), &collection, None)
            .is_err()
    );
    assert!(!directory.path().join("collection.0.json").exists());
    NativeParallelWorker::publish_collection_snapshot(
        directory.path(),
        &collection,
        Some(&binding),
    )
    .expect("actual fresh collection admitted");
    let restored =
        super::super::read_worker_collection(&directory.path().join("collection.0.json"))
            .expect("actual atomic worker manifest");
    assert_eq!(restored.index, 0);
    assert!(restored.nodeids.is_empty());
    assert!(
        NativeParallelWorker::publish_collection_snapshot(directory.path(), &collection, None)
            .is_err()
    );
}

#[test]
fn fresh_collection_binding_checks_its_width_one_plan_and_zero_failure_budget() {
    let (directory, binding) = issued(InvocationPurpose::FreshCollection, 1);
    assert_eq!(binding.purpose(), InvocationPurpose::FreshCollection);
    assert!(binding.mutation_input().unwrap().is_none());
    NativeParallelWorker::publish_collection_snapshot(
        directory.path(),
        &empty_collection(0),
        Some(&binding),
    )
    .expect("an actual fresh collect-only binding can publish collection");
    assert!(BootstrapIdentity::create_fresh_collection(directory.path(), 2).is_err());
    let mut plan = ParallelPlan {
        scheduling: super::super::ParallelScheduling::File,
        nodeids: vec!["a.py::test_case".to_owned()],
        assignments: vec![vec![0]],
        maxfail: 1,
        initial_failures: 0,
    };
    assert!(binding.identity.check_plan(&plan).is_err());
    plan.maxfail = 0;
    binding
        .identity
        .check_plan(&plan)
        .expect("collect-only purpose uses its zero execution failure budget");
}

#[test]
fn collection_preflight_rejects_foreign_width_index_process_and_assigned_owner() {
    let (directory, mut binding) = issued(InvocationPurpose::FreshSerial, 1);
    let other = tempfile::tempdir().expect("foreign collection directory");
    assert!(
        binding
            .validate_collection_binding(other.path(), 0, 1)
            .is_err()
    );
    assert!(
        binding
            .validate_collection_binding(directory.path(), 1, 1)
            .is_err()
    );
    assert!(
        binding
            .validate_collection_binding(directory.path(), 0, 2)
            .is_err()
    );
    binding.pid = binding.pid.wrapping_add(1);
    assert!(
        binding
            .validate_collection_binding(directory.path(), 0, 1)
            .is_err()
    );
    binding.pid = std::process::id();
    binding.assignment = Some(std::sync::Arc::new(AssignedInvocation::Static(vec![0])));
    assert!(
        binding
            .validate_collection_binding(directory.path(), 0, 1)
            .is_err()
    );
    assert!(!directory.path().join("collection.0.json").exists());
}

#[test]
fn stale_identity_cannot_bypass_preflight_with_an_existing_manifest() {
    let (directory, binding) = issued(InvocationPurpose::FreshSerial, 1);
    let path = directory.path().join("collection.0.json");
    let previous = b"previous collection must remain unchanged\n";
    std::fs::write(&path, previous).expect("existing actual path");
    // A reissued bootstrap carries a new run nonce, so the old binding no longer matches.
    BootstrapIdentity::create_fresh(directory.path(), 1, None)
        .expect("new issued identity in reused directory");
    assert!(
        NativeParallelWorker::publish_collection_snapshot(
            directory.path(),
            &empty_collection(0),
            Some(&binding)
        )
        .is_err()
    );
    assert_eq!(std::fs::read(&path).expect("preserved file"), previous);
    let mut current = binding.identity.clone();
    current.purpose = InvocationPurpose::Parallel;
    write_json_atomic(
        &directory.path().join("invocation-bootstrap.json"),
        &current,
    )
    .expect("changed purpose");
    assert!(
        NativeParallelWorker::publish_collection_snapshot(
            directory.path(),
            &empty_collection(0),
            Some(&binding)
        )
        .is_err()
    );
    assert_eq!(std::fs::read(&path).expect("preserved file"), previous);
}

#[test]
fn ordinary_parallel_binding_still_requires_distributed_width_and_preserves_publication() {
    let (directory, binding) = issued(InvocationPurpose::Parallel, 1);
    assert!(
        NativeParallelWorker::publish_collection_snapshot(
            directory.path(),
            &empty_collection(0),
            Some(&binding)
        )
        .is_err()
    );
    assert!(!directory.path().join("collection.0.json").exists());
    let (directory, binding) = issued(InvocationPurpose::Parallel, 2);
    NativeParallelWorker::publish_collection_snapshot(
        directory.path(),
        &empty_collection(0),
        Some(&binding),
    )
    .expect("ordinary distributed collection unchanged");
    assert!(
        NativeParallelWorker::publish_collection_snapshot(
            directory.path(),
            &empty_collection(2),
            None
        )
        .is_err()
    );
    assert!(!directory.path().join("collection.2.json").exists());
}
