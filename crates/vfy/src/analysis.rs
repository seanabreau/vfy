//! Python bindings for source analysis results.

use pyo3::prelude::*;

pub(crate) use vfy_core::analysis::{SourceAnalysis, multiline_map};

#[pyclass(name = "SourceAnalysis", module = "vfy._native", frozen)]
pub(crate) struct PySourceAnalysis(SourceAnalysis);

#[pymethods]
impl PySourceAnalysis {
    #[getter]
    fn executable(&self) -> Vec<i32> {
        self.0.executable.clone()
    }
    #[getter]
    fn excluded(&self) -> Vec<i32> {
        self.0.excluded.clone()
    }
    #[getter]
    fn possible_arcs(&self) -> Vec<(i32, i32)> {
        self.0.possible_arcs.clone()
    }
    #[getter]
    fn no_branch(&self) -> Vec<i32> {
        self.0.no_branch.clone()
    }
    #[getter]
    fn source_missing(&self) -> bool {
        self.0.source_missing()
    }
    #[getter]
    fn source_error_kind(&self) -> Option<&str> {
        self.0.source_error_kind()
    }
    #[getter]
    fn source_error_detail(&self) -> Option<&str> {
        self.0.source_error_detail()
    }
}

pub(crate) fn python_semantics(py: Python<'_>) -> PyResult<vfy_core::analysis::PythonSemantics> {
    Ok(vfy_core::analysis::PythonSemantics {
        class_bare_annotations_execute: py.version_info() < (3, 14),
        debug_enabled: py.eval(c"__debug__", None, None)?.extract()?,
    })
}

#[pyfunction]
pub(crate) fn clear_analysis_cache() {
    vfy_core::analysis::clear_analysis_cache();
}

#[pyfunction]
pub(crate) fn analysis_cache_stats() -> (usize, usize, usize, usize) {
    vfy_core::analysis::analysis_cache_stats()
}

#[pyfunction]
#[pyo3(signature = (path, excludes, collect_arcs, partials=Vec::new()))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "The extension extracts owned pattern lists."
)]
pub(crate) fn analyze_file(
    py: Python<'_>,
    path: &str,
    excludes: Vec<String>,
    collect_arcs: bool,
    partials: Vec<String>,
) -> PyResult<PySourceAnalysis> {
    let semantics = python_semantics(py)?;
    py.detach(|| {
        vfy_core::analysis::analyze_file(path, &excludes, collect_arcs, &partials, semantics)
    })
    .map(PySourceAnalysis)
    .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}
