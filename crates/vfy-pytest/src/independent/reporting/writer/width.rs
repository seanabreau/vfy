//! Terminal column policy uses the running interpreter's Unicode database.

use crate::module_cache::Module;
use pyo3::exceptions::PyOverflowError;
use pyo3::prelude::*;
use pyo3::types::PyString;

fn character_width(character: &Bound<'_, PyAny>, database: &Bound<'_, PyAny>) -> PyResult<i64> {
    let code = character
        .py()
        .import("builtins")?
        .call_method1("ord", (character,))?
        .extract::<u32>()?;
    if (0x20..0x7f).contains(&code) {
        return Ok(1);
    }
    if code == 0
        || (0x200b..=0x200f).contains(&code)
        || (0x2028..=0x202e).contains(&code)
        || (0x2060..=0x2063).contains(&code)
    {
        return Ok(0);
    }
    let category = database.call_method1("category", (character,))?;
    if category.eq("Cc")? {
        return Ok(-1);
    }
    if category.eq("Me")? || category.eq("Mn")? {
        return Ok(0);
    }
    let east = database.call_method1("east_asian_width", (character,))?;
    Ok(if east.eq("F")? || east.eq("W")? { 2 } else { 1 })
}

pub(super) fn current(writer: &Bound<'_, PyAny>) -> PyResult<i64> {
    measure(&writer.getattr("_current_line")?)
}

pub(crate) fn measure(line: &Bound<'_, PyAny>) -> PyResult<i64> {
    let database = Module::UnicodeData.get(line.py())?;
    let normalized = database.call_method1("normalize", ("NFC", line))?;
    // Normalization stays live; only exact strings have callback-free iteration.
    if normalized.is_exact_instance_of::<PyString>()
        && let Ok(text) = normalized.cast::<PyString>()?.to_str()
        && text.bytes().all(|byte| (0x20..0x7f).contains(&byte))
    {
        return i64::try_from(text.len()).map_err(|_| {
            PyOverflowError::new_err("terminal column width exceeds the native integer range")
        });
    }
    let mut width = 0_i64;
    for character in normalized.try_iter()? {
        let next = character_width(&character?, database.as_any())?;
        if next < 0 {
            return Ok(-1);
        }
        width = width.checked_add(next).ok_or_else(|| {
            PyOverflowError::new_err("terminal column width exceeds the native integer range")
        })?;
    }
    Ok(width)
}
