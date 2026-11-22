//! Guarded stock Function construction after generation callbacks have completed.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyList, PyMappingProxy, PySet, PyString, PyTuple};

/// Revalidate after the Python context factory's return/profile callbacks.
/// Captured C calls bypass Python's `c_return` profile callbacks.
pub(crate) fn admitted(context: &Bound<'_, PyDict>) -> PyResult<bool> {
    let Some(guard) = context.get_item("native_guard")? else {
        return Ok(false);
    };
    let guard = guard.cast_exact::<PyDict>()?;
    let read = |name: &str| {
        guard.get_item(name)?.ok_or_else(|| {
            PyValueError::new_err(format!("missing Function batch admission {name}"))
        })
    };
    let helpers = read("helpers")?.cast_into_exact::<PyTuple>()?;
    let helper_type = read("helper_type")?;
    if helpers
        .iter()
        .any(|helper| !helper.is_exact_instance(&helper_type))
    {
        return Ok(false);
    }
    let get_referents = read("get_referents")?.cast_into_exact::<PyCFunction>()?;
    let referents = get_referents
        .call(&helpers, None)?
        .cast_into_exact::<PyList>()?;
    // An audit hook can enable GC or install tracing before traversal. Reject
    // immediately, before allocating mapping views or other tracked objects.
    if read("gc_enabled")?
        .cast_exact::<PyCFunction>()?
        .call0()?
        .extract::<bool>()?
        || !read("gettrace")?
            .cast_exact::<PyCFunction>()?
            .call0()?
            .is_none()
        || !read("getprofile")?
            .cast_exact::<PyCFunction>()?
            .call0()?
            .is_none()
    {
        return Ok(false);
    }
    let expected = read("referents")?.cast_into_exact::<PyTuple>()?;
    if referents.len() != expected.len()
        || referents
            .iter()
            .zip(expected.iter())
            .any(|(value, original)| !value.is(&original))
    {
        return Ok(false);
    }
    let cell_get = read("cell_get")?;
    for entry in read("cells")?.cast_exact::<PyTuple>()? {
        let entry = entry.cast_exact::<PyTuple>()?;
        let contents = match cell_get.call1((entry.get_item(0)?,)) {
            Ok(contents) => contents,
            Err(error) if error.is_instance_of::<PyValueError>(context.py()) => {
                return Ok(false);
            }
            Err(error) => return Err(error),
        };
        if !contents.is(entry.get_item(1)?) {
            return Ok(false);
        }
    }
    for entry in read("members")?.cast_exact::<PyTuple>()? {
        let entry = entry.cast_exact::<PyTuple>()?;
        if !entry
            .get_item(0)?
            .call1((entry.get_item(1)?,))?
            .is(entry.get_item(2)?)
        {
            return Ok(false);
        }
    }
    if !keyword_defaults_admitted(read("kwdefaults")?.cast_exact::<PyTuple>()?)? {
        return Ok(false);
    }
    let mro_get = read("mro_get")?;
    for entries in [read("mros")?, read("dynamic_mros")?] {
        for entry in entries.cast_exact::<PyTuple>()? {
            let entry = entry.cast_exact::<PyTuple>()?;
            if !mro_get.call1((entry.get_item(0)?,))?.is(entry.get_item(1)?) {
                return Ok(false);
            }
        }
    }
    let absent = read("absent")?;
    for entries in [read("maps")?, read("dynamic_maps")?] {
        for entry in entries.cast_exact::<PyTuple>()? {
            if !mapping_admitted(&entry, &absent)? {
                return Ok(false);
            }
        }
    }
    let marker_type = context
        .get_item("marker_type")?
        .ok_or_else(|| PyValueError::new_err("missing Function batch marker type"))?;
    let base_markers = context
        .get_item("base_markers")?
        .ok_or_else(|| PyValueError::new_err("missing Function batch base markers"))?
        .cast_into_exact::<PyList>()?;
    markers_admitted(&base_markers, &marker_type, &read("marker_dict_get")?)
}

fn keyword_defaults_admitted(entries: &Bound<'_, PyTuple>) -> PyResult<bool> {
    for entry in entries {
        let entry = entry.cast_exact::<PyTuple>()?;
        let values = entry.get_item(0)?.cast_into_exact::<PyDict>()?;
        let originals = entry.get_item(1)?.cast_into_exact::<PyTuple>()?;
        if values.len() != originals.len() {
            return Ok(false);
        }
        for ((key, value), original) in values.iter().zip(originals.iter()) {
            let original = original.cast_exact::<PyTuple>()?;
            if !key.is(original.get_item(0)?) || !value.is(original.get_item(1)?) {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

fn markers_admitted(
    base_markers: &Bound<'_, PyList>,
    marker_type: &Bound<'_, PyAny>,
    marker_dict_get: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    for marker in base_markers {
        if !marker.is_exact_instance(marker_type) {
            return Ok(false);
        }
        let raw = marker_dict_get
            .call1((marker,))?
            .cast_into_exact::<PyDict>()?;
        if raw
            .iter()
            .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
            || raw.contains("mark")?
            || !raw
                .get_item("name")?
                .is_some_and(|name| name.is_exact_instance_of::<PyString>())
        {
            return Ok(false);
        }
    }
    Ok(true)
}

fn mapping_admitted(entry: &Bound<'_, PyAny>, absent: &Bound<'_, PyAny>) -> PyResult<bool> {
    let entry = entry.cast_exact::<PyTuple>()?;
    let mapping = entry.get_item(0)?;
    let originals = entry.get_item(1)?.cast_into_exact::<PyTuple>()?;
    if let Ok(mapping) = mapping.cast_exact::<PyDict>() {
        if mapping
            .iter()
            .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
        {
            return Ok(false);
        }
        for original in originals {
            let original = original.cast_exact::<PyTuple>()?;
            let key = original.get_item(0)?;
            if !key.is_exact_instance_of::<PyString>() {
                return Ok(false);
            }
            let expected = original.get_item(1)?;
            match mapping.get_item(&key)? {
                Some(value) if value.is(&expected) && !expected.is(absent) => {}
                None if expected.is(absent) => {}
                _ => return Ok(false),
            }
        }
        return Ok(true);
    }
    let Ok(mapping) = mapping.cast_exact::<PyMappingProxy>() else {
        return Ok(false);
    };
    // Exact class mappingproxies wrap real type dictionaries. Check all keys
    // before hashing a string so foreign dictionary keys cannot run equality.
    if mapping
        .keys()?
        .iter()
        .any(|key| !key.is_exact_instance_of::<PyString>())
    {
        return Ok(false);
    }
    for original in originals {
        let original = original.cast_exact::<PyTuple>()?;
        let key = original.get_item(0)?;
        if !key.is_exact_instance_of::<PyString>() {
            return Ok(false);
        }
        let expected = original.get_item(1)?;
        if mapping.as_mapping().contains(&key)? {
            if expected.is(absent) || !mapping.get_item(&key)?.is(&expected) {
                return Ok(false);
            }
        } else if !expected.is(absent) {
            return Ok(false);
        }
    }
    Ok(true)
}

pub(crate) struct Case {
    pub(crate) callspec: Py<PyAny>,
    pub(crate) identifier: Py<PyAny>,
    pub(crate) has_ids: bool,
    pub(crate) markers: Vec<Py<PyAny>>,
}

/// These objects are captured builtins or exact stock types, admitted once by
/// the lifecycle adapter. No Python function or overridable lookup occurs in
/// the header loop. Source references stay alive until every header is complete.
#[expect(
    clippy::too_many_lines,
    reason = "the guarded constructor preserves pytest field initialization order in one allocation loop"
)]
pub(crate) fn build<'py>(
    py: Python<'py>,
    context: &Bound<'py, PyDict>,
    cases: &[Case],
) -> PyResult<Bound<'py, PyList>> {
    let read = |name: &str| {
        context.get_item(name)?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "missing Function batch context {name}"
            ))
        })
    };
    let allocator = read("allocator")?;
    let function_type = read("function_type")?;
    let keyword_type = read("keyword_type")?;
    let stash_type = read("stash_type")?;
    let request_type = read("request_type")?;
    let parent = read("parent")?;
    let config = read("config")?;
    let session = read("session")?;
    let path = read("path")?;
    let parent_id = read("parent_id")?;
    let original_name = read("name")?;
    let source = read("source")?;
    let test_class = read("test_class")?;
    let method_get = read("method_get")?;
    let fixture_info = read("fixture_info")?;
    let fixture_names = read("fixture_names")?;
    let fixture_defs = read("fixture_defs")?;
    let base_markers = read("base_markers")?.cast_into::<PyList>()?;
    let metadata = read("metadata")?.cast_into::<PyDict>()?;
    let subname_format = PyString::new(py, "{}[{}]").getattr(pyo3::intern!(py, "format"))?;
    let nodeid_format = PyString::new(py, "{}::{}").getattr(pyo3::intern!(py, "format"))?;
    let output = PyList::empty(py);
    for case in cases {
        let identifier = case.identifier.bind(py);
        let name = if case.has_ids {
            subname_format.call1((&original_name, identifier))?
        } else {
            original_name.clone()
        };
        let item = allocator.call1((&function_type,))?;
        // Node's seven scalar fields and _store are real C member slots.
        item.setattr(pyo3::intern!(py, "name"), &name)?;
        item.setattr(pyo3::intern!(py, "parent"), &parent)?;
        item.setattr(pyo3::intern!(py, "config"), &config)?;
        item.setattr(pyo3::intern!(py, "session"), &session)?;
        item.setattr(pyo3::intern!(py, "path"), &path)?;
        let raw = item
            .getattr(pyo3::intern!(py, "__dict__"))?
            .cast_into::<PyDict>()?;
        let keywords = allocator.call1((&keyword_type,))?;
        let marker_map = PyDict::new(py);
        marker_map.set_item(&name, true)?;
        keywords.setattr(pyo3::intern!(py, "node"), &item)?;
        keywords.setattr(pyo3::intern!(py, "parent"), &parent)?;
        keywords.setattr(pyo3::intern!(py, "_markers"), &marker_map)?;
        raw.set_item(pyo3::intern!(py, "keywords"), &keywords)?;
        let markers = PyList::empty(py);
        raw.set_item(pyo3::intern!(py, "own_markers"), &markers)?;
        raw.set_item(
            pyo3::intern!(py, "extra_keyword_matches"),
            PySet::empty(py)?,
        )?;
        item.setattr(
            pyo3::intern!(py, "_nodeid"),
            nodeid_format.call1((&parent_id, &name))?,
        )?;
        let stash = allocator.call1((&stash_type,))?;
        stash.setattr(pyo3::intern!(py, "_storage"), PyDict::new(py))?;
        raw.set_item(pyo3::intern!(py, "stash"), &stash)?;
        item.setattr(pyo3::intern!(py, "_store"), &stash)?;
        raw.set_item(pyo3::intern!(py, "_report_sections"), PyList::empty(py))?;
        raw.set_item(pyo3::intern!(py, "user_properties"), PyList::empty(py))?;
        raw.set_item(pyo3::intern!(py, "originalname"), &original_name)?;
        let instance = if test_class.is_none() {
            py.None().into_bound(py)
        } else {
            allocator.call1((&test_class,))?
        };
        let callable = if instance.is_none() {
            source.clone()
        } else {
            method_get.call1((&source, &instance, &test_class))?
        };
        raw.set_item(pyo3::intern!(py, "_instance"), &instance)?;
        raw.set_item(pyo3::intern!(py, "_obj"), &callable)?;
        for marker in base_markers
            .iter()
            .chain(case.markers.iter().map(|mark| mark.bind(py).clone()))
        {
            markers.append(&marker)?;
            marker_map.set_item(marker.getattr(pyo3::intern!(py, "name"))?, &marker)?;
        }
        raw.set_item(pyo3::intern!(py, "callspec"), case.callspec.bind(py))?;
        for (key, value) in &metadata {
            marker_map.set_item(key, value)?;
        }
        marker_map.set_item(identifier, true)?;
        raw.set_item(pyo3::intern!(py, "_fixtureinfo"), &fixture_info)?;
        raw.set_item(pyo3::intern!(py, "fixturenames"), &fixture_names)?;
        raw.set_item(pyo3::intern!(py, "funcargs"), PyDict::new(py))?;
        let request = allocator.call1((&request_type,))?;
        // Keep CPython's inline instance storage until a public observer asks
        // for the request mapping; these setters are guarded object C slots.
        request.setattr(pyo3::intern!(py, "fixturename"), py.None())?;
        request.setattr(pyo3::intern!(py, "_pyfuncitem"), &item)?;
        request.setattr(pyo3::intern!(py, "_arg2fixturedefs"), &fixture_defs)?;
        request.setattr(pyo3::intern!(py, "_fixture_defs"), PyDict::new(py))?;
        raw.set_item(pyo3::intern!(py, "_request"), &request)?;
        output.append(&item)?;
    }
    Ok(output)
}
