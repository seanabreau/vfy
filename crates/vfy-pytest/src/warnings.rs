//! Warning filters and records for the native test loop.

use crate::lifecycle::HookGlobals;
use pyo3::exceptions::PyAttributeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PyString, PyTuple};
use std::collections::HashMap;
use vfy_core::test_execution::WarningRecord;

struct ActiveScope {
    context: Py<PyAny>,
    log: Py<PyList>,
    nodeid: String,
}

struct TemplateProfile {
    _owner: Py<PyAny>,
    own: Vec<String>,
    inherited: Vec<String>,
    default: usize,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum WarningFilterOwnership {
    Native,
    PytestProtocol,
}

pub(crate) struct WarningService {
    warnings: Py<PyAny>,
    config: Option<Py<PyAny>>,
    configured_warning_scope: Option<Py<PyAny>>,
    hook_globals: Option<HookGlobals>,
    formatter: Option<Py<PyAny>>,
    enabled: bool,
    filter_ownership: WarningFilterOwnership,
    profiles: Vec<Vec<String>>,
    marks_to_profile: HashMap<Vec<String>, usize>,
    templates: HashMap<usize, TemplateProfile>,
    active: Option<ActiveScope>,
    records: Vec<WarningRecord>,
    messages: Vec<(String, Py<PyAny>)>,
    retain_messages: bool,
}

impl WarningService {
    pub(crate) fn new(
        py: Python<'_>,
        items: &Bound<'_, PyAny>,
        filter_ownership: WarningFilterOwnership,
    ) -> PyResult<Self> {
        let warnings = py.import("warnings")?.into_any().unbind();
        let mut service = Self {
            warnings,
            config: None,
            configured_warning_scope: None,
            hook_globals: None,
            formatter: None,
            enabled: true,
            filter_ownership,
            profiles: Vec::new(),
            marks_to_profile: HashMap::new(),
            templates: HashMap::new(),
            active: None,
            records: Vec::new(),
            messages: Vec::new(),
            retain_messages: false,
        };
        for item in items.try_iter()? {
            let item = item?;
            if service.templates.is_empty() {
                let config = item.getattr(intern!(py, "config"))?;
                service.config = Some(config.clone().unbind());
                service.configured_warning_scope =
                    match config.getattr(intern!(py, "_catch_configured_warnings")) {
                        Ok(factory) => Some(factory.unbind()),
                        Err(error) if error.is_instance_of::<PyAttributeError>(py) => None,
                        Err(error) => return Err(error),
                    };
                if !config
                    .getattr(intern!(py, "pluginmanager"))?
                    .call_method1(intern!(py, "hasplugin"), ("warnings",))?
                    .is_truthy()?
                {
                    service.enabled = false;
                    return Ok(service);
                }
                let implementations = config
                    .getattr(intern!(py, "pluginmanager"))?
                    .getattr(intern!(py, "hook"))?
                    .getattr(intern!(py, "pytest_runtest_protocol"))?
                    .call_method0(intern!(py, "get_hookimpls"))?;
                for implementation in implementations.try_iter()? {
                    let implementation = implementation?;
                    if implementation
                        .getattr(intern!(py, "plugin_name"))?
                        .extract::<String>()?
                        == "warnings"
                    {
                        service.hook_globals = HookGlobals::new(
                            &implementation.getattr(intern!(py, "function"))?,
                            "_pytest.warnings",
                        )?;
                        break;
                    }
                }
                service.formatter = Some(
                    service
                        .warning_helper(py, intern!(py, "warning_record_to_str"))?
                        .unbind(),
                );
            }
            if filter_ownership == WarningFilterOwnership::PytestProtocol {
                // The live upstream protocol reads each dispatched item's marks.
                // Its workers do not need a second full-suite marker inventory.
                break;
            }
            let own = warning_filters(&item.getattr(intern!(py, "own_markers"))?)?;
            let parent = item.getattr(intern!(py, "parent"))?;
            let inherited = if parent.is_none() {
                Vec::new()
            } else {
                warning_filters(
                    &parent.call_method1(intern!(py, "iter_markers"), ("filterwarnings",))?,
                )?
            };
            let mut marks = own.clone();
            marks.extend_from_slice(&inherited);
            let default = service.register_profile(marks);
            service.templates.insert(
                item.as_ptr() as usize,
                TemplateProfile {
                    _owner: item.unbind(),
                    own,
                    inherited,
                    default,
                },
            );
        }
        Ok(service)
    }

    pub(crate) fn finish_compilation(&mut self) {
        // The input items and compiled suite retain the Python owners. Runtime
        // refresh reads live markers, so only the compiled profiles survive.
        drop(std::mem::take(&mut self.templates));
    }

    fn register_profile(&mut self, marks: Vec<String>) -> usize {
        if let Some(index) = self.marks_to_profile.get(&marks) {
            return *index;
        }
        let index = self.profiles.len();
        self.profiles.push(marks.clone());
        self.marks_to_profile.insert(marks, index);
        index
    }

    pub(crate) fn profile_for_case(
        &mut self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        callspec: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<usize> {
        if !self.enabled || self.filter_ownership == WarningFilterOwnership::PytestProtocol {
            return Ok(0);
        }
        let template = &self.templates[&(item.as_ptr() as usize)];
        let parameter_filters = match callspec {
            Some(callspec) => warning_filters(&callspec.getattr(intern!(py, "marks"))?)?,
            None => Vec::new(),
        };
        if parameter_filters.is_empty() {
            return Ok(template.default);
        }
        // Function marks precede parameter marks, then parent marks, matching
        // Function.own_markers and Node.iter_markers in the retained collector.
        let mut marks = template.own.clone();
        marks.extend(parameter_filters);
        marks.extend_from_slice(&template.inherited);
        Ok(self.register_profile(marks))
    }

    pub(crate) fn refresh_profile_for_case(
        &mut self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        prior_profile: usize,
        callspec_marks: Option<&[Py<PyAny>]>,
    ) -> PyResult<usize> {
        if !self.enabled || self.filter_ownership == WarningFilterOwnership::PytestProtocol {
            return Ok(0);
        }
        // Earlier tests can change this item's or its parents' marks. Compact
        // templates keep parameter marks separate until their case is materialized.
        let mut marks = match callspec_marks {
            Some(parameters) => warning_filters(
                crate::runner::compact_case_markers(py, item, parameters)?.as_any(),
            )?,
            None => warning_filters(&item.getattr(intern!(py, "own_markers"))?)?,
        };
        let parent = item.getattr(intern!(py, "parent"))?;
        if !parent.is_none() {
            marks.extend(warning_filters(
                &parent.call_method1(intern!(py, "iter_markers"), ("filterwarnings",))?,
            )?);
        }
        if marks == self.profiles[prior_profile] {
            return Ok(prior_profile);
        }
        Ok(self.register_profile(marks))
    }

    pub(crate) fn enter_config(&mut self, py: Python<'_>) -> PyResult<()> {
        let profile = self.register_profile(Vec::new());
        self.enter_scope(py, "", profile, WarningFilterOwnership::Native)
    }

    pub(crate) fn enter_profile(
        &mut self,
        py: Python<'_>,
        nodeid: &str,
        profile: usize,
    ) -> PyResult<()> {
        self.enter_scope(py, nodeid, profile, self.filter_ownership)
    }

    fn enter_scope(
        &mut self,
        py: Python<'_>,
        nodeid: &str,
        profile: usize,
        filter_ownership: WarningFilterOwnership,
    ) -> PyResult<()> {
        if !self.enabled {
            return Ok(());
        }
        let filters = if filter_ownership == WarningFilterOwnership::Native {
            let (config_filters, cmdline_filters) = if let Some(config) = &self.config {
                let config = config.bind(py);
                let config_filters =
                    config.call_method1(intern!(py, "getini"), ("filterwarnings",))?;
                let cmdline_filters = config
                    .getattr(intern!(py, "known_args_namespace"))?
                    .getattr(intern!(py, "pythonwarnings"))?;
                let cmdline_filters = if cmdline_filters.is_truthy()? {
                    cmdline_filters
                } else {
                    PyList::empty(py).into_any()
                };
                (config_filters, cmdline_filters)
            } else {
                (PyList::empty(py).into_any(), PyList::empty(py).into_any())
            };
            Some((config_filters, cmdline_filters))
        } else {
            // Reapplying item filters here would also overwrite changes made by
            // pytest_runtest_logstart after upstream installed those filters.
            None
        };
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "record"), true)?;
        let context = match (&self.configured_warning_scope, filter_ownership) {
            (Some(factory), WarningFilterOwnership::Native) => {
                factory.bind(py).call((), Some(&kwargs))?
            }
            _ => self.warnings.bind(py).call_method(
                intern!(py, "catch_warnings"),
                (),
                Some(&kwargs),
            )?,
        };
        let log = context
            .call_method0(intern!(py, "__enter__"))?
            .cast_into::<PyList>()?;
        if let Some((config_filters, cmdline_filters)) = filters
            && let Err(error) = self.apply_filters(
                py,
                &self.profiles[profile],
                &config_filters,
                &cmdline_filters,
            )
        {
            let _ =
                context.call_method1(intern!(py, "__exit__"), (py.None(), py.None(), py.None()));
            return Err(error);
        }
        self.active = Some(ActiveScope {
            context: context.unbind(),
            log: log.unbind(),
            nodeid: nodeid.to_owned(),
        });
        Ok(())
    }

    pub(crate) fn exit_item(&mut self, py: Python<'_>) -> PyResult<()> {
        let Some(scope) = self.active.take() else {
            return Ok(());
        };
        let formatted = (|| -> PyResult<()> {
            for message in scope.log.bind(py).iter() {
                self.records.push(WarningRecord {
                    message: self
                        .formatter
                        .as_ref()
                        .expect("active warning scope has a formatter")
                        .bind(py)
                        .call1((&message,))?
                        .extract()?,
                    nodeid: scope.nodeid.clone(),
                    filename: message.getattr(intern!(py, "filename"))?.extract()?,
                    lineno: message.getattr(intern!(py, "lineno"))?.extract()?,
                    ..WarningRecord::default()
                });
                if self.retain_messages {
                    self.messages.push((scope.nodeid.clone(), message.unbind()));
                }
            }
            Ok(())
        })();
        let restored = scope
            .context
            .bind(py)
            .call_method1(intern!(py, "__exit__"), (py.None(), py.None(), py.None()));
        formatted?;
        restored.map(|_| ())
    }

    pub(crate) fn records(&self) -> &[WarningRecord] {
        &self.records
    }

    pub(crate) fn retain_pytest_messages(&mut self) {
        self.retain_messages = true;
    }

    pub(crate) fn take_messages(&mut self) -> Vec<(String, Py<PyAny>)> {
        std::mem::take(&mut self.messages)
    }

    pub(crate) fn into_records(self) -> Vec<WarningRecord> {
        self.records
    }

    fn warning_helper<'py>(
        &self,
        py: Python<'py>,
        name: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if let Some(globals) = &self.hook_globals {
            globals.lookup(py, name)
        } else {
            py.import("_pytest.warnings")?.getattr(name)
        }
    }

    fn apply_filters(
        &self,
        py: Python<'_>,
        marks: &[String],
        config_filters: &Bound<'_, PyAny>,
        cmdline_filters: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let warnings = self.warnings.bind(py);
        // Pytest 9.2 applies defaults and configured filters in its Config
        // context. Earlier releases leave that work in the warnings hook.
        if self.configured_warning_scope.is_none() {
            let defaults = !py
                .import("sys")?
                .getattr(intern!(py, "warnoptions"))?
                .is_truthy()?;
            if defaults {
                let builtins = py.import("builtins")?;
                for category in ["DeprecationWarning", "PendingDeprecationWarning"] {
                    let kwargs = PyDict::new(py);
                    kwargs.set_item(intern!(py, "category"), builtins.getattr(category)?)?;
                    warnings.call_method(
                        intern!(py, "filterwarnings"),
                        ("always",),
                        Some(&kwargs),
                    )?;
                }
            }
            if let Some(config) = &self.config {
                py.import("vfy._pytest.native_pytest")?
                    .getattr(intern!(py, "apply_upstream_warning_defaults"))?
                    .call1((config.bind(py),))?;
            }
            self.warning_helper(py, intern!(py, "apply_warning_filters"))?
                .call1((config_filters, cmdline_filters))?;
        }
        for arg in marks {
            let kwargs = PyDict::new(py);
            kwargs.set_item(intern!(py, "escape"), false)?;
            let parsed = self
                .warning_helper(py, intern!(py, "parse_warning_filter"))?
                .call((arg,), Some(&kwargs))?;
            warnings
                .getattr(intern!(py, "filterwarnings"))?
                .call1(parsed.cast::<PyTuple>()?)?;
        }
        Ok(())
    }
}

fn warning_filters(marks: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let mut filters = Vec::new();
    for mark in marks.try_iter()? {
        append_warning_filters(&mark?, &mut filters)?;
    }
    Ok(filters)
}

fn append_warning_filters(mark: &Bound<'_, PyAny>, filters: &mut Vec<String>) -> PyResult<()> {
    if mark
        .getattr(intern!(mark.py(), "name"))?
        .extract::<String>()?
        == "filterwarnings"
    {
        for arg in mark.getattr(intern!(mark.py(), "args"))?.try_iter()? {
            filters.push(arg?.extract::<String>()?);
        }
    }
    Ok(())
}
