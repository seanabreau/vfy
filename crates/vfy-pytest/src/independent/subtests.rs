//! Context timing, failure state, and exception propagation for builtin subtests.

use super::controls::{self, ControlKind};
use super::reporting::call::{PhaseClock, PhaseCompletion};
use pyo3::exceptions::PyOverflowError;
use pyo3::prelude::*;
use std::collections::BTreeMap;
use vfy_core::test_execution::{ExecutionOutcome, ExecutionPhase, PhasePayload};

mod runtime;
pub(super) mod saferepr;
pub(crate) use runtime::{NativeSubtestContext, NativeSubtestsStatus};

pub(crate) fn register(module: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    runtime::register(module)?;
    saferepr::register(module)
}

pub(crate) struct SubtestStatus {
    pub(crate) category: &'static str,
    pub(crate) letter: &'static str,
    pub(crate) word: String,
}

pub(crate) fn status(
    outcome: ExecutionOutcome,
    description: &str,
    quiet: bool,
) -> Option<SubtestStatus> {
    let (category, letter, word) = match outcome {
        ExecutionOutcome::Failed | ExecutionOutcome::Error => {
            ("failed", "u", format!("SUBFAILED{description}"))
        }
        _ if quiet => ("", "", String::new()),
        ExecutionOutcome::Passed => ("subtests passed", "u", format!("SUBPASSED{description}")),
        ExecutionOutcome::Skipped => ("skipped", "-", format!("SUBSKIPPED{description}")),
        ExecutionOutcome::XFailed => ("xfailed", "y", format!("SUBXFAIL{description}")),
        ExecutionOutcome::XPassed => return None,
    };
    Some(SubtestStatus {
        category,
        letter,
        word,
    })
}

/// A subtest always constructs a call report, even during fixture setup or teardown.
pub(super) struct SubtestClock(PhaseClock);

impl SubtestClock {
    pub(super) fn start() -> Self {
        Self(PhaseClock::start())
    }

    pub(super) fn complete(self, result: PyResult<()>, capture: PhasePayload) -> PhaseCompletion {
        let mut completion = self.0.complete(ExecutionPhase::Call, result);
        completion.capture = capture;
        completion
    }
}

/// This counter records failures before logreport hooks can mutate retained reports.
#[derive(Default)]
pub(super) struct SubtestFailures {
    by_nodeid: BTreeMap<String, usize>,
}

impl SubtestFailures {
    pub(super) fn record(&mut self, nodeid: &str, outcome: ExecutionOutcome) -> PyResult<()> {
        if !matches!(outcome, ExecutionOutcome::Failed | ExecutionOutcome::Error) {
            return Ok(());
        }
        let previous = self.by_nodeid.get(nodeid).copied().unwrap_or_default();
        let next = previous
            .checked_add(1)
            .ok_or_else(|| PyOverflowError::new_err("subtest failure count exceeds usize"))?;
        self.by_nodeid.insert(nodeid.to_owned(), next);
        Ok(())
    }

    /// The status hook changes only parents whose current pytest outcome is passed.
    pub(super) fn parent_detail(&self, nodeid: &str, outcome: ExecutionOutcome) -> Option<String> {
        if !matches!(
            outcome,
            ExecutionOutcome::Passed | ExecutionOutcome::XPassed
        ) {
            return None;
        }
        let count = self.by_nodeid.get(nodeid).copied().unwrap_or_default();
        if count == 0 {
            return None;
        }
        let suffix = if count == 1 { "" } else { "s" };
        Some(format!("contains {count} failed subtest{suffix}"))
    }
}

/// Evaluate after logreport and exception-interaction callbacks have completed.
pub(super) fn must_reraise(
    py: Python<'_>,
    config: &Bound<'_, PyAny>,
    error: &PyErr,
) -> PyResult<bool> {
    match controls::kind(py, error)? {
        Some(ControlKind::Exit) => Ok(true),
        Some(ControlKind::KeyboardInterrupt) => Ok(!config
            .call_method1("getoption", ("usepdb", false))?
            .is_truthy()?),
        None => Ok(false),
    }
}
