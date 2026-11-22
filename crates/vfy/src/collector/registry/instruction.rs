//! Avoid `CPython` 3.12's quadratic LINE lookup on large straight-line code.
//! Remove this module and its INSTRUCTION wiring when Python 3.12 support ends.

use pyo3::prelude::*;
use pyo3::types::PyBytes;

use super::{CodeRegistry, OpcodeSets, line_for_word_offset};

impl CodeRegistry {
    pub(in crate::collector) fn prepare_instruction_monitoring(
        &mut self,
        py: Python<'_>,
    ) -> PyResult<()> {
        self.opcodes = Some(OpcodeSets::new(py)?);
        Ok(())
    }

    pub(in crate::collector) fn prepare_instruction_lines(
        &mut self,
        code: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        // CPython 3.13 stores complete line deltas. Its LINE event has no
        // repeated linetable scan to replace with instruction callbacks.
        if code.py().version_info() >= (3, 13) {
            return Ok(false);
        }
        let code_id = Self::code_id(code);
        if let Some(lines) = self.instruction_lines.get(&code_id) {
            return Ok(lines.is_some());
        }
        let lines = self.instruction_lines_for_code(code)?;
        let enabled = lines.is_some();
        self.instruction_lines.insert(code_id, lines);
        Ok(enabled)
    }

    fn instruction_lines_for_code(
        &mut self,
        code: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Vec<Option<i32>>>> {
        let code_id = Self::code_id(code);
        let code_bytes = code.getattr("co_code")?.cast_into::<PyBytes>()?;
        let bytecode = code_bytes.as_bytes();
        if bytecode.len() < 16_384 {
            return Ok(None);
        }
        let flags: u32 = code.getattr("co_flags")?.extract()?;
        if flags & (0x20 | 0x80 | 0x200) != 0 || code.getattr("co_exceptiontable")?.len()? != 0 {
            return Ok(None);
        }
        // Jump targets need runtime predecessor tracking. Reject them before
        // retaining bytecode and dense line maps for the LINE fallback.
        if self.opcodes.as_ref().is_none_or(|opcodes| {
            bytecode
                .chunks_exact(2)
                .any(|instruction| opcodes.jumps.contains(&instruction[0]))
        }) {
            return Ok(None);
        }
        self.cache_bytecode(code)?;
        self.offset_line_table(code)?;
        let first_line = self.first_line(code)?;
        let Some(opcodes) = self.opcodes.as_ref() else {
            return Ok(None);
        };
        let Some(bytecode) = self.bytecodes.get(&code_id) else {
            return Ok(None);
        };
        let Some(lines) = self.offset_lines.get(&code_id) else {
            return Ok(None);
        };
        let mut event_lines = Vec::new();
        event_lines.try_reserve_exact(lines.len()).map_err(|_| {
            pyo3::exceptions::PyMemoryError::new_err("failed to allocate instruction line map")
        })?;
        event_lines.resize(lines.len(), None);
        let mut previous = None;
        let mut started = false;
        let mut instructions = 0_usize;
        let mut spilled_points = 0_usize;
        for (word, instruction) in bytecode.chunks_exact(2).enumerate() {
            let opcode = instruction[0];
            if opcodes.resume == Some(opcode) {
                // COPY_FREE_VARS and MAKE_CELL can precede the first RESUME.
                // CPython starts LINE instrumentation at that entry boundary.
                started = true;
                continue;
            }
            if !started {
                continue;
            }
            if opcodes.cache == Some(opcode) || opcodes.line_skips.contains(&opcode) {
                continue;
            }
            instructions += 1;
            let line = lines.get(word).copied().flatten();
            if line != previous
                && let Some(line) = line
            {
                event_lines[word] = Some(line);
                // 3.12's inline line delta falls back to PyCode_Addr2Line
                // outside this range, scanning the linetable from its start.
                let word_offset = i64::try_from(word).map_err(|_| {
                    pyo3::exceptions::PyOverflowError::new_err("Python bytecode is too large")
                })?;
                let delta = i64::from(line) - i64::from(first_line) - (word_offset >> 4);
                if !(-126..=127).contains(&delta) {
                    spilled_points += 1;
                }
            }
            previous = line;
        }
        // Bound both the avoided scans and the additional callback density.
        // Ordinary large functions retain the cheaper LINE event mask.
        if spilled_points < 1_024 || spilled_points < instructions.div_ceil(8) {
            return Ok(None);
        }
        Ok(Some(event_lines))
    }

    pub(in crate::collector) fn instruction_line(
        &self,
        code_id: usize,
        offset: i32,
    ) -> Option<i32> {
        self.instruction_lines
            .get(&code_id)
            .and_then(Option::as_ref)
            .and_then(|lines| line_for_word_offset(lines, offset))
    }
}
