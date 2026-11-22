//! Captured reports retain logs without claiming preexisting user item sections.

use pyo3::prelude::*;
use pyo3::types::PyList;
use vfy_core::test_execution::{ExecutionPhase, PhasePayload};

#[derive(Default)]
pub(super) struct ItemReportSections {
    generated_logs: Vec<Py<PyAny>>,
}

impl ItemReportSections {
    pub(super) fn append(
        &mut self,
        item: &Bound<'_, PyAny>,
        phase: ExecutionPhase,
        payload: &PhasePayload,
    ) -> PyResult<()> {
        let add_report_section = pyo3::intern!(item.py(), "add_report_section");
        for (key, output) in [
            ("stdout", &payload.stdout),
            ("stderr", &payload.stderr),
            ("log", &payload.log),
        ] {
            let previous = if key == "log" && !output.is_empty() {
                // Do not extend user section lifetimes through the overridable callback.
                item.getattr("_report_sections")?
                    .cast_into::<PyList>()?
                    .iter()
                    .map(|section| section.as_ptr() as usize)
                    .collect::<Vec<_>>()
            } else {
                Vec::new()
            };
            item.call_method1(add_report_section, (phase.as_str(), key, output))?;
            if key == "log" && !output.is_empty() {
                self.record_log(item, phase, output, &previous)?;
            }
        }
        Ok(())
    }

    fn record_log(
        &mut self,
        item: &Bound<'_, PyAny>,
        phase: ExecutionPhase,
        expected: &str,
        previous: &[usize],
    ) -> PyResult<()> {
        let sections = item.getattr("_report_sections")?.cast_into::<PyList>()?;
        let Some(last) = sections.len().checked_sub(1) else {
            return Ok(());
        };
        let section = sections.get_item(last)?;
        if previous.contains(&(section.as_ptr() as usize)) {
            return Ok(());
        }
        let (when, key, content) = section.extract::<(String, String, String)>()?;
        if when == phase.as_str() && key == "log" && content == expected {
            self.generated_logs.push(section.unbind());
        }
        Ok(())
    }

    pub(super) fn release_logs(&self, item: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.generated_logs.is_empty() {
            return Ok(());
        }
        let sections = item.getattr("_report_sections")?.cast_into::<PyList>()?;
        for index in (0..sections.len()).rev() {
            let section = sections.get_item(index)?;
            if self
                .generated_logs
                .iter()
                .any(|owned| section.is(owned.bind(item.py())))
            {
                sections.del_item(index)?;
            }
        }
        Ok(())
    }
}
