//! Native collection publication and validated process-worker assignments.

use super::CollectedSuite;
use super::observations;
use crate::results::{NativeRunResult, native_run_result};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{BTreeMap, BTreeSet};
use vfy_core::test_execution::{CollectionReport, RunRecord, compute_run_counts};
use vfy_core::test_plan::{SuitePlan, TestId};

pub(super) fn publish(
    py: Python<'_>,
    suite: &mut CollectedSuite,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<()> {
    let Some(callback) = options
        .map(|options| options.get_item("_collection_callback"))
        .transpose()?
        .flatten()
    else {
        return Ok(());
    };
    let nodeids = suite
        .context
        .items
        .iter()
        .map(|item| item.bind(py).getattr("nodeid")?.extract::<String>())
        .collect::<PyResult<Vec<_>>>()?;
    let result = collection_result(py, suite)?;
    let selected = callback.call1((&nodeids, Py::new(py, result)?))?;
    if suite.context.invocation.surplus.get() {
        // A released spare retains the collection its session hooks observed,
        // while its native execution bindings contain no assigned tests.
        return select_indices(py, suite, &[]);
    }
    if suite.context.invocation.demand_assignment() {
        // Demand workers retain the authenticated full collection; the cursor
        // chooses global plan indices without slicing or reindexing bindings.
        return Ok(());
    }
    if suite.context.invocation.is_fresh_serial() && !selected.is_none() {
        let requested = selected.extract::<Vec<String>>()?;
        if requested != nodeids {
            return select(py, suite, &requested);
        }
    }
    let bound_indices = suite.context.invocation.assigned_indices();
    if let Some(indices) = bound_indices {
        return select_indices(py, suite, &indices);
    }
    if selected.is_none() {
        return Ok(());
    }
    let selected = selected.extract::<Vec<String>>()?;
    select(py, suite, &selected)
}

pub(super) fn select(
    py: Python<'_>,
    suite: &mut CollectedSuite,
    selected: &[String],
) -> PyResult<()> {
    let nodeids = suite
        .plan
        .tests()
        .iter()
        .map(|test| test.nodeid.clone())
        .collect::<Vec<_>>();
    let mut indexes = BTreeMap::new();
    for (index, nodeid) in nodeids.iter().enumerate() {
        if indexes.insert(nodeid.as_str(), index).is_some() {
            return Err(PyValueError::new_err(
                "string selection requires unique collected node IDs",
            ));
        }
    }
    let mut seen = BTreeSet::new();
    let mut order = Vec::new();
    for nodeid in selected {
        let index = indexes.get(nodeid.as_str()).copied().ok_or_else(|| {
            PyValueError::new_err(format!(
                "worker assignment contains an uncollected test: {nodeid}"
            ))
        })?;
        if !seen.insert(index) {
            return Err(PyValueError::new_err(format!(
                "worker assignment repeats test: {nodeid}"
            )));
        }
        order.push(index);
    }
    let arena = super::collection::arena::for_context(py, &suite.context)?;
    let items = super::collection::arena::NativeCollectionArena::items(&arena)?;
    let selected = PyList::new(
        py,
        order
            .iter()
            .map(|index| suite.context.items[*index].bind(py)),
    )?;
    items.set_slice(0, items.len(), &selected)?;
    suite
        .context
        .session
        .bind(py)
        .setattr("testscollected", order.len())?;
    super::lifecycle::synchronize_items(py, suite)
}

fn select_indices(py: Python<'_>, suite: &mut CollectedSuite, order: &[usize]) -> PyResult<()> {
    let mut seen = BTreeSet::new();
    if order
        .iter()
        .any(|index| *index >= suite.tests.len() || !seen.insert(*index))
    {
        return Err(PyValueError::new_err(
            "native assignment has invalid or repeated test indices",
        ));
    }
    let mut bindings = std::mem::take(&mut suite.tests)
        .into_iter()
        .map(Some)
        .collect::<Vec<_>>();
    let mut tests = Vec::new();
    suite.context.items = order
        .iter()
        .map(|index| suite.context.items[*index].clone_ref(py))
        .collect();
    for &index in order {
        let mut test = suite.plan.tests()[index].clone();
        test.id = TestId::from_index(tests.len());
        tests.push(test);
        suite.tests.push(
            bindings[index]
                .take()
                .ok_or_else(|| PyValueError::new_err("worker assignment duplicated a binding"))?,
        );
    }
    suite.plan = SuitePlan::new(tests, suite.plan.fixtures().to_vec(), None)
        .map_err(PyValueError::new_err)?;
    Ok(())
}

pub(super) fn collection_result(
    py: Python<'_>,
    suite: &CollectedSuite,
) -> PyResult<NativeRunResult> {
    published_result(
        py,
        &suite.context,
        suite.plan.tests().len(),
        &suite.collection_reports,
        &suite.deselected_nodeids,
    )
}

/// A session that fails before any suite is published still reports an empty run.
pub(in crate::independent) fn empty_result(
    py: Python<'_>,
    context: &super::lifecycle::Context,
) -> PyResult<NativeRunResult> {
    published_result(py, context, 0, &[], &[])
}

fn published_result(
    py: Python<'_>,
    context: &super::lifecycle::Context,
    collected: usize,
    collection_reports: &[CollectionReport],
    deselected_nodeids: &[String],
) -> PyResult<NativeRunResult> {
    let record = RunRecord {
        internal_errors: Vec::new(),
        engine: "vfy-independent-v1".to_owned(),
        exitstatus: None,
        execution_identity: None,
        observations: Some(observations::snapshot(py, context)?),
        collected,
        counts: compute_run_counts(&[], collection_reports),
        duration_ms: 0.0,
        warnings: context.warnings.borrow().clone(),
        cases: Vec::new(),
        collection_reports: collection_reports.to_vec(),
        deselected_nodeids: deselected_nodeids.to_vec(),
    };
    Ok(native_run_result(record, None))
}
