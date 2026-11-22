//! Batch ordinary first parametrizations into public callspecs.
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyComplex, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

/// Resolve callback-free numeric tables; leave duplicate repair to pytest.
fn unique_numeric_ids<'py>(
    sets: &Bound<'py, PyList>,
    parameter_set: &Bound<'py, PyAny>,
    columns: usize,
) -> PyResult<Option<Bound<'py, PyList>>> {
    // Reject the whole family before conversion. Even exact strings and bytes
    // can invoke customized codecs during pytest's ID escaping.
    for set in sets {
        if !set.get_type().is(parameter_set) {
            return Ok(None);
        }
        let fields = set.cast::<PyTuple>()?;
        if !fields.get_item(2)?.is_none() {
            return Ok(None);
        }
        let values = fields.get_item(0)?;
        let Ok(values) = values.cast_exact::<PyTuple>() else {
            return Ok(None);
        };
        if values.len() != columns
            || values.iter().any(|value| {
                !(value.is_none()
                    || value.is_exact_instance_of::<PyBool>()
                    || value.is_exact_instance_of::<PyInt>()
                    || value.is_exact_instance_of::<PyFloat>()
                    || value.is_exact_instance_of::<PyComplex>())
            })
        {
            return Ok(None);
        }
    }

    let result = PyList::empty(sets.py());
    let mut seen = std::collections::HashSet::with_capacity(sets.len());
    for set in sets {
        let values = set.cast::<PyTuple>()?.get_item(0)?;
        let mut id = String::new();
        for (column, value) in values.cast::<PyTuple>()?.iter().enumerate() {
            if column != 0 {
                id.push('-');
            }
            // CPython owns signed zero, NaN, complex and large integer output.
            let rendered = match value.str() {
                Ok(rendered) => rendered,
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyValueError>(sets.py()) => {
                    // Integer digit limits fail inside canonical ID generation;
                    // preserve pytest's exception context and traceback frames.
                    return Ok(None);
                }
                Err(error) => return Err(error),
            };
            id.push_str(rendered.to_str()?);
        }
        if !seen.insert(id.clone()) {
            // Only pure scalar conversions have run. The existing resolver
            // retains strict errors and suffix-collision behavior.
            return Ok(None);
        }
        result.append(id)?;
    }
    Ok(Some(result))
}

fn global<'py>(
    module: &Bound<'py, PyAny>,
    builtins: &Bound<'py, PyAny>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    match module.getattr(name) {
        Ok(value) => Ok(value),
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(module.py()) => {
            builtins.getattr(name)
        }
        Err(error) => Err(error),
    }
}

fn pair<'py>(value: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let mut iter = value.try_iter()?;
    let mut values = Vec::with_capacity(2);
    for count in 0..2 {
        let Some(value) = iter.next() else {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "not enough values to unpack (expected 2, got {count})"
            )));
        };
        values.push(value?);
    }
    if let Some(value) = iter.next() {
        value?;
        return Err(pyo3::exceptions::PyValueError::new_err(
            "too many values to unpack (expected 2)",
        ));
    }
    Ok((values.remove(0), values.remove(0)))
}

/// Dispatch without a Python frame between the caller and canonical pytest.
#[pyfunction]
#[pyo3(signature = (original, can_admit, standard, /, *args, **kwargs))]
pub(crate) fn parametrize_family(
    original: &Bound<'_, PyAny>,
    can_admit: &Bound<'_, PyAny>,
    standard: &Bound<'_, PyAny>,
    args: &Bound<'_, PyTuple>,
    kwargs: Option<&Bound<'_, PyDict>>,
) -> PyResult<Py<PyAny>> {
    let py = original.py();
    let kwargs = kwargs.cloned().unwrap_or_else(|| PyDict::new(py));
    if !args.is_empty() {
        let metafunc = args.get_item(0)?;
        if can_admit.call1((&metafunc,))?.is_truthy()?
            && try_parametrize_family(&metafunc, &args.get_slice(1, args.len()), &kwargs, standard)?
        {
            return Ok(py.None());
        }
    }
    Ok(original.call(args, Some(&kwargs))?.unbind())
}

/// Return false before invoking callbacks when the canonical path is required.
#[expect(
    clippy::too_many_lines,
    reason = "Keep callback phases and retained Python values in canonical order"
)]
fn try_parametrize_family(
    metafunc: &Bound<'_, PyAny>,
    args: &Bound<'_, PyTuple>,
    kwargs: &Bound<'_, PyDict>,
    admitted: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let py = metafunc.py();
    let python = py.import("_pytest.python")?;
    let builtins = py.import("builtins")?;
    if args.len() != 2 {
        return Ok(false);
    }
    for (key, value) in kwargs {
        let Ok(key) = key.cast_exact::<PyString>() else {
            return Ok(false);
        };
        match key.to_str()? {
            "indirect" if value.is(false.into_pyobject(py)?) => {}
            "scope" | "ids" if value.is_none() => {}
            "_param_mark" => {
                if !value.is_none()
                    && (!value.get_type().is(&python.getattr("Mark")?)
                        || !value.getattr("_param_ids_from")?.is_none())
                {
                    return Ok(false);
                }
            }
            _ => return Ok(false),
        }
    }
    let argument = args.get_item(0)?;
    let values = args.get_item(1)?;
    let mut rows = Vec::new();
    let numeric = values.get_type().is(&builtins.getattr("range")?);
    let mut argnames = argument.clone();
    let mut numeric_table_ids = false;
    if numeric {
        let Ok(argname) = argument.cast_exact::<PyString>() else {
            return Ok(false);
        };
        let Ok(name) = argname.to_str() else {
            return Ok(false);
        };
        if name.is_empty()
            || name == "request"
            || name.contains(',')
            || name.trim() != name
            || !matches!(values.len(), Ok(1..))
        {
            return Ok(false);
        }
        argnames = PyList::new(py, [argname])?.into_any();
    } else {
        if !(values.is_exact_instance_of::<PyList>() || values.is_exact_instance_of::<PyTuple>())
            || values.is_empty()?
        {
            return Ok(false);
        }
        if let Ok(name) = argument.cast_exact::<PyString>() {
            let Ok(name) = name.to_str() else {
                return Ok(false);
            };
            // Keep the reserved-name failure in the original pytest operation.
            if name.contains("request") {
                return Ok(false);
            }
            for row in values.try_iter()? {
                let row = row?;
                if !(row.is_none()
                    || row.is_exact_instance_of::<PyBool>()
                    || row.is_exact_instance_of::<PyInt>()
                    || row.is_exact_instance_of::<PyFloat>()
                    || row.is_exact_instance_of::<PyComplex>()
                    || row.is_exact_instance_of::<PyString>()
                    || row.is_exact_instance_of::<PyBytes>()
                    || row.is_exact_instance_of::<PyTuple>()
                    || row.is_exact_instance_of::<PyList>())
                {
                    return Ok(false);
                }
            }
        } else if let Ok(names) = argument.cast_exact::<PyTuple>() {
            if names.is_empty() {
                return Ok(false);
            }
            let mut seen = std::collections::HashSet::new();
            for name in names {
                let Ok(name) = name.cast_exact::<PyString>() else {
                    return Ok(false);
                };
                let Ok(name) = name.to_str() else {
                    return Ok(false);
                };
                if name.is_empty() || name == "request" || !seen.insert(name.to_owned()) {
                    return Ok(false);
                }
            }
            numeric_table_ids = values.is_exact_instance_of::<PyList>();
            for row in values.try_iter()? {
                let row = row?;
                if !(row.is_exact_instance_of::<PyTuple>() || row.is_exact_instance_of::<PyList>())
                    || row.len()? != names.len()
                {
                    return Ok(false);
                }
                numeric_table_ids &= row.is_exact_instance_of::<PyTuple>();
            }
        } else {
            return Ok(false);
        }
    }
    // Admission must not retain containers that callbacks can replace.
    {
        let calls = metafunc.getattr("_calls")?;
        let Ok(calls) = calls.cast_exact::<PyList>() else {
            return Ok(false);
        };
        if !calls.is_empty() {
            return Ok(false);
        }
        let directness = metafunc.getattr("_params_directness")?;
        let fixtures = metafunc.getattr("_arg2fixturedefs")?;
        let (Ok(directness), Ok(fixtures)) = (
            directness.cast_exact::<PyDict>(),
            fixtures.cast_exact::<PyDict>(),
        ) else {
            return Ok(false);
        };
        if directness
            .iter()
            .chain(fixtures.iter())
            .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
        {
            return Ok(false);
        }
    }
    let config = metafunc.getattr("config")?;
    if numeric
        && (!admitted.call1((&config,))?.is_truthy()?
            || !config
                .getattr("hook")?
                .getattr("pytest_make_parametrize_id")?
                .call_method0("get_hookimpls")?
                .is_empty()?)
    {
        return Ok(false);
    }

    // The generic continuation can reconstruct these private normalization
    // records without replaying range iteration or any validation callback.
    let parameter_set = python.getattr("ParameterSet")?;
    let tuple_new = py.get_type::<PyTuple>().getattr("__new__")?;
    let object_setattr = builtins.getattr("object")?.getattr("__setattr__")?;
    if numeric {
        for value in values.try_iter()? {
            rows.push(value?);
        }
    }
    // Preserve the original spelling: "value" wraps tuple-valued scalars,
    // while "value," and tuple argnames use tuple-style rows.
    let mut parametersets = if numeric {
        None
    } else {
        let options = PyDict::new(py);
        options.set_item("nodeid", metafunc.getattr("definition")?.getattr("nodeid")?)?;
        let normalized = parameter_set.call_method(
            "_for_parametrize",
            (&argument, &values, metafunc.getattr("function")?, &config),
            Some(&options),
        )?;
        argnames = normalized.get_item(0)?;
        Some(normalized.get_item(1)?.cast_exact::<PyList>()?.clone())
    };
    let scope = python.getattr("Scope")?.getattr("Function")?;
    let nodeid = metafunc.getattr("definition")?.getattr("nodeid")?;
    metafunc.call_method1("_validate_if_using_arg_names", (&argnames, false))?;
    let still_admitted = admitted.call1((&config,))?.is_truthy()?
        && config
            .getattr("hook")?
            .getattr("pytest_make_parametrize_id")?
            .call_method0("get_hookimpls")?
            .is_empty()?;
    let make_parametersets = || -> PyResult<Bound<'_, PyList>> {
        let result = PyList::empty(py);
        for row in &rows {
            let fields = PyTuple::new(
                py,
                [
                    if numeric {
                        PyTuple::new(py, [row])?.into_any()
                    } else {
                        row.clone()
                    },
                    PyTuple::empty(py).into_any(),
                    py.None().into_bound(py),
                ],
            )?;
            result.append(tuple_new.call1((&parameter_set, fields))?)?;
        }
        Ok(result)
    };
    let param_mark = kwargs.get_item("_param_mark")?;
    let ids_from = param_mark
        .as_ref()
        .filter(|mark| !mark.is_none())
        .map(|mark| mark.getattr("_param_ids_from"))
        .transpose()?
        .filter(|mark| !mark.is_none());
    let generated = ids_from
        .as_ref()
        .map(|mark| mark.getattr("_param_ids_generated"))
        .transpose()?;
    let table_ids = if numeric_table_ids && still_admitted && ids_from.is_none() {
        match &parametersets {
            Some(sets) => unique_numeric_ids(sets, &parameter_set, argnames.len()?)?,
            None => None,
        }
    } else {
        None
    };
    let ids = if numeric && still_admitted && ids_from.is_none() {
        PyList::new(
            py,
            rows.iter().map(Bound::str).collect::<PyResult<Vec<_>>>()?,
        )?
        .into_any()
    } else if let Some(ids) = table_ids {
        ids.into_any()
    } else {
        let sets = match parametersets.take() {
            Some(sets) => sets,
            None => make_parametersets()?,
        };
        let resolve_ids = metafunc.getattr("_resolve_parameter_set_ids")?;
        let options = PyDict::new(py);
        options.set_item("nodeid", metafunc.getattr("definition")?.getattr("nodeid")?)?;
        let ids = resolve_ids.call(
            (
                &argnames,
                generated
                    .as_ref()
                    .filter(|ids| !ids.is_none())
                    .cloned()
                    .unwrap_or_else(|| py.None().into_bound(py)),
                &sets,
            ),
            Some(&options),
        )?;
        parametersets = Some(sets);
        ids
    };
    if let Some(mark) = ids_from
        && generated.as_ref().is_none_or(Bound::is_none)
    {
        object_setattr.call1((mark, "_param_ids_generated", &ids))?;
    }
    let directions = python.getattr("_resolve_args_directness")?.call1((
        &argnames,
        false,
        metafunc.getattr("definition")?.getattr("nodeid")?,
    ))?;
    metafunc
        .getattr("_params_directness")?
        .call_method1("update", (&directions,))?;
    for argname in argnames.try_iter()? {
        let argname = argname?;
        if !directions.get_item(&argname)?.eq("indirect")? {
            let fixture_constructor = python.getattr("DirectParamFixtureDef")?;
            let fixture_args = PyDict::new(py);
            fixture_args.set_item("node", metafunc.getattr("definition")?.getattr("session")?)?;
            fixture_args.set_item("argname", &argname)?;
            fixture_args.set_item("scope", &scope)?;
            let fixture = fixture_constructor.call((), Some(&fixture_args))?;
            metafunc
                .getattr("_arg2fixturedefs")?
                .set_item(&argname, PyList::new(py, [fixture])?)?;
        }
    }

    let current_calls = metafunc.getattr("_calls")?;
    let helpers_unchanged = admitted.call0()?.is_truthy()?;
    let mut ordinary_sets = true;
    if helpers_unchanged && !numeric && argument.is_exact_instance_of::<PyString>() {
        let mut seen = std::collections::HashSet::new();
        ordinary_sets = !argnames.is_empty()?;
        for name in argnames.try_iter()? {
            let name = name?;
            let Ok(name) = name.cast_exact::<PyString>() else {
                ordinary_sets = false;
                break;
            };
            let Ok(name) = name.to_str() else {
                ordinary_sets = false;
                break;
            };
            if !seen.insert(name.to_owned()) {
                ordinary_sets = false;
                break;
            }
        }
    }
    if helpers_unchanged && let Some(sets) = &parametersets {
        for set in sets {
            if !set.get_type().is(&parameter_set) || !set.getattr("id")?.is_none() {
                ordinary_sets = false;
                break;
            }
            let values = set.getattr("values")?;
            let marks = set.getattr("marks")?;
            if !values.is_exact_instance_of::<PyTuple>()
                || values.len()? != argnames.len()?
                || !(marks.is_exact_instance_of::<PyTuple>()
                    || marks.is_exact_instance_of::<PyList>())
                || !marks.is_empty()?
            {
                ordinary_sets = false;
                break;
            }
        }
    }
    let ordinary_ids = numeric
        || ids.cast_exact::<PyList>().is_ok_and(|ids| {
            parametersets
                .as_ref()
                .is_some_and(|sets| ids.len() == sets.len())
                && ids.iter().all(|id| id.is_exact_instance_of::<PyString>())
        });
    if !helpers_unchanged
        || (numeric && parametersets.is_some())
        || !ordinary_sets
        || !ordinary_ids
        || !current_calls.is_exact_instance_of::<PyList>()
        || !current_calls.is_empty()?
    {
        // Callback changes continue from completed validation/ID resolution.
        // Do not restart parametrize or repeat already-observed callbacks.
        let sets = match parametersets {
            Some(sets) => sets,
            None => make_parametersets()?,
        };
        let parents = if current_calls.is_truthy()? {
            current_calls
        } else {
            PyList::new(py, [python.getattr("CallSpec2")?.call0()?])?.into_any()
        };
        let newcalls = PyList::empty(py);
        for callspec in parents.try_iter()? {
            let callspec = callspec?;
            let zipped = PyDict::new(py);
            zipped.set_item("strict", true)?;
            let pairs = global(&python, &builtins, "zip")?.call((&ids, &sets), Some(&zipped))?;
            let rows = global(&python, &builtins, "enumerate")?.call1((pairs,))?;
            for row in rows.try_iter()? {
                let (index, entry) = pair(&row?)?;
                let (id, parameterset) = pair(&entry)?;
                // Python binds the method before evaluating values and marks.
                let setmulti = callspec.getattr("setmulti")?;
                let callargs = PyDict::new(py);
                callargs.set_item("argnames", &argnames)?;
                callargs.set_item("valset", parameterset.getattr("values")?)?;
                callargs.set_item("id", id)?;
                callargs.set_item("marks", parameterset.getattr("marks")?)?;
                callargs.set_item("scope", &scope)?;
                callargs.set_item("param_index", index)?;
                callargs.set_item("nodeid", &nodeid)?;
                let newcall = setmulti.call((), Some(&callargs))?;
                drop(setmulti);
                newcalls.append(newcall)?;
            }
        }
        metafunc.setattr("_calls", newcalls)?;
        return Ok(true);
    }
    // Canonical ParameterSets own table values through callbacks. Read their
    // final rows only after callbacks finish, without pinning discarded values.
    let rows = match &parametersets {
        Some(sets) if !numeric => sets
            .iter()
            .map(|set| set.getattr("values"))
            .collect::<PyResult<Vec<_>>>()?,
        _ => rows,
    };
    let ids = ids.cast_exact::<PyList>()?;
    let constructor = python.getattr("CallSpec2")?;
    let lazy = py.import("vfy._pytest.lazy_function")?;
    let inventory = lazy.call_method1("direct_parameter_inventory", (&config,))?;
    if !inventory.is_none() {
        let family = crate::dense_callspec::DenseCallSpecFamily::first_table(
            py,
            &argnames,
            &rows,
            ids,
            if numeric {
                crate::dense_callspec::FirstTableFormat::Numeric
            } else {
                crate::dense_callspec::FirstTableFormat::TupleRows
            },
            &scope,
        )?;
        let newcalls =
            crate::deferred_function::NativeDeferredFunctionInventory::first_table_callspecs(
                inventory
                    .cast::<crate::deferred_function::NativeDeferredFunctionInventory>()?
                    .clone()
                    .unbind(),
                py,
                family,
                &constructor,
                &lazy.getattr("_callspec_allocator")?,
            )?;
        if let Some(newcalls) = newcalls {
            metafunc.setattr("_calls", newcalls)?;
            return Ok(true);
        }
    }
    let newcalls = PyList::empty(py);
    for (index, (row, id)) in rows.iter().zip(ids.iter()).enumerate() {
        let index = index.into_pyobject(py)?;
        let params = PyDict::new(py);
        let indices = PyDict::new(py);
        let scopes = PyDict::new(py);
        for (column, argname) in argnames.try_iter()?.enumerate() {
            let argname = argname?;
            let value = if numeric {
                row.clone()
            } else {
                row.get_item(column)?
            };
            params.set_item(&argname, value)?;
            indices.set_item(&argname, &index)?;
            scopes.set_item(&argname, &scope)?;
        }
        let fields = PyDict::new(py);
        fields.set_item("params", params)?;
        fields.set_item("indices", indices)?;
        fields.set_item("_arg2scope", scopes)?;
        fields.set_item("_idlist", PyList::new(py, [id])?)?;
        fields.set_item("marks", PyList::empty(py))?;
        newcalls.append(constructor.call((), Some(&fields))?)?;
    }

    metafunc.setattr("_calls", newcalls)?;
    Ok(true)
}
