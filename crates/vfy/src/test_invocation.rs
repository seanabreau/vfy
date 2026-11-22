//! Python-object preserving boundary for shared test invocation preparation.
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PySet, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use vfy_core::test_invocation::{
    self, Backend, ControllerWorkers, Coverage, Distribution, DistributionRequest, Inputs, Mode,
    Replay, Scheduling, SchedulingAction, Scope, Selection, Workers,
};

/// Keeps original argv and path objects, including lone surrogate characters.
#[pyclass(module = "vfy._native")]
#[derive(Default)]
struct NativeTestInvocation {
    targets: Option<Py<PyList>>,
    location: Option<Py<PyAny>>,
    pytest_args: Option<Py<PyList>>,
    baseline_pytest_args: Option<Py<PyList>>,
}

fn list_copy<'py>(py: Python<'py>, source: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    out.call_method1("extend", (source,))?;
    Ok(out)
}

fn value<'py>(facts: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    facts
        .get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("missing invocation fact: {key}")))
}
fn flag(facts: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    value(facts, key)?.is_truthy()
}

fn coverage_policy(facts: &Bound<'_, PyDict>) -> PyResult<Coverage> {
    if !flag(facts, "coverage_enabled")? {
        Ok(Coverage::Disabled)
    } else if flag(facts, "cov_append")? {
        Ok(Coverage::Append)
    } else {
        Ok(Coverage::Replace)
    }
}

struct DecodedFacts<'py> {
    input: Inputs,
    workers: Bound<'py, PyAny>,
    scheduling: Bound<'py, PyAny>,
    explicit: Bound<'py, PyAny>,
}

fn decode_facts<'py>(
    py: Python<'py>,
    facts: &Bound<'py, PyDict>,
    location_is_none: bool,
) -> PyResult<DecodedFacts<'py>> {
    let engine_name = value(facts, "engine")?.extract::<String>()?;
    let backend = match engine_name.as_str() {
        "native" => Backend::Independent {
            workers_explicit: flag(facts, "independent_workers_configured")?,
        },
        "pytest-native" => Backend::Native {
            xdist_requested: flag(facts, "native_xdist_requested")?,
        },
        "pytest" => Backend::Pytest,
        other => {
            return Err(PyValueError::new_err(format!(
                "invalid test engine {other:?}; expected 'native', 'pytest-native' or 'pytest'"
            )));
        }
    };
    let mode_name = value(facts, "mode")?.extract::<String>()?;
    let mode = match mode_name.as_str() {
        "full" => Mode::Full,
        "safe" => Mode::Safe,
        "balanced" => Mode::Balanced,
        "aggressive" => Mode::Aggressive,
        other => {
            return Err(PyValueError::new_err(format!(
                "invalid test mode {other:?}; expected 'full', 'safe', 'balanced' or 'aggressive'"
            )));
        }
    };
    let workers = value(facts, "workers")?;
    let worker_kind = if workers.is_none() {
        Workers::Unset
    } else if workers.eq(1)? {
        Workers::One
    } else {
        Workers::Other
    };
    let scheduling = value(facts, "scheduling")?;
    let scheduling = if scheduling.is_truthy()? {
        scheduling
    } else {
        value(facts, "env_scheduling")?
    };
    let scheduling = if scheduling.is_none() {
        let default = test_invocation::default_scheduling(backend, worker_kind);
        default.into_pyobject(py)?.into_any()
    } else {
        scheduling
    };
    let schedule_kind = if scheduling.eq("scope")? {
        Scheduling::Scope
    } else if scheduling.eq("cases")? {
        Scheduling::Cases
    } else if scheduling.eq("striped")? {
        Scheduling::Striped
    } else if scheduling.eq("demand")? {
        Scheduling::Demand
    } else {
        Scheduling::Other
    };
    let explicit = value(facts, "explicit_distribution")?;
    let coverage = coverage_policy(facts)?;
    let input = Inputs {
        backend,
        selection: Selection {
            mode,
            force_full: flag(facts, "force_full")?,
            fresh: flag(facts, "fresh")?,
        },
        scope: Scope {
            target_location: !location_is_none,
            invocation_location: flag(facts, "invocation_location")?,
            filtered: flag(facts, "filtered_scope")?,
        },
        coverage,
        replay: if flag(facts, "no_replay")? {
            Replay::Disabled
        } else {
            Replay::Allowed
        },
        workers: worker_kind,
        scheduling: schedule_kind,
        distribution: if explicit.is_none() {
            DistributionRequest::Infer
        } else {
            DistributionRequest::Explicit
        },
    };
    Ok(DecodedFacts {
        input,
        workers,
        scheduling,
        explicit,
    })
}

fn project_plan<'py>(
    py: Python<'py>,
    plan: test_invocation::Plan,
    workers: Bound<'py, PyAny>,
    scheduling: &Bound<'py, PyAny>,
    explicit: Bound<'py, PyAny>,
) -> PyResult<Py<PyDict>> {
    let out = PyDict::new(py);
    out.set_item(
        "mode",
        match plan.mode {
            Mode::Full => "full",
            Mode::Safe => "safe",
            Mode::Balanced => "balanced",
            Mode::Aggressive => "aggressive",
        },
    )?;
    out.set_item("fresh", plan.fresh)?;
    if plan.controller_workers == ControllerWorkers::None {
        out.set_item("controller_workers", py.None())?;
    } else {
        out.set_item("controller_workers", workers)?;
    }
    if plan.scheduling == SchedulingAction::Requested {
        out.set_item("scheduling", scheduling)?;
    } else {
        out.set_item("scheduling", py.None())?;
    }
    match plan.distribution {
        Distribution::Explicit => out.set_item("distribution", explicit)?,
        Distribution::No => out.set_item("distribution", "no")?,
        Distribution::PytestConfig => out.set_item("distribution", "pytest-config")?,
        Distribution::Load => out.set_item("distribution", "load")?,
        Distribution::Loadfile => out.set_item("distribution", "loadfile")?,
        Distribution::NativeScheduling => out.set_item(
            "distribution",
            format!("native-{}", scheduling.str()?.to_str()?),
        )?,
    }
    out.set_item("replay_enabled", plan.replay == Replay::Allowed)?;
    Ok(out.unbind())
}

#[pymethods]
impl NativeTestInvocation {
    /// Construct the pre-configuration token sequence once for every engine.
    #[staticmethod]
    fn start(
        py: Python<'_>,
        targets: &Bound<'_, PyAny>,
        location: Py<PyAny>,
        pytest_args: &Bound<'_, PyAny>,
        baseline_pytest_args: &Bound<'_, PyAny>,
        rcfile: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let targets = list_copy(py, targets)?;
        let args = list_copy(py, pytest_args)?;
        let baseline = list_copy(py, baseline_pytest_args)?;
        if rcfile.is_truthy()? {
            let mut supplied = false;
            for arg in baseline.iter() {
                // Use the same live Python equality/startswith semantics as controller.
                let prefixes = PyTuple::new(py, ["--config-file=", "-c"])?;
                if arg.eq("-c")?
                    || arg.eq("--config-file")?
                    || arg.call_method1("startswith", (prefixes,))?.is_truthy()?
                {
                    supplied = true;
                    break;
                }
            }
            if !supplied {
                baseline.insert(0, rcfile.str()?)?;
                baseline.insert(0, "-c")?;
            }
        }
        if !targets.is_empty() && location.bind(py).is_none() {
            for (index, target) in targets.iter().enumerate() {
                args.insert(index, target)?;
            }
        }
        Ok(Self {
            targets: Some(targets.unbind()),
            location: Some(location),
            pytest_args: Some(args.unbind()),
            baseline_pytest_args: Some(baseline.unbind()),
        })
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes the GC visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(value) = &self.targets {
            visit.call(value)?;
        }
        if let Some(value) = &self.location {
            visit.call(value)?;
        }
        if let Some(value) = &self.pytest_args {
            visit.call(value)?;
        }
        if let Some(value) = &self.baseline_pytest_args {
            visit.call(value)?;
        }
        Ok(())
    }
    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = std::mem::take(&mut *slf.borrow_mut());
        drop(removed);
    }
    #[getter]
    fn pytest_args(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        self.pytest_args
            .as_ref()
            .map(|v| v.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("cleared invocation"))
    }
    #[getter]
    fn baseline_pytest_args(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        self.baseline_pytest_args
            .as_ref()
            .map(|v| v.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("cleared invocation"))
    }
    #[getter]
    fn targets(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        self.targets
            .as_ref()
            .map(|v| v.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("cleared invocation"))
    }
    #[getter]
    fn location(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.location
            .as_ref()
            .map(|v| v.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("cleared invocation"))
    }
    /// Keep pytest's first positional out of the mutation location slot.
    #[staticmethod]
    fn mutation_cli_arguments(
        target: Option<String>,
        mut pytest_args: Vec<String>,
    ) -> PyResult<(Option<String>, Vec<String>)> {
        if vfy_core::mutation::mutation_location(target.as_deref())
            .map_err(PyValueError::new_err)?
            .is_some()
        {
            return Ok((target, pytest_args));
        }
        if let Some(target) = target {
            pytest_args.insert(0, target);
        }
        Ok((None, pytest_args))
    }

    /// Override inherited pytest concurrency within the mutation worker budget.
    #[staticmethod]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts owned vectors for pytest argument lists"
    )]
    fn mutation_worker_args(
        py: Python<'_>,
        effective_args: Vec<String>,
        cli_args: Vec<String>,
    ) -> PyResult<Vec<String>> {
        let version = py.version_info();
        vfy_core::pytest_config::mutation_worker_arguments(
            &effective_args,
            &cli_args,
            vfy_core::pytest_config::PytestParserBehavior::for_cpython(
                version.major,
                version.minor,
                version.patch,
            ),
        )
        .map_err(PyValueError::new_err)
    }

    /// Keep implicit mutation collection from activating unrelated report gates.
    #[staticmethod]
    fn mutation_baseline_coverage_policy(
        py: Python<'_>,
        arguments: &Bound<'_, PyAny>,
    ) -> PyResult<&'static str> {
        let arguments = arguments.extract::<Vec<String>>()?;
        let version = py.version_info();
        Ok(vfy_core::pytest_config::mutation_baseline_coverage_policy(
            &arguments,
            vfy_core::pytest_config::PytestParserBehavior::for_cpython(
                version.major,
                version.minor,
                version.patch,
            ),
        )
        .as_str())
    }

    /// Preserve original Python text while reproducing pytest plugin token scanning.
    #[staticmethod]
    fn plugin_names(py: Python<'_>, args: &Bound<'_, PyAny>) -> PyResult<Py<PyList>> {
        // The source sequence stays live: iteration order, mutations and errors
        // must match pytest's preparse helper. Strip only after the complete scan.
        let raw_names = PyList::empty(py);
        let mut index = 0;
        let switches = PySet::new(py, ["-p", "--plugin"])?;
        while index < args.len()? {
            let arg = args.get_item(index)?;
            if switches.contains(&arg)? && index + 1 < args.len()? {
                raw_names.append(args.get_item(index + 1)?)?;
                index += 2;
                continue;
            }
            let prefixes = PyTuple::new(py, ["-p=", "--plugin="])?;
            if arg.call_method1("startswith", (prefixes,))?.is_truthy()? {
                raw_names.append(arg.call_method1("partition", ("=",))?.get_item(2)?)?;
            } else if arg.call_method1("startswith", ("-p",))?.is_truthy()?
                && arg
                    .rich_compare("-p", pyo3::basic::CompareOp::Ne)?
                    .is_truthy()?
            {
                let slice = pyo3::types::PySlice::new(py, 2, isize::MAX, 1);
                raw_names.append(arg.get_item(slice)?)?;
            }
            index += 1;
        }
        let names = PyList::empty(py);
        for name in raw_names.iter() {
            names.append(name.call_method0("strip")?)?;
        }
        Ok(names.unbind())
    }

    /// Keep coverage CLI source overrides distinct from selection graph roots.
    #[staticmethod]
    fn coverage_report_sources(
        py: Python<'_>,
        arguments: &Bound<'_, PyAny>,
        fallback_roots: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Vec<String>>> {
        let arguments = arguments.extract::<Vec<String>>()?;
        let fallback_roots = fallback_roots.extract::<Vec<String>>()?;
        let version = py.version_info();
        Ok(vfy_core::pytest_config::coverage_report_sources(
            &arguments,
            &fallback_roots,
            vfy_core::pytest_config::PytestParserBehavior::for_cpython(
                version.major,
                version.minor,
                version.patch,
            ),
        ))
    }

    /// Independent prefix, evaluated from live configured and execution names.
    #[staticmethod]
    fn independent_plugin_prefix(
        py: Python<'_>,
        configured: &Bound<'_, PyAny>,
        pytest_args: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyList>> {
        let names = list_copy(py, configured)?;
        let execution_names = Self::plugin_names(py, pytest_args)?;
        names.call_method1("extend", (execution_names,))?;
        let mut has_asyncio = false;
        let mut blocks_reporting = false;
        let reporting_names = PySet::new(
            py,
            [
                "html",
                "pytest_html",
                "pytest_html.plugin",
                "vfy.pytest_reporting",
            ],
        )?;
        let asyncio_names = PySet::new(
            py,
            [
                "asyncio",
                "pytest_asyncio",
                "pytest_asyncio.plugin",
                "vfy.independent.asyncio_support",
            ],
        )?;
        for name in names.iter() {
            let blocked = name.call_method1("startswith", ("no:",))?.is_truthy()?;
            let name = name.call_method1("removeprefix", ("no:",))?;
            let name = name.call_method1("removeprefix", ("pytest-",))?;
            has_asyncio |= asyncio_names.contains(&name)?;
            blocks_reporting |= blocked && reporting_names.contains(&name)?;
        }
        let result = PyList::empty(py);
        // Registering the adapter before the user's `-p no:` would register it twice
        // when the user enables it again; pytest registers a plug-in once per activation.
        let adapter: &[&str] = if blocks_reporting {
            &["-p", "vfy.independent.results"]
        } else {
            &[
                "-p",
                "vfy.independent.results",
                "-p",
                "vfy.pytest_reporting",
            ]
        };
        for name in adapter {
            result.append(*name)?;
        }
        if !has_asyncio {
            result.append("-p")?;
            result.append("vfy.independent.asyncio_support")?;
        }
        Ok(result.unbind())
    }
    /// Finish after projecting the actual configuration; no repeated config lookup.
    fn finish(slf: &Bound<'_, Self>, facts: &Bound<'_, PyDict>) -> PyResult<Py<PyDict>> {
        let py = slf.py();
        let location_is_none = {
            let borrowed = slf.borrow();
            borrowed
                .location
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("cleared invocation"))?
                .bind(py)
                .is_none()
        };
        let decoded = decode_facts(py, facts, location_is_none)?;
        let plan = test_invocation::finish(&decoded.input).map_err(|e| {
            PyValueError::new_err(match e {
                test_invocation::Error::AllRequiresUnfilteredSafe => {
                    "--all requires the unfiltered safe test workflow"
                }
                test_invocation::Error::AllWithSelection => {
                    "--all cannot be combined with pytest test-selection options"
                }
            })
        })?;
        project_plan(
            py,
            plan,
            decoded.workers,
            &decoded.scheduling,
            decoded.explicit,
        )
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeTestInvocation>()?;
    Ok(())
}
