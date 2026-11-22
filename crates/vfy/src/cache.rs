//! Python binding for cache directory removal.

use std::path::PathBuf;

use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;

#[pyfunction]
pub(crate) fn clean_cache(py: Python<'_>, path: PathBuf) -> PyResult<bool> {
    py.detach(move || {
        vfy_core::clean_cache(&path)
            .map(|outcome| outcome == vfy_core::CacheCleanup::Removed)
            .map_err(|error| {
                PyOSError::new_err(format!("failed to clean cache {}: {error}", path.display()))
            })
    })
}
