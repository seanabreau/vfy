//! `CPython` path resources for the canonical Rust mutation artifact transaction.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::path::PathBuf;
use vfy_core::mutation::{MutationOutput, MutationPublication};

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeMutationPublication {
    publication: MutationPublication,
}

impl NativeMutationPublication {
    fn resource(&self, kind: MutationOutput) -> PyResult<PathBuf> {
        let path = self.publication.staged(kind);
        path.map(std::path::Path::to_path_buf)
            .ok_or_else(|| PyRuntimeError::new_err("mutation artifact resource is unavailable"))
    }
}

#[pymethods]
impl NativeMutationPublication {
    #[new]
    #[pyo3(signature = (data_file, cache_file, baseline_file, report_file=None))]
    fn new(
        py: Python<'_>,
        data_file: PathBuf,
        cache_file: PathBuf,
        baseline_file: PathBuf,
        report_file: Option<PathBuf>,
    ) -> PyResult<Self> {
        let mut outputs = vec![
            (MutationOutput::Coverage, data_file),
            (MutationOutput::Cache, cache_file),
            (MutationOutput::Baseline, baseline_file),
        ];
        if let Some(report) = report_file {
            outputs.push((MutationOutput::Report, report));
        }
        py.detach(|| MutationPublication::new(&outputs))
            .map(|publication| Self { publication })
            .map_err(super::io_error)
    }

    #[getter]
    fn data_file(&self) -> PyResult<PathBuf> {
        self.resource(MutationOutput::Coverage)
    }

    #[getter]
    fn cache_file(&self) -> PyResult<PathBuf> {
        self.resource(MutationOutput::Cache)
    }

    #[getter]
    fn baseline_file(&self) -> PyResult<PathBuf> {
        self.resource(MutationOutput::Baseline)
    }

    #[getter]
    fn report_file(&self) -> Option<PathBuf> {
        self.publication
            .staged(MutationOutput::Report)
            .map(std::path::Path::to_path_buf)
    }

    fn baseline_valid(&mut self, py: Python<'_>, request: &str) -> PyResult<bool> {
        py.detach(|| self.publication.baseline_valid(request))
            .map_err(pyo3::exceptions::PyValueError::new_err)
    }

    fn write_baseline(&mut self, py: Python<'_>, request: &str) -> PyResult<()> {
        py.detach(|| self.publication.write_baseline(request))
            .map_err(pyo3::exceptions::PyOSError::new_err)
    }

    fn commit(&mut self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.publication.commit())
            .map_err(super::io_error)
    }
}
