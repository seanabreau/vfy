//! Report graph wire conversion follows the dataclass protocol boundary.

use super::unpack;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

fn asdict<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value
        .py()
        .import("dataclasses")?
        .call_method1("asdict", (value,))
}

fn serialize_entry<'py>(entry: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let data = asdict(entry)?.cast_into::<PyDict>()?;
    for item in data.call_method0("items")?.try_iter()? {
        let [key, value] = unpack::<2>(&item?)?;
        if value.hasattr("__dict__")? {
            data.set_item(key, asdict(&value)?)?;
        }
    }
    let result = PyDict::new(entry.py());
    result.set_item("type", entry.get_type().name()?)?;
    result.set_item("data", data)?;
    Ok(result)
}

fn serialize_traceback<'py>(traceback: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let result = asdict(traceback)?;
    let entries = PyList::empty(traceback.py());
    for entry in traceback.getattr("reprentries")?.try_iter()? {
        entries.append(serialize_entry(&entry?)?)?;
    }
    result.set_item("reprentries", entries)?;
    Ok(result)
}

fn serialize_crash<'py>(crash: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if crash.is_none() {
        Ok(crash.clone())
    } else {
        asdict(crash)
    }
}

/// Called once by the report serializer; never calls `source._to_json` itself.
pub(super) fn to_json(report: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = report.py();
    let representation = report.getattr("longrepr")?;
    if !representation.hasattr("toterminal")? {
        return Ok(representation.unbind());
    }
    if !representation.hasattr("reprtraceback")? || !representation.hasattr("reprcrash")? {
        return Ok(representation.str()?.into_any().unbind());
    }
    if representation.is_none() {
        return Err(pyo3::exceptions::PyAssertionError::new_err(""));
    }
    let result = PyDict::new(py);
    result.set_item(
        "reprcrash",
        serialize_crash(&representation.getattr("reprcrash")?)?,
    )?;
    result.set_item(
        "reprtraceback",
        serialize_traceback(&representation.getattr("reprtraceback")?)?,
    )?;
    result.set_item("sections", representation.getattr("sections")?)?;
    let chain_type = py
        .import("vfy.independent.diagnostics")?
        .getattr("ExceptionChainRepr")?;
    if representation.is_instance(&chain_type)? {
        let chain = PyList::empty(py);
        for node in representation.getattr("chain")?.try_iter()? {
            let [traceback, crash, description] = unpack::<3>(&node?)?;
            chain.append((
                serialize_traceback(&traceback)?,
                serialize_crash(&crash)?,
                description,
            ))?;
        }
        result.set_item("chain", chain)?;
    } else {
        result.set_item("chain", py.None())?;
    }
    Ok(result.into_any().unbind())
}

fn construct<'py>(
    types: &Bound<'py, PyModule>,
    name: &str,
    data: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    types.getattr(name)?.call((), Some(data.cast::<PyDict>()?))
}

fn deserialize_entry<'py>(
    types: &Bound<'py, PyModule>,
    entry: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let data = entry.get_item("data")?;
    let kind = entry.get_item("type")?;
    if kind.eq("ReprEntry")? {
        let mut args = None;
        let mut file = None;
        let mut locals = None;
        if data.get_item("reprfuncargs")?.is_truthy()? {
            args = Some(construct(
                types,
                "ReprFuncArgs",
                &data.get_item("reprfuncargs")?,
            )?);
        }
        if data.get_item("reprfileloc")?.is_truthy()? {
            file = Some(construct(
                types,
                "ReprFileLocation",
                &data.get_item("reprfileloc")?,
            )?);
        }
        if data.get_item("reprlocals")?.is_truthy()? {
            locals = Some(
                types
                    .getattr("ReprLocals")?
                    .call1((data.get_item("reprlocals")?.get_item("lines")?,))?,
            );
        }
        let kwargs = PyDict::new(types.py());
        kwargs.set_item("lines", data.get_item("lines")?)?;
        kwargs.set_item("reprfuncargs", args)?;
        kwargs.set_item("reprlocals", locals)?;
        kwargs.set_item("reprfileloc", file)?;
        kwargs.set_item("style", data.get_item("style")?)?;
        types.getattr("ReprEntry")?.call((), Some(&kwargs))
    } else if kind.eq("ReprEntryNative")? {
        types
            .getattr("ReprEntryNative")?
            .call1((data.get_item("lines")?,))
    } else {
        Err(crate::UnsupportedFeatureError::new_err(format!(
            "unknown traceback entry type {}",
            kind.str()?
        )))
    }
}

fn deserialize_traceback<'py>(
    types: &Bound<'py, PyModule>,
    data: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let entries = PyList::empty(types.py());
    for entry in data.get_item("reprentries")?.try_iter()? {
        entries.append(deserialize_entry(types, &entry?)?)?;
    }
    data.set_item("reprentries", entries)?;
    construct(types, "ReprTraceback", data)
}

fn deserialize_crash<'py>(
    types: &Bound<'py, PyModule>,
    data: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if data.is_none() {
        Ok(data.clone())
    } else {
        construct(types, "ReprFileLocation", data)
    }
}

/// Rebuild the graph at the same mutable report-dictionary boundary as pytest.
pub(super) fn from_json(data: &Bound<'_, PyDict>) -> PyResult<Py<PyAny>> {
    let py = data.py();
    let longrepr = data.as_any().get_item("longrepr")?;
    if !longrepr.is_truthy()?
        || !longrepr.contains("reprcrash")?
        || !longrepr.contains("reprtraceback")?
    {
        return Ok(longrepr.unbind());
    }
    let types = py.import("vfy.independent.diagnostics")?;
    let traceback = deserialize_traceback(&types, &longrepr.get_item("reprtraceback")?)?;
    let crash = deserialize_crash(&types, &longrepr.get_item("reprcrash")?)?;
    let result = if longrepr.get_item("chain")?.is_truthy()? {
        let chain = PyList::empty(py);
        for node in longrepr.get_item("chain")?.try_iter()? {
            let [traceback, crash, description] = unpack::<3>(&node?)?;
            chain.append((
                deserialize_traceback(&types, &traceback)?,
                deserialize_crash(&types, &crash)?,
                description,
            ))?;
        }
        types.getattr("ExceptionChainRepr")?.call1((chain,))?
    } else {
        let kwargs = PyDict::new(py);
        kwargs.set_item("reprtraceback", traceback)?;
        kwargs.set_item("reprcrash", crash)?;
        types
            .getattr("ReprExceptionInfo")?
            .call((), Some(&kwargs))?
    };
    for section in longrepr.get_item("sections")?.try_iter()? {
        // Starred positional arguments use the actual iterable, including length hints.
        let addsection = result.getattr("addsection")?;
        let args = py
            .get_type::<pyo3::types::PyTuple>()
            .call1((section?,))?
            .cast_into::<pyo3::types::PyTuple>()?;
        addsection.call1(args)?;
    }
    data.set_item("longrepr", &result)?;
    Ok(result.unbind())
}
