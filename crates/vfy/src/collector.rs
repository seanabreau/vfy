//! `CPython` monitoring callbacks that record coverage into Rust stores.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::Arc;
use std::thread::ThreadId;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

use crate::data::CoverageData;
use vfy_core::{TraceOptions, TraceStore};

mod registry;
use registry::{
    CodeRegistry, DiscoveryOutcome, ExcludedCodeRetention, FrameSource, PathPolicy, PluginEvent,
    PluginRequest, interpreter_roots,
};

type BranchEvents = HashMap<usize, HashMap<(String, String), HashSet<(i32, i32)>>>;

struct ResolvedPluginEvent {
    path: Option<String>,
    line_range: Option<(i32, i32)>,
}

#[derive(Clone, Copy)]
enum PluginEventKind {
    Frame,
    Line,
    Branch { origin: i32, destination: i32 },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CollectionStrategy {
    Lines,
    LegacyBranches,
    DirectBranches { retire_events: bool },
    Semantic { direct_arcs: bool },
}

impl CollectionStrategy {
    const fn decisions(self) -> bool {
        matches!(self, Self::Semantic { .. })
    }

    fn cancellation_exception(self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        self.decisions()
            .then(|| {
                py.import("asyncio")?
                    .getattr("CancelledError")
                    .map(Bound::unbind)
            })
            .transpose()
    }

    const fn retires_events(self) -> bool {
        matches!(
            self,
            Self::DirectBranches {
                retire_events: true
            }
        )
    }

    const fn direct_arcs(self) -> bool {
        matches!(
            self,
            Self::DirectBranches { .. } | Self::Semantic { direct_arcs: true }
        )
    }

    /// Line collection disables excluded code on its first event and keys no
    /// collector state by its address, so the registry can hold it weakly.
    const fn excluded_code_retention(self, can_disable_events: bool) -> ExcludedCodeRetention {
        if matches!(self, Self::Lines) && can_disable_events {
            ExcludedCodeRetention::Weak
        } else {
            ExcludedCodeRetention::Strong
        }
    }

    const fn derives_arcs_from_lines(self) -> bool {
        matches!(
            self,
            Self::LegacyBranches | Self::Semantic { direct_arcs: false }
        )
    }
}

// Importing `sys` from a callback resolves `__import__` through the measured
// frame's builtins, which restricted globals such as namedtuple's need not have.
static GET_FRAME: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();

/// Resolve `sys._getframe` at module import, before any measured callback runs.
pub(crate) fn prepare_frame_lookup(py: Python<'_>) -> PyResult<()> {
    frame_lookup(py).map(|_| ())
}

fn frame_lookup(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    GET_FRAME
        .get_or_try_init(py, || {
            py.import("sys")?.getattr("_getframe").map(Bound::unbind)
        })
        .map(|lookup| lookup.bind(py))
}

fn current_frame(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    // sys.monitoring enters Rust while the measured Python frame is current.
    frame_lookup(py)?.call0()
}

fn is_cancelled_exception(
    exception: &Bound<'_, PyAny>,
    cancelled_error: &Py<PyAny>,
) -> PyResult<bool> {
    let py = exception.py();
    let mut pending = vec![exception.clone().unbind()];
    let mut seen = HashSet::default();
    while let Some(candidate) = pending.pop() {
        let candidate = candidate.bind(py);
        let identity = candidate.as_ptr() as usize;
        if !seen.insert(identity) {
            continue;
        }
        if candidate.is_instance(cancelled_error.bind(py))? {
            return Ok(true);
        }
        match candidate.getattr("exceptions") {
            Ok(children) => pending.extend(children.extract::<Vec<Py<PyAny>>>()?),
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(false)
}

/// Collect line, branch, and semantic events from `sys.monitoring`.
#[pyclass(module = "vfy._native")]
pub(crate) struct CollectorCore {
    context: String,
    base_context: String,
    disable_value: Option<Py<PyAny>>,
    strategy: CollectionStrategy,
    local_legacy_frames: bool,
    suppress_plain_yield_exit: bool,
    monitoring: Py<PyAny>,
    monitoring_tool_id: i32,
    line_event: u32,
    instruction_event: u32,
    local_branch_events: u32,
    // Strong references keep installed masks tied to stable code identities
    // until shutdown clears them, including masks restored after a fork.
    locally_configured: HashMap<usize, Py<PyAny>>,
    // Measured code objects with locations retired in the current context.
    retired_in_context: HashSet<usize>,
    cancelled_error: Option<Py<PyAny>>,
    dynamic_switchers: DynamicContextSwitchers,
    // Each thread owns its starting frame and recursive re-entries. Context
    // labels are shared between threads, as in Coverage.py's data collector.
    active_dynamic: HashMap<ThreadId, ActiveDynamicContext>,
    registry: CodeRegistry,
    trace: TraceStore,
    // Deduplicated bytecode edges retain context until snapshot-time resolution.
    // Source parsing and branch-trail walking never run on this event path.
    branch_events: BranchEvents,
}

#[pymethods]
impl CollectorCore {
    #[new]
    #[pyo3(signature = (branch, source_roots, package_file, include, omit, context="", plugins=Vec::new(), dynamic_plugins=Vec::new(), cover_pylib=false, sysmon_arcs=false, relative_files=false, decisions=false, disable_sysmon_events=false, source_pkgs=Vec::new(), dynamic_context=""))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts constructor arguments by value."
    )]
    #[expect(
        clippy::too_many_arguments,
        reason = "Python constructor mirrors collector configuration."
    )]
    #[expect(
        clippy::fn_params_excessive_bools,
        reason = "Compatibility-shaped Coverage options retain their documented booleans."
    )]
    fn new(
        py: Python<'_>,
        branch: bool,
        source_roots: Vec<String>,
        package_file: String,
        include: Vec<String>,
        omit: Vec<String>,
        context: &str,
        plugins: Vec<Py<PyAny>>,
        dynamic_plugins: Vec<Py<PyAny>>,
        cover_pylib: bool,
        sysmon_arcs: bool,
        relative_files: bool,
        decisions: bool,
        disable_sysmon_events: bool,
        source_pkgs: Vec<String>,
        dynamic_context: &str,
    ) -> PyResult<Self> {
        let dynamic_switchers = DynamicContextSwitchers::new(py, dynamic_plugins, dynamic_context)?;
        let has_dynamic_contexts = dynamic_switchers.enabled();
        let package_root = Path::new(&package_file).parent().ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "package file {package_file:?} has no parent directory; pass the path of a file inside the package"
            ))
        })?;
        let can_disable_events =
            plugins.is_empty() && !has_dynamic_contexts && (!branch || !decisions);
        let retire_events = disable_sysmon_events && branch && sysmon_arcs;
        let strategy = if decisions {
            CollectionStrategy::Semantic {
                direct_arcs: sysmon_arcs,
            }
        } else if sysmon_arcs {
            CollectionStrategy::DirectBranches { retire_events }
        } else if branch {
            CollectionStrategy::LegacyBranches
        } else {
            CollectionStrategy::Lines
        };
        let local_legacy_frames =
            branch && !sysmon_arcs && !decisions && plugins.is_empty() && !has_dynamic_contexts;
        let monitoring = py.import("sys")?.getattr("monitoring")?;
        let disable_value = if can_disable_events || strategy.retires_events() {
            Some(monitoring.getattr("DISABLE")?.unbind())
        } else {
            None
        };
        let monitoring_tool_id = monitoring.getattr("COVERAGE_ID")?.extract()?;
        let events = monitoring.getattr("events")?;
        let line_event = events.getattr("LINE")?.extract()?;
        let instruction_event = events.getattr("INSTRUCTION")?.extract()?;
        let local_branch_events = if strategy.retires_events() {
            line_event
                | events.getattr("BRANCH_LEFT")?.extract::<u32>()?
                | events.getattr("BRANCH_RIGHT")?.extract::<u32>()?
        } else if local_legacy_frames {
            let branch_event = if events.hasattr("BRANCH")? {
                events.getattr("BRANCH")?.extract::<u32>()?
            } else {
                events.getattr("BRANCH_LEFT")?.extract::<u32>()?
                    | events.getattr("BRANCH_RIGHT")?.extract::<u32>()?
            };
            line_event
                | events.getattr("PY_RESUME")?.extract::<u32>()?
                | events.getattr("PY_RETURN")?.extract::<u32>()?
                | events.getattr("PY_YIELD")?.extract::<u32>()?
                | branch_event
                | events.getattr("JUMP")?.extract::<u32>()?
        } else {
            line_event
        };
        let cancelled_error = strategy.cancellation_exception(py)?;
        let mut registry = CodeRegistry::new_with_unicode_version(
            &source_roots,
            &interpreter_roots(py)?,
            &package_root.to_string_lossy(),
            &include,
            &omit,
            plugins,
            PathPolicy::new(cover_pylib, relative_files),
            vfy_core::UnicodeCaseVersion::for_python_minor(py.version_info().minor),
        )?;
        registry.set_source_packages(py, source_pkgs)?;
        registry.ignore_annotation_code = strategy.direct_arcs()
            || (matches!(strategy, CollectionStrategy::Lines) && py.version_info() >= (3, 14));
        registry.excluded_code = strategy.excluded_code_retention(can_disable_events);
        if py.version_info() < (3, 13) && can_disable_events && !decisions {
            registry.prepare_instruction_monitoring(py)?;
        }
        if strategy.derives_arcs_from_lines() {
            registry.prepare_generator_exit_tracking(py)?;
        }
        Ok(Self {
            context: context.to_owned(),
            base_context: context.to_owned(),
            disable_value,
            strategy,
            local_legacy_frames,
            // CTracer on CPython 3.14+ records an exit arc when a plain yield
            // unwinds during close; older interpreters omit that arc.
            suppress_plain_yield_exit: py.version_info() < (3, 14),
            monitoring: monitoring.unbind(),
            monitoring_tool_id,
            line_event,
            instruction_event,
            local_branch_events,
            locally_configured: HashMap::default(),
            retired_in_context: HashSet::default(),
            cancelled_error,
            dynamic_switchers,
            active_dynamic: HashMap::default(),
            registry,
            trace: TraceStore::with_options(TraceOptions {
                branch,
                sysmon_arcs: strategy.direct_arcs(),
                decisions,
            }),
            branch_events: BranchEvents::default(),
        })
    }

    fn switch_context(&mut self, context: Option<&str>) {
        self.trace.share_finished_lines();
        context.unwrap_or_default().clone_into(&mut self.context);
        context
            .unwrap_or_default()
            .clone_into(&mut self.base_context);
    }

    /// Re-enable the locations retired in the previous context.
    ///
    /// Reinstall local masks when that restores all retired events. Returns
    /// `false` when the caller must restart monitoring.
    fn reactivate_retired_locations(&mut self, py: Python<'_>) -> PyResult<bool> {
        let Some(base_mask) = self.context_local_mask() else {
            return Ok(false);
        };
        // On CPython 3.13+, toggling the local LINE mask does not restore
        // disabled events on the first line of code such as a one-line lambda.
        if self.strategy == CollectionStrategy::Lines
            && !self.retired_in_context.is_empty()
            && py.version_info() >= (3, 13)
        {
            self.retired_in_context.clear();
            return Ok(false);
        }
        let mut retired: Vec<usize> = self.retired_in_context.drain().collect();
        retired.sort_unstable();
        for code_id in retired {
            let code = self.registry.code_object(py, code_id)?;
            let mask = self.local_line_mask(code.bind(py), base_mask)?;
            // Setting an equal mask is a no-op; clearing first re-instruments
            // locations that a callback disabled.
            for events in [0, mask] {
                self.monitoring.call_method1(
                    py,
                    "set_local_events",
                    (self.monitoring_tool_id, &code, events),
                )?;
            }
        }
        Ok(true)
    }

    fn reset_local_instrumentation(&mut self, py: Python<'_>) -> PyResult<()> {
        // free_tool_id retains code-local masks. Remove them before a later
        // collector acquires the same slot with a different event strategy.
        let mut configured: Vec<_> = self.locally_configured.iter().collect();
        configured.sort_unstable_by_key(|(code_id, _)| **code_id);
        for (_, code) in configured {
            self.monitoring.call_method1(
                py,
                "set_local_events",
                (self.monitoring_tool_id, code, 0_u32),
            )?;
        }
        self.locally_configured.clear();
        self.retired_in_context.clear();
        Ok(())
    }

    /// Install a local mask and retain its code identity until cleanup succeeds.
    fn set_local_events(&mut self, code: &Bound<'_, PyAny>, events: u32) -> PyResult<()> {
        self.monitoring.call_method1(
            code.py(),
            "set_local_events",
            (self.monitoring_tool_id, code, events),
        )?;
        self.locally_configured
            .insert(CodeRegistry::code_id(code), code.clone().unbind());
        Ok(())
    }

    #[pyo3(signature = (code, frame_globals=None))]
    fn instrument_existing_frame(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        frame_globals: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        let lookup = {
            let mut collector = slf.try_borrow_mut()?;
            if !collector.local_legacy_frames {
                return Ok(());
            }
            collector.registry.frame_lookup(code)?
        };
        let frame_source = match &lookup {
            Some(lookup) => {
                let (mut source, globals) =
                    CodeRegistry::capture_frame_source(code, lookup, frame_globals)?;
                source.module_name =
                    CodeRegistry::capture_module_name(globals.as_ref(), code.py())?;
                drop(globals);
                Some(source)
            }
            None => None,
        };
        drop(lookup);
        slf.try_borrow_mut()?
            .instrument_frame(code, frame_source.as_ref())
    }

    fn callback_start<'py>(
        slf: &Bound<'py, Self>,
        code: &Bound<'py, PyAny>,
        offset: i32,
    ) -> PyResult<Py<PyAny>> {
        drop(Self::begin_dynamic_frame(slf, code)?);
        Self::with_plugin_event(slf, code, PluginEventKind::Frame, |collector, event| {
            // Keep the monitoring signature; bytecode positions are resolved only
            // when a resume, return, or branch event consumes them.
            let _ = offset;
            let code_id = CodeRegistry::code_id(code);
            if collector.strategy.retires_events()
                && Self::event_filename(&mut collector.registry, code, event)?.is_none()
                && let Some(disable) = &collector.disable_value
            {
                // Direct branch events do not infer arcs from caller frames.
                // Excluded frames therefore need neither events nor stack slots.
                return Ok(disable.clone_ref(code.py()));
            }
            if collector.local_legacy_frames
                && Self::event_filename(&mut collector.registry, code, event)?.is_none()
            {
                if !collector.locally_configured.contains_key(&code_id) {
                    collector.set_local_events(code, 0)?;
                }
                if let Some(disable) = &collector.disable_value {
                    return Ok(disable.clone_ref(code.py()));
                }
                return Ok(code.py().None());
            }
            if (collector.strategy.retires_events() || collector.local_legacy_frames)
                && !collector.locally_configured.contains_key(&code_id)
            {
                let events = if collector.local_legacy_frames {
                    collector.local_line_mask(code, collector.local_branch_events)?
                } else {
                    collector.local_branch_events
                };
                collector.set_local_events(code, events)?;
            }
            if collector.strategy.decisions()
                && let Some(path) =
                    Self::event_filename(&mut collector.registry, code, event)?.map(str::to_owned)
            {
                collector
                    .trace
                    .register_semantic_path(&path)
                    .map_err(|error| match error {
                        vfy_core::CoreError::ArtifactIo { .. } => {
                            pyo3::exceptions::PyOSError::new_err(error.to_string())
                        }
                        _ => pyo3::exceptions::PyValueError::new_err(error.to_string()),
                    })?;
            }
            if !collector.trace.has_arcs()
                && let Some(disable) = collector
                    .disable_value
                    .as_ref()
                    .map(|value| value.clone_ref(code.py()))
            {
                if Self::event_filename(&mut collector.registry, code, event)?.is_some() {
                    let events = collector.local_line_mask(code, collector.line_event)?;
                    collector.set_local_events(code, events)?;
                }
                return Ok(disable);
            }
            if collector.trace.has_arcs() {
                Self::event_filename(&mut collector.registry, code, event)?;
            }
            collector.trace.start_frame(code_id);
            if collector.strategy.retires_events()
                && let Some(disable) = &collector.disable_value
            {
                // Direct branch and line locations keep their own evidence state.
                // Once this code object's local mask is installed, repeated calls
                // do not need another frame-start callback. Context changes and
                // collection resets reactivate the location through restart_events.
                return Ok(disable.clone_ref(code.py()));
            }
            Ok(code.py().None())
        })
    }

    fn callback_resume<'py>(
        slf: &Bound<'py, Self>,
        code: &Bound<'py, PyAny>,
        offset: i32,
    ) -> PyResult<Py<PyAny>> {
        drop(Self::begin_dynamic_frame(slf, code)?);
        Self::with_plugin_event(slf, code, PluginEventKind::Frame, |collector, event| {
            if collector.local_legacy_frames {
                // A generator may have yielded before collection started, so it
                // has no active thread frame for startup discovery. The global
                // PY_RESUME event installs its local mask before execution resumes.
                collector.instrument_frame(code, None)?;
                if Self::event_filename(&mut collector.registry, code, event)?.is_none() {
                    if let Some(disable) = &collector.disable_value {
                        return Ok(disable.clone_ref(code.py()));
                    }
                    return Ok(code.py().None());
                }
            }
            if collector.strategy.retires_events()
                && Self::event_filename(&mut collector.registry, code, event)?.is_none()
            {
                return Ok(code.py().None());
            }
            let code_id = CodeRegistry::code_id(code);
            let suspended_line =
                if Self::event_filename(&mut collector.registry, code, event)?.is_some() {
                    collector.registry.line_for_offset_exact(code, offset)?
                } else {
                    None
                };
            collector.trace.resume_frame(code_id, suspended_line);
            Ok(code.py().None())
        })
    }

    fn callback_throw<'py>(
        slf: &Bound<'py, Self>,
        code: &Bound<'py, PyAny>,
        offset: i32,
        exception: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        drop(Self::begin_dynamic_frame(slf, code)?);
        Self::with_exception_event(slf, code, exception, |collector, event, cancelled| {
            if collector.local_legacy_frames {
                // Closing or throwing into a pre-existing suspended generator can
                // enter its finally/except suite without a PY_RESUME callback.
                collector.instrument_frame(code, None)?;
                if Self::event_filename(&mut collector.registry, code, event)?.is_none() {
                    return Ok(());
                }
            }
            if collector.strategy.retires_events()
                && Self::event_filename(&mut collector.registry, code, event)?.is_none()
            {
                return Ok(());
            }
            let code_id = CodeRegistry::code_id(code);
            let suspended_line =
                if Self::event_filename(&mut collector.registry, code, event)?.is_some() {
                    collector.registry.line_for_offset_exact(code, offset)?
                } else {
                    None
                };
            collector.trace.resume_frame(code_id, suspended_line);
            if collector.strategy.derives_arcs_from_lines()
                && collector.suppress_plain_yield_exit
                && !collector.registry.is_delegated_yield(code_id, offset)
            {
                // Throwing through a plain yield has no exit arc until another
                // source line runs. Delegation unwinds through its cleanup instruction.
                collector.trace.suppress_frame_exit(code_id);
            }
            if collector.strategy.decisions() {
                collector.trace.clear_semantic_frame(code_id);
                collector.observe_exception(code, offset, cancelled, event)?;
            }
            Ok(())
        })
    }

    /// Frame start for dynamic contexts when line collection has no frame events.
    #[pyo3(signature = (code, _offset, _value=None))]
    fn callback_context_enter<'py>(
        slf: &Bound<'py, Self>,
        code: &Bound<'py, PyAny>,
        _offset: i32,
        _value: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        Self::begin_dynamic_frame(slf, code).map(drop)
    }

    /// Frame exit for dynamic contexts when line collection has no frame events.
    fn callback_context_leave(
        &mut self,
        code: &Bound<'_, PyAny>,
        _offset: i32,
        _value: &Bound<'_, PyAny>,
    ) {
        self.leave_dynamic_context(CodeRegistry::code_id(code));
    }

    fn callback_suspend(
        &mut self,
        code: &Bound<'_, PyAny>,
        _offset: i32,
        _value: &Bound<'_, PyAny>,
    ) {
        let code_id = CodeRegistry::code_id(code);
        if self.strategy.retires_events()
            && self
                .registry
                .filenames
                .get(&code_id)
                .is_some_and(Option::is_none)
        {
            return;
        }
        if self.strategy.decisions() {
            // A completed decision is pending at a yield boundary just like
            // it is at a line boundary. Commit it before the frame is
            // suspended; an incomplete condition is cleared by
            // `suspend_frame` below and must not leak into the next resume.
            self.trace.flush_semantic_current();
        }
        self.trace.suspend_frame(code_id);
        self.leave_dynamic_context(code_id);
    }

    fn callback_line(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        line: i32,
    ) -> PyResult<Py<PyAny>> {
        Self::with_plugin_event(slf, code, PluginEventKind::Line, |collector, event| {
            collector.collect_line(code, line, event)
        })
    }

    fn callback_instruction(
        &mut self,
        code: &Bound<'_, PyAny>,
        offset: i32,
    ) -> PyResult<Py<PyAny>> {
        if let Some(line) = self
            .registry
            .instruction_line(CodeRegistry::code_id(code), offset)
        {
            return self.collect_line(code, line, None);
        }
        // This straight-line offset never contributes a LINE event. Retiring
        // it remains valid across repeated execution and context changes.
        Ok(self
            .disable_value
            .as_ref()
            .map_or_else(|| code.py().None(), |disable| disable.clone_ref(code.py())))
    }

    fn callback_branch(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        origin: i32,
        destination: i32,
    ) -> PyResult<Py<PyAny>> {
        Self::with_plugin_event(
            slf,
            code,
            PluginEventKind::Branch {
                origin,
                destination,
            },
            |collector, event| {
                collector.collect_branch_direction(code, origin, destination, None, event)
            },
        )
    }

    fn callback_branch_left(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        origin: i32,
        destination: i32,
    ) -> PyResult<Py<PyAny>> {
        Self::with_plugin_event(
            slf,
            code,
            PluginEventKind::Branch {
                origin,
                destination,
            },
            |collector, event| {
                collector.collect_branch_direction(code, origin, destination, Some(true), event)
            },
        )
    }

    fn callback_branch_right(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        origin: i32,
        destination: i32,
    ) -> PyResult<Py<PyAny>> {
        Self::with_plugin_event(
            slf,
            code,
            PluginEventKind::Branch {
                origin,
                destination,
            },
            |collector, event| {
                collector.collect_branch_direction(code, origin, destination, Some(false), event)
            },
        )
    }

    fn callback_jump(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        origin: i32,
        destination: i32,
    ) -> PyResult<Py<PyAny>> {
        Self::with_plugin_event(
            slf,
            code,
            PluginEventKind::Branch {
                origin,
                destination,
            },
            |collector, event| {
                let result =
                    collector.collect_branch_direction(code, origin, destination, None, event)?;
                if collector.local_legacy_frames
                    && let Some(disable) = &collector.disable_value
                {
                    collector
                        .retired_in_context
                        .insert(CodeRegistry::code_id(code));
                    return Ok(disable.clone_ref(code.py()));
                }
                Ok(result)
            },
        )
    }

    fn callback_raise(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        offset: i32,
        exception: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        Self::with_exception_event(slf, code, exception, |collector, event, cancelled| {
            collector.observe_exception(code, offset, cancelled, event)
        })
    }

    fn callback_unwind(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        offset: i32,
        exception: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        Self::with_exception_event(slf, code, exception, |collector, event, cancelled| {
            collector.observe_exception(code, offset, cancelled, event)?;
            collector.trace.flush_semantic_current();
            let code_id = CodeRegistry::code_id(code);
            collector
                .trace
                .finish_paths(&collector.context, code_id, true);
            if collector.strategy.direct_arcs() {
                collector.trace.suppress_frame_exit(code_id);
            }
            collector.finish_frame(code, offset, event)
        })
    }

    fn callback_end(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        offset: i32,
        _value: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Self::with_plugin_event(slf, code, PluginEventKind::Frame, |collector, event| {
            if collector.local_legacy_frames
                && Self::event_filename(&mut collector.registry, code, event)?.is_none()
            {
                return Ok(code.py().None());
            }
            if collector.strategy.retires_events()
                && Self::event_filename(&mut collector.registry, code, event)?.is_none()
                && let Some(disable) = &collector.disable_value
            {
                return Ok(disable.clone_ref(code.py()));
            }
            if collector.strategy.decisions() {
                collector.trace.flush_semantic_current();
                collector.trace.finish_paths(
                    &collector.context,
                    CodeRegistry::code_id(code),
                    false,
                );
            }
            collector.finish_frame(code, offset, event)?;
            if collector.strategy.retires_events()
                && let Some(disable) = &collector.disable_value
            {
                return Ok(disable.clone_ref(code.py()));
            }
            Ok(code.py().None())
        })
    }

    fn line_records(&self) -> Vec<(String, String, Vec<i32>)> {
        self.trace.line_records()
    }

    #[expect(
        clippy::type_complexity,
        reason = "PyO3 returns context-partitioned arc records."
    )]
    fn arc_records(&mut self, py: Python<'_>) -> PyResult<Vec<(String, String, Vec<(i32, i32)>)>> {
        self.resolve_branches(py)?;
        Ok(self.trace.arc_records())
    }

    fn tracer_records(&self) -> Vec<(String, String)> {
        self.registry
            .tracers
            .iter()
            .map(|(path, tracer)| (path.clone(), tracer.clone()))
            .collect()
    }

    #[pyo3(signature = (discover_sources=false))]
    fn coverage_data(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        discover_sources: bool,
    ) -> PyResult<CoverageData> {
        let discovery = Self::discover(slf, py, discover_sources)?;
        slf.try_borrow_mut()?
            .build_coverage_data(py, discovery, TraceTransfer::Copy)
    }

    /// Return the collected data and release the collector's copy.
    ///
    /// Only a stopped collector calls this; later snapshots start empty.
    #[pyo3(signature = (discover_sources=false))]
    fn take_coverage_data(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        discover_sources: bool,
    ) -> PyResult<CoverageData> {
        let discovery = Self::discover(slf, py, discover_sources)?;
        let mut collector = slf.try_borrow_mut()?;
        let data = collector.build_coverage_data(py, discovery, TraceTransfer::Move)?;
        collector.trace.clear_records();
        Ok(data)
    }

    #[pyo3(signature = (target, discover_sources=false))]
    fn flush_coverage_data(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        target: PyRef<'_, CoverageData>,
        discover_sources: bool,
    ) -> PyResult<CoverageData> {
        let mut data = target.clone();
        drop(target);
        let discovery = Self::discover(slf, py, discover_sources)?;
        let mut collector = slf.try_borrow_mut()?;
        let snapshot = collector.build_coverage_data(py, discovery, TraceTransfer::Copy)?;
        data.merge_from(snapshot, true)
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        collector.trace.clear_records();
        Ok(data)
    }

    /// Requested source packages that no traced module matched so far.
    fn unmatched_source_packages(&self) -> Vec<String> {
        self.registry.unmatched_source_packages()
    }

    fn reset_coverage_data(&mut self) {
        self.branch_events.clear();
        self.trace.reset_records();
    }
}

/// Whether building coverage data keeps or releases the collector's evidence.
#[derive(Clone, Copy)]
enum TraceTransfer {
    Copy,
    Move,
}

impl CollectorCore {
    fn discover(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        enabled: bool,
    ) -> PyResult<Option<DiscoveryOutcome>> {
        if !enabled {
            return Ok(None);
        }
        let request = {
            let collector = slf.try_borrow_mut()?;
            collector.registry.prepare_discovery(py)
        };
        let result = request.invoke(py);
        drop(request);
        result.map(Some)
    }

    fn instrument_frame(
        &mut self,
        code: &Bound<'_, PyAny>,
        frame_source: Option<&FrameSource>,
    ) -> PyResult<()> {
        if !self.local_legacy_frames {
            return Ok(());
        }
        let code_id = CodeRegistry::code_id(code);
        if self.locally_configured.contains_key(&code_id) {
            return Ok(());
        }
        let local_events = if self
            .registry
            .filename_with_globals(code, frame_source)?
            .is_none()
        {
            0
        } else {
            self.local_line_mask(code, self.local_branch_events)?
        };
        self.set_local_events(code, local_events)
    }

    fn observe_exception(
        &mut self,
        code: &Bound<'_, PyAny>,
        offset: i32,
        cancelled: bool,
        event: Option<&ResolvedPluginEvent>,
    ) -> PyResult<()> {
        if !self.strategy.decisions() {
            return Ok(());
        }
        let Some(path) = Self::event_filename(&mut self.registry, code, event)?.map(str::to_owned)
        else {
            return Ok(());
        };
        let line = self.registry.line_for_offset(code, offset)?;
        self.trace.observe_path_raise(
            &self.context,
            &path,
            CodeRegistry::code_id(code),
            line,
            cancelled,
        );
        Ok(())
    }

    fn finish_frame(
        &mut self,
        code: &Bound<'_, PyAny>,
        offset: i32,
        event: Option<&ResolvedPluginEvent>,
    ) -> PyResult<()> {
        let code_id = CodeRegistry::code_id(code);
        if Self::event_filename(&mut self.registry, code, event)?.is_none() {
            // Excluded frames need no bytecode decoding. Dynamic tracers can
            // still have a last measured path that needs its exit arc.
            let first_line = self
                .registry
                .first_lines
                .get(&code_id)
                .copied()
                .unwrap_or(0);
            self.trace
                .end_frame(&self.context, None, code_id, first_line, None);
            self.leave_dynamic_context(code_id);
            return Ok(());
        }
        let first_line = self
            .registry
            .first_lines
            .get(&code_id)
            .copied()
            .map_or_else(|| self.registry.first_line(code), Ok)?;
        if self.trace.sysmon_arcs() {
            let return_line = self.registry.line_for_offset_exact(code, offset)?;
            let path = Self::event_filename(&mut self.registry, code, event)?;
            self.trace
                .end_frame(&self.context, path, code_id, first_line, return_line);
        } else {
            let path = Self::event_filename(&mut self.registry, code, event)?;
            self.trace
                .end_frame(&self.context, path, code_id, first_line, None);
        }
        self.leave_dynamic_context(code_id);
        Ok(())
    }

    fn collect_line(
        &mut self,
        code: &Bound<'_, PyAny>,
        line: i32,
        event: Option<&ResolvedPluginEvent>,
    ) -> PyResult<Py<PyAny>> {
        if self.strategy.decisions() {
            self.trace.flush_semantic_current();
            let code_id = CodeRegistry::code_id(code);
            self.trace.flush_path_current(&self.context, code_id);
            if let Some(path) =
                Self::event_filename(&mut self.registry, code, event)?.map(str::to_owned)
            {
                self.trace
                    .observe_path_line(&self.context, &path, code_id, line);
            }
        }
        if self.strategy.retires_events()
            && let Some(disable) = &self.disable_value
        {
            if let Some(path) =
                Self::event_filename(&mut self.registry, code, event)?.map(str::to_owned)
            {
                let first_line = self.registry.first_line(code)?;
                self.trace.line_event(
                    &self.context,
                    &path,
                    CodeRegistry::code_id(code),
                    first_line,
                    line,
                );
            }
            // Excluded locations cannot contribute evidence either. Context
            // switches and collector startup re-enable all disabled locations.
            return Ok(disable.clone_ref(code.py()));
        }
        if !self.trace.has_arcs()
            && let Some(disable) = &self.disable_value
        {
            if let Some(path) = Self::event_filename(&mut self.registry, code, event)? {
                self.trace.add_line(&self.context, path, line);
                self.retired_in_context.insert(CodeRegistry::code_id(code));
            }
            return Ok(disable.clone_ref(code.py()));
        }
        let code_id = CodeRegistry::code_id(code);
        if self.registry.plugins.is_empty() {
            if let (Some(path), Some(&first_line)) = (
                self.registry.filenames.get(&code_id),
                self.registry.first_lines.get(&code_id),
            ) {
                if let Some(path) = path.as_deref() {
                    self.trace
                        .line_event(&self.context, path, code_id, first_line, line);
                } else if let Some(disable) = &self.disable_value {
                    // Excluded lines never update the frame's previous line.
                    // Keep frame events active for calls back into measured code.
                    return Ok(disable.clone_ref(code.py()));
                }
                return Ok(code.py().None());
            }
            let first_line = self.registry.first_line(code)?;
            if let Some(path) = Self::event_filename(&mut self.registry, code, event)? {
                self.trace
                    .line_event(&self.context, path, code_id, first_line, line);
            } else if let Some(disable) = &self.disable_value {
                return Ok(disable.clone_ref(code.py()));
            }
            return Ok(code.py().None());
        }

        let first_line = self.registry.first_line(code)?;
        let path = Self::event_filename(&mut self.registry, code, event)?.map(str::to_owned);
        if let Some(path) = path {
            if let Some((start, end)) = event.and_then(|event| event.line_range) {
                if start >= 0 {
                    for mapped_line in start..=end.max(start) {
                        self.trace.line_event(
                            &self.context,
                            &path,
                            code_id,
                            first_line,
                            mapped_line,
                        );
                    }
                }
            } else {
                self.trace
                    .line_event(&self.context, &path, code_id, first_line, line);
            }
        }
        Ok(code.py().None())
    }

    fn collect_branch_direction(
        &mut self,
        code: &Bound<'_, PyAny>,
        origin: i32,
        destination: i32,
        direction: Option<bool>,
        event: Option<&ResolvedPluginEvent>,
    ) -> PyResult<Py<PyAny>> {
        let none = code.py().None();
        if self.local_legacy_frames && destination >= origin {
            return Ok(none);
        }
        if self.strategy.decisions() {
            self.trace.flush_semantic_current();
        }
        let code_id = CodeRegistry::code_id(code);
        if Self::event_filename(&mut self.registry, code, event)?.is_none() {
            if self.strategy.retires_events()
                && let Some(disable) = &self.disable_value
            {
                return Ok(disable.clone_ref(code.py()));
            }
            return Ok(none);
        }
        let arc = if self.trace.sysmon_arcs() {
            if let Some(path) = Self::event_filename(&mut self.registry, code, event)? {
                self.branch_events
                    .entry(code_id)
                    .or_default()
                    .entry((self.context.clone(), path.to_owned()))
                    .or_default()
                    .insert((origin, destination));
            }
            None
        } else if self.strategy.derives_arcs_from_lines() {
            // Async-for cleanup can jump between instructions with no source
            // positions. Falling back to co_firstlineno invents a repeated
            // definition-line arc instead of observing a real LINE event.
            self.registry
                .line_for_offset_exact(code, origin)?
                .zip(self.registry.line_for_offset_exact(code, destination)?)
        } else {
            let start = self.registry.line_for_offset(code, origin)?;
            let end = self.registry.line_for_offset(code, destination)?;
            Some((start, end))
        };
        if let Some((start, end)) = arc {
            if self.strategy.derives_arcs_from_lines() && start == end && destination < origin {
                self.record_repeated_line(code, start, event)?;
                // This strategy derives arcs from LINE events. After observing
                // this self-arc, neither branch outcome can add another arc.
                if self.local_legacy_frames
                    && let Some(disable) = &self.disable_value
                {
                    self.retired_in_context.insert(code_id);
                    return Ok(disable.clone_ref(code.py()));
                }
            } else if self.strategy != CollectionStrategy::LegacyBranches
                && let Some(path) = Self::event_filename(&mut self.registry, code, event)?
            {
                self.trace.add_arc(&self.context, path, start, end);
            }
        }
        if self.strategy.decisions()
            && let Some(path) =
                Self::event_filename(&mut self.registry, code, event)?.map(str::to_owned)
            && let Some(branch) = self.registry.semantic_branch(code, origin)?
        {
            // CPython 3.14's LEFT/RIGHT event names describe the interpreter
            // edge, not the expression's truth polarity. Resolve the opcode
            // direction from the Rust bytecode model first; the event hint
            // is only a compatibility fallback for an unknown opcode.
            let outcome = self
                .registry
                .semantic_branch_outcome(code, origin, destination)?
                .or(direction)
                .unwrap_or(destination != origin);
            self.trace.observe_semantic(
                &self.context,
                &path,
                branch.line,
                branch.column,
                branch.ordinal,
                outcome,
            );
            if let Some(range) = branch.expression_range {
                self.trace.observe_path_branch(
                    &self.context,
                    &path,
                    code_id,
                    range,
                    branch.expression_ordinal,
                    outcome,
                );
            }
        }
        if self.strategy.retires_events()
            && let Some(disable) = &self.disable_value
        {
            return Ok(disable.clone_ref(code.py()));
        }
        Ok(none)
    }

    fn with_plugin_event<T>(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        kind: PluginEventKind,
        collect: impl FnOnce(&mut Self, Option<&ResolvedPluginEvent>) -> PyResult<T>,
    ) -> PyResult<T> {
        let event = Self::resolve_plugin_event(slf, code, kind)?;
        let mut collector = slf.try_borrow_mut()?;
        collect(&mut collector, event.as_ref())
    }

    fn with_exception_event<T>(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        exception: &Bound<'_, PyAny>,
        collect: impl FnOnce(&mut Self, Option<&ResolvedPluginEvent>, bool) -> PyResult<T>,
    ) -> PyResult<T> {
        let event = Self::resolve_plugin_event(slf, code, PluginEventKind::Frame)?;
        let mut collector = slf.try_borrow_mut()?;
        let cancelled_error = if collector.strategy.decisions()
            && Self::event_filename(&mut collector.registry, code, event.as_ref())?.is_some()
        {
            collector
                .cancelled_error
                .as_ref()
                .map(|error| error.clone_ref(code.py()))
        } else {
            None
        };
        drop(collector);
        let cancelled = match &cancelled_error {
            Some(cancelled_error) => is_cancelled_exception(exception, cancelled_error)?,
            None => false,
        };
        drop(cancelled_error);
        let mut collector = slf.try_borrow_mut()?;
        collect(&mut collector, event.as_ref(), cancelled)
    }

    fn resolve_plugin_event(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        kind: PluginEventKind,
    ) -> PyResult<Option<ResolvedPluginEvent>> {
        let mut collector = slf.try_borrow_mut()?;
        let lookup = collector.registry.frame_lookup(code)?;
        let has_plugins = !collector.registry.plugins.is_empty();
        if !has_plugins && lookup.is_none() {
            return Ok(None);
        }
        drop(collector);
        let (mut frame_source, mut frame_globals) = match &lookup {
            Some(lookup) => CodeRegistry::capture_frame_source(code, lookup, None)?,
            None => (FrameSource::default(), None),
        };
        drop(lookup);
        if !has_plugins {
            frame_source.module_name =
                CodeRegistry::capture_module_name(frame_globals.as_ref(), code.py())?;
            drop(frame_globals);
            let mut collector = slf.try_borrow_mut()?;
            collector
                .registry
                .filename_with_globals(code, Some(&frame_source))?;
            return Ok(None);
        }
        let mut collector = slf.try_borrow_mut()?;
        let event = loop {
            match collector
                .registry
                .prepare_plugin_event(code, &frame_source)?
            {
                Ok(event) => break event,
                Err(request) => {
                    drop(collector);
                    let committed = Self::invoke_plugin_request(
                        slf,
                        code,
                        request,
                        &mut frame_source,
                        &mut frame_globals,
                    )?;
                    collector = slf.try_borrow_mut()?;
                    if let Some(event) = committed {
                        break event;
                    }
                }
            }
        };
        let wants_line = (|| -> PyResult<bool> {
            Ok(match kind {
                PluginEventKind::Frame => false,
                PluginEventKind::Line => true,
                PluginEventKind::Branch {
                    origin,
                    destination,
                } => {
                    destination < origin
                        && collector.strategy.derives_arcs_from_lines()
                        && collector
                            .registry
                            .line_for_offset_exact(code, origin)?
                            .zip(
                                collector
                                    .registry
                                    .line_for_offset_exact(code, destination)?,
                            )
                            .is_some_and(|(start, end)| start == end)
                }
            })
        })();
        drop(collector);
        drop(frame_globals);
        let wants_line = wants_line?;
        let line_range = if wants_line && event.path.is_some() {
            if let Some(tracer) = &event.tracer {
                let frame = current_frame(code.py())?;
                Some(
                    tracer
                        .call_method1(code.py(), "line_number_range", (frame,))?
                        .extract(code.py())?,
                )
            } else {
                None
            }
        } else {
            None
        };
        let resolved = ResolvedPluginEvent {
            path: event.path.clone(),
            line_range,
        };
        drop(event);
        Ok(Some(resolved))
    }

    /// Call a plug-in and obtain only the uncached module metadata needed to
    /// commit its result. Python references are released after the borrow.
    fn invoke_plugin_request(
        slf: &Bound<'_, Self>,
        code: &Bound<'_, PyAny>,
        request: PluginRequest,
        frame_source: &mut FrameSource,
        frame_globals: &mut Option<Py<PyAny>>,
    ) -> PyResult<Option<PluginEvent>> {
        let outcome = request.invoke(code.py())?;
        let collector = slf.try_borrow_mut()?;
        let needs_module = collector
            .registry
            .outcome_needs_module(code, &request, &outcome);
        let module_lookup = if needs_module && frame_globals.is_none() {
            collector.registry.module_frame_lookup(code.py())
        } else {
            None
        };
        drop(collector);
        if needs_module {
            if let Some(lookup) = &module_lookup {
                *frame_globals = CodeRegistry::capture_frame_globals(code, lookup, None)?;
            }
            frame_source.module_name =
                CodeRegistry::capture_module_name(frame_globals.as_ref(), code.py())?;
        }
        drop(module_lookup);
        let mut collector = slf.try_borrow_mut()?;
        let committed =
            collector
                .registry
                .commit_plugin_event(code, &request, &outcome, frame_source);
        drop(collector);
        // Dropping a plug-in can invoke Python through finalizers or weakrefs.
        drop(outcome);
        drop(request);
        committed
    }

    fn event_filename<'a>(
        registry: &'a mut CodeRegistry,
        code: &Bound<'_, PyAny>,
        event: Option<&'a ResolvedPluginEvent>,
    ) -> PyResult<Option<&'a str>> {
        if let Some(event) = event {
            Ok(event.path.as_deref())
        } else {
            registry.filename(code)
        }
    }

    fn build_coverage_data(
        &mut self,
        py: Python<'_>,
        discovery: Option<DiscoveryOutcome>,
        transfer: TraceTransfer,
    ) -> PyResult<CoverageData> {
        self.resolve_branches(py)?;
        let arcs = self.trace.arc_records();
        let semantic = self.trace.semantic_data().clone();
        let paths: BTreeSet<String> = self
            .trace
            .line_paths()
            .chain(arcs.iter().map(|(_, path, _)| path.as_str()))
            .chain(semantic.observations().iter().filter_map(|observation| {
                semantic
                    .definitions()
                    .get(observation.decision())
                    .map(vfy_core::DecisionDefinition::path)
            }))
            .chain(
                semantic
                    .path_observations()
                    .iter()
                    .filter_map(|observation| {
                        semantic
                            .path_definitions()
                            .get(observation.decision())
                            .map(vfy_core::PathDecisionDefinition::path)
                    }),
            )
            .map(str::to_owned)
            .collect();
        let mut paths = paths;
        let plugin_files = if let Some(discovery) = discovery {
            let plugin_files = self.registry.commit_discovery(discovery);
            paths.extend(self.registry.discover_source_files());
            plugin_files
        } else {
            Vec::new()
        };
        paths.extend(plugin_files.iter().map(|(path, _)| path.clone()));
        let mut tracers: BTreeMap<_, _> = self
            .tracer_records()
            .into_iter()
            .filter(|(path, _)| paths.contains(path))
            .collect();
        vfy_core::data::validate_file_tracers(
            &tracers,
            plugin_files
                .iter()
                .map(|(path, tracer)| (path.as_str(), tracer.as_str())),
        )
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
        tracers.extend(plugin_files);
        // Discovery and tracer validation can call plug-ins or reject a batch.
        // Move authoritative line chunks only after all fallible work succeeds.
        let lines = match transfer {
            TraceTransfer::Copy => self.trace.copy_lines(),
            TraceTransfer::Move => self.trace.take_lines(),
        };
        let mut data = CoverageData::from_trace(
            self.trace.has_arcs(),
            lines,
            arcs,
            tracers.into_iter().collect(),
            semantic,
        );
        for path in &paths {
            data.touch_file(path);
        }
        data.set_metadata("has_arcs", if self.trace.has_arcs() { "1" } else { "0" });
        Ok(data.into_selected_files(paths.into_iter().collect()))
    }

    /// The code-local event mask that context-scoped retirement disables.
    fn context_local_mask(&self) -> Option<u32> {
        self.disable_value.as_ref()?;
        match self.strategy {
            CollectionStrategy::LegacyBranches if self.local_legacy_frames => {
                Some(self.local_branch_events)
            }
            CollectionStrategy::Lines if !self.has_dynamic_contexts() => Some(self.line_event),
            _ => None,
        }
    }

    fn local_line_mask(&mut self, code: &Bound<'_, PyAny>, mask: u32) -> PyResult<u32> {
        if !self.strategy.decisions() && self.registry.prepare_instruction_lines(code)? {
            Ok((mask & !self.line_event) | self.instruction_event)
        } else {
            Ok(mask)
        }
    }

    fn record_repeated_line(
        &mut self,
        code: &Bound<'_, PyAny>,
        line: i32,
        event: Option<&ResolvedPluginEvent>,
    ) -> PyResult<()> {
        let code_id = CodeRegistry::code_id(code);
        let mapped_range = event.and_then(|event| event.line_range);
        let first_line = if mapped_range.is_some_and(|(start, _)| start >= 0) {
            Some(self.registry.first_line(code)?)
        } else {
            None
        };
        let Some(path) = Self::event_filename(&mut self.registry, code, event)? else {
            return Ok(());
        };
        if let (Some((start, end)), Some(first_line)) = (mapped_range, first_line) {
            // File tracers can change the virtual line on every loop iteration.
            // Feed the mapped range through the same frame state as LINE events.
            for line in start..=end.max(start) {
                self.trace
                    .line_event(&self.context, path, code_id, first_line, line);
            }
        } else if mapped_range.is_none() {
            self.trace.add_repeated_arc(&self.context, path, line);
        }
        Ok(())
    }

    fn resolve_branches(&mut self, py: Python<'_>) -> PyResult<()> {
        let mut resolved = Vec::new();
        for (&code_id, records) in &self.branch_events {
            let code = self.registry.code_object(py, code_id)?;
            let code = code.bind(py);
            for ((context, path), edges) in records {
                for &(origin, destination) in edges {
                    let arc = self.registry.resolve_branch(code, origin, destination)?;
                    // Coverage.py retains raw exception edges when a normal
                    // branch trail cannot resolve them to a logical statement.
                    let arc = if arc.is_some() {
                        arc
                    } else {
                        let start = self.registry.branch_line_for_offset(code, origin)?;
                        let end = self.registry.branch_line_for_offset(code, destination)?;
                        start.zip(end).filter(|(start, end)| start != end)
                    };
                    if let Some((start, end)) = arc {
                        resolved.push((context.clone(), path.clone(), start, end));
                    }
                }
            }
        }
        for (context, path, start, end) in resolved {
            self.trace.add_arc(&context, &path, start, end);
        }
        self.branch_events.clear();
        Ok(())
    }

    fn has_dynamic_contexts(&self) -> bool {
        self.dynamic_switchers.enabled()
    }

    fn begin_dynamic_frame<'py>(
        slf: &Bound<'py, Self>,
        code: &Bound<'py, PyAny>,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let mut collector = slf.try_borrow_mut()?;
        if !collector.has_dynamic_contexts() {
            return Ok(collector);
        }
        let code_id = CodeRegistry::code_id(code);
        let thread = std::thread::current().id();
        if let Some(active) = collector.active_dynamic.get_mut(&thread) {
            if active.code_id == code_id {
                active.reentries += 1;
            }
            return Ok(collector);
        }
        if collector.dynamic_switchers.plugins.is_empty()
            && !collector.dynamic_switchers.matches_builtin(code)?
        {
            return Ok(collector);
        }
        let py = code.py();
        let switchers = collector.dynamic_switchers.clone_ref(py);
        // A plug-in, audit hook, or receiver descriptor can release the GIL.
        // Other threads must be able to enter the collector while it runs.
        drop(collector);
        let frame = current_frame(py)?;
        let dynamic = switchers.select(&frame)?;
        drop(frame);
        drop(switchers);
        let mut collector = slf.try_borrow_mut()?;
        if let Some(dynamic) = dynamic {
            collector.context = if dynamic.is_empty() {
                collector.base_context.clone()
            } else if collector.base_context.is_empty() {
                dynamic
            } else {
                format!("{}|{}", collector.base_context, dynamic)
            };
            let previous = collector.base_context.clone();
            // Owners are thread-local; the data label switches globally.
            collector.active_dynamic.insert(
                thread,
                ActiveDynamicContext {
                    code_id,
                    reentries: 0,
                    previous,
                },
            );
        }
        Ok(collector)
    }

    fn leave_dynamic_context(&mut self, code_id: usize) {
        if self.active_dynamic.is_empty() {
            return;
        }
        let thread = std::thread::current().id();
        let Some(active) = self.active_dynamic.get_mut(&thread) else {
            return;
        };
        if active.code_id != code_id {
            return;
        }
        if active.reentries > 0 {
            active.reentries -= 1;
        } else if let Some(active) = self.active_dynamic.remove(&thread) {
            self.context = active.previous;
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TestFunctionContexts {
    Disabled,
    /// `[run] dynamic_context = test_function`.
    Enabled,
}

impl TestFunctionContexts {
    fn parse(dynamic_context: &str) -> PyResult<Self> {
        match dynamic_context {
            "" | "none" => Ok(Self::Disabled),
            "test_function" => Ok(Self::Enabled),
            other => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Don't understand dynamic_context setting: {other:?}"
            ))),
        }
    }

    fn enabled(self) -> bool {
        self == Self::Enabled
    }
}

struct ActiveDynamicContext {
    code_id: usize,
    reentries: usize,
    previous: String,
}

struct DynamicContextSwitchers {
    test_function_names: Option<Arc<TestFunctionNames>>,
    plugins: Vec<Py<PyAny>>,
}

impl DynamicContextSwitchers {
    fn new(py: Python<'_>, plugins: Vec<Py<PyAny>>, setting: &str) -> PyResult<Self> {
        let builtin = TestFunctionContexts::parse(setting)?.enabled();
        Ok(Self {
            test_function_names: builtin.then(|| Arc::new(TestFunctionNames::new(py))),
            plugins,
        })
    }

    fn enabled(&self) -> bool {
        self.test_function_names.is_some() || !self.plugins.is_empty()
    }

    fn matches_builtin(&self, code: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.test_function_names
            .as_ref()
            .map_or(Ok(false), |names| is_test_function(code, names))
    }

    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            test_function_names: self.test_function_names.clone(),
            plugins: self
                .plugins
                .iter()
                .map(|plugin| plugin.clone_ref(py))
                .collect(),
        }
    }

    fn select(&self, frame: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        if let Some(names) = &self.test_function_names
            && let Some(context) = test_function_context(frame, names)?
        {
            return Ok(Some(context));
        }
        for plugin in &self.plugins {
            let selected = plugin.call_method1(frame.py(), "dynamic_context", (frame,))?;
            if let Some(dynamic) = selected.extract::<Option<String>>(frame.py())? {
                return Ok(Some(dynamic));
            }
        }
        Ok(None)
    }
}

// PyO3's intern! initializes its cache with the GIL released. Resolve these
// names before monitoring starts, while no callback holds the collector borrow.
struct TestFunctionNames {
    code: Py<PyString>,
    name: Py<PyString>,
    argcount: Py<PyString>,
    varnames: Py<PyString>,
    locals: Py<PyString>,
    globals: Py<PyString>,
    get: Py<PyString>,
    class: Py<PyString>,
}

impl TestFunctionNames {
    fn new(py: Python<'_>) -> Self {
        Self {
            code: PyString::intern(py, "f_code").unbind(),
            name: PyString::intern(py, "co_name").unbind(),
            argcount: PyString::intern(py, "co_argcount").unbind(),
            varnames: PyString::intern(py, "co_varnames").unbind(),
            locals: PyString::intern(py, "f_locals").unbind(),
            globals: PyString::intern(py, "f_globals").unbind(),
            get: PyString::intern(py, "get").unbind(),
            class: PyString::intern(py, "__class__").unbind(),
        }
    }
}

fn is_test_function(code: &Bound<'_, PyAny>, names: &TestFunctionNames) -> PyResult<bool> {
    let name: String = code.getattr(names.name.bind(code.py()))?.extract()?;
    Ok(name.starts_with("test") || name == "runTest")
}

/// Coverage.py's `should_start_context_test_function` and `qualname_from_frame`.
fn test_function_context(
    frame: &Bound<'_, PyAny>,
    names: &TestFunctionNames,
) -> PyResult<Option<String>> {
    let py = frame.py();
    let code = frame.getattr(names.code.bind(py))?;
    let name: String = code.getattr(names.name.bind(py))?.extract()?;
    if !is_test_function(&code, names)? {
        return Ok(None);
    }
    let mut method = None;
    let argcount: usize = code.getattr(names.argcount.bind(py))?.extract()?;
    if argcount > 0
        && code
            .getattr(names.varnames.bind(py))?
            .get_item(0)?
            .extract::<String>()?
            == "self"
    {
        let receiver = frame
            .getattr(names.locals.bind(py))?
            .call_method1(names.get.bind(py), ("self", py.None()))?;
        let bound = getattr_or_none(&receiver, &name)?;
        if !bound.is_none() {
            method = Some((receiver, bound));
        }
    }
    let attribute = |object: &Bound<'_, PyAny>, attribute: &str| -> PyResult<String> {
        object.getattr(attribute)?.str()?.extract()
    };
    let Some((instance, method)) = method else {
        let function = frame
            .getattr(names.globals.bind(py))?
            .call_method1(names.get.bind(py), (&name,))?;
        if function.is_none() {
            return Ok(None);
        }
        return Ok(Some(format!(
            "{}.{name}",
            attribute(&function, "__module__")?
        )));
    };
    let function = getattr_or_none(&method, "__func__")?;
    if function.is_none() {
        let class = instance.getattr(names.class.bind(py))?;
        return Ok(Some(format!(
            "{}.{}.{name}",
            attribute(&class, "__module__")?,
            attribute(&class, "__name__")?
        )));
    }
    Ok(Some(format!(
        "{}.{}",
        attribute(&function, "__module__")?,
        attribute(&function, "__qualname__")?
    )))
}

/// Python's `getattr(object, name, None)`.
fn getattr_or_none<'py>(object: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    match object.getattr(name) {
        Ok(value) => Ok(value),
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(object.py()) => {
            Ok(object.py().None().into_bound(object.py()))
        }
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests;
