//! Collector traceback cuts use the source path, never a Function code.

use pyo3::prelude::*;

fn entry_path(py: Python<'_>, entry: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let code = entry.getattr("tb_frame")?.getattr("f_code")?;
    let filename = code.getattr("co_filename")?.extract::<String>()?;
    super::super::code_path(py, filename)
}

pub(super) fn start(
    py: Python<'_>,
    collector: &Bound<'_, PyAny>,
    entries: &[Py<PyAny>],
) -> PyResult<usize> {
    if !collector.hasattr("path")? {
        return Ok(0);
    }
    let target = py
        .import("os")?
        .call_method1("fspath", (collector.getattr("path")?,))?;
    for (index, entry) in entries.iter().enumerate() {
        if entry_path(py, entry.bind(py))?
            .bind(py)
            .str()?
            .as_any()
            .eq(&target)?
        {
            if index != 0 {
                return Ok(index);
            }
            break;
        }
    }
    // Like Collector._traceback_filter, a cut that retained the original start
    // falls back to the real implementation package before hidden-frame filtering.
    let pathlib = py.import("pathlib")?;
    let path_type = pathlib.getattr("Path")?;
    let implementation = path_type
        .call1((py.import("vfy.independent.protocol")?.getattr("__file__")?,))?
        .getattr("parent")?
        .str()?;
    for (index, entry) in entries.iter().enumerate() {
        let path = entry_path(py, entry.bind(py))?;
        let path = path.bind(py);
        let mut excluded = false;
        if path.is_instance(&path_type)? {
            for parent in path.getattr("parents")?.try_iter()? {
                if parent?.str()?.as_any().eq(&implementation)? {
                    excluded = true;
                    break;
                }
            }
        }
        if !excluded {
            return Ok(index);
        }
    }
    Ok(0)
}
