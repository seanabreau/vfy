//! Capture actual Mapping protocols for the shared dictionary explanation.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet};
use vfy_core::assertion_diagnostics::{
    ContainerComparison, DictionaryDetail, DictionaryEnding, Side,
};

pub(super) fn is_mapping(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    value.is_instance(&value.py().import("collections.abc")?.getattr("Mapping")?)
}

fn supported(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    super::supported(value, 32, &mut 10_000)
}

fn extra(
    mapping: &Bound<'_, PyAny>,
    keys: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
    side: Side,
    details: &mut Vec<DictionaryDetail>,
) -> PyResult<()> {
    let count = keys.len()?;
    if count == 0 {
        return Ok(());
    }
    // pytest yields the heading before it reads or formats the extra values.
    details.push(DictionaryDetail::Extra {
        side,
        count,
        items: None,
    });
    let values = PyDict::new(mapping.py());
    for key in keys.try_iter()? {
        let key = key?;
        values.set_item(&key, mapping.get_item(&key)?)?;
    }
    let items = super::ordered_entries(&values, callback)?;
    if let Some(DictionaryDetail::Extra {
        items: captured, ..
    }) = details.last_mut()
    {
        *captured = Some(items);
    }
    Ok(())
}

fn differing(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
    common: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
    details: &mut Vec<DictionaryDetail>,
) -> PyResult<()> {
    let keys = PySet::empty(left.py())?;
    for key in common.try_iter()? {
        let key = key?;
        if left.get_item(&key)?.ne(right.get_item(&key)?)? {
            keys.add(key)?;
        }
    }
    if keys.is_empty() {
        return Ok(());
    }
    details.push(DictionaryDetail::Different(Vec::new()));
    for key in keys.iter() {
        let pair = (
            super::one_item(callback, &key, &left.get_item(&key)?)?,
            super::one_item(callback, &key, &right.get_item(&key)?)?,
        );
        if let Some(DictionaryDetail::Different(values)) = details.last_mut() {
            values.push(pair);
        }
    }
    Ok(())
}

fn mapping_details(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
    details: &mut Vec<DictionaryDetail>,
) -> PyResult<bool> {
    let constructor = left.py().get_type::<PySet>();
    let left_keys = constructor.call1((left,))?;
    let right_keys = constructor.call1((right,))?;
    if left_keys.len()? > 10_000 || right_keys.len()? > 10_000 {
        return Ok(false);
    }
    for keys in [&left_keys, &right_keys] {
        for key in keys.try_iter()? {
            if !supported(&key?)? {
                return Ok(false);
            }
        }
    }
    let common = left_keys.call_method1("intersection", (&right_keys,))?;
    let same = PyDict::new(left.py());
    for key in common.try_iter()? {
        let key = key?;
        let left_value = left.get_item(&key)?;
        let right_value = right.get_item(&key)?;
        if !supported(&left_value)? || !supported(&right_value)? {
            return Ok(false);
        }
        if left_value.eq(&right_value)? {
            // The reference comprehension reads and retains matching values again.
            same.set_item(&key, left.get_item(&key)?)?;
        }
    }
    if !same.is_empty() {
        details.push(DictionaryDetail::Identical(same.len()));
    }
    differing(left, right, &common, callback, details)?;
    extra(
        left,
        &left_keys.call_method1("difference", (&right_keys,))?,
        callback,
        Side::Left,
        details,
    )?;
    extra(
        right,
        &right_keys.call_method1("difference", (&left_keys,))?,
        callback,
        Side::Right,
        details,
    )?;
    // Keep the comprehension's retained values alive until its generator returns,
    // before the later iterability probe can call back into either mapping.
    drop(same);
    Ok(true)
}

fn iterable(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    match value.try_iter() {
        Ok(iterator) => {
            drop(iterator);
            Ok(true)
        }
        Err(error) if error.is_instance_of::<PyException>(value.py()) => Ok(false),
        Err(error) => Err(error),
    }
}

pub(super) fn describe(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
) -> PyResult<Option<ContainerComparison>> {
    let left_repr = super::summary(callback, left, 6)?;
    let right_repr = super::summary(callback, right, 6)?;
    let mut details = Vec::new();
    let ending = match mapping_details(left, right, callback, &mut details) {
        Ok(false) => return Ok(None),
        Ok(true) => {
            // The reference catches Exception only in this final iterability probe.
            if iterable(left)? && iterable(right)? {
                DictionaryEnding::Hint
            } else if details.is_empty() {
                return Ok(None);
            } else {
                DictionaryEnding::Silent
            }
        }
        Err(error)
            if error.is_instance_of::<PyException>(left.py())
                && !crate::independent::controls::is_control_error(left.py(), &error)? =>
        {
            DictionaryEnding::Failure(
                crate::independent::reporting::assertion_representation_crash(left.py(), &error)?,
            )
        }
        Err(error) => return Err(error),
    };
    Ok(Some(ContainerComparison::Dictionary {
        left: left_repr,
        right: right_repr,
        details,
        ending,
    }))
}
