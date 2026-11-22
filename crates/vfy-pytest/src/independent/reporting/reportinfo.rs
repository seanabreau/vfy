//! Item location resolution for report locations and `reportinfo`.

use crate::module_cache::Module;
use crate::results::ReportLocation;
use pyo3::exceptions::{PyAttributeError, PyOSError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyString;

/// Unpacks exactly `N` items in iteration order, with the messages `CPython` raises for sequence unpacking.
pub(super) fn unpack<'py, const N: usize>(
    value: &Bound<'py, PyAny>,
) -> PyResult<[Bound<'py, PyAny>; N]> {
    let mut iter = value.try_iter()?;
    let items = iter.by_ref().take(N).collect::<PyResult<Vec<_>>>()?;
    let fields = <[Bound<'py, PyAny>; N]>::try_from(items).map_err(|items| {
        PyValueError::new_err(format!(
            "not enough values to unpack (expected {N}, got {})",
            items.len()
        ))
    })?;
    if let Some(extra) = iter.next() {
        extra?;
        return Err(PyValueError::new_err(format!(
            "too many values to unpack (expected {N})"
        )));
    }
    Ok(fields)
}

fn function_object<'py>(py: Python<'py>, item: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let inspect = Module::Inspect.get(py)?;
    let mut function = inspect.call_method1(
        pyo3::intern!(py, "unwrap"),
        (item.getattr(pyo3::intern!(py, "obj"))?,),
    )?;
    if function.is_instance(
        &Module::Functools
            .get(py)?
            .getattr(pyo3::intern!(py, "partial"))?,
    )? {
        function = function.getattr(pyo3::intern!(py, "func"))?;
    }
    if function.hasattr(pyo3::intern!(py, "place_as"))? {
        function = function.getattr(pyo3::intern!(py, "place_as"))?;
    }
    Ok(function)
}

pub(in crate::independent) fn function_code<'py>(
    py: Python<'py>,
    item: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    function_object(py, item)?.getattr(pyo3::intern!(py, "__code__"))
}

pub(in crate::independent) fn location(item: &Bound<'_, PyAny>) -> PyResult<ReportLocation> {
    item.getattr(pyo3::intern!(item.py(), "location"))?
        .extract()
}

fn item_domain(item: &Bound<'_, PyAny>) -> PyResult<String> {
    let module = {
        let nodeid = item.getattr(pyo3::intern!(item.py(), "nodeid"))?;
        let nodeid = nodeid.cast::<PyString>()?.to_str()?;
        nodeid
            .split_once("::")
            .map_or(nodeid, |(module, _)| module)
            .to_owned()
    };
    let mut names = Vec::new();
    let mut node = item.clone();
    while !node.is_none()
        && node
            .getattr(pyo3::intern!(item.py(), "nodeid"))?
            .cast::<PyString>()?
            .to_str()?
            != module
    {
        names.push(
            node.getattr(pyo3::intern!(item.py(), "name"))?
                .extract::<String>()?,
        );
        node = node.getattr(pyo3::intern!(item.py(), "parent"))?;
    }
    names.reverse();
    Ok(names.join("."))
}

/// Resolve a code filename using the shared public Code.path policy.
pub(super) fn code_path(py: Python<'_>, filename: String) -> PyResult<Py<PyAny>> {
    let path = if filename.is_empty() {
        filename.into_pyobject(py)?.into_any().unbind()
    } else {
        let resolved = (|| -> PyResult<Option<Py<PyAny>>> {
            let path = Module::Pathlib
                .get(py)?
                .getattr(pyo3::intern!(py, "Path"))?
                .call1((Module::Os
                    .get(py)?
                    .getattr(pyo3::intern!(py, "path"))?
                    .call_method1(pyo3::intern!(py, "abspath"), (&filename,))?,))?;
            let exists = path
                .call_method0(pyo3::intern!(py, "exists"))?
                .is_truthy()?;
            Ok(exists.then(|| path.unbind()))
        })();
        match resolved {
            Ok(Some(path)) => path,
            Ok(None) => filename.into_pyobject(py)?.into_any().unbind(),
            Err(error) if error.is_instance_of::<PyOSError>(py) => {
                filename.into_pyobject(py)?.into_any().unbind()
            }
            Err(error) => return Err(error),
        }
    };
    Ok(path)
}

/// Resolve the current function source through the public item's live fields.
#[pyfunction]
pub(crate) fn independent_item_reportinfo(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
) -> PyResult<(Py<PyAny>, i64, String)> {
    let info = item_reportinfo(py, item, ReportInfoPath::Public)?;
    Ok((info.path, info.line, info.domain))
}

#[derive(Clone, Copy)]
pub(in crate::independent) enum ReportInfoPath {
    Public,
    Location,
}

pub(in crate::independent) enum LocationNormalization {
    Required,
    Complete,
}

pub(in crate::independent) struct ItemReportInfo {
    pub(in crate::independent) path: Py<PyAny>,
    pub(in crate::independent) line: i64,
    pub(in crate::independent) domain: String,
    pub(in crate::independent) normalization: LocationNormalization,
}

pub(in crate::independent) fn item_reportinfo(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    projection: ReportInfoPath,
) -> PyResult<ItemReportInfo> {
    let function = function_object(py, item)?;
    let code = match function.getattr(pyo3::intern!(py, "__code__")) {
        Ok(code) => Some(code),
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => {
            match function
                .getattr(pyo3::intern!(py, "__call__"))
                .and_then(|call| call.getattr(pyo3::intern!(py, "__code__")))
            {
                Ok(code) => Some(code),
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => None,
                Err(error) => return Err(error),
            }
        }
        Err(error) => return Err(error),
    };
    if let Some(code) = code {
        let filename = code
            .getattr(pyo3::intern!(py, "co_filename"))?
            .extract::<String>()?;
        let (path, normalization) = match projection {
            ReportInfoPath::Location if std::path::Path::new(&filename).is_absolute() => (
                Module::Os
                    .get(py)?
                    .getattr(pyo3::intern!(py, "path"))?
                    .call_method1(pyo3::intern!(py, "abspath"), (filename,))?
                    .unbind(),
                LocationNormalization::Complete,
            ),
            ReportInfoPath::Public | ReportInfoPath::Location => {
                (code_path(py, filename)?, LocationNormalization::Required)
            }
        };
        let line = code
            .getattr(pyo3::intern!(py, "co_firstlineno"))?
            .extract::<i64>()?
            - 1;
        return Ok(ItemReportInfo {
            path,
            line,
            domain: item_domain(item)?,
            normalization,
        });
    }
    let inspect = Module::Inspect.get(py)?;
    let filename = inspect
        .call_method1("getsourcefile", (&function,))
        .and_then(|path| {
            if path.is_none() {
                inspect.call_method1("getfile", (&function,))
            } else {
                Ok(path)
            }
        });
    let filename = match filename {
        Ok(filename) if filename.is_truthy()? => filename,
        Ok(_) => {
            return Ok(ItemReportInfo {
                path: "".into_pyobject(py)?.into_any().unbind(),
                line: -1,
                domain: item_domain(item)?,
                normalization: LocationNormalization::Required,
            });
        }
        Err(error) if error.is_instance_of::<PyTypeError>(py) => {
            return Ok(ItemReportInfo {
                path: "".into_pyobject(py)?.into_any().unbind(),
                line: -1,
                domain: item_domain(item)?,
                normalization: LocationNormalization::Required,
            });
        }
        Err(error) => return Err(error),
    };
    let path = Module::Pathlib.get(py)?.getattr("Path")?.call1((Module::Os
        .get(py)?
        .getattr("path")?
        .call_method1("abspath", (&filename,))?,))?;
    let line = match inspect.call_method1("findsource", (&function,)) {
        Ok(source) => source.get_item(1)?.extract::<i64>()?,
        Err(error) if error.is_instance_of::<PyOSError>(py) => -1,
        Err(error) => return Err(error),
    };
    Ok(ItemReportInfo {
        path: path.unbind(),
        line,
        domain: item_domain(item)?,
        normalization: LocationNormalization::Required,
    })
}
