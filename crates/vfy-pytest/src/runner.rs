//! Native pytest execution: fixture resolution, setup, call and teardown phases, and run records.

use pyo3::prelude::{Bound, PyAnyMethods, PyResult};
use pyo3::types::PyAny;

mod capture;
mod compile;
mod execute;
mod fixture_lookup;
mod fixture_setup;
mod model;
mod native_session;
mod report;
mod request;
mod runtime;
mod subtest;
mod teardown;
mod test_call;
mod test_setup;

pub(crate) use self::compile::{compact_case_markers, native_unsupported_items};
pub(crate) use self::native_session::{NativeExecutionSession, run_native_suite};
pub(crate) use self::request::NativeRequest;

#[cfg(test)]
mod tests;

fn project_session_stopped(item: &Bound<'_, PyAny>) -> PyResult<bool> {
    let session = item.getattr("session")?;
    Ok(
        session.getattr("shouldfail")?.is_truthy()?
            || session.getattr("shouldstop")?.is_truthy()?,
    )
}

fn skipping_plugin_enabled(item: &Bound<'_, PyAny>) -> PyResult<bool> {
    item.getattr("config")?
        .getattr("pluginmanager")?
        .call_method1("hasplugin", ("skipping",))?
        .extract()
}
