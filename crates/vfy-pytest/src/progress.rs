//! Terminal progress for native pytest cases.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use vfy_core::test_execution::{
    CaseReportEvent, CaseResult, ExecutionOutcome, ExecutionPhase, SubtestResult,
};

pub(crate) struct Progress {
    terminal: Py<PyAny>,
    writer: Py<PyAny>,
    verbosity: i32,
    quiet_subtests: bool,
    style: String,
    collected: usize,
    completed: usize,
    path: String,
    failed: bool,
    status_written: bool,
    duration_ms: f64,
    verbose_prefix: Option<String>,
    order: CaseOrder,
    line_owner: LineOwner,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum LineOwner {
    Reporter,
    Progress,
}

/// Whether cases arrive in collection order or as parallel workers finish them.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum CaseOrder {
    Collection,
    /// Like pytest-xdist, progress omits file prefixes because files interleave.
    Completion,
}

#[derive(Clone, Copy)]
enum StatusSource<'a> {
    Phase,
    Subtest(&'a SubtestResult),
}

impl Progress {
    pub(crate) fn new(items: &Bound<'_, PyAny>, collected: usize) -> PyResult<Option<Self>> {
        let Some(first) = items.try_iter()?.next() else {
            return Ok(None);
        };
        let config = first?.getattr(intern!(items.py(), "config"))?;
        Self::from_config(&config, collected, CaseOrder::Collection)
    }

    pub(crate) fn from_config(
        config: &Bound<'_, PyAny>,
        collected: usize,
        order: CaseOrder,
    ) -> PyResult<Option<Self>> {
        let py = config.py();
        let terminal = config
            .getattr(intern!(py, "pluginmanager"))?
            .call_method1(intern!(py, "getplugin"), ("terminalreporter",))?;
        if terminal.is_none() {
            return Ok(None);
        }
        let line_owner = if order == CaseOrder::Completion
            && terminal.hasattr("_status_index")?
            && let Ok(index) = terminal
                .getattr("_status_index")?
                .cast_into::<crate::independent::NativeSessionReports>()
        {
            // Final worker projection records stats; this display owns its output.
            index.borrow_mut().mark_parallel_progress();
            LineOwner::Progress
        } else {
            LineOwner::Reporter
        };
        let verbosity = config
            .call_method1(intern!(py, "get_verbosity"), ("test_cases",))?
            .extract()?;
        let style = terminal
            .getattr(intern!(py, "_show_progress_info"))?
            .extract::<String>()
            .unwrap_or_default();
        Ok(Some(Self {
            writer: terminal.getattr(intern!(py, "_tw"))?.unbind(),
            terminal: terminal.unbind(),
            verbosity,
            quiet_subtests: config.call_method1("get_verbosity", ("subtests",))?.eq(0)?,
            style,
            collected,
            completed: 0,
            path: String::new(),
            failed: false,
            status_written: false,
            duration_ms: 0.0,
            verbose_prefix: None,
            order,
            line_owner,
        }))
    }

    pub(crate) fn start(&mut self, py: Python<'_>, nodeid: &str) -> PyResult<()> {
        self.status_written = false;
        if self.verbosity < 0 {
            self.terminal
                .bind(py)
                .setattr(intern!(py, "currentfspath"), nodeid)?;
            return Ok(());
        }
        if self.verbosity > 0 {
            self.duration_ms = 0.0;
            if self.line_owner == LineOwner::Reporter
                || self
                    .writer
                    .bind(py)
                    .getattr(intern!(py, "width_of_current_line"))?
                    .is_truthy()?
            {
                self.newline(py)?;
            }
            // The collected nodeid is already normalized by pytest. Keep the
            // same trailing-space prefix as TerminalReporter.logstart.
            let prefix = format!("{nodeid} ");
            self.write(py, &prefix, None)?;
            self.verbose_prefix = Some(prefix.clone());
            self.terminal
                .bind(py)
                .setattr(intern!(py, "currentfspath"), prefix)?;
        } else if self.verbosity == 0 && self.order == CaseOrder::Completion {
            if self.completed == 0 && self.line_owner == LineOwner::Reporter {
                self.newline(py)?;
            }
        } else if self.verbosity == 0 {
            let path = nodeid.split("::").next().unwrap_or(nodeid);
            if self.path != path {
                if !self.path.is_empty() {
                    self.progress(py)?;
                }
                self.newline(py)?;
                self.write(py, &format!("{path} "), None)?;
                path.clone_into(&mut self.path);
                self.duration_ms = 0.0;
            }
        }
        if self.verbosity <= 0 {
            self.terminal
                .bind(py)
                .setattr(intern!(py, "currentfspath"), nodeid)?;
        }
        self.writer.bind(py).call_method0(intern!(py, "flush"))?;
        Ok(())
    }

    pub(crate) fn start_writes(&self) -> bool {
        self.verbosity >= 0
    }

    pub(crate) fn case(&mut self, py: Python<'_>, case: &CaseResult) -> PyResult<()> {
        self.completed += 1;
        // The subtest hook reports progress from inside the unittest call, so
        // keep pytest's live completed-node set current between native cases.
        self.terminal
            .bind(py)
            .getattr(intern!(py, "_progress_nodeids_reported"))?
            .call_method1(intern!(py, "add"), (&case.nodeid,))?;
        if case.report_events().is_empty() {
            for phase in &case.phases {
                self.phase(py, case, phase)?;
            }
        } else {
            for event in case.report_events() {
                match event {
                    CaseReportEvent::Phase(index) => {
                        let phase = case.phases.get(index).ok_or_else(|| {
                            pyo3::exceptions::PyValueError::new_err(
                                "case phase event is outside its report array",
                            )
                        })?;
                        self.phase(py, case, phase)?;
                    }
                    CaseReportEvent::Subtest(index) => {
                        let subtest = case.subtests().get(index).ok_or_else(|| {
                            pyo3::exceptions::PyValueError::new_err(
                                "case subtest event is outside its report array",
                            )
                        })?;
                        self.status(
                            py,
                            &case.nodeid,
                            subtest.outcome,
                            subtest.exception.as_ref(),
                            StatusSource::Subtest(subtest),
                        )?;
                    }
                }
            }
        }
        if self.verbosity <= 0 && self.completed < self.collected && !self.style.is_empty() {
            let writer = self.writer.bind(py);
            let width = writer
                .getattr(intern!(py, "width_of_current_line"))?
                .extract::<usize>()?;
            let fullwidth = match self
                .terminal
                .bind(py)
                .getattr_opt(intern!(py, "_screen_width"))?
            {
                Some(width) => width.extract::<usize>()?,
                None => writer
                    .getattr(intern!(py, "fullwidth"))?
                    .extract::<usize>()?,
            };
            if width + self.message().len() + 1 >= fullwidth {
                self.write(py, &format!("{}\n", self.message()), Some(self.color()))?;
            }
        }
        Ok(())
    }

    fn phase(
        &mut self,
        py: Python<'_>,
        case: &CaseResult,
        phase: &vfy_core::test_execution::PhaseResult,
    ) -> PyResult<()> {
        self.duration_ms += phase.duration_ms;
        if phase.outcome == ExecutionOutcome::Passed && phase.phase != ExecutionPhase::Call {
            return Ok(());
        }
        self.status(
            py,
            &case.nodeid,
            phase.outcome,
            phase.exception.as_ref(),
            StatusSource::Phase,
        )
    }

    fn status(
        &mut self,
        py: Python<'_>,
        nodeid: &str,
        outcome: ExecutionOutcome,
        reason: Option<&vfy_core::test_execution::PythonText>,
        source: StatusSource<'_>,
    ) -> PyResult<()> {
        let (mut letter, mut word, color) = match outcome {
            ExecutionOutcome::Passed => (".", "PASSED", "green"),
            ExecutionOutcome::Failed => ("F", "FAILED", "red"),
            ExecutionOutcome::Error => ("E", "ERROR", "red"),
            ExecutionOutcome::Skipped => ("s", "SKIPPED", "yellow"),
            ExecutionOutcome::XFailed => ("x", "XFAIL", "yellow"),
            ExecutionOutcome::XPassed => ("X", "XPASS", "yellow"),
        };
        let subtest_status = match source {
            StatusSource::Phase => None,
            StatusSource::Subtest(subtest) => {
                crate::independent::subtests::status(outcome, &subtest.context, self.quiet_subtests)
            }
        };
        if matches!(source, StatusSource::Subtest(_)) {
            let Some(status) = &subtest_status else {
                return Ok(());
            };
            if status.category.is_empty() {
                return Ok(());
            }
            letter = status.letter;
            word = &status.word;
        }
        self.failed |= color == "red";
        if self.verbosity <= 0 {
            self.write(py, letter, Some(color))?;
        } else {
            let width = self
                .writer
                .bind(py)
                .getattr(intern!(py, "width_of_current_line"))?
                .extract::<usize>()?;
            let terminal = self.terminal.bind(py);
            let currentfspath = terminal.getattr(intern!(py, "currentfspath"))?;
            let forwarded_report_changed_prefix =
                self.verbose_prefix.as_ref().is_some_and(|prefix| {
                    match currentfspath.extract::<String>() {
                        Ok(current) => current != *prefix,
                        Err(_) => true,
                    }
                });
            if self.status_written || width == 0 || forwarded_report_changed_prefix {
                if width > 0 {
                    self.newline(py)?;
                }
                let prefix = self
                    .verbose_prefix
                    .as_deref()
                    .map_or_else(|| format!("{nodeid} "), str::to_owned);
                self.write(py, &prefix, None)?;
                if let Some(prefix) = self.verbose_prefix.as_deref() {
                    terminal.setattr(intern!(py, "currentfspath"), prefix)?;
                }
            }
            self.write(py, word, Some(color))?;
            if matches!(
                outcome,
                ExecutionOutcome::Skipped | ExecutionOutcome::XFailed | ExecutionOutcome::XPassed
            ) && let Some(reason) = reason
            {
                let reason = if outcome == ExecutionOutcome::Skipped {
                    reason.strip_prefix("Skipped: ").unwrap_or_else(|| {
                        if reason.as_scalar() == Some("Skipped") {
                            vfy_core::test_execution::PythonText::default()
                        } else {
                            reason.clone()
                        }
                    })
                } else {
                    reason
                        .strip_prefix("reason: ")
                        .unwrap_or_else(|| reason.clone())
                };
                if !reason.is_empty() {
                    let formatted = if self.verbosity < 2 {
                        self.trim_reason(py, &reason)?
                    } else {
                        Some(vfy_core::test_execution::PythonText::joined(&[
                            &" (".into(),
                            &reason,
                            &")".into(),
                        ]))
                    };
                    if let Some(formatted) = formatted {
                        self.write_python_text(py, &formatted)?;
                    }
                }
            }
            self.progress(py)?;
            self.status_written = true;
        }
        self.writer.bind(py).call_method0(intern!(py, "flush"))?;
        Ok(())
    }

    pub(crate) fn finish(&self, py: Python<'_>) -> PyResult<()> {
        if self.verbosity <= 0 && self.completed > 0 {
            self.progress(py)?;
        }
        if self.line_owner == LineOwner::Progress {
            // Close streaming output before captured worker output is replayed.
            // The independent summary must not close this same line a second time.
            if self
                .writer
                .bind(py)
                .getattr(intern!(py, "width_of_current_line"))?
                .is_truthy()?
            {
                self.newline(py)?;
            }
            self.writer.bind(py).call_method0(intern!(py, "flush"))?;
        }
        if self.verbosity < 0 || self.line_owner == LineOwner::Progress {
            // Pytest's quiet progress leaves no active path, so its summary
            // starts without a separating blank line.
            self.terminal
                .bind(py)
                .setattr(intern!(py, "currentfspath"), py.None())?;
        }
        // Subtest protocol callbacks can populate pytest's progress set. Rust
        // has already emitted the final indicator, so suppress the loop wrapper's duplicate.
        self.terminal
            .bind(py)
            .setattr(intern!(py, "_show_progress_info"), false)?;
        Ok(())
    }

    fn trim_reason(
        &self,
        py: Python<'_>,
        reason: &vfy_core::test_execution::PythonText,
    ) -> PyResult<Option<vfy_core::test_execution::PythonText>> {
        use vfy_core::test_execution::PythonText;
        let writer = self.writer.bind(py);
        let available = writer.getattr(intern!(py, "fullwidth"))?.extract::<i64>()?
            - writer
                .getattr(intern!(py, "width_of_current_line"))?
                .extract::<i64>()?
            - 8;
        if available < 6 {
            return Ok(None);
        }
        let points = reason.codepoints();
        let points = points
            .split(|point| *point == u32::from('\n'))
            .next()
            .unwrap_or_default();
        let reason = PythonText::from_codepoints(points.to_vec())
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        let width = |text: &PythonText| {
            crate::independent::reporting::writer::measure_width(
                &crate::reporting::python_text_to_python(py, text)?,
            )
        };
        let text = if width(&reason)? + 3 > available {
            let limit = usize::try_from(available - 3).unwrap_or_default();
            let mut points = reason.codepoints();
            points.truncate(limit);
            loop {
                let text = PythonText::from_codepoints(points.clone())
                    .map_err(pyo3::exceptions::PyValueError::new_err)?;
                if width(&text)? + 3 <= available - 3 {
                    break PythonText::joined(&[&text, &"...".into()]);
                }
                points.pop();
            }
        } else {
            reason
        };
        Ok(Some(PythonText::joined(&[
            &" (".into(),
            &text,
            &")".into(),
        ])))
    }

    fn write_python_text(
        &self,
        py: Python<'_>,
        text: &vfy_core::test_execution::PythonText,
    ) -> PyResult<()> {
        let kwargs = PyDict::new(py);
        self.writer.bind(py).call_method(
            intern!(py, "write"),
            (crate::reporting::python_text_to_python(py, text)?,),
            Some(&kwargs),
        )?;
        Ok(())
    }

    fn color(&self) -> &'static str {
        if self.failed { "red" } else { "green" }
    }

    fn message(&self) -> String {
        match self.style.as_str() {
            "count" => format!(
                " [{:width$}/{}]",
                self.completed,
                self.collected,
                width = self.collected.to_string().len()
            ),
            "times" => format_duration(self.duration_ms / 1_000.0),
            "progress" => format!(" [{:3}%]", self.completed * 100 / self.collected.max(1)),
            _ => String::new(),
        }
    }

    fn progress(&self, py: Python<'_>) -> PyResult<()> {
        let message = self.message();
        if message.is_empty() {
            return Ok(());
        }
        let writer = self.writer.bind(py);
        let width = writer
            .getattr(intern!(py, "width_of_current_line"))?
            .extract::<usize>()?;
        let fullwidth = writer
            .getattr(intern!(py, "fullwidth"))?
            .extract::<usize>()?;
        let padding = fullwidth.saturating_sub(width + 1 + message.len());
        self.write(
            py,
            &format!("{}{message}", " ".repeat(padding)),
            Some(self.color()),
        )
    }

    fn newline(&self, py: Python<'_>) -> PyResult<()> {
        self.writer.bind(py).call_method0(intern!(py, "line"))?;
        Ok(())
    }

    fn write(&self, py: Python<'_>, text: &str, color: Option<&str>) -> PyResult<()> {
        let kwargs = PyDict::new(py);
        if let Some(color) = color {
            kwargs.set_item(color, true)?;
        }
        self.writer
            .bind(py)
            .call_method(intern!(py, "write"), (text,), Some(&kwargs))?;
        Ok(())
    }
}

fn format_duration(seconds: f64) -> String {
    if seconds < 0.000_01 {
        format!(" {:.3}us", seconds * 1_000_000.0)
    } else if seconds < 0.000_1 {
        format!(" {:.2}us", seconds * 1_000_000.0)
    } else if seconds < 0.001 {
        format!(" {:.1}us", seconds * 1_000_000.0)
    } else if seconds < 0.01 {
        format!(" {:.3}ms", seconds * 1_000.0)
    } else if seconds < 0.1 {
        format!(" {:.2}ms", seconds * 1_000.0)
    } else if seconds < 1.0 {
        format!(" {:.1}ms", seconds * 1_000.0)
    } else if seconds < 60.0 {
        format!(" {seconds:.3}s")
    } else if seconds < 3_600.0 {
        format!(" {:.0}m {:.0}s", (seconds / 60.0).floor(), seconds % 60.0)
    } else {
        format!(
            " {:.0}h {:.0}m",
            (seconds / 3_600.0).floor(),
            ((seconds % 3_600.0) / 60.0).floor()
        )
    }
}

#[cfg(test)]
mod tests;
