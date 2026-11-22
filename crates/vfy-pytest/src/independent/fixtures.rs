//! Adapt fixture declarations into plan bindings.

use super::ArgumentBinding;
use super::lifecycle::Context;
use pyo3::exceptions::{PyAttributeError, PyException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyType};
use vfy_core::FixtureScope;

const MAX_FIXTURE_DESCRIPTOR_DEPTH: usize = 64;

pub(super) struct Declaration<'py> {
    pub(super) metadata: Bound<'py, PyDict>,
    pub(super) function: Bound<'py, PyAny>,
    pub(super) callable: Bound<'py, PyAny>,
}

#[derive(Clone, Copy)]
enum Descriptor {
    Static,
    Class,
}

impl Descriptor {
    fn name(self) -> &'static str {
        match self {
            Self::Static => "staticmethod",
            Self::Class => "classmethod",
        }
    }
}

fn descriptor(value: &Bound<'_, PyAny>) -> PyResult<Option<Descriptor>> {
    let builtins = value.py().import("builtins")?;
    for kind in [Descriptor::Static, Descriptor::Class] {
        if value
            .get_type()
            .is_subclass(&builtins.getattr(kind.name())?)?
        {
            return Ok(Some(kind));
        }
    }
    Ok(None)
}

pub(super) fn is_declaration(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    let value = if descriptor(value)?.is_some() {
        value.getattr("__func__")?
    } else {
        value.clone()
    };
    Ok(!value.is_instance_of::<PyType>() && metadata(&value)?.is_some())
}

/// A declaration's metadata. Objects such as `unittest.mock.call` answer every
/// attribute lookup, so only a metadata dictionary marks a declaration. Like
/// pytest's `safe_getattr`, a lookup that raises an `Exception`, as an unbound
/// context proxy does, marks no declaration.
fn metadata<'py>(value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
    match value.getattr("__vfy_fixture__") {
        Ok(metadata) => Ok(metadata.cast_into::<PyDict>().ok()),
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyException>(value.py()) => Ok(None),
        Err(error) => Err(error),
    }
}

pub(super) fn declaration<'py>(
    source: &Bound<'py, PyAny>,
    class_fixture: bool,
) -> PyResult<Option<Declaration<'py>>> {
    if source.is_instance_of::<PyType>() {
        return Ok(None);
    }
    let py = source.py();
    let outer = if class_fixture {
        descriptor(source)?
    } else {
        None
    };
    let declaration = if outer.is_some() {
        source.getattr("__func__")?
    } else {
        source.clone()
    };
    let Some(metadata) = metadata(&declaration)? else {
        return Ok(None);
    };
    let definition = py
        .import("vfy.independent.fixtures")?
        .getattr("FixtureFunctionDefinition")?;
    let function = if declaration.get_type().is_subclass(&definition)? {
        declaration.call_method0("_get_wrapped_function")?
    } else {
        declaration
    };
    let callable = match outer {
        Some(kind) => py
            .import("builtins")?
            .getattr(kind.name())?
            .call1((&function,))?,
        None => function.clone(),
    };
    let mut function = function;
    let mut seen = Vec::new();
    while class_fixture && descriptor(&function)?.is_some() {
        if seen.len() == MAX_FIXTURE_DESCRIPTOR_DEPTH {
            return Err(PyValueError::new_err(
                "fixture descriptor nesting exceeds 64",
            ));
        }
        let identity = function.as_ptr() as usize;
        if seen.contains(&identity) {
            return Err(PyValueError::new_err("recursive fixture descriptor"));
        }
        seen.push(identity);
        function = function.getattr("__func__")?;
    }
    if !function.is_callable() {
        return Err(PyTypeError::new_err(
            "fixture declaration must wrap a callable",
        ));
    }
    Ok(Some(Declaration {
        metadata,
        function,
        callable,
    }))
}

/// A recognized declaration resolves on the exact temporary Class scan holder.
pub(super) struct HolderDeclaration<'py> {
    pub(super) declaration: Declaration<'py>,
    pub(super) arguments: ArgumentBinding,
}

pub(super) fn declaration_on_holder<'py>(
    holder: &Bound<'py, PyAny>,
    name: &str,
    original: &Bound<'py, PyAny>,
) -> PyResult<Option<HolderDeclaration<'py>>> {
    let definition = holder
        .py()
        .import("vfy.independent.fixtures")?
        .getattr("FixtureFunctionDefinition")?;
    let unbound = match holder.get_type().getattr(name) {
        Ok(unbound) => unbound,
        Err(error) if error.is_instance_of::<PyException>(holder.py()) => return Ok(None),
        Err(error) => return Err(error),
    };
    // Pytest admits the class lookup's actual definition type. A classmethod
    // that returns a bound method can forward metadata without being a fixture.
    if !unbound.get_type().is(&definition) {
        return Ok(None);
    }
    match holder.getattr(name) {
        Ok(actual) => Ok(
            declaration(&actual, true)?.map(|declaration| HolderDeclaration {
                declaration,
                arguments: ArgumentBinding::Function,
            }),
        ),
        Err(error) if error.is_instance_of::<PyAttributeError>(holder.py()) => {
            let Some(declaration) = declaration(original, true)? else {
                return Ok(None);
            };
            let arguments = ArgumentBinding::Function;
            Ok(Some(HolderDeclaration {
                declaration,
                arguments,
            }))
        }
        Err(error) => Err(error),
    }
}

pub(super) fn scope(
    py: Python<'_>,
    metadata: &Bound<'_, PyDict>,
    name: &str,
    context: Option<&Context>,
) -> PyResult<FixtureScope> {
    let Some(value) = metadata.get_item("scope")? else {
        return Ok(FixtureScope::Function);
    };
    let name = if value.is_callable() {
        let context = context.ok_or_else(|| {
            PyValueError::new_err("callable fixture scope requires a configured session")
        })?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("fixture_name", name)?;
        kwargs.set_item("config", &context.config)?;
        let result = match value.call((), Some(&kwargs)) {
            Ok(result) => result,
            Err(error) if error.is_instance_of::<PyException>(py) => {
                let wrapped = PyTypeError::new_err(format!(
                    "Error evaluating {} while defining fixture '{name}'.\nExpected a function with the signature (*, fixture_name, config)",
                    value.str()?
                ));
                wrapped.set_context(py, Some(error.clone_ref(py)));
                wrapped.set_cause(py, Some(error));
                return Err(wrapped);
            }
            Err(error) => return Err(error),
        };
        if !result.is_instance_of::<PyString>() {
            let kwargs = PyDict::new(py);
            kwargs.set_item("pytrace", false)?;
            py.import("vfy.independent.facade")?.getattr("fail")?.call(
                (format!(
                    "Expected {} to return a 'str' while defining fixture '{name}', but it returned:\n{}",
                    value.str()?, result.repr()?
                ),),
                Some(&kwargs),
            )?;
        }
        result.extract::<String>()?
    } else {
        value.extract::<String>()?
    };
    FixtureScope::parse(&name)
        .ok_or_else(|| PyValueError::new_err(format!("invalid fixture scope: {name}")))
}
