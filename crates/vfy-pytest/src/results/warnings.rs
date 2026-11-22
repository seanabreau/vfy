//! Preserve per-test warning rows and merge repeated worker session occurrences.

use super::NativeRunResult;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BTreeMap;
use vfy_core::test_execution::WarningRecord;

impl NativeRunResult {
    pub(crate) fn add_worker_session_warnings(
        &mut self,
        collection: Vec<WarningRecord>,
        finish: Vec<WarningRecord>,
    ) {
        self.worker_collection_warnings.push(collection);
        self.worker_finish_warnings.push(finish);
    }

    pub(crate) fn partition_worker_warnings(
        &mut self,
        collection_end: usize,
        finish_start: usize,
    ) -> PyResult<()> {
        if collection_end > finish_start || finish_start > self.record.warnings.len() {
            return Err(PyValueError::new_err(
                "worker warning phase boundaries are invalid",
            ));
        }
        let finish = self.record.warnings.split_off(finish_start);
        let cases = self.record.warnings.split_off(collection_end);
        let collection = std::mem::replace(&mut self.record.warnings, cases);
        self.worker_collection_warnings = vec![collection];
        self.worker_finish_warnings = vec![finish];
        Ok(())
    }

    pub(crate) fn worker_session_warning_records(
        &self,
    ) -> (Vec<WarningRecord>, Vec<WarningRecord>) {
        (
            self.worker_collection_warnings
                .iter()
                .flatten()
                .cloned()
                .collect(),
            self.worker_finish_warnings
                .iter()
                .flatten()
                .cloned()
                .collect(),
        )
    }

    pub(crate) fn merged_worker_warnings(
        &self,
        before: &[WarningRecord],
        after: &[WarningRecord],
    ) -> Vec<WarningRecord> {
        let mut records = before.to_vec();
        records.extend(merge_session_warnings(
            &self.worker_collection_warnings,
            before,
        ));
        records.extend_from_slice(&self.record.warnings);
        records.extend(merge_session_warnings(&self.worker_finish_warnings, after));
        records.extend_from_slice(after);
        records
    }
}

pub(super) fn merge_session_warnings(
    groups: &[Vec<WarningRecord>],
    controller: &[WarningRecord],
) -> Vec<WarningRecord> {
    let identity = |warning: &WarningRecord| {
        (
            warning.message.clone(),
            warning.filename.clone(),
            warning.lineno,
            warning.nodeid.clone(),
        )
    };
    let mut known = BTreeMap::new();
    for warning in controller {
        *known.entry(identity(warning)).or_insert(0usize) += 1;
    }
    let mut merged = Vec::new();
    for warnings in groups {
        let mut occurrences = BTreeMap::new();
        for warning in warnings {
            let key = identity(warning);
            let occurrence = occurrences.entry(key.clone()).or_insert(0usize);
            *occurrence += 1;
            if *occurrence > known.get(&key).copied().unwrap_or(0) {
                merged.push(warning.clone());
            }
        }
        // Repeated worker sessions preserve the largest occurrence count.
        // Warnings emitted by tests remain outside this session-only merge.
        for (key, count) in occurrences {
            let prior = known.entry(key).or_insert(0);
            *prior = (*prior).max(count);
        }
    }
    merged
}
