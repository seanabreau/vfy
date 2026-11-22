//! Builtin collection filters execute at their position in hook dispatch.

use super::Context;
use crate::hooks::NativeHookRegistry;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use vfy_core::independent_collection::{KeywordExpression, MarkerExpression};

#[pyclass(frozen, unsendable)]
struct CollectionFilter {
    items: Py<PyList>,
    registry: Py<NativeHookRegistry>,
    keyword: Option<KeywordExpression>,
    marker: Option<MarkerExpression>,
    deselect: Vec<String>,
}

#[pymethods]
impl CollectionFilter {
    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "PyO3 requires a bound instance getter for the callback signature protocol"
    )]
    fn __signature__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        self.filter(py, |item| {
            self.keyword.as_ref().map_or(Ok(true), |expression| {
                Ok(expression.matches_keywords(&keywords(item)?))
            })
        })?;
        self.filter(py, |item| {
            self.marker.as_ref().map_or(Ok(true), |expression| {
                Ok(expression.matches_names(&marker_names(item)?))
            })
        })?;
        self.filter(py, |item| {
            let id = item.getattr("nodeid")?.extract::<String>()?;
            Ok(!self.deselect.iter().any(|prefix| id.starts_with(prefix)))
        })
    }
}

impl CollectionFilter {
    fn filter(
        &self,
        py: Python<'_>,
        predicate: impl Fn(&Bound<'_, PyAny>) -> PyResult<bool>,
    ) -> PyResult<()> {
        let retained = PyList::empty(py);
        let removed = PyList::empty(py);
        let items = self.items.bind(py);
        for item in items.iter() {
            if predicate(&item)? {
                retained.append(item)?;
            } else {
                removed.append(item)?;
            }
        }
        if !removed.is_empty() {
            let kwargs = PyDict::new(py);
            kwargs.set_item("items", removed)?;
            NativeHookRegistry::call(
                self.registry.bind(py),
                py,
                "pytest_deselected",
                &kwargs,
                false,
            )?;
            items.set_slice(0, items.len(), &retained)?;
        }
        Ok(())
    }
}

fn marker_names(item: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    item.call_method0("iter_markers")?
        .try_iter()?
        .map(|mark| mark?.getattr("name")?.extract())
        .collect()
}

fn keywords(item: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let mut names = Vec::new();
    for node in item.call_method0("listchain")?.try_iter()? {
        names.push(node?.getattr("name")?.extract()?);
    }
    names.extend(
        item.call_method0("listextrakeywords")?
            .try_iter()?
            .map(|value| value?.extract::<String>())
            .collect::<PyResult<Vec<_>>>()?,
    );
    names.extend(marker_names(item)?);
    if item.hasattr("function")? {
        let function = item.getattr("function")?;
        if function.hasattr("__dict__")? {
            for key in function
                .getattr("__dict__")?
                .call_method0("keys")?
                .try_iter()?
            {
                let key = key?.extract::<String>()?;
                if !key.starts_with("__vfy_") {
                    names.push(key);
                }
            }
        }
    }
    Ok(names)
}

pub(super) fn modify_items(
    py: Python<'_>,
    context: &Context,
    items: &Bound<'_, PyList>,
    keyword: Option<&str>,
) -> PyResult<()> {
    let options = context.config.bind(py).getattr("option")?;
    let markexpr = if options.hasattr("markexpr")? {
        options.getattr("markexpr")?.extract::<Option<String>>()?
    } else {
        None
    };
    let deselect = if options.hasattr("deselect")? {
        options.getattr("deselect")?.extract()?
    } else {
        Vec::new()
    };
    let callback = Py::new(
        py,
        CollectionFilter {
            items: items.clone().unbind(),
            registry: context.registry.clone_ref(py),
            keyword: keyword
                .map(KeywordExpression::parse)
                .transpose()
                .map_err(crate::IndependentUsageError::new_err)?,
            marker: markexpr
                .as_deref()
                .map(MarkerExpression::parse)
                .transpose()
                .map_err(crate::IndependentUsageError::new_err)?,
            deselect,
        },
    )?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("items", items)?;
    kwargs.set_item("config", &context.config)?;
    kwargs.set_item("session", &context.session)?;
    context.registry.bind(py).call_method1(
        "call_with_default",
        ("pytest_collection_modifyitems", kwargs, callback),
    )?;
    Ok(())
}
