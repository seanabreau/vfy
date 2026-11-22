//! Hook specifications validate plugin signatures before records enter dispatch.

use super::{Implementation, PluginValidationError, Style, argument_names, metadata};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

#[pyclass(name = "NativeHookSpec", module = "vfy._native._pytest")]
pub(crate) struct Specification {
    pub(super) name: String,
    pub(super) namespace: Py<PyAny>,
    function: Py<PyAny>,
    arguments: Py<PyTuple>,
    keyword_arguments: Py<PyTuple>,
    options: Py<PyAny>,
    warn_on_impl: Py<PyAny>,
    warn_on_impl_args: Py<PyAny>,
    pub(super) historic: bool,
}

impl Specification {
    pub(super) fn permits_private_default(&self, py: Python<'_>) -> bool {
        self.warn_on_impl.bind(py).is_none() && self.warn_on_impl_args.bind(py).is_none()
    }
}

#[pymethods]
impl Specification {
    #[getter]
    fn name(&self) -> &str {
        &self.name
    }
    #[getter]
    fn namespace(&self, py: Python<'_>) -> Py<PyAny> {
        self.namespace.clone_ref(py)
    }
    #[getter]
    fn function(&self, py: Python<'_>) -> Py<PyAny> {
        self.function.clone_ref(py)
    }
    #[getter]
    fn argnames(&self, py: Python<'_>) -> Py<PyTuple> {
        self.arguments.clone_ref(py)
    }
    #[getter]
    fn kwargnames(&self, py: Python<'_>) -> Py<PyTuple> {
        self.keyword_arguments.clone_ref(py)
    }
    #[getter]
    fn opts(&self, py: Python<'_>) -> Py<PyAny> {
        self.options.clone_ref(py)
    }
    #[getter]
    fn warn_on_impl(&self, py: Python<'_>) -> Py<PyAny> {
        self.warn_on_impl.clone_ref(py)
    }
    #[setter]
    fn set_warn_on_impl(slf: &Bound<'_, Self>, value: Py<PyAny>) -> PyResult<()> {
        let removed = std::mem::replace(&mut slf.try_borrow_mut()?.warn_on_impl, value);
        drop(removed);
        Ok(())
    }
    #[getter]
    fn warn_on_impl_args(&self, py: Python<'_>) -> Py<PyAny> {
        self.warn_on_impl_args.clone_ref(py)
    }
    #[setter]
    fn set_warn_on_impl_args(slf: &Bound<'_, Self>, value: Py<PyAny>) -> PyResult<()> {
        let removed = std::mem::replace(&mut slf.try_borrow_mut()?.warn_on_impl_args, value);
        drop(removed);
        Ok(())
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.namespace)?;
        visit.call(&self.function)?;
        visit.call(&self.arguments)?;
        visit.call(&self.keyword_arguments)?;
        visit.call(&self.options)?;
        visit.call(&self.warn_on_impl)?;
        visit.call(&self.warn_on_impl_args)
    }
    fn __clear__(slf: &Bound<'_, Self>, py: Python<'_>) {
        let removed = {
            let Ok(mut specification) = slf.try_borrow_mut() else {
                return;
            };
            (
                std::mem::replace(&mut specification.namespace, py.None()),
                std::mem::replace(&mut specification.function, py.None()),
                std::mem::replace(&mut specification.options, py.None()),
                std::mem::replace(&mut specification.warn_on_impl, py.None()),
                std::mem::replace(&mut specification.warn_on_impl_args, py.None()),
            )
        };
        drop(removed);
    }
}

pub(super) fn read_specification(
    namespace: &Bound<'_, PyAny>,
    name: &str,
    project_name: &str,
    discover_prefix: bool,
) -> PyResult<Option<Specification>> {
    let py = namespace.py();
    let function = namespace.getattr(name)?;
    let options = metadata(
        &function,
        "__vfy_hookspec__",
        &format!("{project_name}_spec"),
    )?;
    let options = match options {
        Some(options) => options,
        None if discover_prefix && name.starts_with(&format!("{project_name}_")) => {
            PyDict::new(py).into_any()
        }
        None => return Ok(None),
    };
    let (arguments, keyword_arguments) = argument_names(&function)?;
    let warn_on_impl = options.call_method1("get", ("warn_on_impl",))?.unbind();
    let warn_on_impl_args = options
        .call_method1("get", ("warn_on_impl_args",))?
        .unbind();
    let historic = options
        .call_method1("get", ("historic", false))?
        .is_truthy()?;
    Ok(Some(Specification {
        name: name.to_owned(),
        namespace: namespace.clone().unbind(),
        function: function.unbind(),
        arguments,
        keyword_arguments,
        options: options.unbind(),
        warn_on_impl,
        warn_on_impl_args,
        historic,
    }))
}

pub(super) fn validation_error(plugin: &Bound<'_, PyAny>, message: String) -> PyResult<PyErr> {
    let error = plugin
        .py()
        .get_type::<PluginValidationError>()
        .call1((plugin, message))?;
    Ok(PyErr::from_value(error))
}

fn warning_for_function(
    py: Python<'_>,
    warning: &Bound<'_, PyAny>,
    function: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let code = function.getattr("__code__")?;
    let keywords = PyDict::new(py);
    keywords.set_item("lineno", code.getattr("co_firstlineno")?)?;
    keywords.set_item("filename", code.getattr("co_filename")?)?;
    py.import("warnings")?.call_method(
        "warn_explicit",
        (warning, warning.get_type()),
        Some(&keywords),
    )?;
    Ok(())
}

pub(super) fn verify(
    py: Python<'_>,
    specification: &Py<Specification>,
    implementation: &Implementation,
) -> PyResult<()> {
    let (name, historic, arguments) = {
        let specification = specification.borrow(py);
        (
            specification.name.clone(),
            specification.historic,
            specification.arguments.clone_ref(py),
        )
    };
    if historic && implementation.style != Style::Plain {
        return Err(validation_error(
            implementation.plugin.bind(py),
            format!(
                "Plugin '{}'\nhook '{name}'\nhistoric incompatible with yield/wrapper/hookwrapper",
                implementation.plugin_name
            ),
        )?);
    }
    let warns = {
        let warning = specification.borrow(py).warn_on_impl.clone_ref(py);
        warning.bind(py).is_truthy()?
    };
    if warns {
        let warning = specification.borrow(py).warn_on_impl.clone_ref(py);
        warning_for_function(py, warning.bind(py), implementation.function.bind(py))?;
    }
    let mut missing = Vec::new();
    for argument in implementation.arguments.bind(py).iter() {
        if !arguments.bind(py).contains(&argument)? {
            missing.push(argument);
        }
    }
    let signature = || -> PyResult<String> {
        let function = implementation.function.bind(py);
        Ok(format!(
            "{}{}",
            function.getattr("__name__")?.extract::<String>()?,
            py.import("inspect")?
                .call_method1("signature", (function,))?
                .str()?
        ))
    };
    if !missing.is_empty() {
        let missing = PySet::new(py, missing)?;
        return Err(validation_error(
            implementation.plugin.bind(py),
            format!(
                "Plugin '{}' for hook '{name}'\nhookimpl definition: {}\nArgument(s) {} are declared in the hookimpl but can not be found in the hookspec",
                implementation.plugin_name,
                signature()?,
                missing.repr()?
            ),
        )?);
    }
    let warns_arguments = {
        let warnings = specification.borrow(py).warn_on_impl_args.clone_ref(py);
        warnings.bind(py).is_truthy()?
    };
    if warns_arguments {
        for argument in implementation.arguments.bind(py).iter() {
            let warning = {
                let warnings = specification.borrow(py).warn_on_impl_args.clone_ref(py);
                warnings.bind(py).call_method1("get", (argument,))?
            };
            if !warning.is_none() {
                warning_for_function(py, &warning, implementation.function.bind(py))?;
            }
        }
    }
    if implementation.style != Style::Plain
        && matches!(&implementation.body, super::HookBody::Dynamic)
        && !py
            .import("inspect")?
            .call_method1("isgeneratorfunction", (implementation.function.bind(py),))?
            .is_truthy()?
    {
        return Err(validation_error(
            implementation.plugin.bind(py),
            format!(
                "Plugin '{}' for hook '{name}'\nhookimpl definition: {}\nDeclared as wrapper=True or hookwrapper=True but function is not a generator function",
                implementation.plugin_name,
                signature()?
            ),
        )?);
    }
    if implementation.wrapper() && implementation.hookwrapper() {
        return Err(validation_error(
            implementation.plugin.bind(py),
            format!(
                "Plugin '{}' for hook '{name}'\nhookimpl definition: {}\nThe wrapper=True and hookwrapper=True options are mutually exclusive",
                implementation.plugin_name,
                signature()?
            ),
        )?);
    }
    Ok(())
}

pub(super) fn first_result(py: Python<'_>, specification: &Py<Specification>) -> PyResult<bool> {
    let options = specification.borrow(py).options.clone_ref(py);
    if let Ok(options) = options.bind(py).cast_exact::<PyDict>() {
        return options
            .get_item(pyo3::intern!(py, "firstresult"))?
            .map_or(Ok(false), |value| value.is_truthy());
    }
    options
        .bind(py)
        .call_method1(
            pyo3::intern!(py, "get"),
            (pyo3::intern!(py, "firstresult"), false),
        )?
        .is_truthy()
}

pub(super) fn verify_call(
    py: Python<'_>,
    specification: &Py<Specification>,
    kwargs: &super::invocation::CallArguments<'_, '_>,
) -> PyResult<()> {
    let arguments = specification.borrow(py).arguments.clone_ref(py);
    let mut missing = Vec::new();
    for argument in arguments.bind(py).iter() {
        if !kwargs.contains(&argument)? {
            missing.push(format!("'{argument}'"));
        }
    }
    if !missing.is_empty() {
        py.import("warnings")?.call_method1("warn", (format!("Argument(s) {} which are declared in the hookspec cannot be found in this hook call", missing.join(", ")),))?;
    }
    Ok(())
}
