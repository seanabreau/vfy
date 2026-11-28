//! Native plugin activation over `CPython`'s standard distribution metadata protocol.
use super::Collector;
use pyo3::prelude::*;

mod arguments;
use arguments::{PluginDirective, plugin_arguments, raw_provider_excluded};

pub(super) struct PluginInputs {
    pub(super) configured: Vec<String>,
    pub(super) raw: Vec<String>,
}

type EntryPoint = (String, Py<PyAny>, Py<PyAny>);

enum PluginSource {
    Explicit,
    Autoload {
        entry: Py<PyAny>,
        distribution: Py<PyAny>,
    },
}

const COVERAGE_PLUGIN: &str = "vfy.independent.coverage_support";
const CONTROLLER_EVIDENCE_PLUGIN: &str = "vfy.independent.results";
const HYPOTHESIS_PLUGIN: &str = "hypothesispytest";
const HYPOTHESIS_ADAPTER: &str = "vfy.independent.hypothesis_support";
pub(super) const ASYNCIO_PLUGIN: &str = "asyncio";
const REPORTING_ADAPTER: &str = "vfy.pytest_reporting";
const CODSPEED_ADAPTER: &str = "vfy.independent.codspeed_support";

/// Import names that the plug-in manager reports under their canonical plug-in name.
const ALIASES: &[(&str, &str)] = &[
    ("vfy.pytest_plugin", COVERAGE_PLUGIN),
    ("pytest_cov", COVERAGE_PLUGIN),
    ("pytest_cov.plugin", COVERAGE_PLUGIN),
    ("_hypothesis_pytestplugin", HYPOTHESIS_PLUGIN),
    ("hypothesis.extra.pytestplugin", HYPOTHESIS_PLUGIN),
    (HYPOTHESIS_ADAPTER, HYPOTHESIS_PLUGIN),
    ("pytest_codspeed.plugin", "codspeed"),
    ("pytest_asyncio", ASYNCIO_PLUGIN),
    ("pytest_asyncio.plugin", ASYNCIO_PLUGIN),
    ("vfy.independent.asyncio_support", ASYNCIO_PLUGIN),
    (
        "vfy.independent.mock_fixtures",
        "vfy.independent.mock_support",
    ),
];

/// A vfy plug-in that fills in for a provider that did not load.
struct Replacement {
    entry_points: &'static [&'static str],
    explicit_names: &'static [&'static str],
    module: &'static str,
    alias_module: &'static str,
    aliases: &'static [&'static str],
}

const REPLACEMENTS: &[Replacement] = &[
    Replacement {
        entry_points: &[ASYNCIO_PLUGIN],
        explicit_names: &[],
        module: "vfy.independent.asyncio_support",
        alias_module: "vfy.independent.asyncio_support",
        aliases: &[],
    },
    Replacement {
        entry_points: &["pytest_mock"],
        explicit_names: &["pytest_mock.plugin"],
        module: "vfy.independent.mock_fixtures",
        alias_module: "vfy.independent.mock_support",
        aliases: &["pytest_mock"],
    },
    Replacement {
        entry_points: &["benchmark", "codspeed"],
        explicit_names: &["pytest_benchmark.plugin"],
        module: "vfy.independent.benchmark",
        alias_module: "vfy.independent.benchmark",
        aliases: &[],
    },
    Replacement {
        entry_points: &["timeout"],
        explicit_names: &["pytest_timeout"],
        module: "vfy.independent.timeout",
        alias_module: "vfy.independent.timeout",
        aliases: &[],
    },
    Replacement {
        entry_points: &["xdist"],
        explicit_names: &["xdist.plugin"],
        module: "vfy.independent.xdist_support",
        alias_module: "vfy.independent.xdist_support",
        aliases: &[],
    },
];

fn canonical_name(name: &str) -> &str {
    if name == REPORTING_ADAPTER {
        return "html";
    }
    ALIASES
        .iter()
        .find(|(alias, _)| *alias == name)
        .map_or(name, |&(_, canonical)| canonical)
}

/// Apply `action` to a plug-in name and to its `pytest_` spelling, which pytest also accepts.
fn each_spelling(name: &str, mut action: impl FnMut(&str) -> PyResult<()>) -> PyResult<()> {
    action(canonical_name(name))?;
    if !name.starts_with("pytest_") {
        action(canonical_name(&format!("pytest_{name}")))?;
    }
    Ok(())
}

fn block_plugin(manager: &Bound<'_, PyAny>, name: &str) -> PyResult<()> {
    arguments::validate_exclusion(name).map_err(crate::IndependentUsageError::new_err)?;
    if name == "cacheprovider" {
        manager.call_method1("set_blocked", ("stepwise",))?;
        manager.call_method1("set_blocked", ("pytest_stepwise",))?;
    }
    each_spelling(name, |spelling| {
        manager.call_method1("set_blocked", (spelling,)).map(drop)
    })
}

pub(super) fn exclude_cli_plugins(
    manager: &Bound<'_, PyAny>,
    arguments: &[String],
) -> PyResult<()> {
    install_aliases(manager)?;
    for argument in plugin_arguments(arguments) {
        if let PluginDirective::Disable(name) = PluginDirective::parse(argument) {
            block_plugin(manager, name)?;
        }
    }
    Ok(())
}

fn install_aliases(manager: &Bound<'_, PyAny>) -> PyResult<()> {
    let aliases = manager.getattr("_plugin_aliases")?;
    for &(alias, canonical) in ALIASES {
        aliases.set_item(alias, canonical)?;
    }
    Ok(())
}

/// pytest and this engine both skip plug-in autoloading when this variable is set.
fn autoload_disabled() -> bool {
    std::env::var_os("PYTEST_DISABLE_PLUGIN_AUTOLOAD").is_some_and(|value| !value.is_empty())
}

fn installed_distribution<'py>(py: Python<'py>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    let metadata = py.import("importlib.metadata")?;
    match metadata.call_method1("distribution", (name,)) {
        Ok(distribution) => Ok(Some(distribution)),
        Err(error) if error.is_instance(py, &metadata.getattr("PackageNotFoundError")?) => Ok(None),
        Err(error) => Err(error),
    }
}

/// The adapter substitutes for a single upstream pytest11 entry point.
fn adapter_distribution(py: Python<'_>, requested: &str) -> PyResult<Option<Py<PyAny>>> {
    let name = canonical_name(requested);
    let (package, module) = match name {
        ASYNCIO_PLUGIN if requested != "vfy.independent.asyncio_support" => {
            ("pytest-asyncio", "pytest_asyncio.plugin")
        }
        "codspeed" => ("pytest-codspeed", "pytest_codspeed.plugin"),
        _ => return Ok(None),
    };
    let distribution = installed_distribution(py, package)?;
    let entries = entrypoints(py)?
        .into_iter()
        .filter(|(key, _, _)| key == name)
        .collect::<Vec<_>>();
    let upstream_plugin = match (entries.as_slice(), &distribution) {
        ([], _) => return Ok(None),
        ([(_, entry, owner)], Some(installed)) => {
            let owner = owner.bind(py);
            let entry = entry.bind(py);
            let owner_name = owner
                .getattr("metadata")?
                .get_item("Name")?
                .extract::<String>()?;
            owner_name.to_ascii_lowercase().replace(['_', '.'], "-") == package
                && owner
                    .getattr("version")?
                    .eq(installed.getattr("version")?)?
                && owner
                    .call_method1("locate_file", ("",))?
                    .eq(installed.call_method1("locate_file", ("",))?)?
                && entry.getattr("value")?.extract::<String>()? == module
                && installed.getattr("entry_points")?.contains(entry)?
        }
        _ => false,
    };
    if !upstream_plugin {
        return Err(crate::UnsupportedFeatureError::new_err(format!(
            "unsupported {package} pytest11 entry point; expected one {name} = {module} from {package}. Use --engine=pytest to load the installed plugin."
        )));
    }
    Ok(distribution.map(Bound::unbind))
}

fn record_admitted_distribution(
    manager: &Bound<'_, PyAny>,
    plugin: &Bound<'_, PyAny>,
    distribution: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let entries = manager.getattr("_plugin_distinfo")?;
    for entry in entries.try_iter()? {
        if entry?.get_item(0)?.is(plugin) {
            return Ok(());
        }
    }
    entries.call_method1("append", ((plugin, distribution),))?;
    Ok(())
}

/// Accept final 6.x releases from 6.152.1, whose pytest plug-in the adapter mirrors.
fn hypothesis_version_supported(version: &str) -> bool {
    let release = version
        .split('.')
        .map(str::parse::<u64>)
        .collect::<Result<Vec<_>, _>>();
    matches!(release.as_deref(), Ok([6, minor, patch]) if (*minor, *patch) >= (152, 1))
}

fn hypothesis_plugin<'py>(
    py: Python<'py>,
    config: &Bound<'py, PyAny>,
    entry: Option<EntryPoint>,
) -> PyResult<(Bound<'py, PyAny>, Py<PyAny>)> {
    // Validate the maintained contract before acquiring aliases or changing the
    // fixture marker. Preserve real distribution provenance without importing
    // the distribution's pytest implementation.
    let distribution = match entry {
        Some((_, _, distribution)) => distribution,
        None => py
            .import("importlib.metadata")?
            .call_method1("distribution", ("hypothesis",))?
            .unbind(),
    };
    let version = distribution
        .bind(py)
        .getattr("version")?
        .extract::<String>()?;
    if !hypothesis_version_supported(&version) {
        return Err(crate::UnsupportedFeatureError::new_err(format!(
            "the independent Hypothesis adapter requires a 6.x release from 6.152.1; found {version}"
        )));
    }
    let plugin = py
        .import(HYPOTHESIS_ADAPTER)?
        .call_method1("build_plugin", (config, version))?;
    Ok((plugin, distribution))
}

fn entrypoints(py: Python<'_>) -> PyResult<Vec<EntryPoint>> {
    let mut result = Vec::new();
    for distribution in py
        .import("importlib.metadata")?
        .call_method0("distributions")?
        .try_iter()?
    {
        let distribution = distribution?;
        for entry in distribution.getattr("entry_points")?.try_iter()? {
            let entry = entry?;
            if entry.getattr("group")?.extract::<String>()? == "pytest11" {
                result.push((
                    entry.getattr("name")?.extract()?,
                    entry.unbind(),
                    distribution.clone().unbind(),
                ));
            }
        }
    }
    Ok(result)
}

impl<'py> Collector<'py> {
    pub(super) fn initialize_asyncio_imports(&self) -> PyResult<()> {
        // Public fixture declarations remain importable when the plugin is
        // disabled. Validate the adapter contract only when loading that plugin.
        let context = self.context.as_ref().expect("initialized plugin context");
        self.py
            .import("vfy.independent.asyncio_modules")?
            .call_method1("install", (&context.config,))?;
        Ok(())
    }

    pub(super) fn configured_plugins(
        &self,
        options: Option<&Bound<'_, pyo3::types::PyDict>>,
    ) -> PyResult<PluginInputs> {
        let Some(options) = options else {
            return Ok(PluginInputs {
                configured: Vec::new(),
                raw: Vec::new(),
            });
        };
        let context = self.context.as_ref().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "plugin arguments require a configured collector",
            )
        })?;
        let (_, arguments) = context
            .config
            .bind(self.py)
            .getattr("_configuration")?
            .call_method0("plugin_preparse_args")?
            .extract::<(Vec<String>, Vec<String>)>()?;
        let configured = plugin_arguments(&arguments)
            .map(str::to_owned)
            .collect::<Vec<_>>();
        // Reuse the actual parser merge's raw-input suffix. Iterating the caller's
        // original object again would repeat public iterable callbacks.
        let mut raw = Vec::new();
        if let Some(prefix) = context.raw_plugin_prefix.get()
            && let Some(raw_plugins) = options.get_item("plugins")?
        {
            raw.extend(
                raw_plugins
                    .extract::<Vec<String>>()?
                    .into_iter()
                    .skip(prefix),
            );
        }
        Ok(PluginInputs { configured, raw })
    }

    pub(super) fn exclude_raw_provider(&self, arguments: &[String], name: &str) -> PyResult<()> {
        if raw_provider_excluded(arguments, name) {
            let context = self.context.as_ref().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "raw provider exclusions require a configured collector",
                )
            })?;
            let manager = context.config.bind(self.py).getattr("pluginmanager")?;
            block_plugin(&manager, name)?;
        }
        Ok(())
    }

    pub(super) fn configure_plugin(&mut self, argument: &str) -> PyResult<()> {
        let context = self.context.as_ref().expect("initialized plugin context");
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        install_aliases(&manager)?;
        match PluginDirective::parse(argument) {
            PluginDirective::Disable(name) => block_plugin(&manager, name),
            PluginDirective::Enable(name) => {
                each_spelling(name, |spelling| {
                    manager.call_method1("unblock", (spelling,)).map(drop)
                })?;
                self.load_plugin(name)
            }
        }
    }

    pub(super) fn load_plugin(&mut self, requested: &str) -> PyResult<()> {
        self.load_plugin_from(requested, PluginSource::Explicit)
    }

    fn load_plugin_from(&mut self, requested: &str, source: PluginSource) -> PyResult<()> {
        let owned_reporting = requested == REPORTING_ADAPTER;
        let name = canonical_name(requested);
        let manager = self
            .context
            .as_ref()
            .map(|context| context.config.bind(self.py).getattr("pluginmanager"))
            .transpose()?;
        if let Some(manager) = &manager {
            install_aliases(manager)?;
            if manager.call_method1("is_blocked", (name,))?.is_truthy()? {
                return Ok(());
            }
        }
        // Owned support has no upstream dependency. Explicit aliases still need
        // admission when the owned plugin was already cached.
        let distribution = adapter_distribution(self.py, requested)?;
        if let Some(plugin) = self.plugins.get(name) {
            if owned_reporting
                && plugin
                    .bind(self.py)
                    .getattr("__name__")?
                    .extract::<String>()?
                    != REPORTING_ADAPTER
            {
                return Err(crate::IndependentUsageError::new_err(
                    "vfy replaces the 'html' plug-in; load -p vfy.pytest_reporting before pytest-html",
                ));
            }
            if let (Some(context), Some(manager)) = (&self.context, &manager)
                && !manager.call_method1("has_plugin", (name,))?.is_truthy()?
            {
                context.register(self.py, plugin.clone_ref(self.py), name)?;
            }
            if let (Some(distribution), Some(manager)) = (&distribution, &manager) {
                record_admitted_distribution(
                    manager,
                    plugin.bind(self.py),
                    distribution.bind(self.py),
                )?;
            }
            return Ok(());
        }
        if name == "terminal" {
            let (Some(context), Some(manager)) = (&self.context, &manager) else {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "terminal activation requires a configured collector",
                ));
            };
            if !manager.call_method1("has_plugin", (name,))?.is_truthy()? {
                let plugin = context.terminal_status_hooks(self.py)?;
                context.register(self.py, plugin, name)?;
            }
            return Ok(());
        }
        match name {
            // pytest loads these builtin plug-ins itself; `-p` names them as no-ops.
            // vfy does not run pytest in-process, so `pytester` is a no-op too and its
            // fixture is absent; tests that request it report the missing fixture.
            "mark" | "main" | "runner" | "helpconfig" | "python" | "debugging" | "unittest"
            | "skipping" | "pastebin" | "assertion" | "freeze_support" | "setuponly"
            | "setupplan" | "stepwise" | "reports" | "python_path" | "pytester" => {
                return Ok(());
            }
            "warnings" => return self.activate_warning_provider(),
            "fixtures" | "legacypath" | "tmpdir" | "monkeypatch" | "recwarn" | "logging" => {
                return self.activate_builtin_provider(name);
            }
            "capture" => return self.activate_capture_provider(),
            "subtests" => return self.activate_subtests_provider(),
            "cacheprovider" => return self.activate_cache_provider(),
            "junitxml" => return self.activate_property_provider(),
            "doctest" => return self.activate_doctest_provider(),
            "unraisableexception" => return self.activate_unraisable_provider(),
            "threadexception" | "faulthandler" => return self.activate_runtime_provider(name),
            _ => {}
        }
        let (plugin, distribution) = self.import_plugin(requested, distribution, source)?;
        self.plugins
            .insert(name.to_owned(), plugin.clone().unbind());
        // Only explicit owned activation gives this alias the HTML identity;
        // raw pytest-html autoloading still registers its upstream provider.
        if owned_reporting && let Some(manager) = &manager {
            manager
                .getattr("_plugin_aliases")?
                .set_item(REPORTING_ADAPTER, "html")?;
        }
        if let Some(context) = &self.context {
            context.register(self.py, plugin.clone().unbind(), name)?;
        }
        if let (Some(distribution), Some(manager)) = (distribution, manager) {
            record_admitted_distribution(&manager, &plugin, distribution.bind(self.py))?;
        }
        self.load_declared_plugins(&plugin)
    }

    fn import_plugin(
        &self,
        requested: &str,
        adapter_distribution: Option<Py<PyAny>>,
        source: PluginSource,
    ) -> PyResult<(Bound<'py, PyAny>, Option<Py<PyAny>>)> {
        let name = canonical_name(requested);
        let owned_reporting = requested == REPORTING_ADAPTER;
        // vfy-owned modules cannot be replaced by same-named entry points.
        let owned_plugin = matches!(requested, COVERAGE_PLUGIN | CONTROLLER_EVIDENCE_PLUGIN)
            || REPLACEMENTS.iter().any(|replacement| {
                requested == replacement.module || requested == replacement.alias_module
            });
        let entry =
            if name == ASYNCIO_PLUGIN || name == "codspeed" || owned_reporting || owned_plugin {
                None
            } else {
                match source {
                    PluginSource::Autoload {
                        entry,
                        distribution,
                    } if requested == name => Some((name.to_owned(), entry, distribution)),
                    PluginSource::Explicit | PluginSource::Autoload { .. } => entrypoints(self.py)?
                        .into_iter()
                        .find(|(entry_name, _, _)| entry_name == name),
                }
            };
        let (plugin, distribution) = if owned_reporting {
            let context = self.context.as_ref().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "owned reporting activation requires an independent configuration",
                )
            })?;
            let plugin = self
                .py
                .import("vfy.independent.reporting")?
                .call_method1("install", (context.config.bind(self.py),))?;
            (plugin, None)
        } else if name == ASYNCIO_PLUGIN {
            let context = self.context.as_ref().expect("initialized plugin context");
            let imports = context
                .config
                .bind(self.py)
                .getattr("_vfy_asyncio_imports")?;
            let plugin = if requested == "vfy.independent.asyncio_support" {
                imports.getattr("plugin")?
            } else {
                imports.call_method0("upstream_plugin")?
            };
            (plugin, adapter_distribution)
        } else if name == "codspeed" && adapter_distribution.is_some() {
            let plugin = self.py.import(CODSPEED_ADAPTER)?.into_any();
            let context = self.context.as_ref().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "CodSpeed adapter activation requires an independent configuration",
                )
            })?;
            plugin.call_method1("install_import_alias", (context.config.bind(self.py),))?;
            (plugin, adapter_distribution)
        } else if name == HYPOTHESIS_PLUGIN {
            let context = self.context.as_ref().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "Hypothesis plugin activation requires an independent configuration",
                )
            })?;
            let (plugin, distribution) =
                hypothesis_plugin(self.py, context.config.bind(self.py), entry)?;
            (plugin, Some(distribution))
        } else if let Some((_, entry, distribution)) = entry {
            let module = entry.bind(self.py).getattr("module")?.extract::<String>()?;
            self.py
                .import("vfy.independent.imports")?
                .call_method1("register_assert_rewrite", (module,))?;
            (
                entry.bind(self.py).call_method0("load")?,
                Some(distribution),
            )
        } else {
            // With no admitted CodSpeed distribution, preserve the explicit
            // module spelling so the import failure names the missing package.
            let import_name = if owned_plugin || name == "codspeed" {
                requested
            } else {
                name
            };
            self.py
                .import("vfy.independent.imports")?
                .call_method1("register_assert_rewrite", (import_name,))?;
            (self.py.import(import_name)?.into_any(), None)
        };
        Ok((plugin, distribution))
    }

    pub(super) fn load_environment_plugins(&mut self) -> PyResult<()> {
        let specifications = self
            .py
            .import("os")?
            .getattr("environ")?
            .call_method1("get", ("PYTEST_PLUGINS",))?
            .extract::<Option<String>>()?;
        if let Some(specifications) = specifications
            && !specifications.is_empty()
        {
            for name in specifications.split(',') {
                self.load_plugin(name)?;
            }
        }
        Ok(())
    }

    pub(super) fn autoload_plugins(&mut self) -> PyResult<()> {
        if autoload_disabled() {
            return Ok(());
        }
        let distributions = self
            .py
            .import("importlib.metadata")?
            .call_method0("distributions")?;
        let distributions = self
            .py
            .import("builtins")?
            .getattr("list")?
            .call1((distributions,))?;
        for distribution in distributions.try_iter()? {
            let distribution = distribution?;
            for entry in distribution.getattr("entry_points")?.try_iter()? {
                let entry = entry?;
                if entry.getattr("group")?.extract::<String>()? != "pytest11" {
                    continue;
                }
                let name = entry.getattr("name")?.extract::<String>()?;
                self.load_plugin_from(
                    &name,
                    PluginSource::Autoload {
                        entry: entry.unbind(),
                        distribution: distribution.clone().unbind(),
                    },
                )?;
            }
        }
        Ok(())
    }

    /// Fill in missing providers after explicit and automatic plug-in loading.
    pub(super) fn load_replacements(&mut self) -> PyResult<()> {
        // pytest would not autoload the replaced distributions either.
        if autoload_disabled() {
            return Ok(());
        }
        let Some(config) = self
            .context
            .as_ref()
            .map(|context| context.config.clone_ref(self.py))
        else {
            return Ok(());
        };
        let config = config.bind(self.py);
        let manager = config.getattr("pluginmanager")?;
        'replacements: for replacement in REPLACEMENTS {
            for name in replacement.entry_points {
                if manager.call_method1("is_blocked", (*name,))?.is_truthy()? {
                    continue 'replacements;
                }
            }
            // Explicit module loads do not need an entry point to own their hooks.
            for name in replacement
                .entry_points
                .iter()
                .chain(replacement.explicit_names)
            {
                if manager.call_method1("has_plugin", (*name,))?.is_truthy()? {
                    continue 'replacements;
                }
            }
            if manager
                .call_method1("is_blocked", (replacement.module,))?
                .is_truthy()?
            {
                continue;
            }
            self.load_plugin(replacement.module)?;
            for alias in replacement.aliases {
                let modules = self.py.import("sys")?.getattr("modules")?;
                if !modules.contains(*alias)?
                    && self
                        .py
                        .import("importlib.util")?
                        .call_method1("find_spec", (*alias,))?
                        .is_none()
                {
                    self.py
                        .import("vfy.independent.plugin_aliases")?
                        .call_method1(
                            "install_plugin_alias",
                            (config, *alias, replacement.alias_module),
                        )?;
                }
            }
        }
        Ok(())
    }

    pub(super) fn load_controller_defaults(&mut self) -> PyResult<()> {
        if std::env::var_os("VFY_TEST_RESULTS").is_none_or(|value| value.is_empty()) {
            return Ok(());
        }
        let (Some(evidence), Some(context)) =
            (self.plugins.get(CONTROLLER_EVIDENCE_PLUGIN), &self.context)
        else {
            return Ok(());
        };
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if !manager
            .call_method1("getplugin", (CONTROLLER_EVIDENCE_PLUGIN,))?
            .is(evidence.bind(self.py))
        {
            return Ok(());
        }
        // The controller's existing result transport and registered evidence
        // adapter identify its owned child. Installed entry points retain their
        // real activation order; an explicit loaded or blocked coverage provider
        // keeps its user-directed priority through the ordinary loader.
        self.load_plugin(COVERAGE_PLUGIN)
    }
}

#[cfg(test)]
mod tests;
