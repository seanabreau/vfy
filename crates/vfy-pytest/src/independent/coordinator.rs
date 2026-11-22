//! Run a configured session through the worker supervisor and publish its result.
use super::{error_payload, lifecycle, observations};
use crate::results::NativeRunResult;
use pyo3::exceptions::{PyKeyboardInterrupt, PyValueError};
use pyo3::prelude::*;

mod dispatch;

fn failure_status(py: Python<'_>, error: &PyErr) -> PyResult<i32> {
    if let Some(failure) = crate::parallel::independent::validated_failure(py, error)
        && let super::lifecycle::state::Disposition::Return(status) = failure.disposition
    {
        return Ok(status);
    }
    if let Some(status) = super::controls::exitstatus(py, error)? {
        return Ok(status);
    }
    Ok(if error.is_instance_of::<PyKeyboardInterrupt>(py) {
        2
    } else if error.is_instance_of::<crate::IndependentUsageError>(py)
        || error.is_instance_of::<crate::UnsupportedFeatureError>(py)
    {
        4
    } else {
        3
    })
}

fn normal_status(py: Python<'_>, context: &lifecycle::Context) -> PyResult<i32> {
    let session = context.session.bind(py);
    Ok(if session.getattr("testsfailed")?.extract::<usize>()? > 0 {
        1
    } else if session.getattr("testscollected")?.extract::<usize>()? == 0 {
        5
    } else {
        0
    })
}

pub(super) fn run_context(
    py: Python<'_>,
    context: &lifecycle::Context,
    callback: Py<PyAny>,
) -> PyResult<Py<NativeRunResult>> {
    let before_warnings = context.warnings.borrow().len();
    let outcome = match dispatch::run(py, context, callback) {
        Ok(outcome) => outcome,
        Err(error) => {
            let diagnostic: PyResult<()> = Err(error.clone_ref(py));
            context.report_error(py, &diagnostic)?;
            if super::controls::is_control_error(py, &error)? {
                context.finish_current(py)?;
            } else {
                // No worker run was published, so the controller's document is empty,
                // as stock xdist writes it when a worker fails during collection.
                context.bind_empty_junit_result(py)?;
                context.finish(py, failure_status(py, &error)?)?;
            }
            return Err(error);
        }
    };
    let result = outcome.completed.result;
    super::cache_provider::collect_worker_result(py, context, result.bind(py))?;
    context.bind_junit_result(py, &result)?;
    context.bind_terminal_result(
        py,
        &result,
        super::reporting::terminal::session::SummaryOrigin::WorkerMerge {
            warning_split: before_warnings,
        },
    )?;
    let (baseline, worker_control) = {
        let result = result.borrow(py);
        (
            result
                .record()
                .exitstatus
                .expect("validated worker exitstatus"),
            result.worker_interruption().is_some(),
        )
    };
    let status = if let Some(error) = &outcome.error {
        let diagnostic: PyResult<()> = Err(error.clone_ref(py));
        context.report_error(py, &diagnostic)?;
        let status = failure_status(py, error)?;
        if status == 3 && !super::controls::is_control_error(py, error)? {
            let payload = error_payload(py, error)?;
            result
                .borrow_mut(py)
                .record_mut()
                .internal_errors
                .push(payload);
        }
        status
    } else if outcome.interrupted {
        2
    } else if outcome.completed.interrupted || (!worker_control && matches!(baseline, 0 | 1 | 5)) {
        normal_status(py, context)?
    } else {
        baseline
    };
    let status = if outcome
        .error
        .as_ref()
        .map(|error| super::controls::is_control_error(py, error))
        .transpose()?
        .unwrap_or(false)
        || outcome
            .error
            .as_ref()
            .is_some_and(|error| !error.is_instance_of::<crate::NativeParallelError>(py))
    {
        context.finish_current(py)?
    } else {
        context.finish(py, status)?
    };
    context
        .stage
        .set(super::lifecycle::state::LifecycleStage::Materializing);
    super::reporting::materialize_reports(py, context.session.bind(py), &result)?;
    finalize_worker_result(py, context, &result, before_warnings, status)?;
    super::reporting::html::bind_result(py, context, &result)?;
    Ok(result)
}

fn finalize_worker_result(
    py: Python<'_>,
    context: &lifecycle::Context,
    result: &Py<NativeRunResult>,
    before_warnings: usize,
    status: i32,
) -> PyResult<()> {
    let plugins = context.registry.borrow(py).plugin_observations()?;
    let mut recorded = {
        let result = result.borrow(py);
        vfy_core::test_execution::validate_run_record(result.record())
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        result
            .record()
            .observations
            .clone()
            .ok_or_else(|| PyValueError::new_err("missing worker observations"))?
    };
    recorded
        .merge_plugins(plugins)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    if recorded.coordinator_configuration.is_some() {
        return Err(PyValueError::new_err(
            "worker result already contains coordinator configuration",
        ));
    }
    recorded.coordinator_configuration = Some(observations::configuration(py, context)?);
    let history = context
        .session
        .bind(py)
        .getattr("_vfy_report")?
        .cast_into::<super::reporting::NativeSessionReports>()?;
    let mut result_ref = result.borrow_mut(py);
    let warnings = context.warnings.borrow();
    let combined =
        history
            .borrow()
            .summary
            .project_warnings(&result_ref, &warnings, before_warnings)?;
    let record = result_ref.record_mut();
    record.observations = Some(recorded);
    record.warnings = combined;
    record.exitstatus = Some(status);
    vfy_core::test_execution::validate_run_record(record)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    drop(result_ref);
    Ok(())
}
