//! Capture container comparison protocols before formatting their descriptions.

use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple,
};
use vfy_core::assertion_diagnostics::{
    ContainerComparison, DictionaryDetail, DictionaryEnding, ExtraItems, IndexDifference, Side,
    shorten_representation,
};

#[path = "mappings.rs"]
mod mappings;

fn sequence(value: &Bound<'_, PyAny>) -> bool {
    value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>()
}

fn set(value: &Bound<'_, PyAny>) -> bool {
    value.is_exact_instance_of::<PySet>() || value.is_exact_instance_of::<PyFrozenSet>()
}

pub(super) fn builtin(value: &Bound<'_, PyAny>) -> bool {
    sequence(value) || set(value) || value.is_exact_instance_of::<PyDict>()
}

fn supported(value: &Bound<'_, PyAny>, depth: usize, remaining: &mut usize) -> PyResult<bool> {
    if depth == 0 || *remaining == 0 {
        return Ok(false);
    }
    *remaining -= 1;
    if value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyBytes>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
    {
        return Ok(true);
    }
    if value.is_exact_instance_of::<PyDict>() {
        for (key, item) in value.cast::<PyDict>()?.iter() {
            if !supported(&key, depth - 1, remaining)? || !supported(&item, depth - 1, remaining)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if sequence(value) || set(value) {
        for item in value.try_iter()? {
            if !supported(&item?, depth - 1, remaining)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    Ok(false)
}

fn repr(callback: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    callback.call1((value,))?.extract()
}

fn summary(
    callback: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
    depth: usize,
) -> PyResult<String> {
    let (open, close, limit) = if value.is_exact_instance_of::<PyDict>() {
        ("{", "}", 4)
    } else if value.is_exact_instance_of::<PyList>() {
        ("[", "]", 6)
    } else if value.is_exact_instance_of::<PyTuple>() {
        ("(", ")", 6)
    } else if value.is_exact_instance_of::<PyFrozenSet>() {
        if value.is_empty()? {
            return Ok("frozenset()".to_owned());
        }
        ("frozenset({", "})", 6)
    } else if value.is_exact_instance_of::<PySet>() {
        if value.is_empty()? {
            return Ok("set()".to_owned());
        }
        ("{", "}", 6)
    } else {
        return Ok(shorten_representation(&repr(callback, value)?, 30));
    };
    if depth == 0 && !value.is_empty()? {
        return Ok(format!("{open}...{close}"));
    }
    let mut parts = Vec::new();
    if let Ok(dict) = value.cast::<PyDict>() {
        for (key, item) in dict.iter().take(limit) {
            parts.push(format!(
                "{}: {}",
                summary(callback, &key, depth.saturating_sub(1))?,
                summary(callback, &item, depth.saturating_sub(1))?
            ));
        }
    } else {
        let ordered;
        let elements = if set(value) {
            match value
                .py()
                .import("builtins")?
                .getattr("sorted")?
                .call1((value,))
            {
                Ok(items) => {
                    ordered = items;
                    &ordered
                }
                Err(error) if error.is_instance_of::<PyTypeError>(value.py()) => value,
                Err(error) => return Err(error),
            }
        } else {
            value
        };
        for item in elements.try_iter()?.take(limit) {
            parts.push(summary(callback, &item?, depth.saturating_sub(1))?);
        }
    }
    if value.len()? > limit {
        parts.push("...".to_owned());
    }
    let suffix = if value.is_exact_instance_of::<PyTuple>() && value.len()? == 1 {
        ","
    } else {
        ""
    };
    Ok(format!("{open}{}{suffix}{close}", parts.join(", ")))
}

fn sequence_comparison(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
) -> PyResult<ContainerComparison> {
    let left_repr = summary(callback, left, 6)?;
    let right_repr = summary(callback, right, 6)?;
    let left_len = left.len()?;
    let right_len = right.len()?;
    let common = left_len.min(right_len);
    let mut difference = None;
    for index in 0..common {
        let left_value = left.get_item(index)?;
        let right_value = right.get_item(index)?;
        if !left_value.eq(&right_value)? {
            difference = Some(IndexDifference {
                index,
                left: repr(callback, &left_value)?,
                right: repr(callback, &right_value)?,
            });
            break;
        }
    }
    let extra = match left_len.cmp(&right_len) {
        std::cmp::Ordering::Greater => Some(ExtraItems {
            side: Side::Left,
            count: left_len - right_len,
            first: repr(callback, &left.get_item(common)?)?,
        }),
        std::cmp::Ordering::Less => Some(ExtraItems {
            side: Side::Right,
            count: right_len - left_len,
            first: repr(callback, &right.get_item(common)?)?,
        }),
        std::cmp::Ordering::Equal => None,
    };
    Ok(ContainerComparison::Sequence {
        left: left_repr,
        right: right_repr,
        difference,
        extra,
    })
}

fn one_item(
    callback: &Bound<'_, PyAny>,
    key: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let dict = PyDict::new(callback.py());
    dict.set_item(key, value)?;
    repr(callback, dict.as_any())
}

fn ordered_entries(
    dict: &Bound<'_, PyDict>,
    callback: &Bound<'_, PyAny>,
) -> PyResult<Vec<(String, String)>> {
    let py = dict.py();
    let sorted = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((dict.keys(),));
    match sorted {
        Ok(keys) => keys
            .try_iter()?
            .map(|key| {
                let key = key?;
                let value = dict.get_item(&key)?.ok_or_else(|| {
                    PyRuntimeError::new_err("dictionary changed while describing its comparison")
                })?;
                Ok((repr(callback, &key)?, repr(callback, &value)?))
            })
            .collect(),
        Err(error) if error.is_instance_of::<PyTypeError>(py) => {
            let mut entries = dict
                .iter()
                .map(|(key, value)| Ok((repr(callback, &key)?, repr(callback, &value)?)))
                .collect::<PyResult<Vec<_>>>()?;
            entries.sort_by(|left, right| left.0.cmp(&right.0));
            Ok(entries)
        }
        Err(error) => Err(error),
    }
}

fn dictionary_comparison(
    left: &Bound<'_, PyDict>,
    right: &Bound<'_, PyDict>,
    callback: &Bound<'_, PyAny>,
) -> PyResult<ContainerComparison> {
    let left_repr = summary(callback, left.as_any(), 6)?;
    let right_repr = summary(callback, right.as_any(), 6)?;
    let common = left
        .call_method0("keys")?
        .call_method1("__and__", (right.call_method0("keys")?,))?;
    let mut identical = 0;
    let mut different = Vec::new();
    for key in common.try_iter()? {
        let key = key?;
        let left_value = left.get_item(&key)?.ok_or_else(|| {
            PyRuntimeError::new_err("dictionary changed while describing its comparison")
        })?;
        let right_value = right.get_item(&key)?.ok_or_else(|| {
            PyRuntimeError::new_err("dictionary changed while describing its comparison")
        })?;
        if left_value.eq(&right_value)? {
            identical += 1;
        } else {
            different.push((
                one_item(callback, &key, &left_value)?,
                one_item(callback, &key, &right_value)?,
            ));
        }
    }
    let left_only = PyDict::new(left.py());
    for (key, value) in left.iter() {
        if !right.contains(&key)? {
            left_only.set_item(key, value)?;
        }
    }
    let right_only = PyDict::new(left.py());
    for (key, value) in right.iter() {
        if !left.contains(&key)? {
            right_only.set_item(key, value)?;
        }
    }
    let mut details = Vec::new();
    if identical != 0 {
        details.push(DictionaryDetail::Identical(identical));
    }
    if !different.is_empty() {
        details.push(DictionaryDetail::Different(different));
    }
    let left_only = ordered_entries(&left_only, callback)?;
    let right_only = ordered_entries(&right_only, callback)?;
    for (side, values) in [(Side::Left, left_only), (Side::Right, right_only)] {
        if !values.is_empty() {
            details.push(DictionaryDetail::Extra {
                side,
                count: values.len(),
                items: Some(values),
            });
        }
    }
    Ok(ContainerComparison::Dictionary {
        left: left_repr,
        right: right_repr,
        details,
        ending: DictionaryEnding::Hint,
    })
}

fn set_comparison(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
) -> PyResult<ContainerComparison> {
    let left_repr = summary(callback, left, 6)?;
    let right_repr = summary(callback, right, 6)?;
    let left_only = left
        .call_method1("difference", (right,))?
        .try_iter()?
        .map(|value| repr(callback, &value?))
        .collect::<PyResult<Vec<_>>>()?;
    let right_only = right
        .call_method1("difference", (left,))?
        .try_iter()?
        .map(|value| repr(callback, &value?))
        .collect::<PyResult<Vec<_>>>()?;
    Ok(ContainerComparison::Set {
        left: left_repr,
        right: right_repr,
        left_only,
        right_only,
    })
}

pub(super) fn describe(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
) -> PyResult<Option<ContainerComparison>> {
    let are_sequences = sequence(left) && sequence(right);
    let are_dicts = left.is_exact_instance_of::<PyDict>() && right.is_exact_instance_of::<PyDict>();
    let are_sets = set(left) && set(right);
    if !are_sequences
        && !are_dicts
        && !are_sets
        && mappings::is_mapping(left)?
        && mappings::is_mapping(right)?
    {
        let callback = left
            .py()
            .import("vfy.independent.imports")?
            .getattr("_safe_repr")?;
        return mappings::describe(left, right, &callback);
    }
    if !are_sequences && !are_dicts && !are_sets {
        return Ok(None);
    }
    if !supported(left, 32, &mut 10_000)? || !supported(right, 32, &mut 10_000)? {
        return Ok(None);
    }
    let callback = left
        .py()
        .import("vfy.independent.imports")?
        .getattr("_safe_repr")?;
    if are_sequences {
        sequence_comparison(left, right, &callback).map(Some)
    } else if are_dicts {
        dictionary_comparison(left.cast::<PyDict>()?, right.cast::<PyDict>()?, &callback).map(Some)
    } else {
        set_comparison(left, right, &callback).map(Some)
    }
}
