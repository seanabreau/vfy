use super::{
    Backend, ControllerWorkers, Coverage, Distribution, DistributionRequest, Error, Inputs, Mode,
    Replay, Scheduling, SchedulingAction, Scope, Selection, Workers, default_scheduling, finish,
};

fn base() -> Inputs {
    Inputs {
        backend: Backend::Independent {
            workers_explicit: false,
        },
        selection: Selection {
            mode: Mode::Safe,
            force_full: false,
            fresh: false,
        },
        scope: Scope {
            target_location: false,
            invocation_location: false,
            filtered: false,
        },
        coverage: Coverage::Disabled,
        replay: Replay::Allowed,
        workers: Workers::One,
        scheduling: Scheduling::Other,
        distribution: DistributionRequest::Infer,
    }
}

#[test]
fn common_independent_policy_keeps_worker_and_history_contracts() {
    let value = finish(&base()).expect("valid invocation");
    assert_eq!(value.controller_workers, ControllerWorkers::None);
    assert_eq!(value.scheduling, SchedulingAction::Requested);
    assert_eq!(value.distribution, Distribution::No);
    assert_eq!(value.replay, Replay::Disabled);
}

#[test]
fn demand_defaults_only_for_independent_process_workers() {
    let independent = Backend::Independent {
        workers_explicit: true,
    };
    assert_eq!(default_scheduling(independent, Workers::Other), "demand");
    for workers in [Workers::Unset, Workers::One] {
        assert_eq!(default_scheduling(independent, workers), "cases");
    }
    for backend in [
        Backend::Native {
            xdist_requested: false,
        },
        Backend::Pytest,
    ] {
        assert_eq!(default_scheduling(backend, Workers::Other), "file");
    }
}

#[test]
fn all_scope_error_order_and_forced_recovery_are_stable() {
    let mut input = base();
    input.selection.fresh = true;
    input.scope.target_location = true;
    input.scope.filtered = true;
    assert_eq!(finish(&input), Err(Error::AllRequiresUnfilteredSafe));
    input.scope.target_location = false;
    assert_eq!(finish(&input), Err(Error::AllWithSelection));
    input.selection.force_full = true;
    let result = finish(&input).expect("forced recovery clears --all");
    assert_eq!(result.mode, Mode::Full);
    assert!(!result.fresh);
}

#[test]
fn replay_requires_its_existing_conditions() {
    let mut input = base();
    input.coverage = Coverage::Replace;
    for backend in [
        Backend::Independent {
            workers_explicit: false,
        },
        Backend::Native {
            xdist_requested: false,
        },
        Backend::Pytest,
    ] {
        input.backend = backend;
        assert_eq!(
            finish(&input).expect("covered safe run").replay,
            Replay::Allowed
        );
    }
    input.scope.filtered = true;
    assert_eq!(finish(&input).expect("filtered").replay, Replay::Disabled);
    input.scope.filtered = false;
    input.coverage = Coverage::Append;
    assert_eq!(finish(&input).expect("append").replay, Replay::Disabled);
}

#[test]
fn explicit_distribution_and_native_scheduling_do_not_override_each_other() {
    let mut input = base();
    input.workers = Workers::Other;
    input.scheduling = Scheduling::Scope;
    assert_eq!(
        finish(&input).expect("scoped").distribution,
        Distribution::NativeScheduling
    );
    input.distribution = DistributionRequest::Explicit;
    assert_eq!(
        finish(&input).expect("explicit").distribution,
        Distribution::Explicit
    );
    input.distribution = DistributionRequest::Infer;
    input.backend = Backend::Pytest;
    assert_eq!(
        finish(&input).expect("pytest").distribution,
        Distribution::Load
    );
}
