//! Coverage data combination and Python adapters.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::semantic::SemanticEvidenceRecord;

use vfy_core::data::LineShift;

use super::{Arc, CoverageData, ReportInput};

#[cfg(test)]
mod tests;

#[cfg(test)]
pub(super) fn encode_lines(lines: &BTreeSet<i32>) -> Vec<u8> {
    vfy_core::encode_lines(lines.iter().copied())
}

fn data_error(path: &Path, error: &impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(format!(
        "failed to read Coverage.py schema-7 data from {}: {error}",
        path.display()
    ))
}

fn path_alias_search_paths(
    py: Python<'_>,
    aliases: &[Vec<String>],
    relative_files: bool,
) -> PyResult<Vec<PathBuf>> {
    if aliases.is_empty() || relative_files {
        return Ok(Vec::new());
    }
    Ok(py
        .import("sys")?
        .getattr("path")?
        .extract::<Vec<Option<PathBuf>>>()?
        .into_iter()
        .flatten()
        .collect())
}

/// Alias groups and filename policy shared by the combine and publish entry points.
fn alias_options(
    py: Python<'_>,
    aliases: Vec<Vec<String>>,
    relative_files: bool,
) -> PyResult<vfy_core::data::CombineOptions> {
    let path_alias_search_paths = path_alias_search_paths(py, &aliases, relative_files)?;
    Ok(vfy_core::data::CombineOptions {
        aliases,
        path_alias_mode: if relative_files {
            vfy_core::data::PathAliasMode::Relative
        } else {
            vfy_core::data::PathAliasMode::Absolute
        },
        path_alias_search_paths,
        ..vfy_core::data::CombineOptions::default()
    })
}

#[pymethods]
impl CoverageData {
    #[staticmethod]
    #[pyo3(signature = (has_arcs, files, tracers, metadata, semantic=None))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts one owned batch of mutable compatibility records."
    )]
    fn from_records(
        has_arcs: bool,
        files: Vec<ReportInput>,
        tracers: BTreeMap<String, String>,
        metadata: BTreeMap<String, String>,
        semantic: Option<PyRef<'_, Self>>,
    ) -> PyResult<Self> {
        Self::from_materialized_records(has_arcs, files, tracers, metadata, semantic.as_deref())
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    #[new]
    fn new(has_arcs: bool) -> Self {
        Self::from(vfy_core::data::CoverageData::new(has_arcs))
    }

    #[staticmethod]
    fn read(path: &str) -> PyResult<Self> {
        let path = Path::new(path);
        Self::read_path(path).map_err(|error| data_error(path, &error))
    }

    #[staticmethod]
    #[pyo3(signature = (paths, output, base=None, keep=false, strict=false, include_output=false, aliases=Vec::new(), relative_files=false))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a bound pyclass reference by value."
    )]
    #[expect(
        clippy::too_many_arguments,
        clippy::fn_params_excessive_bools,
        reason = "PyO3 passes the batch flags positionally; relative_files maps directly into PathAliasMode"
    )]
    fn combine_transaction(
        py: Python<'_>,
        paths: Vec<String>,
        output: &str,
        base: Option<PyRef<'_, Self>>,
        keep: bool,
        strict: bool,
        include_output: bool,
        aliases: Vec<Vec<String>>,
        relative_files: bool,
    ) -> PyResult<(Option<Self>, usize)> {
        vfy_core::data::combine_transaction(
            paths,
            Path::new(output),
            base.as_deref().map(|data| data.inner.as_ref().clone()),
            vfy_core::data::CombineOptions {
                keep,
                strict,
                include_output,
                ..alias_options(py, aliases, relative_files)?
            },
        )
        .map(|(data, count)| (data.map(CoverageData::from), count))
        .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    /// Commit this in-memory shard with the discovered shards in one transaction.
    #[staticmethod]
    #[pyo3(signature = (incoming, paths, output, include_output=false, aliases=Vec::new(), relative_files=false))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a bound pyclass reference by value."
    )]
    fn publish_transaction(
        py: Python<'_>,
        incoming: PyRef<'_, Self>,
        paths: Vec<String>,
        output: &str,
        include_output: bool,
        aliases: Vec<Vec<String>>,
        relative_files: bool,
    ) -> PyResult<Self> {
        vfy_core::data::publish_transaction(
            incoming.inner.as_ref().clone(),
            paths,
            Path::new(output),
            vfy_core::data::CombineOptions {
                include_output,
                ..alias_options(py, aliases, relative_files)?
            },
        )
        .map(|(data, _count)| Self::from(data))
        .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    fn copy(&self) -> Self {
        self.clone()
    }

    fn serialize<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        self.inner
            .serialize()
            .map(|payload| PyBytes::new(py, &payload))
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    #[staticmethod]
    fn deserialize(payload: &[u8]) -> PyResult<Self> {
        vfy_core::data::CoverageData::deserialize(payload)
            .map(Self::from)
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a bound pyclass reference by value."
    )]
    fn with_measurements(&self, measurements: PyRef<'_, Self>) -> Self {
        Self::from(self.inner.with_measurements(&measurements.inner))
    }

    fn purge_files(&mut self, paths: Vec<String>) {
        std::sync::Arc::make_mut(&mut self.inner).purge_files(paths);
    }

    fn map_files(&self, paths: Vec<(String, String)>) -> PyResult<Self> {
        let mapped: BTreeMap<String, String> = paths.into_iter().collect();
        if mapped.keys().cloned().collect::<BTreeSet<_>>()
            != self.inner.measured_files().into_iter().collect()
        {
            return Err(PyValueError::new_err(
                "path mapping must include every measured file",
            ));
        }
        self.clone()
            .remapped_files(&mapped)
            .map_err(PyValueError::new_err)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a bound pyclass reference by value."
    )]
    fn merge(&mut self, other: PyRef<'_, Self>) -> PyResult<()> {
        self.merge_from(other.clone(), false)
            .map_err(PyValueError::new_err)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a bound pyclass reference by value."
    )]
    fn merge_replace(&mut self, other: PyRef<'_, Self>) -> PyResult<()> {
        self.merge_from(other.clone(), true)
            .map_err(PyValueError::new_err)
    }

    #[pyo3(signature = (aliases, relative_files=false))]
    fn remap_paths(
        &self,
        py: Python<'_>,
        aliases: Vec<Vec<String>>,
        relative_files: bool,
    ) -> PyResult<Self> {
        let mode = if relative_files {
            vfy_core::data::PathAliasMode::Relative
        } else {
            vfy_core::data::PathAliasMode::Absolute
        };
        let search_paths = path_alias_search_paths(py, &aliases, relative_files)?;
        let data = self.inner.clone();
        py.detach(move || {
            data.as_ref()
                .clone()
                .remap_paths_with_mode(aliases, mode, search_paths)
        })
        .map(Self::from)
        .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    #[pyo3(signature = (aliases, relative_files=false))]
    fn remap_paths_for_reporting(
        &self,
        py: Python<'_>,
        aliases: Vec<Vec<String>>,
        relative_files: bool,
    ) -> PyResult<Self> {
        let mode = if relative_files {
            vfy_core::data::PathAliasMode::Relative
        } else {
            vfy_core::data::PathAliasMode::Absolute
        };
        let search_paths = path_alias_search_paths(py, &aliases, relative_files)?;
        let data = self.inner.clone();
        py.detach(move || {
            data.as_ref()
                .clone()
                .remap_paths_for_reporting(aliases, mode, search_paths)
        })
        .map(Self::from)
        .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    pub(crate) fn select_files(&self, paths: Vec<String>) -> Self {
        Self::from(self.inner.select_files(paths))
    }

    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    fn has_tracers(&self) -> bool {
        self.inner.has_tracers()
    }

    fn lines_for(&self, path: &str) -> Option<Vec<i32>> {
        self.inner.lines_for(path)
    }

    fn arcs_for(&self, path: &str) -> Option<Vec<Arc>> {
        self.inner.arcs_for(path)
    }

    pub(crate) fn set_metadata(&mut self, key: &str, value: &str) {
        if self.inner.metadata().get(key).map(String::as_str) == Some(value) {
            // An unchanged value must not copy a store shared with other handles.
            return;
        }
        std::sync::Arc::make_mut(&mut self.inner).set_metadata(key, value);
    }

    /// Reconstruct coverage from a saved baseline and fresh evidence.
    ///
    /// Replay consumes the owned store read from `baseline`. Neither the
    /// baseline file nor `fresh` is modified.
    #[staticmethod]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts owned replay inputs at the boundary."
    )]
    #[pyo3(signature = (baseline, fresh, invalidated_tests, changed_paths, scoped_paths=BTreeMap::new()))]
    fn replay_baseline(
        baseline: &str,
        fresh: PyRef<'_, Self>,
        invalidated_tests: Vec<String>,
        changed_paths: Vec<String>,
        scoped_paths: BTreeMap<String, Vec<(i32, i32, i32)>>,
    ) -> PyResult<Self> {
        let scoped_paths = scoped_paths
            .into_iter()
            .map(|(path, shifts)| {
                let shifts = shifts
                    .into_iter()
                    .map(|(old_start, old_count, new_count)| LineShift {
                        old_start,
                        old_count,
                        new_count,
                    })
                    .collect();
                (path, shifts)
            })
            .collect();
        let path = Path::new(baseline);
        vfy_core::data::CoverageData::read_path(path)
            .map_err(|error| data_error(path, &error))?
            .replay_merge(
                fresh.inner.as_ref().clone(),
                invalidated_tests,
                &changed_paths,
                &scoped_paths,
            )
            .map(Self::from)
            .map_err(PyValueError::new_err)
    }

    pub(crate) fn touch_file(&mut self, path: &str) {
        std::sync::Arc::make_mut(&mut self.inner).touch_file(path);
    }

    fn touch_files(&mut self, paths: Vec<String>, tracer: &str) -> PyResult<()> {
        self.update_checked(|inner| inner.touch_files(paths, tracer))
            .map_err(PyValueError::new_err)
    }

    fn add_file_tracers(&mut self, tracers: Vec<(String, String)>) -> PyResult<()> {
        self.update_checked(|inner| inner.add_file_tracers(tracers))
            .map_err(PyValueError::new_err)
    }

    pub(crate) fn set_tracer(&mut self, path: &str, tracer: &str) {
        std::sync::Arc::make_mut(&mut self.inner).set_tracer(path, tracer);
    }

    fn add_lines(&mut self, context: &str, path: &str, lines: Vec<i32>) {
        std::sync::Arc::make_mut(&mut self.inner).add_lines(context, path, lines);
    }

    fn add_arcs(&mut self, context: &str, path: &str, arcs: Vec<Arc>) {
        std::sync::Arc::make_mut(&mut self.inner).add_arcs(context, path, arcs);
    }

    #[pyo3(signature = (path, metadata=BTreeMap::new()))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the metadata mapping by value."
    )]
    fn write(&self, path: &str, metadata: BTreeMap<String, String>) -> PyResult<()> {
        let path = Path::new(path);
        self.write_path_with_metadata(path, &metadata)
            .map_err(|error| {
                PyValueError::new_err(format!(
                    "failed to write coverage data to {}: {error}",
                    path.display()
                ))
            })
    }

    #[getter]
    fn has_arcs(&self) -> bool {
        self.inner.branch_mode()
    }

    /// Whether lines or arcs were ever chosen for this data.
    #[getter]
    fn mode_is_known(&self) -> bool {
        self.inner.mode_is_known()
    }

    pub(super) fn measured_files(&self) -> Vec<String> {
        self.inner.measured_files()
    }

    fn measured_contexts(&self) -> Vec<String> {
        self.inner.measured_contexts()
    }

    fn metadata_records(&self) -> Vec<(String, String)> {
        self.inner.metadata_records()
    }

    fn tracer_records(&self) -> Vec<(String, String)> {
        self.inner.tracer_records()
    }

    pub(super) fn line_records(&self) -> Vec<(String, String, Vec<i32>)> {
        self.inner.line_records()
    }

    fn arc_records(&self) -> Vec<(String, String, Vec<Arc>)> {
        self.inner.arc_records()
    }

    fn has_semantic_data(&self) -> bool {
        self.inner.has_semantic_data()
    }

    #[expect(
        clippy::type_complexity,
        reason = "Coverage.py compatibility exposes semantic record tuples."
    )]
    fn semantic_records(&self) -> Vec<(String, String, i32, i32, String, String, String, String)> {
        self.inner.semantic_data().records()
    }

    fn semantic_observation_records(&self) -> Vec<(String, String, String, bool)> {
        self.inner.semantic_data().observation_records()
    }

    #[expect(
        clippy::type_complexity,
        reason = "Coverage.py compatibility exposes named path records."
    )]
    fn semantic_path_records(
        &self,
    ) -> Vec<(String, String, i32, i32, String, String, String, i32, i32)> {
        self.inner.semantic_data().path_records()
    }

    fn semantic_path_outcome_records(&self) -> Vec<(String, usize, String, i32, i32, i32, i32)> {
        self.inner.semantic_data().path_outcome_records()
    }

    fn semantic_path_observation_records(&self) -> Vec<(String, String, usize)> {
        self.inner.semantic_data().path_observation_records()
    }

    fn semantic_condition_records(&self) -> Vec<(String, usize, i32, i32, i32, i32)> {
        self.inner.semantic_data().condition_records()
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts owned vectors from Python."
    )]
    fn semantic_evidence_records(
        &self,
        selected: Vec<String>,
        contexts: Vec<String>,
    ) -> Vec<SemanticEvidenceRecord> {
        self.inner
            .semantic_data()
            .evidence_records(&selected, &contexts)
    }
}
