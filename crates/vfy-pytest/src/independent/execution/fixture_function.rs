//! Resolve the live factory against the fixture request instance.

use crate::independent::fixture_binding::NativeFixtureBinding;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict};
use vfy_core::test_plan::FixtureExecution;

fn class_method_warning(py: Python<'_>) -> PyResult<()> {
    let warning = py.import("vfy.independent.facade")?.getattr("PytestRemovedIn10Warning")?.call1((concat!(
        "Class-scoped fixture defined as instance method is deprecated.\n",
        "Instance attributes set in this fixture will NOT be visible to test methods,\n",
        "as each test gets a new instance while the fixture runs only once per class.\n",
        "Use @classmethod decorator and set attributes on cls instead.\n",
        "See https://docs.pytest.org/en/stable/deprecations.html#class-scoped-fixture-as-instance-method"
    ),))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("stacklevel", 2)?;
    py.import("warnings")?
        .call_method("warn", (warning,), Some(&kwargs))?;
    Ok(())
}

pub(super) fn resolve(
    py: Python<'_>,
    binding: &Bound<'_, NativeFixtureBinding>,
    request: &Bound<'_, PyAny>,
    execution: FixtureExecution,
    definition: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let initial = binding.borrow().callable(py)?;
    let instance = request.getattr(pyo3::intern!(py, "instance"))?;
    let callable = initial.bind(py);
    if binding
        .getattr(pyo3::intern!(py, "scope"))?
        .extract::<String>()?
        == "class"
        && crate::module_cache::has_attr(callable, pyo3::intern!(py, "__self__"))?
    {
        let bound_to = callable.getattr(pyo3::intern!(py, "__self__"))?;
        // A module-bound builtin has __self__, but is not a class-bound method.
        // The definition is the original collected callable, not a mutable cache.
        let direct_parameter = execution == FixtureExecution::DirectParameter
            && callable.is(definition)
            && callable.is_instance_of::<PyCFunction>()
            && callable.getattr("__name__")?.eq("direct_parameter")?
            && callable.getattr("__module__")?.eq("vfy._native._pytest")?;
        if !direct_parameter && !bound_to.is_instance(&py.import("builtins")?.getattr("type")?)? {
            class_method_warning(py)?;
        }
    }
    if instance.is_none()
        || (crate::module_cache::has_attr(callable, pyo3::intern!(py, "__self__"))?
            && !instance.is_instance(
                &callable
                    .getattr(pyo3::intern!(py, "__self__"))?
                    .getattr(pyo3::intern!(py, "__class__"))?,
            )?)
    {
        return Ok(initial);
    }
    let selected = binding.borrow().callable(py)?;
    let function =
        crate::module_cache::optional_attr(selected.bind(py), pyo3::intern!(py, "__func__"))?
            .map_or_else(|| selected, Bound::unbind);
    // Both fresh reads are authoritative: descriptors/equality may mutate func.
    let compared = binding.borrow().callable(py)?;
    if function.bind(py).ne(compared.bind(py))? {
        return Ok(function
            .bind(py)
            .call_method1(pyo3::intern!(py, "__get__"), (instance,))?
            .unbind());
    }
    Ok(function)
}
