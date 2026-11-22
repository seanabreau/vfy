//! Capture pytest's short description of a singleton exception-group branch.

use pyo3::prelude::*;
use std::collections::HashSet;

// Custom exceptions descriptors can produce cycles or unbounded fresh groups.
// Their recursive protocol is unadmitted; never turn that into a partial summary.
const MAX_SINGLE_GROUP_DEPTH: usize = 1024;

fn single_subexception<'py>(
    group: &Bound<'py, PyAny>,
    group_type: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    // Visited groups stay alive, so their addresses identify them in the cycle check.
    let mut ancestors = Vec::new();
    let mut current = group.clone();
    let mut seen = HashSet::new();
    loop {
        if ancestors.len() >= MAX_SINGLE_GROUP_DEPTH || !seen.insert(current.as_ptr() as usize) {
            return Err(crate::UnsupportedFeatureError::new_err(
                "singleton group summaries do not support cyclic or deeply recursive descriptors",
            ));
        }
        let exceptions = current.getattr("exceptions")?;
        if exceptions.len()? != 1 {
            return Ok(None);
        }
        let child = exceptions.get_item(0)?;
        if !child.is_instance(group_type)? {
            // None is the upstream no-single-leaf sentinel, including descriptor results.
            return Ok((!child.is_none()).then_some(child));
        }
        ancestors.push(current);
        current = child;
    }
}

pub(super) fn short_description(
    py: Python<'_>,
    exception: &Bound<'_, PyAny>,
) -> PyResult<Option<String>> {
    let group_type = py.import("builtins")?.getattr("BaseExceptionGroup")?;
    if !exception.is_instance(&group_type)? {
        return Ok(None);
    }
    let Some(leaf) = single_subexception(exception, &group_type)? else {
        return Ok(None);
    };
    let leaf_text = leaf.repr()?.to_str()?.to_owned();
    let name = exception
        .get_type()
        .getattr("__name__")?
        .str()?
        .to_str()?
        .to_owned();
    Ok(Some(format!("{leaf_text} [single exception in {name}]")))
}
