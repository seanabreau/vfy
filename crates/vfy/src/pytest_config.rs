//! Python bindings for resolving pytest configuration files and options.

use std::path::PathBuf;

use pyo3::basic::CompareOp;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
use vfy_core::pytest_config::{PytestIniMode, PytestIniSetting};

fn parser_behavior(py: Python<'_>) -> vfy_core::pytest_config::PytestParserBehavior {
    let version = py.version_info();
    vfy_core::pytest_config::PytestParserBehavior::for_cpython(
        version.major,
        version.minor,
        version.patch,
    )
}

#[pyfunction]
#[pyo3(signature = (arguments, invocation_dir, env_addopts, policy))]
fn resolve_pytest_configuration(
    py: Python<'_>,
    arguments: Vec<String>,
    invocation_dir: PathBuf,
    env_addopts: Option<String>,
    policy: &str,
) -> PyResult<Py<PyDict>> {
    let policy = match policy {
        "legacy_without_argument_files" => vfy_core::PytestConfigPolicy::LegacyWithoutArgumentFiles,
        "legacy" => vfy_core::PytestConfigPolicy::Legacy,
        "toml" => vfy_core::PytestConfigPolicy::Toml,
        "toml_with_non_file_config" => vfy_core::PytestConfigPolicy::TomlWithNonFileConfig,
        "toml_with_addopts_overrides" => vfy_core::PytestConfigPolicy::TomlWithAddoptsOverrides,
        _ => {
            return Err(PyValueError::new_err(
                "pytest config policy must be 'legacy_without_argument_files', 'legacy', 'toml', or 'toml_with_addopts_overrides'",
            ));
        }
    };
    let parser_behavior = parser_behavior(py);
    let projection = py
        .detach(move || {
            vfy_core::pytest_config::resolve_pytest_configuration_with_parser(
                &arguments,
                &invocation_dir,
                env_addopts.as_deref(),
                policy,
                parser_behavior,
            )
        })
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    projection_record(py, &projection)
}

fn projection_record(
    py: Python<'_>,
    projection: &vfy_core::PytestConfigProjection,
) -> PyResult<Py<PyDict>> {
    let result = PyDict::new(py);
    result.set_item(
        "config_file",
        projection
            .config_file
            .as_deref()
            .map(|path| path.to_string_lossy()),
    )?;
    result.set_item("rootdir", projection.rootdir.to_string_lossy().as_ref())?;
    result.set_item(
        "config_contents_fingerprint",
        &projection.config_contents_fingerprint,
    )?;
    result.set_item("configured_args", &projection.configured_args)?;
    result.set_item("effective_args", &projection.effective_args)?;
    result.set_item("cli_args", &projection.cli_args)?;
    result.set_item("filtered_scope", projection.filtered_scope)?;
    result.set_item("collect_only", projection.collect_only)?;
    if let Some(location) = &projection.invocation_location {
        result.set_item("invocation_location", location)?;
    }
    result.set_item("cache_dir", projection.cache_dir.to_string_lossy().as_ref())?;
    result.set_item(
        "basetemp",
        projection
            .basetemp
            .as_deref()
            .map(|path| path.to_string_lossy()),
    )?;
    Ok(result.unbind())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(resolve_pytest_configuration, module)?)?;
    module.add_class::<NativePytestConfiguration>()?;
    Ok(())
}

fn independent_configuration_error(py: Python<'_>, error: &vfy_core::PytestConfigError) -> PyErr {
    if let Some((_, bytes)) = error.invalid_utf8() {
        return match PyBytes::new(py, bytes).call_method1("decode", ("utf-8",)) {
            Err(error) => error,
            Ok(_) => PyRuntimeError::new_err("configuration UTF-8 failure contained valid bytes"),
        };
    }
    if let Some(message) = error.value_error_message() {
        return PyValueError::new_err(message.to_owned());
    }
    let Some((path, cause)) = error.read_error() else {
        #[cfg(unix)]
        {
            return vfy_pytest::IndependentUsageError::new_err(error.to_string());
        }
        #[cfg(not(unix))]
        {
            return PyValueError::new_err(error.to_string());
        }
    };
    let exception = (|| -> PyResult<PyErr> {
        let message = match cause.raw_os_error() {
            Some(number) => py.import("os")?.call_method1("strerror", (number,))?,
            None => cause.to_string().into_pyobject(py)?.into_any(),
        };
        let value = py.get_type::<PyOSError>().call1((
            cause.raw_os_error(),
            message,
            path.to_string_lossy().as_ref(),
        ))?;
        Ok(PyErr::from_value(value))
    })();
    exception.unwrap_or_else(|error| error)
}

/// Parsed immutable settings used by the independent pytest protocol adapter.
#[pyclass(module = "vfy._native", frozen)]
struct NativePytestConfiguration {
    projection: vfy_core::PytestConfigProjection,
    invocation_dir: PathBuf,
    plugin_cli_args: Vec<String>,
}

#[pymethods]
impl NativePytestConfiguration {
    #[new]
    #[pyo3(signature = (arguments, invocation_dir, env_addopts))]
    fn new(
        py: Python<'_>,
        arguments: Vec<String>,
        invocation_dir: PathBuf,
        env_addopts: Option<String>,
    ) -> PyResult<Self> {
        let stored_invocation_dir = invocation_dir.clone();
        let parser_behavior = parser_behavior(py);
        let (projection, plugin_cli_args) = py
            .detach(move || {
                vfy_core::pytest_config::resolve_pytest_configuration_with_parser(
                    &arguments,
                    &invocation_dir,
                    env_addopts.as_deref(),
                    vfy_core::PytestConfigPolicy::TomlWithAddoptsOverrides,
                    parser_behavior,
                )
                .map(|projection| (projection, arguments))
            })
            .map_err(|error| independent_configuration_error(py, &error))?;
        Ok(Self {
            projection,
            invocation_dir: stored_invocation_dir,
            plugin_cli_args,
        })
    }

    #[staticmethod]
    #[pyo3(signature = (kind=None))]
    fn ini_default(py: Python<'_>, kind: Option<&str>) -> PyResult<Py<PyAny>> {
        Ok(match kind {
            Some("paths" | "pathlist" | "args" | "linelist") => {
                PyList::empty(py).into_any().unbind()
            }
            Some("bool") => false.into_pyobject(py)?.to_owned().into_any().unbind(),
            Some("int") => 0_i64.into_pyobject(py)?.into_any().unbind(),
            Some("float") => 0.0_f64.into_pyobject(py)?.into_any().unbind(),
            _ => PyString::new(py, "").into_any().unbind(),
        })
    }

    fn projection(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        projection_record(py, &self.projection)
    }

    /// Plugin bootstrap scans original tokens, independently of argparse expansion.
    fn plugin_preparse_args(&self) -> (Vec<String>, Vec<String>) {
        let configured = self
            .projection
            .configured_args
            .iter()
            .chain(&self.plugin_cli_args)
            .cloned()
            .collect();
        (self.plugin_cli_args.clone(), configured)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts owned filesystem paths, including non-UTF-8 filenames"
    )]
    fn assertion_rewrite_matches(
        _slf: &Bound<'_, Self>,
        config: &Bound<'_, PyAny>,
        path: PathBuf,
    ) -> PyResult<bool> {
        let python_files = config
            .call_method1("getini", ("python_files",))?
            .extract()?;
        Ok(path.file_name().is_some_and(|name| name == "conftest.py")
            || vfy_core::independent_collection::matches_python_file(&path, python_files))
    }

    fn cwd_relative_nodeid(
        slf: &Bound<'_, Self>,
        config: &Bound<'_, PyAny>,
        nodeid: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let changed = {
            let comparison = {
                let invocation = config.getattr("_invocation_dir")?;
                let root = config.getattr("rootpath")?;
                invocation.rich_compare(root, CompareOp::Ne)?
            };
            comparison.is_truthy()?
        };
        if changed {
            slf.py()
                .import("vfy.independent.terminal_resources")?
                .call_method1("cwd_relative_nodeid", (config, nodeid))
                .map(Bound::unbind)
        } else {
            Ok(nodeid.clone().unbind())
        }
    }

    fn nodeid_fullpath(
        _slf: &Bound<'_, Self>,
        config: &Bound<'_, PyAny>,
        path: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Ok(config.getattr("rootpath")?.div(path)?.unbind())
    }

    fn nodeid_relative_path(
        slf: &Bound<'_, Self>,
        config: &Bound<'_, PyAny>,
        fullpath: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let startpath = config.getattr("_invocation_dir")?;
        let start = startpath.extract::<PathBuf>()?;
        let fullpath = fullpath.extract::<PathBuf>()?;
        let relative = vfy_core::relative_path(&start, &fullpath);
        let relative = if relative.as_os_str().is_empty() {
            std::path::Path::new(".")
        } else {
            relative.as_path()
        };
        Ok(relative
            .as_os_str()
            .into_pyobject(slf.py())?
            .into_any()
            .unbind())
    }

    fn join_relative_nodeid(
        slf: &Bound<'_, Self>,
        relative: &Bound<'_, PyAny>,
        parts: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let values = PyList::new(slf.py(), [relative])?;
        values.call_method1("extend", (parts,))?;
        Ok(PyString::new(slf.py(), "::")
            .call_method1("join", (values,))?
            .unbind())
    }

    fn ini_sha256(&self) -> Option<&str> {
        self.projection.config_file_sha256.as_deref()
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the caller's Python sequence into an owned vector."
    )]
    fn unknown_ini(&self, known_names: Vec<String>) -> Vec<String> {
        self.projection.unknown_ini(&known_names)
    }

    /// Names of every selected setting in configuration and override order.
    fn ini_names(&self) -> Vec<String> {
        self.projection.ini_values.keys().cloned().collect()
    }

    /// The setting as written in its source: text for INI files, the TOML value otherwise.
    fn ini_value(&self, py: Python<'_>, name: &str) -> PyResult<Option<Py<PyAny>>> {
        self.projection
            .ini_values
            .get(name)
            .map(|setting| setting_object(py, setting).map(Bound::unbind))
            .transpose()
    }

    #[pyo3(signature = (name, kind, default, aliases=None))]
    fn getini(
        &self,
        py: Python<'_>,
        name: &str,
        kind: &str,
        default: Py<PyAny>,
        aliases: Option<Vec<String>>,
    ) -> PyResult<Py<PyAny>> {
        let aliases = aliases.unwrap_or_default();
        let Some(setting) = self.projection.ini_setting(name, &aliases) else {
            return Ok(default);
        };
        let value = setting_object(py, setting)?;
        let strings = match setting.mode {
            PytestIniMode::Ini => ini_value(py, name, kind, &value)?,
            PytestIniMode::Toml => self.toml_value(py, name, kind, &value)?,
        };
        if kind != "paths" {
            return Ok(strings.unbind());
        }
        let directory = self
            .projection
            .config_file
            .as_deref()
            .and_then(std::path::Path::parent)
            .unwrap_or(&self.invocation_dir);
        let base = py
            .import("pathlib")?
            .getattr("Path")?
            .call1((directory.to_string_lossy().as_ref(),))?;
        let paths = PyList::empty(py);
        for item in strings.try_iter()? {
            paths.append(base.call_method1("__truediv__", (item?,))?)?;
        }
        Ok(paths.into_any().unbind())
    }
}

impl NativePytestConfiguration {
    fn toml_value<'py>(
        &self,
        py: Python<'py>,
        name: &str,
        kind: &str,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        match kind {
            "args" | "paths" | "linelist" => {
                let Ok(list) = value.cast::<PyList>() else {
                    return Err(self.type_error(
                        name,
                        &format!("a list for type '{kind}'"),
                        value,
                    )?);
                };
                for (index, item) in list.iter().enumerate() {
                    if !item.is_instance_of::<PyString>() {
                        return Err(PyTypeError::new_err(format!(
                            "{}: config option '{name}' expects a list of strings, but item at index {index} is {}: {}",
                            self.inipath(),
                            item.get_type().name()?,
                            item.repr()?
                        )));
                    }
                }
                Ok(PyList::new(py, list.iter())?.into_any())
            }
            "bool" if value.is_instance_of::<PyBool>() => Ok(value.clone()),
            "int" if value.is_instance_of::<PyInt>() && !value.is_instance_of::<PyBool>() => {
                Ok(value.clone())
            }
            "float"
                if (value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>())
                    && !value.is_instance_of::<PyBool>() =>
            {
                Ok(value.clone())
            }
            "string" if value.is_instance_of::<PyString>() => Ok(value.clone()),
            "bool" | "int" | "float" | "string" => {
                let expected = match kind {
                    "bool" => "a bool",
                    "int" => "an int",
                    "float" => "a float",
                    _ => "a string",
                };
                Err(self.type_error(name, expected, value)?)
            }
            _ => Err(PyValueError::new_err(format!(
                "unknown option type: {kind}"
            ))),
        }
    }

    fn inipath(&self) -> String {
        self.projection
            .config_file
            .as_ref()
            .map_or_else(|| "None".to_owned(), |path| path.display().to_string())
    }

    fn type_error(&self, name: &str, expected: &str, value: &Bound<'_, PyAny>) -> PyResult<PyErr> {
        Ok(PyTypeError::new_err(format!(
            "{}: config option '{name}' expects {expected}, got {}: {}",
            self.inipath(),
            value.get_type().name()?,
            value.repr()?
        )))
    }
}

fn setting_object<'py>(py: Python<'py>, setting: &PytestIniSetting) -> PyResult<Bound<'py, PyAny>> {
    let value = toml_object(py, &setting.value)?;
    if setting.mode == PytestIniMode::Ini && !value.is_instance_of::<PyList>() {
        Ok(value.str()?.into_any())
    } else {
        Ok(value)
    }
}

fn toml_object<'py>(py: Python<'py>, value: &toml::Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        toml::Value::String(value) => value.into_pyobject(py)?.into_any(),
        toml::Value::Integer(value) => value.into_pyobject(py)?.into_any(),
        toml::Value::Float(value) => value.into_pyobject(py)?.into_any(),
        toml::Value::Boolean(value) => value.into_pyobject(py)?.to_owned().into_any(),
        toml::Value::Array(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(toml_object(py, value)?)?;
            }
            list.into_any()
        }
        toml::Value::Table(values) => {
            let dict = PyDict::new(py);
            for (name, value) in values {
                dict.set_item(name, toml_object(py, value)?)?;
            }
            dict.into_any()
        }
        toml::Value::Datetime(value) => {
            let kind = if value.date.is_some() {
                if value.time.is_some() {
                    "datetime"
                } else {
                    "date"
                }
            } else {
                "time"
            };
            py.import("datetime")?
                .getattr(kind)?
                .call_method1("fromisoformat", (value.to_string(),))?
        }
    })
}

fn ini_value<'py>(
    py: Python<'py>,
    name: &str,
    kind: &str,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match kind {
        "args" | "paths" => Ok(if let Ok(value) = value.cast::<PyString>() {
            let words = vfy_core::pytest_config::split_shell_words(value.to_str()?)
                .map_err(PyValueError::new_err)?;
            PyList::new(py, words)?.into_any()
        } else {
            value.clone()
        }),
        "linelist" => Ok(if let Ok(value) = value.cast::<PyString>() {
            let lines: Vec<String> = value
                .to_str()?
                .split('\n')
                .map(|line| line.trim_matches(python_whitespace))
                .filter(|line| !line.is_empty())
                .map(str::to_owned)
                .collect();
            PyList::new(py, lines)?.into_any()
        } else {
            value.clone()
        }),
        "bool" => {
            let text = value
                .str()?
                .to_str()?
                .trim_matches(python_whitespace)
                .to_lowercase();
            match text.as_str() {
                "y" | "yes" | "t" | "true" | "on" | "1" => {
                    Ok(PyBool::new(py, true).to_owned().into_any())
                }
                "n" | "no" | "f" | "false" | "off" | "0" => {
                    Ok(PyBool::new(py, false).to_owned().into_any())
                }
                _ => Err(PyValueError::new_err(format!(
                    "invalid truth value {}",
                    PyString::new(py, &text).repr()?
                ))),
            }
        }
        "string" => Ok(value.clone()),
        "int" | "float" => {
            if !value.is_instance_of::<PyString>() {
                let article = if kind == "int" { "an int" } else { "a float" };
                let declared = if kind == "int" { "integer" } else { "float" };
                return Err(PyTypeError::new_err(format!(
                    "Expected {article} string for option {name} of type {declared}, but got: {}",
                    value.repr()?
                )));
            }
            return py.import("builtins")?.getattr(kind)?.call1((&value,));
        }
        _ => Err(PyValueError::new_err(format!(
            "unknown option type: {kind}"
        ))),
    }
}

// CPython includes these four control separators in str.isspace().
fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}
