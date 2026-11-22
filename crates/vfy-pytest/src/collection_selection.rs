//! Parameter selection for collection arguments that name one parametrization.

use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyList, PyTuple};

struct CollectionArgument {
    path: String,
    parts: Vec<String>,
    parametrization: Option<String>,
}

fn parameter_indices(
    arguments: &[CollectionArgument],
    path: &str,
    ancestors: &[&str],
    name: &str,
    ids: &[String],
) -> Option<Vec<usize>> {
    if arguments.is_empty() {
        return None;
    }
    let mut requested = HashSet::new();
    for argument in arguments {
        // Broad and overlapping selectors must retain pytest's complete collection.
        if argument.parts.is_empty() {
            return None;
        }
        if argument.path != path {
            continue;
        }
        if argument.parts.len() != ancestors.len() + 1 {
            return None;
        }
        if !argument
            .parts
            .iter()
            .zip(ancestors)
            .all(|(part, ancestor)| part == ancestor)
        {
            continue;
        }
        if argument.parts.last().map(String::as_str) != Some(name) {
            continue;
        }
        let parameter = argument.parametrization.as_deref()?;
        let id = parameter.strip_prefix('[')?.strip_suffix(']')?;
        requested.insert(id);
    }
    let single_request = if requested.len() == 1 {
        requested.iter().next().copied()
    } else {
        None
    };
    let mut found = HashSet::new();
    let mut indices = Vec::new();
    for (index, id) in ids.iter().enumerate() {
        let matches = match single_request {
            Some(request) => id.as_str() == request,
            None => requested.contains(id.as_str()),
        };
        if matches {
            if single_request.is_none() {
                found.insert(id.as_str());
            }
            indices.push(index);
        }
    }
    // A missing case must reach pytest with the full family for its diagnostics.
    let all_found = if single_request.is_some() {
        !indices.is_empty()
    } else {
        found.len() == requested.len()
    };
    all_found.then_some(indices)
}

fn collection_argument(value: &Bound<'_, PyAny>) -> PyResult<Option<CollectionArgument>> {
    let (path, mut parts, parametrization) = if let Ok(tuple) = value.cast::<PyTuple>() {
        if tuple.len() != 2 {
            return Ok(None);
        }
        let path = tuple.get_item(0)?;
        let mut parts: Vec<String> = tuple.get_item(1)?.extract()?;
        let parametrization = parts.last_mut().and_then(|last| {
            last.find('[').map(|position| {
                let parameter = last[position..].to_owned();
                last.truncate(position);
                parameter
            })
        });
        (path, parts, parametrization)
    } else {
        if !value.hasattr("path")? || !value.hasattr("parts")? {
            return Ok(None);
        }
        let path = value.getattr("path")?;
        let parts: Vec<String> = value.getattr("parts")?.extract()?;
        let parametrization = if value.hasattr("parametrization")? {
            value
                .getattr("parametrization")?
                .extract::<Option<String>>()?
        } else {
            None
        };
        (path, parts, parametrization)
    };
    // Retain pytest's lexical absolute paths; resolving symlinks changes node identity.
    let path = path.call_method0("__fspath__")?.extract()?;
    if parametrization.is_none()
        && let Some(last) = parts.last_mut()
        && let Some(position) = last.find('[')
    {
        let parametrization = Some(last[position..].to_owned());
        last.truncate(position);
        return Ok(Some(CollectionArgument {
            path,
            parts,
            parametrization,
        }));
    }
    Ok(Some(CollectionArgument {
        path,
        parts,
        parametrization,
    }))
}

/// Python checks hook and lifecycle eligibility; pytest still owns final matching.
/// `None` means use complete expansion; returned indices retain declaration order.
#[pyfunction]
pub(crate) fn selected_parameter_indices(
    py: Python<'_>,
    collector: &Bound<'_, PyAny>,
    name: &str,
    callspecs: &Bound<'_, PyList>,
) -> PyResult<Option<Vec<usize>>> {
    let initial_parts = collector.getattr("session")?.getattr("_initial_parts")?;
    let mut arguments = Vec::new();
    for value in initial_parts.try_iter()? {
        let Some(argument) = collection_argument(&value?)? else {
            return Ok(None);
        };
        arguments.push(argument);
    }
    let callspec_type = py.import("_pytest.python")?.getattr("CallSpec2")?;
    let mut ids = Vec::with_capacity(callspecs.len());
    for callspec in callspecs.iter() {
        if !callspec.is_exact_instance(&callspec_type) {
            return Ok(None);
        }
        ids.push(callspec.getattr("id")?.extract::<String>()?);
    }
    let path: String = collector
        .getattr("path")?
        .call_method0("__fspath__")?
        .extract()?;
    let nodeid: String = collector.getattr("nodeid")?.extract()?;
    let ancestors: Vec<&str> = nodeid.split("::").skip(1).collect();
    Ok(parameter_indices(&arguments, &path, &ancestors, name, &ids))
}

#[cfg(test)]
mod tests;
