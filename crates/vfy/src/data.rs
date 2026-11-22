//! Coverage data handles exposed through `PyO3`.

use std::path::Path;
use std::sync::Arc as Shared;

use pyo3::prelude::*;

use crate::semantic::SemanticData;

mod operations;

pub(crate) use vfy_core::data::{Arc, DataError, DataValidationError, ReportInput};

#[pyclass(module = "vfy._native", skip_from_py_object)]
#[derive(Clone)]
pub(crate) struct CoverageData {
    inner: Shared<vfy_core::data::CoverageData>,
}

impl CoverageData {
    fn update_checked<T, E>(
        &mut self,
        update: impl FnOnce(&mut vfy_core::data::CoverageData) -> Result<T, E>,
    ) -> Result<T, E> {
        if let Some(inner) = Shared::get_mut(&mut self.inner) {
            return update(inner);
        }
        let mut staged = self.inner.as_ref().clone();
        let result = update(&mut staged)?;
        self.inner = Shared::new(staged);
        Ok(result)
    }

    pub(crate) fn from_materialized_records(
        has_arcs: bool,
        files: Vec<ReportInput>,
        tracers: std::collections::BTreeMap<String, String>,
        metadata: std::collections::BTreeMap<String, String>,
        semantic: Option<&Self>,
    ) -> Result<Self, DataValidationError> {
        vfy_core::data::CoverageData::from_materialized_records(
            has_arcs,
            files,
            tracers,
            metadata,
            semantic.map(|data| data.inner.as_ref()),
        )
        .map(Self::from)
    }

    #[cfg(test)]
    pub(crate) fn from_trace_records(
        has_arcs: bool,
        lines: Vec<(String, String, Vec<i32>)>,
        arcs: Vec<(String, String, Vec<Arc>)>,
        tracers: Vec<(String, String)>,
        semantic: SemanticData,
    ) -> Self {
        Self::from(vfy_core::data::CoverageData::from_trace_records(
            has_arcs, lines, arcs, tracers, semantic,
        ))
    }

    pub(crate) fn from_trace(
        has_arcs: bool,
        lines: vfy_core::TraceLines,
        arcs: Vec<(String, String, Vec<Arc>)>,
        tracers: Vec<(String, String)>,
        semantic: SemanticData,
    ) -> Self {
        Self::from(vfy_core::data::CoverageData::from_trace(
            has_arcs, lines, arcs, tracers, semantic,
        ))
    }

    pub(crate) fn into_selected_files(self, paths: Vec<String>) -> Self {
        Self::from(Shared::unwrap_or_clone(self.inner).into_selected_files(paths))
    }

    pub(crate) fn read_path(path: &Path) -> Result<Self, rusqlite::Error> {
        vfy_core::data::CoverageData::read_path(path).map(Self::from)
    }

    #[cfg(test)]
    pub(crate) fn empty() -> Self {
        Self::from(vfy_core::data::CoverageData::empty())
    }

    pub(crate) fn merge_from(
        &mut self,
        other: Self,
        replace_changed: bool,
    ) -> Result<(), &'static str> {
        if self.inner.merge_yields(&other.inner) {
            self.inner = other.inner;
            return Ok(());
        }
        let other = Shared::unwrap_or_clone(other.inner);
        self.update_checked(|inner| inner.merge_from(other, replace_changed))
    }

    pub(crate) fn branch_mode(&self) -> bool {
        self.inner.branch_mode()
    }

    pub(crate) fn core_snapshot(&self) -> Shared<vfy_core::data::CoverageData> {
        Shared::clone(&self.inner)
    }

    pub(crate) fn semantic_data(&self) -> &SemanticData {
        self.inner.semantic_data()
    }

    pub(crate) fn metadata(&self) -> &std::collections::BTreeMap<String, String> {
        self.inner.metadata()
    }

    #[cfg(test)]
    pub(crate) fn report_inputs(&self) -> Vec<ReportInput> {
        self.inner.report_inputs()
    }

    #[cfg(test)]
    pub(crate) fn write_path(&self, path: &Path) -> Result<(), DataError> {
        self.inner.write_path(path)
    }

    pub(crate) fn write_path_with_metadata(
        &self,
        path: &Path,
        overrides: &std::collections::BTreeMap<String, String>,
    ) -> Result<(), DataError> {
        self.inner.write_path_with_metadata(path, overrides)
    }

    pub(crate) fn stamp_watch_identity(path: &Path, identity: &str) -> Result<(), DataError> {
        vfy_core::data::CoverageData::stamp_watch_identity(path, identity)
    }

    pub(crate) fn remapped_files(
        self,
        mapped: &std::collections::BTreeMap<String, String>,
    ) -> Result<Self, &'static str> {
        Shared::unwrap_or_clone(self.inner)
            .remapped_files(mapped)
            .map(Self::from)
    }
}

impl From<vfy_core::data::CoverageData> for CoverageData {
    fn from(inner: vfy_core::data::CoverageData) -> Self {
        Self {
            inner: Shared::new(inner),
        }
    }
}
