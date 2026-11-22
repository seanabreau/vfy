//! Expand supported Python containers for a line-by-line assertion difference.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFrozenSet, PyList, PySet, PyString, PyTuple};

fn formatted(value: &Bound<'_, PyAny>, indent: usize) -> PyResult<String> {
    let py = value.py();
    let (open, close) = if value.is_exact_instance_of::<PyDict>() {
        ("{", "}")
    } else if value.is_exact_instance_of::<PyList>() {
        ("[", "]")
    } else if value.is_exact_instance_of::<PyTuple>() {
        ("(", ")")
    } else if value.is_exact_instance_of::<PyFrozenSet>() {
        ("frozenset({", "})")
    } else if value.is_exact_instance_of::<PySet>() {
        ("{", "}")
    } else if value.is_exact_instance_of::<PyString>() || value.is_exact_instance_of::<PyBytes>() {
        // CPython supplies its scalar quoting and width-sensitive word or byte wrapping.
        let printer = py
            .import("pprint")?
            .getattr("PrettyPrinter")?
            .call1((4, 80))?;
        let output = py.import("io")?.call_method0("StringIO")?;
        let method = if value.is_exact_instance_of::<PyString>() {
            "_pprint_str"
        } else {
            "_pprint_bytes"
        };
        printer.call_method1(method, (value, &output, indent, 1, PySet::empty(py)?, 2))?;
        return output.call_method0("getvalue")?.extract();
    } else {
        return value.repr()?.extract();
    };
    if value.is_empty()? {
        return value.repr()?.extract();
    }
    let child_indent = indent + 4;
    let prefix = format!("\n{}", " ".repeat(child_indent));
    let mut text = open.to_owned();
    if let Ok(dict) = value.cast::<PyDict>() {
        for (key, item) in dict.iter() {
            text.push_str(&prefix);
            text.push_str(&key.repr()?.extract::<String>()?);
            text.push_str(": ");
            text.push_str(&formatted(&item, child_indent)?);
            text.push(',');
        }
    } else {
        let ordered;
        let items = if value.is_exact_instance_of::<PySet>()
            || value.is_exact_instance_of::<PyFrozenSet>()
        {
            let options = PyDict::new(py);
            options.set_item("key", py.import("pprint")?.getattr("_safe_key")?)?;
            ordered = py
                .import("builtins")?
                .getattr("sorted")?
                .call((value,), Some(&options))?;
            &ordered
        } else {
            value
        };
        for item in items.try_iter()? {
            text.push_str(&prefix);
            text.push_str(&formatted(&item?, child_indent)?);
            text.push(',');
        }
    }
    text.push('\n');
    text.push_str(&" ".repeat(indent));
    text.push_str(close);
    Ok(text)
}

pub(super) fn lines(left: &Bound<'_, PyAny>, right: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let py = left.py();
    let expected = PyString::new(py, &formatted(right, 0)?).call_method0("splitlines")?;
    let actual = PyString::new(py, &formatted(left, 0)?).call_method0("splitlines")?;
    py.import("difflib")?
        .call_method1("ndiff", (expected, actual))?
        .try_iter()?
        .map(|line| line?.call_method0("rstrip")?.extract())
        .collect()
}
