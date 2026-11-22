//! Python handle for collected line, arc, and semantic observations.

use pyo3::prelude::*;

type Arc = (i32, i32);
type SourceRange = (i32, i32, i32, i32);

/// Python handle for collected line, arc, and semantic observations.
#[pyclass(module = "vfy._native")]
pub(crate) struct TraceStore(vfy_core::TraceStore);

#[pymethods]
impl TraceStore {
    pub(crate) const fn has_arcs(&self) -> bool {
        self.0.has_arcs()
    }

    #[new]
    pub(crate) fn new(branch: bool) -> Self {
        Self(vfy_core::TraceStore::new(branch))
    }

    pub(crate) fn add_line(&mut self, context: &str, path: &str, line: i32) {
        self.0.add_line(context, path, line);
    }

    pub(crate) fn add_arc(&mut self, context: &str, path: &str, start: i32, end: i32) {
        self.0.add_arc(context, path, start, end);
    }

    pub(crate) fn observe_semantic(
        &mut self,
        context: &str,
        path: &str,
        line: i32,
        column: i32,
        ordinal: usize,
        outcome: bool,
    ) {
        self.0
            .observe_semantic(context, path, line, column, ordinal, outcome);
    }

    pub(crate) fn register_semantic_path(&mut self, path: &str) -> PyResult<()> {
        self.0
            .register_semantic_path(path)
            .map_err(|error| match error {
                vfy_core::CoreError::ArtifactIo { .. } => {
                    pyo3::exceptions::PyOSError::new_err(error.to_string())
                }
                _ => pyo3::exceptions::PyValueError::new_err(error.to_string()),
            })
    }

    pub(crate) fn observe_path_branch(
        &mut self,
        context: &str,
        path: &str,
        code_id: usize,
        expression_range: SourceRange,
        expression_ordinal: usize,
        outcome: bool,
    ) {
        self.0.observe_path_branch(
            context,
            path,
            code_id,
            expression_range,
            expression_ordinal,
            outcome,
        );
    }

    pub(crate) fn flush_path_current(&mut self, context: &str, code_id: usize) {
        self.0.flush_path_current(context, code_id);
    }

    pub(crate) fn observe_path_line(
        &mut self,
        context: &str,
        path: &str,
        code_id: usize,
        line: i32,
    ) {
        self.0.observe_path_line(context, path, code_id, line);
    }

    pub(crate) fn observe_path_raise(
        &mut self,
        context: &str,
        path: &str,
        code_id: usize,
        line: i32,
        cancelled: bool,
    ) {
        self.0
            .observe_path_raise(context, path, code_id, line, cancelled);
    }

    pub(crate) fn finish_paths(&mut self, context: &str, code_id: usize, unwinding: bool) {
        self.0.finish_paths(context, code_id, unwinding);
    }
    pub(crate) fn flush_semantic_current(&mut self) {
        self.0.flush_semantic_current();
    }
    pub(crate) fn start_frame(&mut self, code_id: usize) {
        self.0.start_frame(code_id);
    }
    pub(crate) fn resume_frame(&mut self, code_id: usize, suspended_line: Option<i32>) {
        self.0.resume_frame(code_id, suspended_line);
    }
    pub(crate) fn suspend_frame(&mut self, code_id: usize) {
        self.0.suspend_frame(code_id);
    }

    pub(crate) fn line_event(
        &mut self,
        context: &str,
        path: &str,
        code_id: usize,
        first_line: i32,
        line: i32,
    ) {
        self.0.line_event(context, path, code_id, first_line, line);
    }

    pub(crate) fn end_frame(
        &mut self,
        context: &str,
        path: Option<&str>,
        code_id: usize,
        first_line: i32,
        return_line: Option<i32>,
    ) {
        self.0
            .end_frame(context, path, code_id, first_line, return_line);
    }

    fn pop_frame(&mut self, code_id: usize) -> Option<i32> {
        self.0.pop_frame(code_id)
    }
    fn clear_semantic_depth(&mut self, depth: usize) {
        self.0.clear_semantic_depth(depth);
    }
    fn flush_semantic_depth(&mut self, depth: usize) {
        self.0.flush_semantic_depth(depth);
    }
    pub(crate) fn clear_semantic_frame(&mut self, code_id: usize) {
        self.0.clear_semantic_frame(code_id);
    }

    #[getter]
    pub(crate) const fn branch(&self) -> bool {
        self.0.branch()
    }

    pub(crate) const fn sysmon_arcs(&self) -> bool {
        self.0.sysmon_arcs()
    }
    pub(crate) fn line_records(&self) -> Vec<(String, String, Vec<i32>)> {
        self.0.line_records()
    }
    pub(crate) fn arc_records(&self) -> Vec<(String, String, Vec<Arc>)> {
        self.0.arc_records()
    }
}
