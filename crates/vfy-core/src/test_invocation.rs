//! Shared test invocation policy.

/// Engine and its applicable worker/xdist configuration.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Backend {
    Independent { workers_explicit: bool },
    Native { xdist_requested: bool },
    Pytest,
}
/// Test-selection mode after normal CLI validation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Full,
    Safe,
    Balanced,
    Aggressive,
}
/// Classification of a caller-owned worker value; its original Python object survives.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Workers {
    Unset,
    One,
    Other,
}

/// Default independent process workers consume demand leases; serial invocations
/// retain their ordinary case order and installed-pytest workers retain files.
#[must_use]
pub fn default_scheduling(backend: Backend, workers: Workers) -> &'static str {
    match (backend, workers) {
        (Backend::Independent { .. }, Workers::Other) => "demand",
        (Backend::Independent { .. }, Workers::Unset | Workers::One) => "cases",
        (Backend::Native { .. } | Backend::Pytest, _) => "file",
    }
}

/// Scheduling classification; the original Python object survives in the adapter.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Scheduling {
    Scope,
    Cases,
    Striped,
    Demand,
    Other,
}
/// Measurement action from effective arguments.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Coverage {
    Disabled,
    Replace,
    Append,
}
/// Scope facts are distinct: a target and a configured location affect different policies.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Scope {
    pub target_location: bool,
    pub invocation_location: bool,
    pub filtered: bool,
}
/// The requested selection, including watch's forced full recovery.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Selection {
    pub mode: Mode,
    pub force_full: bool,
    pub fresh: bool,
}
/// Whether selection is permitted to restore prior data.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Replay {
    Allowed,
    Disabled,
}
/// An explicit pytest distribution must retain its original Python value.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DistributionRequest {
    Explicit,
    Infer,
}
/// Policy facts gathered at the Python compatibility boundary.
#[derive(Clone, Debug)]
pub struct Inputs {
    pub backend: Backend,
    pub selection: Selection,
    pub scope: Scope,
    pub coverage: Coverage,
    pub replay: Replay,
    pub workers: Workers,
    pub scheduling: Scheduling,
    pub distribution: DistributionRequest,
}
/// Scope failure selected by controller order.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    AllRequiresUnfilteredSafe,
    AllWithSelection,
}
/// Source of distribution string; explicit and scheduling values remain Python-owned.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Distribution {
    Explicit,
    No,
    PytestConfig,
    Load,
    Loadfile,
    NativeScheduling,
}
/// Which caller-owned worker argument reaches the controller.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ControllerWorkers {
    None,
    Requested,
}
/// Whether native scheduling is applicable to the selected backend.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SchedulingAction {
    None,
    Requested,
}
/// Immutable decisions used by both ordinary and general execution routes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Plan {
    pub mode: Mode,
    pub fresh: bool,
    pub controller_workers: ControllerWorkers,
    pub scheduling: SchedulingAction,
    pub distribution: Distribution,
    pub replay: Replay,
}
/// Resolve after the same pytest configuration has been projected once.
///
/// # Errors
/// Returns the applicable scope error for incompatible `--all` requests.
pub fn finish(input: &Inputs) -> Result<Plan, Error> {
    let mode = if input.selection.force_full {
        Mode::Full
    } else {
        input.selection.mode
    };
    let fresh = input.selection.fresh && !input.selection.force_full;
    if fresh
        && (mode != Mode::Safe || input.scope.target_location || input.scope.invocation_location)
    {
        return Err(Error::AllRequiresUnfilteredSafe);
    }
    if fresh && input.scope.filtered {
        return Err(Error::AllWithSelection);
    }
    let use_scheduling = matches!(
        input.backend,
        Backend::Independent { .. }
            | Backend::Native {
                xdist_requested: false
            }
    );
    let distribution = if input.distribution == DistributionRequest::Explicit {
        Distribution::Explicit
    } else if input.workers == Workers::One {
        Distribution::No
    } else if input.workers == Workers::Unset {
        Distribution::PytestConfig
    } else if use_scheduling && input.scheduling != Scheduling::Other {
        Distribution::NativeScheduling
    } else if input.backend != Backend::Pytest {
        Distribution::Loadfile
    } else {
        Distribution::Load
    };
    Ok(Plan {
        mode,
        fresh,
        controller_workers: if matches!(
            input.backend,
            Backend::Independent {
                workers_explicit: false
            }
        ) {
            ControllerWorkers::None
        } else {
            ControllerWorkers::Requested
        },
        scheduling: if use_scheduling {
            SchedulingAction::Requested
        } else {
            SchedulingAction::None
        },
        distribution,
        replay: if mode == Mode::Safe
            && input.replay == Replay::Allowed
            && !input.scope.target_location
            && !input.scope.filtered
            && input.coverage == Coverage::Replace
        {
            Replay::Allowed
        } else {
            Replay::Disabled
        },
    })
}

#[cfg(test)]
mod tests;
