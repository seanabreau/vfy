//! Identify the compiled test and session that produced a report.
//!
//! The generation names the session history that issued the origin. Logreport rejects a
//! report from another generation, so a replay from a prior session never reaches a case.

use super::NativeSessionReports;
use crate::independent::lifecycle::Context;
use pyo3::prelude::*;
use vfy_core::test_plan::TestId;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(in crate::independent) struct ReportOrigin {
    test: TestId,
    generation: u64,
    transport_summary: bool,
}

impl ReportOrigin {
    pub(in crate::independent) fn bound(
        py: Python<'_>,
        context: &Context,
        index: usize,
    ) -> PyResult<Self> {
        let history = context
            .session
            .bind(py)
            .getattr(pyo3::intern!(py, "_vfy_report"))?
            .cast_into::<NativeSessionReports>()?;
        let generation = history.borrow().generation();
        Ok(Self {
            test: context.invocation.test_id(index)?,
            generation,
            transport_summary: context.invocation.transports_summary(),
        })
    }

    pub(in crate::independent) const fn new(test: TestId, generation: u64) -> Self {
        Self {
            test,
            generation,
            transport_summary: false,
        }
    }

    pub(in crate::independent) const fn transports_summary(&self) -> bool {
        self.transport_summary
    }

    pub(in crate::independent) const fn observed_test(&self) -> TestId {
        self.test
    }

    pub(in crate::independent) const fn generation(&self) -> u64 {
        self.generation
    }

    /// Compatibility for protocol callers outside this area: same test in the same session.
    pub(in crate::independent) fn same(&self, _py: Python<'_>, other: &Self) -> bool {
        self.test == other.test && self.generation == other.generation
    }
}

/// A report source names the compiled test whose request is bound.
pub(in crate::independent) type ReportSource = ReportOrigin;

/// The origin of one phase boundary; each makereport publication binds its reports to it.
#[derive(Clone)]
pub(in crate::independent) struct ReportBoundary {
    pub(super) origin: ReportOrigin,
}

impl ReportBoundary {
    pub(in crate::independent) fn bound(
        py: Python<'_>,
        context: &Context,
        index: usize,
    ) -> PyResult<Self> {
        Ok(Self {
            origin: ReportOrigin::bound(py, context, index)?,
        })
    }
}

#[cfg(test)]
mod tests;
