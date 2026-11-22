//! Report-input views over coverage data.

use std::collections::{BTreeMap, BTreeSet};

use super::{Arc, CoverageData, ReportInput};
use crate::evidence::{SparseArcs, SparseLines};
impl CoverageData {
    /// Return every measured file's merged evidence in path order.
    #[must_use]
    pub fn report_inputs(&self) -> Vec<ReportInput> {
        self.report_inputs_filtered(|_| true, |_, _| Ok::<bool, std::convert::Infallible>(true))
            .unwrap_or_else(|error| match error {})
    }

    /// Return each selected file's merged evidence without per-context copies.
    ///
    /// Lines match the context-preserving inputs: every recorded line plus each
    /// positive arc endpoint. Context lists are empty.
    pub(crate) fn report_union_inputs(
        &self,
        retain_path: impl Fn(&str) -> bool,
    ) -> Vec<ReportInput> {
        self.files
            .iter()
            .filter(|path| retain_path(path))
            .map(|path| {
                let mut lines = SparseLines::default();
                for values in self.lines.get(path).into_iter().flat_map(BTreeMap::values) {
                    lines.extend_set(values);
                }
                let mut arcs = SparseArcs::default();
                for values in self.arcs.get(path).into_iter().flat_map(BTreeMap::values) {
                    arcs.extend_set(values);
                }
                let arcs = arcs.sorted();
                lines.extend(
                    arcs.iter()
                        .flat_map(|(start, end)| [*start, *end])
                        .filter(|line| *line > 0),
                );
                (path.clone(), lines.sorted(), arcs, Vec::new(), Vec::new())
            })
            .collect()
    }

    pub(crate) fn report_inputs_filtered<E>(
        &self,
        retain_path: impl Fn(&str) -> bool,
        mut retain_context: impl FnMut(&str, &str) -> Result<bool, E>,
    ) -> Result<Vec<ReportInput>, E> {
        let mut records = Vec::new();
        for path in self.files.iter().filter(|path| retain_path(path)) {
            let mut context_lines = Vec::new();
            if let Some(contexts) = self.lines.get(path) {
                for (context, lines) in contexts {
                    if retain_context(path, context)? {
                        context_lines.push((context.to_string(), lines.sorted()));
                    }
                }
            }
            let mut context_arcs = Vec::new();
            if let Some(contexts) = self.arcs.get(path) {
                for (context, arcs) in contexts {
                    if retain_context(path, context)? {
                        context_arcs.push((context.to_string(), arcs.sorted()));
                    }
                }
            }
            let mut merged_context_lines: BTreeMap<String, BTreeSet<i32>> = context_lines
                .into_iter()
                .map(|(context, lines)| (context, lines.into_iter().collect()))
                .collect();
            for (context, arcs) in &context_arcs {
                merged_context_lines
                    .entry(context.clone())
                    .or_default()
                    .extend(
                        arcs.iter()
                            .flat_map(|(start, end)| [*start, *end])
                            .filter(|line| *line > 0),
                    );
            }
            let lines: BTreeSet<i32> = merged_context_lines
                .values()
                .flat_map(|values| values.iter().copied())
                .collect();
            let arcs: BTreeSet<Arc> = context_arcs
                .iter()
                .flat_map(|(_, values)| values.iter().copied())
                .collect();
            let context_lines = merged_context_lines
                .into_iter()
                .map(|(context, lines)| (context, lines.into_iter().collect()))
                .collect();
            records.push((
                path.clone(),
                lines.into_iter().collect(),
                arcs.into_iter().collect(),
                context_lines,
                context_arcs,
            ));
        }
        Ok(records)
    }
}
