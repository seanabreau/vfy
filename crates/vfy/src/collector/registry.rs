//! Code registry and instrumentation policy.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyWeakrefMethods, PyWeakrefReference};
use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};
use vfy_core::{CoverageGlobSet, UnicodeCaseVersion, glob_set_with_unicode_version};

use vfy_core::{absolute_path, discover_python_source_files, glob_path, read_python_source};

use super::current_frame;

mod instruction;

#[cfg(test)]
mod tests;

/// Interpreter directories skipped without a source or include filter.
pub(super) struct InterpreterRoots {
    /// The standard library, excluded unless `cover_pylib` is enabled.
    pub(super) stdlib: Vec<String>,
    /// Installed packages, which Coverage.py skips even with `cover_pylib`.
    pub(super) third_party: Vec<String>,
}

pub(super) fn interpreter_roots(py: Python<'_>) -> PyResult<InterpreterRoots> {
    let sysconfig = py.import("sysconfig")?;
    let paths: HashMap<String, String> = sysconfig.call_method0("get_paths")?.extract()?;
    let mut stdlib = Vec::new();
    for key in ["stdlib", "platstdlib"] {
        if let Some(path) = paths.get(key) {
            stdlib.push(path.clone());
        }
    }
    let site = py.import("site")?;
    let mut third_party = site
        .call_method0("getsitepackages")?
        .extract::<Vec<String>>()?;
    if let Ok(user_site) = site
        .call_method0("getusersitepackages")?
        .extract::<String>()
        && !user_site.is_empty()
    {
        third_party.push(user_site);
    }
    for roots in [&mut stdlib, &mut third_party] {
        roots.sort();
        roots.dedup();
    }
    Ok(InterpreterRoots {
        stdlib,
        third_party,
    })
}

pub(super) type SourceRange = (i32, i32, i32, i32);
pub(super) type InstructionPosition = (Option<i32>, Option<i32>, Option<i32>, Option<i32>);

#[derive(Clone, Copy)]
pub(super) struct SemanticBranch {
    pub(super) line: i32,
    pub(super) column: i32,
    pub(super) ordinal: usize,
    pub(super) expression_range: Option<SourceRange>,
    pub(super) expression_ordinal: usize,
}

fn next_ordinal<K: Ord>(ordinals: &mut BTreeMap<K, usize>, key: K) -> usize {
    let next = ordinals.entry(key).or_insert(0);
    let current = *next;
    *next = next.saturating_add(1);
    current
}

fn source_range(position: Option<InstructionPosition>) -> Option<SourceRange> {
    let (line, end_line, column, end_column) = position?;
    Some((line?, column?, end_line?, end_column?))
}

fn line_for_word_offset(lines: &[Option<i32>], offset: i32) -> Option<i32> {
    if offset < 0 || offset % 2 != 0 {
        return None;
    }
    let index = usize::try_from(offset / 2).ok()?;
    lines.get(index).copied().flatten()
}

/// How the registry holds code objects whose events it does not measure.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ExcludedCodeRetention {
    /// Keep excluded code alive because collector state outside the registry
    /// is keyed by its address.
    Strong,
    /// Hold excluded code weakly. Its address-keyed entries are discarded
    /// once `CPython` frees it and can reuse the address for new code.
    Weak,
}

enum CodeHandle {
    Strong(Py<PyAny>),
    Weak(Py<PyWeakrefReference>),
}

pub(super) struct CodeRegistry {
    pub(super) ignore_annotation_code: bool,
    pub(super) excluded_code: ExcludedCodeRetention,
    source_roots: Vec<PathBuf>,
    source_packages: Option<SourcePackages>,
    stdlib_roots: Vec<PathBuf>,
    third_party_roots: Vec<PathBuf>,
    package_root: PathBuf,
    relative_root: Option<PathBuf>,
    working_root: Option<PathBuf>,
    cover_pylib: bool,
    include: Option<CoverageGlobSet>,
    omit: Option<CoverageGlobSet>,
    pub(super) plugins: Vec<Py<PyAny>>,
    // A strong handle makes the code address a stable identity key. A weak
    // handle detects a freed address before its cache entries are reused.
    code_objects: HashMap<usize, CodeHandle>,
    pub(super) filenames: HashMap<usize, Option<String>>,
    filename_dispositions: HashMap<String, FilenameDisposition>,
    dynamic_sources: HashMap<String, Option<String>>,
    offset_lines: HashMap<usize, Vec<Option<i32>>>,
    instruction_lines: HashMap<usize, Option<Vec<Option<i32>>>>,
    multiline_maps: HashMap<String, BTreeMap<i32, i32>>,
    bytecodes: HashMap<usize, Vec<u8>>,
    cache_generator_bytecode: bool,
    semantic_branches: HashMap<usize, BTreeMap<i32, SemanticBranch>>,
    semantic_branch_targets: HashMap<usize, BTreeMap<i32, (i32, bool)>>,
    pub(super) first_lines: HashMap<usize, i32>,
    opcodes: Option<OpcodeSets>,
    pub(super) tracers: BTreeMap<String, String>,
    plugin_tracers: HashMap<usize, Py<PyAny>>,
    plugin_names: HashMap<usize, String>,
    plugin_raw_names: HashMap<usize, String>,
    dynamic_tracers: HashSet<usize>,
}

pub(super) struct PathPolicy {
    pub(super) cover_pylib: bool,
    pub(super) relative_root: Option<PathBuf>,
    pub(super) working_root: Option<PathBuf>,
}

impl PathPolicy {
    pub(super) fn new(cover_pylib: bool, relative_files: bool) -> Self {
        let working_root = std::env::current_dir()
            .ok()
            .map(|path| absolute_path(&path));
        Self {
            cover_pylib,
            relative_root: relative_files.then(|| working_root.clone()).flatten(),
            working_root,
        }
    }
}

#[derive(Clone, Copy)]
enum SourceMatch {
    Path,
    Package,
}

#[derive(Clone, Default)]
pub(super) struct FrameSource {
    pub(super) module_name: Option<String>,
    filename: Option<String>,
}

/// Inputs retained while plug-in code runs without borrowing the collector.
pub(super) enum PluginRequest {
    Register {
        raw: String,
        frame_source: FrameSource,
        plugins: Vec<Py<PyAny>>,
    },
    Dynamic {
        raw: String,
        tracer: Py<PyAny>,
    },
}

pub(super) struct PluginSelection {
    tracer: Py<PyAny>,
    name: String,
    dynamic: bool,
    source: Option<String>,
}

pub(super) enum PluginOutcome {
    Register(Option<PluginSelection>),
    Dynamic(Option<String>),
}

pub(super) struct PluginEvent {
    pub(super) path: Option<String>,
    pub(super) tracer: Option<Py<PyAny>>,
}

/// The Python objects needed for discovery are owned across the unlocked call.
pub(super) struct DiscoveryRequest {
    source_roots: Vec<PathBuf>,
    packages: Option<(Vec<String>, Py<PyAny>)>,
    plugins: Vec<Py<PyAny>>,
}

pub(super) struct DiscoveryOutcome {
    package_roots: Vec<PathBuf>,
    plugin_files: Vec<(String, String)>,
}

impl DiscoveryRequest {
    fn package_roots(&self, py: Python<'_>) -> PyResult<Vec<PathBuf>> {
        let Some((names, modules)) = &self.packages else {
            return Ok(Vec::new());
        };
        let mut roots = Vec::new();
        for package in names {
            let module = modules.bind(py).call_method1("get", (package,))?;
            if module.is_none() {
                continue;
            }
            let filename = match module.getattr("__file__") {
                Ok(filename) => filename,
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {
                    continue;
                }
                Err(error) => return Err(error),
            };
            if let Ok(filename) = filename.extract::<String>() {
                if !Path::new(&filename).exists() {
                    continue;
                }
                let path = absolute_path(Path::new(&python_source_filename(filename)));
                roots.push(
                    if path.file_name().is_some_and(|name| name == "__init__.py") {
                        path.parent().unwrap_or(&path).to_path_buf()
                    } else {
                        path
                    },
                );
            }
        }
        roots.sort();
        roots.dedup();
        Ok(roots)
    }

    pub(super) fn invoke(&self, py: Python<'_>) -> PyResult<DiscoveryOutcome> {
        let package_roots = self.package_roots(py)?;
        let mut plugin_files = Vec::new();
        for root in self.source_roots.iter().chain(&package_roots) {
            let Some(root) = root.to_str() else {
                continue;
            };
            for plugin in &self.plugins {
                let name: String = plugin.getattr(py, "_coverage_plugin_name")?.extract(py)?;
                let files = plugin.call_method1(py, "find_executable_files", (root,))?;
                for path in files.bind(py).try_iter()? {
                    plugin_files.push((path?.extract()?, name.clone()));
                }
            }
        }
        Ok(DiscoveryOutcome {
            package_roots,
            plugin_files,
        })
    }
}

impl PluginRequest {
    pub(super) fn invoke(&self, py: Python<'_>) -> PyResult<PluginOutcome> {
        match self {
            Self::Register {
                raw,
                frame_source,
                plugins,
            } => {
                let effective_raw = frame_source.filename.as_deref().unwrap_or(raw);
                let canonical = frame_source.filename.as_deref().map(|filename| {
                    absolute_path(Path::new(filename))
                        .to_string_lossy()
                        .into_owned()
                });
                for plugin in plugins {
                    if !plugin
                        .getattr(py, "_coverage_enabled")
                        .and_then(|value| value.extract(py))
                        .unwrap_or(true)
                    {
                        continue;
                    }
                    let tracer = plugin.call_method1(
                        py,
                        "file_tracer",
                        (canonical.as_deref().unwrap_or(effective_raw),),
                    )?;
                    if tracer.is_none(py) {
                        continue;
                    }
                    let dynamic: bool = tracer
                        .call_method0(py, "has_dynamic_source_filename")?
                        .extract(py)?;
                    let name: String = plugin.getattr(py, "_coverage_plugin_name")?.extract(py)?;
                    let source = if dynamic {
                        let frame = current_frame(py)?;
                        tracer
                            .call_method1(py, "dynamic_source_filename", (raw, frame))?
                            .extract(py)?
                    } else {
                        Some(tracer.call_method0(py, "source_filename")?.extract(py)?)
                    };
                    return Ok(PluginOutcome::Register(Some(PluginSelection {
                        tracer,
                        name,
                        dynamic,
                        source,
                    })));
                }
                Ok(PluginOutcome::Register(None))
            }
            Self::Dynamic { raw, tracer } => {
                let frame = current_frame(py)?;
                let source = tracer
                    .call_method1(py, "dynamic_source_filename", (raw, frame))?
                    .extract(py)?;
                Ok(PluginOutcome::Dynamic(source))
            }
        }
    }
}

#[derive(Clone)]
enum FilenameDisposition {
    Python(Option<String>),
    Plugin(usize),
}

struct SourcePackages {
    names: Vec<String>,
    /// One flag per name, set when a traced module matched that package.
    matched: Vec<AtomicBool>,
    discovery_roots: Vec<PathBuf>,
    get_frame: Py<PyAny>,
    modules: Py<PyAny>,
    module_suffixes: Vec<String>,
}

fn python_source_filename(mut filename: String) -> String {
    if filename
        .strip_suffix(".pyc")
        .or_else(|| filename.strip_suffix(".pyo"))
        .is_some()
    {
        filename.pop();
        if cfg!(windows)
            && !Path::new(&filename).exists()
            && Path::new(&format!("{filename}w")).exists()
        {
            filename.push('w');
        }
    }
    filename
}

struct OpcodeSets {
    jumps: HashSet<u8>,
    always_jumps: HashSet<u8>,
    backward_jumps: HashSet<u8>,
    returns: HashSet<u8>,
    cache: Option<u8>,
    extended_arg: u8,
    line_skips: HashSet<u8>,
    resume: Option<u8>,
    yield_value: Option<u8>,
}

impl OpcodeSets {
    fn new(py: Python<'_>) -> PyResult<Self> {
        let dis = py.import("dis")?;
        let mut opmap = HashMap::default();
        for entry in dis.getattr("opmap")?.call_method0("items")?.try_iter()? {
            let (name, value): (String, i32) = entry?.extract()?;
            opmap.insert(name, value);
        }
        let opcode = |name: &str| opmap.get(name).and_then(|&value| u8::try_from(value).ok());
        let always_jumps = [
            "JUMP_BACKWARD",
            "JUMP_BACKWARD_NO_INTERRUPT",
            "JUMP_FORWARD",
        ]
        .into_iter()
        .filter_map(opcode)
        .collect();
        let returns = ["RETURN_VALUE", "RETURN_CONST", "RETURN_GENERATOR"]
            .into_iter()
            .filter_map(opcode)
            .collect();
        let cache = opcode("CACHE");
        let extended_arg = opcode("EXTENDED_ARG").ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("Python dis module has no EXTENDED_ARG")
        })?;
        let line_skips = ["RESUME", "END_FOR", "END_SEND", "END_ASYNC_FOR"]
            .into_iter()
            .filter_map(opcode)
            .collect();
        let resume = opcode("RESUME");
        let yield_value = opcode("YIELD_VALUE");
        let jumps = dis
            .getattr("hasjrel")?
            .extract::<Vec<i32>>()?
            .into_iter()
            .filter_map(|value| u8::try_from(value).ok())
            .chain(
                dis.getattr("hasjabs")?
                    .extract::<Vec<i32>>()?
                    .into_iter()
                    .filter_map(|value| u8::try_from(value).ok()),
            )
            .collect::<HashSet<_>>();
        let opname: Vec<String> = dis.getattr("opname")?.extract()?;
        let backward_jumps = jumps
            .iter()
            .filter(|&&op| {
                opname
                    .get(op as usize)
                    .is_some_and(|name| name.contains("JUMP_BACKWARD"))
            })
            .copied()
            .collect();
        Ok(Self {
            jumps,
            always_jumps,
            backward_jumps,
            returns,
            cache,
            extended_arg,
            line_skips,
            resume,
            yield_value,
        })
    }
}

impl CodeRegistry {
    /// Prepare a plug-in decision without calling plug-in code. A code object
    /// stays alive in the callback until its completed disposition is committed.
    pub(super) fn prepare_plugin_event(
        &mut self,
        code: &Bound<'_, PyAny>,
        frame_source: &FrameSource,
    ) -> PyResult<Result<PluginEvent, PluginRequest>> {
        let id = Self::code_id(code);
        if !self.filenames.contains_key(&id) {
            let raw: String = code.getattr("co_filename")?.extract()?;
            if !(raw.starts_with('<')
                || self.filename_dispositions.contains_key(&raw)
                || self.ignore_annotation_code
                    && code.getattr("co_name")?.extract::<String>()? == "__annotate__")
            {
                let frame_source = frame_source.clone();
                let plugins = self
                    .plugins
                    .iter()
                    .map(|plugin| plugin.clone_ref(code.py()))
                    .collect();
                return Ok(Err(PluginRequest::Register {
                    raw,
                    frame_source,
                    plugins,
                }));
            }
            self.register_code_filename(code, id, Some(frame_source))?;
        }
        if self.dynamic_tracers.contains(&id) {
            return Ok(Err(PluginRequest::Dynamic {
                raw: self.plugin_raw_names[&id].clone(),
                tracer: self.plugin_tracers[&id].clone_ref(code.py()),
            }));
        }
        Ok(Ok(PluginEvent {
            path: self.filenames.get(&id).cloned().flatten(),
            tracer: self
                .plugin_tracers
                .get(&id)
                .map(|tracer| tracer.clone_ref(code.py())),
        }))
    }

    /// Commit a result only if another thread did not register the filename
    /// while the plug-in ran. The caller retries against the winning entry.
    pub(super) fn commit_plugin_event(
        &mut self,
        code: &Bound<'_, PyAny>,
        request: &PluginRequest,
        outcome: &PluginOutcome,
        current_frame_source: &FrameSource,
    ) -> PyResult<Option<PluginEvent>> {
        let id = Self::code_id(code);
        match (request, outcome) {
            (
                PluginRequest::Register {
                    raw, frame_source, ..
                },
                PluginOutcome::Register(selection),
            ) => {
                if self.filename_dispositions.contains_key(raw) || self.filenames.contains_key(&id)
                {
                    return Ok(None);
                }
                let path = if let Some(selection) = selection {
                    let path = match &selection.source {
                        Some(source) if selection.dynamic => {
                            self.resolve_dynamic_source(source, current_frame_source)
                        }
                        Some(source) => self.resolve_frame_filename(source, current_frame_source),
                        None => None,
                    };
                    if let Some(path) = &path {
                        self.tracers.insert(path.clone(), selection.name.clone());
                    }
                    self.plugin_tracers
                        .insert(id, selection.tracer.clone_ref(code.py()));
                    self.plugin_names.insert(id, selection.name.clone());
                    self.plugin_raw_names.insert(id, raw.clone());
                    if selection.dynamic {
                        self.dynamic_tracers.insert(id);
                    }
                    self.filename_dispositions
                        .insert(raw.clone(), FilenameDisposition::Plugin(id));
                    path
                } else {
                    let path = self.resolve_frame_filename(
                        frame_source.filename.as_deref().unwrap_or(raw),
                        current_frame_source,
                    );
                    self.filename_dispositions
                        .insert(raw.clone(), FilenameDisposition::Python(path.clone()));
                    path
                };
                self.filenames.insert(id, path.clone());
                self.retain_code(code, id)?;
                Ok(Some(PluginEvent {
                    path,
                    tracer: selection
                        .as_ref()
                        .map(|selection| selection.tracer.clone_ref(code.py())),
                }))
            }
            (PluginRequest::Dynamic { .. }, PluginOutcome::Dynamic(source)) => {
                let path = match source {
                    Some(source) => self.resolve_dynamic_source(source, current_frame_source),
                    None => None,
                };
                if let Some(path) = &path {
                    self.tracers
                        .insert(path.clone(), self.plugin_names[&id].clone());
                }
                Ok(Some(PluginEvent {
                    path,
                    tracer: self
                        .plugin_tracers
                        .get(&id)
                        .map(|tracer| tracer.clone_ref(code.py())),
                }))
            }
            _ => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "invalid plug-in resolution state",
            )),
        }
    }

    /// A mapped source's decision is authoritative once cached. Do not inspect
    /// a later frame's module metadata when the cached decision will be used.
    pub(super) fn outcome_needs_module(
        &self,
        code: &Bound<'_, PyAny>,
        request: &PluginRequest,
        outcome: &PluginOutcome,
    ) -> bool {
        if self.source_packages.is_none() {
            return false;
        }
        match (request, outcome) {
            (PluginRequest::Register { raw, .. }, PluginOutcome::Register(selection)) => {
                if self.filename_dispositions.contains_key(raw)
                    || self.filenames.contains_key(&Self::code_id(code))
                {
                    return false;
                }
                match selection {
                    None => true,
                    Some(selection) if !selection.dynamic => selection.source.is_some(),
                    Some(selection) => selection
                        .source
                        .as_ref()
                        .is_some_and(|source| !self.dynamic_sources.contains_key(source)),
                }
            }
            (PluginRequest::Dynamic { .. }, PluginOutcome::Dynamic(source)) => source
                .as_ref()
                .is_some_and(|source| !self.dynamic_sources.contains_key(source)),
            _ => false,
        }
    }

    pub(super) fn module_frame_lookup(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.source_packages
            .as_ref()
            .map(|packages| packages.get_frame.clone_ref(py))
    }
    pub(super) fn set_source_packages(
        &mut self,
        py: Python<'_>,
        names: Vec<String>,
    ) -> PyResult<()> {
        if !names.is_empty() {
            let sys = py.import("sys")?;
            let mut module_suffixes: Vec<String> = py
                .import("importlib.machinery")?
                .call_method0("all_suffixes")?
                .extract()?;
            module_suffixes.sort_by_key(|suffix| std::cmp::Reverse(suffix.len()));
            self.source_packages = Some(SourcePackages {
                matched: names.iter().map(|_| AtomicBool::new(false)).collect(),
                names,
                discovery_roots: Vec::new(),
                // Restricted application globals need not provide __import__.
                get_frame: sys.getattr("_getframe")?.unbind(),
                modules: sys.getattr("modules")?.unbind(),
                module_suffixes,
            });
        }
        Ok(())
    }

    pub(super) fn prepare_discovery(&self, py: Python<'_>) -> DiscoveryRequest {
        DiscoveryRequest {
            source_roots: self.source_roots.clone(),
            packages: self
                .source_packages
                .as_ref()
                .map(|packages| (packages.names.clone(), packages.modules.clone_ref(py))),
            plugins: self
                .plugins
                .iter()
                .map(|plugin| plugin.clone_ref(py))
                .collect(),
        }
    }

    pub(super) fn commit_discovery(&mut self, outcome: DiscoveryOutcome) -> Vec<(String, String)> {
        if let Some(packages) = &mut self.source_packages {
            packages.discovery_roots = outcome.package_roots;
        }
        let mut files: Vec<_> = outcome
            .plugin_files
            .into_iter()
            .filter_map(|(path, name)| {
                self.discovered_filename(Path::new(&path))
                    .map(|path| (path, name))
            })
            .collect();
        files.sort();
        files
    }

    #[cfg(test)]
    pub(super) fn resolve_loaded_source_packages(&mut self, py: Python<'_>) -> PyResult<()> {
        let roots = self.prepare_discovery(py).package_roots(py)?;
        if let Some(packages) = &mut self.source_packages {
            packages.discovery_roots = roots;
        }
        Ok(())
    }

    /// Retain the frame lookup only when a new disposition needs module metadata.
    pub(super) fn frame_lookup(&mut self, code: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyAny>>> {
        if self.source_packages.is_none() {
            return Ok(None);
        }
        let id = Self::code_id(code);
        if self.excluded_code == ExcludedCodeRetention::Weak {
            self.discard_freed_identity(code, id);
        }
        let Some(packages) = &self.source_packages else {
            return Ok(None);
        };
        if self.dynamic_tracers.contains(&id) {
            return Ok(None);
        }
        if self.filenames.contains_key(&id) {
            return Ok(None);
        }
        let raw: String = code.getattr("co_filename")?.extract()?;
        if raw.starts_with('<') || self.filename_dispositions.contains_key(&raw) {
            return Ok(None);
        }
        Ok(Some(packages.get_frame.clone_ref(code.py())))
    }

    /// Frame access and module metadata may invoke audit hooks and descriptors.
    /// Call this only after releasing the collector borrow.
    pub(super) fn capture_frame_source(
        code: &Bound<'_, PyAny>,
        lookup: &Py<PyAny>,
        supplied: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<(FrameSource, Option<Py<PyAny>>)> {
        let globals = Self::capture_frame_globals(code, lookup, supplied)?;
        let Some(bound_globals) = globals.as_ref().map(|globals| globals.bind(code.py())) else {
            return Ok((FrameSource::default(), globals));
        };
        let raw: String = code.getattr("co_filename")?.extract()?;
        let module_file = bound_globals.call_method1("get", ("__file__",))?;
        let filename = if module_file.is_truthy()? {
            let filename = python_source_filename(module_file.str()?.to_str()?.to_owned());
            (raw.is_empty() || Path::new(&raw).file_name() == Path::new(&filename).file_name())
                .then_some(filename)
        } else {
            None
        };
        Ok((
            FrameSource {
                filename,
                module_name: None,
            },
            globals,
        ))
    }

    pub(super) fn capture_frame_globals(
        code: &Bound<'_, PyAny>,
        lookup: &Py<PyAny>,
        supplied: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Option<Py<PyAny>>> {
        let py = code.py();
        let globals = if let Some(globals) = supplied {
            Some(globals.clone().into_any())
        } else {
            let mut frame = lookup.call0(py)?.into_bound(py);
            let mut found = None;
            while !frame.is_none() {
                if frame.getattr("f_code")?.is(code) {
                    found = Some(frame.getattr("f_globals")?);
                    break;
                }
                frame = frame.getattr("f_back")?;
            }
            found
        };
        Ok(globals.map(Bound::unbind))
    }

    pub(super) fn capture_module_name(
        globals: Option<&Py<PyAny>>,
        py: Python<'_>,
    ) -> PyResult<Option<String>> {
        let Some(globals) = globals else {
            return Ok(None);
        };
        let globals = globals.bind(py);
        let mut name = globals
            .call_method1("get", ("__name__",))?
            .extract::<String>()
            .ok();
        if name.as_deref().is_none_or(|name| name == "__main__") {
            let spec = globals.call_method1("get", ("__spec__",))?;
            if spec.is_truthy()? {
                name = spec.getattr("name")?.extract::<String>().ok();
            }
        }
        Ok(name.filter(|name| name != "__main__"))
    }

    fn resolve_frame_filename(&self, raw: &str, frame_source: &FrameSource) -> Option<String> {
        self.resolve_filename_with_module(raw, frame_source.module_name.as_deref())
    }

    fn source_match(&self, raw: &str, module_name: Option<&str>) -> SourceMatch {
        let Some(packages) = &self.source_packages else {
            return SourceMatch::Path;
        };
        let name = module_name.or_else(|| {
            let filename = Path::new(raw).file_name()?.to_str()?;
            packages
                .module_suffixes
                .iter()
                .find_map(|suffix| filename.strip_suffix(suffix))
        });
        let Some(name) = name else {
            return SourceMatch::Path;
        };
        let mut matched = false;
        for (package, seen) in packages.names.iter().zip(&packages.matched) {
            if name == package
                || name
                    .strip_prefix(package.as_str())
                    .is_some_and(|suffix| suffix.starts_with('.'))
            {
                seen.store(true, Ordering::Relaxed);
                matched = true;
            }
        }
        if matched {
            SourceMatch::Package
        } else {
            SourceMatch::Path
        }
    }

    /// Source package names that no traced module has matched, in request order.
    pub(super) fn unmatched_source_packages(&self) -> Vec<String> {
        self.source_packages
            .as_ref()
            .map_or_else(Vec::new, |packages| {
                packages
                    .names
                    .iter()
                    .zip(&packages.matched)
                    .filter(|(_, seen)| !seen.load(Ordering::Relaxed))
                    .map(|(name, _)| name.clone())
                    .collect()
            })
    }

    pub(super) fn discover_source_files(&self) -> Vec<String> {
        let mut files = std::collections::BTreeSet::new();
        for root in self.source_discovery_roots() {
            if self.source_roots.contains(root) && root.is_file() {
                if let Some(path) = self.discovered_filename(root) {
                    files.insert(path);
                }
                continue;
            }
            for path in discover_python_source_files(root) {
                if let Some(path) = self.discovered_filename(&path) {
                    files.insert(path);
                }
            }
        }
        files.into_iter().collect()
    }

    fn source_discovery_roots(&self) -> impl Iterator<Item = &PathBuf> {
        self.source_roots.iter().chain(
            self.source_packages
                .iter()
                .flat_map(|packages| &packages.discovery_roots),
        )
    }

    #[cfg(test)]
    pub(super) fn new(
        source_roots: &[String],
        excluded_roots: &[String],
        package_root: &str,
        include: &[String],
        omit: &[String],
        plugins: Vec<Py<PyAny>>,
        path_policy: PathPolicy,
    ) -> PyResult<Self> {
        Self::new_with_unicode_version(
            source_roots,
            &InterpreterRoots {
                stdlib: excluded_roots.to_vec(),
                third_party: Vec::new(),
            },
            package_root,
            include,
            omit,
            plugins,
            path_policy,
            UnicodeCaseVersion::default(),
        )
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "Keep interpreter version separate from Python-facing options."
    )]
    pub(super) fn new_with_unicode_version(
        source_roots: &[String],
        interpreter_roots: &InterpreterRoots,
        package_root: &str,
        include: &[String],
        omit: &[String],
        plugins: Vec<Py<PyAny>>,
        path_policy: PathPolicy,
        unicode_case_version: UnicodeCaseVersion,
    ) -> PyResult<Self> {
        Ok(Self {
            ignore_annotation_code: false,
            excluded_code: ExcludedCodeRetention::Strong,
            source_roots: source_roots
                .iter()
                .map(|path| absolute_path(Path::new(path)))
                .collect(),
            source_packages: None,
            stdlib_roots: interpreter_roots
                .stdlib
                .iter()
                .map(|path| absolute_path(Path::new(path)))
                .collect(),
            third_party_roots: interpreter_roots
                .third_party
                .iter()
                .map(|path| absolute_path(Path::new(path)))
                .collect(),
            package_root: absolute_path(Path::new(&package_root)),
            relative_root: path_policy.relative_root,
            working_root: path_policy.working_root,
            cover_pylib: path_policy.cover_pylib,
            include: glob_set_with_unicode_version(include, unicode_case_version)
                .map_err(|error| crate::report::pattern_error(&error))?,
            omit: glob_set_with_unicode_version(omit, unicode_case_version)
                .map_err(|error| crate::report::pattern_error(&error))?,
            plugins,
            code_objects: HashMap::default(),
            filenames: HashMap::default(),
            filename_dispositions: HashMap::default(),
            dynamic_sources: HashMap::default(),
            offset_lines: HashMap::default(),
            instruction_lines: HashMap::default(),
            multiline_maps: HashMap::default(),
            bytecodes: HashMap::default(),
            cache_generator_bytecode: false,
            semantic_branches: HashMap::default(),
            semantic_branch_targets: HashMap::default(),
            first_lines: HashMap::default(),
            opcodes: None,
            tracers: BTreeMap::new(),
            plugin_tracers: HashMap::default(),
            plugin_names: HashMap::default(),
            plugin_raw_names: HashMap::default(),
            dynamic_tracers: HashSet::default(),
        })
    }

    pub(super) fn code_id(code: &Bound<'_, PyAny>) -> usize {
        code.as_ptr() as usize
    }

    pub(super) fn code_object(&self, py: Python<'_>, code_id: usize) -> PyResult<Py<PyAny>> {
        match self.code_objects.get(&code_id) {
            Some(CodeHandle::Strong(code)) => Some(code.clone_ref(py)),
            Some(CodeHandle::Weak(reference)) => reference.bind(py).upgrade().map(Bound::unbind),
            None => None,
        }
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("unregistered branch code"))
    }

    /// Drop cached state for an excluded code object that `CPython` freed.
    ///
    /// A new code object can occupy the freed address. Without this check it
    /// would inherit the excluded disposition and lose its coverage.
    fn discard_freed_identity(&mut self, code: &Bound<'_, PyAny>, code_id: usize) {
        if let Some(CodeHandle::Weak(reference)) = self.code_objects.get(&code_id)
            && !reference
                .bind(code.py())
                .upgrade()
                .is_some_and(|registered| registered.is(code))
        {
            self.forget_code(code_id);
        }
    }

    fn forget_code(&mut self, code_id: usize) {
        self.code_objects.remove(&code_id);
        self.filenames.remove(&code_id);
        self.offset_lines.remove(&code_id);
        self.instruction_lines.remove(&code_id);
        self.bytecodes.remove(&code_id);
        self.semantic_branches.remove(&code_id);
        self.semantic_branch_targets.remove(&code_id);
        self.first_lines.remove(&code_id);
        self.plugin_tracers.remove(&code_id);
        self.plugin_names.remove(&code_id);
        self.plugin_raw_names.remove(&code_id);
        self.dynamic_tracers.remove(&code_id);
    }

    pub(super) fn first_line(&mut self, code: &Bound<'_, PyAny>) -> PyResult<i32> {
        let code_id = Self::code_id(code);
        if let Some(&first_line) = self.first_lines.get(&code_id) {
            return Ok(first_line);
        }
        let first_line: i32 = code.getattr("co_firstlineno")?.extract()?;
        self.first_lines.insert(code_id, first_line);
        Ok(first_line)
    }

    fn resolve_dynamic_source(
        &mut self,
        source: &str,
        frame_source: &FrameSource,
    ) -> Option<String> {
        if let Some(cached) = self.dynamic_sources.get(source) {
            return cached.clone();
        }
        let filename = self.resolve_frame_filename(source, frame_source);
        self.dynamic_sources
            .insert(source.to_owned(), filename.clone());
        filename
    }

    fn reuse_plugin_disposition(
        &mut self,
        code: &Bound<'_, PyAny>,
        code_id: usize,
        original_id: usize,
    ) {
        self.plugin_tracers.insert(
            code_id,
            self.plugin_tracers[&original_id].clone_ref(code.py()),
        );
        self.plugin_names
            .insert(code_id, self.plugin_names[&original_id].clone());
        self.plugin_raw_names
            .insert(code_id, self.plugin_raw_names[&original_id].clone());
        if self.dynamic_tracers.contains(&original_id) {
            self.dynamic_tracers.insert(code_id);
            self.filenames.insert(code_id, None);
        } else {
            self.filenames
                .insert(code_id, self.filenames[&original_id].clone());
        }
    }

    fn register_filename(
        &mut self,
        code_id: usize,
        raw: String,
        frame_source: Option<&FrameSource>,
    ) {
        if raw.starts_with('<') {
            // Generated code often has a unique pseudo-filename per object.
            // The prefix test is cheaper than retaining each name.
            self.filenames.insert(code_id, None);
            return;
        }
        let default_source = FrameSource::default();
        let frame_source = frame_source.unwrap_or(&default_source);
        let filename = self.resolve_frame_filename(
            frame_source.filename.as_deref().unwrap_or(&raw),
            frame_source,
        );
        let disposition = FilenameDisposition::Python(filename.clone());
        self.filename_dispositions.insert(raw, disposition);
        self.filenames.insert(code_id, filename);
    }

    pub(super) fn filename(&mut self, code: &Bound<'_, PyAny>) -> PyResult<Option<&str>> {
        let code_id = Self::code_id(code);
        if self.excluded_code == ExcludedCodeRetention::Weak {
            self.discard_freed_identity(code, code_id);
        }
        if !self.filenames.contains_key(&code_id) {
            self.register_code_filename(code, code_id, None)?;
        }
        Ok(self.filenames.get(&code_id).and_then(Option::as_deref))
    }

    pub(super) fn filename_with_globals(
        &mut self,
        code: &Bound<'_, PyAny>,
        frame_source: Option<&FrameSource>,
    ) -> PyResult<Option<&str>> {
        let code_id = Self::code_id(code);
        if self.excluded_code == ExcludedCodeRetention::Weak {
            self.discard_freed_identity(code, code_id);
        }
        if frame_source.is_some() && !self.filenames.contains_key(&code_id) {
            self.register_code_filename(code, code_id, frame_source)?;
        }
        self.filename(code)
    }

    fn register_code_filename(
        &mut self,
        code: &Bound<'_, PyAny>,
        code_id: usize,
        frame_source: Option<&FrameSource>,
    ) -> PyResult<()> {
        self.register_code_disposition(code, code_id, frame_source)?;
        self.retain_code(code, code_id)
    }

    fn retain_code(&mut self, code: &Bound<'_, PyAny>, code_id: usize) -> PyResult<()> {
        self.code_objects
            .insert(code_id, CodeHandle::Strong(code.clone().unbind()));
        if self.cache_generator_bytecode && code.getattr("co_flags")?.extract::<u32>()? & 0x20 != 0
        {
            self.cache_bytecode(code)?;
        }
        if self.excluded_code == ExcludedCodeRetention::Weak
            && self.filenames.get(&code_id).is_some_and(Option::is_none)
        {
            let reference = PyWeakrefReference::new(code)?.unbind();
            self.code_objects
                .insert(code_id, CodeHandle::Weak(reference));
        }
        Ok(())
    }

    fn register_code_disposition(
        &mut self,
        code: &Bound<'_, PyAny>,
        code_id: usize,
        frame_source: Option<&FrameSource>,
    ) -> PyResult<()> {
        if self.ignore_annotation_code
            && code.getattr("co_name")?.extract::<String>()? == "__annotate__"
        {
            // Coverage.py's monitoring core skips __annotate__ code.
            self.filenames.insert(code_id, None);
            return Ok(());
        }
        let raw: String = code.getattr("co_filename")?.extract()?;
        match self.filename_dispositions.get(&raw).cloned() {
            Some(FilenameDisposition::Python(filename)) => {
                self.filenames.insert(code_id, filename);
            }
            Some(FilenameDisposition::Plugin(original_id)) => {
                self.reuse_plugin_disposition(code, code_id, original_id);
            }
            None => self.register_filename(code_id, raw, frame_source),
        }
        Ok(())
    }

    #[cfg(test)]
    pub(super) fn resolve_filename(&self, raw: &str) -> Option<String> {
        self.resolve_filename_with_module(raw, None)
    }

    fn resolve_filename_with_module(&self, raw: &str, module_name: Option<&str>) -> Option<String> {
        if raw.is_empty() || raw.starts_with('<') {
            return None;
        }
        let path = absolute_path(Path::new(raw));
        if path.starts_with(&self.package_root) {
            return None;
        }
        let has_sources = !self.source_roots.is_empty() || self.source_packages.is_some();
        let source_match = self.source_match(raw, module_name);
        if !has_sources && self.include.is_none() {
            // Installed packages can live inside the standard library directory,
            // so `cover_pylib` alone never admits a third-party root.
            let excluded = |roots: &[PathBuf]| roots.iter().any(|root| path.starts_with(root));
            if excluded(&self.third_party_roots)
                || (!self.cover_pylib && excluded(&self.stdlib_roots))
            {
                return None;
            }
        } else if has_sources
            && !matches!(source_match, SourceMatch::Package)
            && !self.source_roots.iter().any(|root| path.starts_with(root))
        {
            return None;
        }
        let rendered = glob_path(&path);
        let relative = self
            .working_root
            .as_ref()
            .and_then(|root| path.strip_prefix(root).ok())
            .map(glob_path);
        let matches = |patterns: &CoverageGlobSet| {
            patterns.is_match(&rendered)
                || relative
                    .as_ref()
                    .is_some_and(|relative| patterns.is_match(relative))
        };
        if self
            .include
            .as_ref()
            .is_some_and(|patterns| !matches(patterns))
            || self.omit.as_ref().is_some_and(matches)
        {
            return None;
        }
        Some(
            self.relative_root
                .as_ref()
                .and_then(|root| path.strip_prefix(root).ok())
                .unwrap_or(&path)
                .to_string_lossy()
                .into_owned(),
        )
    }

    fn discovered_filename(&self, raw: &Path) -> Option<String> {
        let path = absolute_path(raw);
        let rendered = glob_path(&path);
        let relative = self
            .working_root
            .as_ref()
            .and_then(|root| path.strip_prefix(root).ok())
            .map(glob_path);
        let omitted = self.omit.as_ref().is_some_and(|patterns| {
            patterns.is_match(&rendered)
                || relative
                    .as_ref()
                    .is_some_and(|relative| patterns.is_match(relative))
        });
        if omitted {
            return None;
        }
        Some(
            self.relative_root
                .as_ref()
                .and_then(|root| path.strip_prefix(root).ok())
                .unwrap_or(&path)
                .to_string_lossy()
                .into_owned(),
        )
    }

    pub(super) fn prepare_branch_lines(&mut self, code: &Bound<'_, PyAny>) -> PyResult<()> {
        let Some(path) = self.filename(code)?.map(str::to_owned) else {
            return Ok(());
        };
        if self.tracers.contains_key(&path) || self.multiline_maps.contains_key(&path) {
            return Ok(());
        }
        // Build once at snapshot time, outside monitoring callbacks.
        let source_path = self
            .relative_root
            .as_ref()
            .map_or_else(|| PathBuf::from(&path), |root| root.join(&path));
        let mapping = read_python_source(&source_path).map_or_else(
            |_| BTreeMap::new(),
            |source| crate::analysis::multiline_map(&source),
        );
        self.multiline_maps.insert(path, mapping);
        Ok(())
    }

    fn branch_line(&self, code_id: usize, line: i32) -> i32 {
        self.filenames
            .get(&code_id)
            .and_then(Option::as_ref)
            .and_then(|path| self.multiline_maps.get(path))
            .and_then(|mapping| mapping.get(&line))
            .copied()
            .unwrap_or(line)
    }

    pub(super) fn branch_line_for_offset(
        &mut self,
        code: &Bound<'_, PyAny>,
        offset: i32,
    ) -> PyResult<Option<i32>> {
        Ok(self
            .line_for_offset_exact(code, offset)?
            .map(|line| self.branch_line(Self::code_id(code), line)))
    }

    pub(super) fn line_for_offset(
        &mut self,
        code: &Bound<'_, PyAny>,
        offset: i32,
    ) -> PyResult<i32> {
        let first_line = self.first_line(code)?;
        Ok(self
            .line_for_offset_exact(code, offset)?
            .unwrap_or(first_line))
    }

    pub(super) fn line_for_offset_exact(
        &mut self,
        code: &Bound<'_, PyAny>,
        offset: i32,
    ) -> PyResult<Option<i32>> {
        let lines = self.offset_line_table(code)?;
        Ok(line_for_word_offset(lines, offset))
    }

    fn offset_line_table<'a>(&'a mut self, code: &Bound<'_, PyAny>) -> PyResult<&'a [Option<i32>]> {
        let code_id = Self::code_id(code);
        match self.offset_lines.entry(code_id) {
            std::collections::hash_map::Entry::Occupied(entry) => Ok(entry.into_mut().as_slice()),
            std::collections::hash_map::Entry::Vacant(entry) => {
                let bytecode_length = code.getattr("co_code")?.len()?;
                let max_offset = i32::try_from(bytecode_length).map_err(|_| {
                    pyo3::exceptions::PyOverflowError::new_err("Python bytecode is too large")
                })?;
                let word_count = bytecode_length.div_ceil(2);
                let mut lines = Vec::new();
                lines.try_reserve_exact(word_count).map_err(|_| {
                    pyo3::exceptions::PyMemoryError::new_err("failed to allocate Python line map")
                })?;
                lines.resize(word_count, None);
                for item in code.call_method0("co_lines")?.try_iter()? {
                    let (start, end, line): (i32, i32, Option<i32>) = item?.extract()?;
                    let Some(line) = line else {
                        continue;
                    };
                    let start = start.max(0);
                    let end = end.min(max_offset);
                    if start >= end || start % 2 != 0 {
                        continue;
                    }
                    for offset in (start..end).step_by(2) {
                        let index = usize::try_from(offset / 2).map_err(|_| {
                            pyo3::exceptions::PyOverflowError::new_err(
                                "Python bytecode is too large",
                            )
                        })?;
                        if let Some(slot) = lines.get_mut(index) {
                            *slot = Some(line);
                        }
                    }
                }
                Ok(entry.insert(lines).as_slice())
            }
        }
    }

    pub(super) fn cache_bytecode(&mut self, code: &Bound<'_, PyAny>) -> PyResult<()> {
        let code_id = Self::code_id(code);
        if let std::collections::hash_map::Entry::Vacant(entry) = self.bytecodes.entry(code_id) {
            let bytecode: Vec<u8> = code.getattr("co_code")?.extract()?;
            entry.insert(bytecode);
        }
        Ok(())
    }

    pub(super) fn prepare_generator_exit_tracking(&mut self, py: Python<'_>) -> PyResult<()> {
        if self.opcodes.is_none() {
            self.opcodes = Some(OpcodeSets::new(py)?);
        }
        self.cache_generator_bytecode = true;
        Ok(())
    }

    pub(super) fn is_delegated_yield(&self, code_id: usize, offset: i32) -> bool {
        let Some(opcodes) = &self.opcodes else {
            return false;
        };
        let Some(bytecode) = self.bytecodes.get(&code_id) else {
            return false;
        };
        let Ok(index) = usize::try_from(offset) else {
            return false;
        };
        let Some(instructions) = bytecode.get(index..index + 4) else {
            return false;
        };
        // RESUME's low two bits distinguish yield, yield-from, and await.
        // CPython 3.13+ can set additional flags in the same argument.
        Some(instructions[0]) == opcodes.yield_value
            && Some(instructions[2]) == opcodes.resume
            && instructions[3] & 0x03 == 2
    }

    pub(super) fn semantic_branch(
        &mut self,
        code: &Bound<'_, PyAny>,
        origin: i32,
    ) -> PyResult<Option<SemanticBranch>> {
        let code_id = Self::code_id(code);
        if !self.semantic_branches.contains_key(&code_id) {
            self.cache_bytecode(code)?;
            let dis = code.py().import("dis")?;
            let opnames: Vec<String> = dis.getattr("opname")?.extract()?;
            let lines = self.offset_lines_for(code)?;
            let bytecode = self.bytecodes.get(&code_id).cloned().unwrap_or_default();
            let mut by_line: BTreeMap<i32, usize> = BTreeMap::new();
            let mut by_expression: BTreeMap<SourceRange, usize> = BTreeMap::new();
            let mut branches = BTreeMap::new();
            let mut targets = BTreeMap::new();
            let mut instruction_targets = BTreeMap::new();
            let mut truth_position: Option<(i32, i32)> = None;
            for item in dis.call_method1("get_instructions", (code,))?.try_iter()? {
                let instruction = item?;
                let item_offset: i32 = instruction.getattr("offset")?.extract()?;
                let opname: String = instruction.getattr("opname")?.extract()?;
                let position = instruction.getattr("positions").ok().and_then(|value| {
                    value
                        .extract::<(Option<i32>, Option<i32>, Option<i32>, Option<i32>)>()
                        .ok()
                });
                if opname == "TO_BOOL" {
                    truth_position = position.and_then(|(line, _, column, _)| line.zip(column));
                    continue;
                }
                if !opname.contains("JUMP_IF") && !opname.contains("POP_JUMP") {
                    continue;
                }
                let target: Option<i32> = instruction
                    .getattr("argval")
                    .ok()
                    .and_then(|value| value.extract().ok());
                if let Some(target) = target {
                    instruction_targets.insert(item_offset, (target, opname.contains("IF_TRUE")));
                }
                let condition_position = truth_position
                    .take()
                    .or_else(|| position.and_then(|(line, _, column, _)| line.zip(column)));
                if let Some((line, column)) = condition_position {
                    let ordinal = next_ordinal(&mut by_line, line);
                    let expression_range = source_range(position);
                    let expression_ordinal =
                        expression_range.map_or(0, |range| next_ordinal(&mut by_expression, range));
                    branches.insert(
                        item_offset,
                        SemanticBranch {
                            line,
                            column,
                            ordinal,
                            expression_range,
                            expression_ordinal,
                        },
                    );
                }
            }
            for offset in (0..bytecode.len()).step_by(2) {
                let opcode = bytecode[offset];
                let Some(name) = opnames.get(usize::from(opcode)) else {
                    continue;
                };
                if !name.contains("JUMP_IF") && !name.contains("POP_JUMP") {
                    continue;
                }
                let offset = i32::try_from(offset).map_err(|_| {
                    pyo3::exceptions::PyOverflowError::new_err("Python bytecode is too large")
                })?;
                if branches.contains_key(&offset) {
                    if let Some(&(target, is_true)) = instruction_targets.get(&offset) {
                        targets.insert(offset, (target, is_true));
                    }
                    continue;
                }
                let Some(&line) = lines.get(&offset) else {
                    continue;
                };
                let ordinal = next_ordinal(&mut by_line, line);
                branches.insert(
                    offset,
                    SemanticBranch {
                        line,
                        column: -1,
                        ordinal,
                        expression_range: None,
                        expression_ordinal: 0,
                    },
                );
                if let Some(&(target, is_true)) = instruction_targets.get(&offset) {
                    targets.insert(offset, (target, is_true));
                }
            }
            self.semantic_branches.insert(code_id, branches);
            self.semantic_branch_targets.insert(code_id, targets);
        }
        Ok(self
            .semantic_branches
            .get(&code_id)
            .and_then(|branches| branches.get(&origin).copied()))
    }

    pub(super) fn offset_lines_for(
        &mut self,
        code: &Bound<'_, PyAny>,
    ) -> PyResult<BTreeMap<i32, i32>> {
        let lines = self.offset_line_table(code)?;
        let mut mapping = BTreeMap::new();
        for (index, line) in lines.iter().enumerate() {
            let Some(line) = line else {
                continue;
            };
            let offset = index
                .checked_mul(2)
                .and_then(|offset| i32::try_from(offset).ok())
                .ok_or_else(|| {
                    pyo3::exceptions::PyOverflowError::new_err("Python bytecode is too large")
                })?;
            mapping.insert(offset, *line);
        }
        Ok(mapping)
    }

    pub(super) fn semantic_branch_outcome(
        &mut self,
        code: &Bound<'_, PyAny>,
        origin: i32,
        destination: i32,
    ) -> PyResult<Option<bool>> {
        let code_id = Self::code_id(code);
        if let Some((target, jump_if_true)) = self
            .semantic_branch_targets
            .get(&code_id)
            .and_then(|targets| targets.get(&origin))
        {
            let taken = destination == *target;
            return Ok(Some(if *jump_if_true { taken } else { !taken }));
        }
        self.cache_bytecode(code)?;
        let bytecode = self.bytecodes.get(&code_id).cloned().unwrap_or_default();
        let index = usize::try_from(origin).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err("negative Python bytecode offset")
        })?;
        let Some(&opcode) = bytecode.get(index) else {
            return Ok(None);
        };
        let dis = code.py().import("dis")?;
        let opnames: Vec<String> = dis.getattr("opname")?.extract()?;
        let Some(name) = opnames.get(usize::from(opcode)) else {
            return Ok(None);
        };
        let Some(&argument) = bytecode.get(index + 1) else {
            return Ok(None);
        };
        let next = origin.saturating_add(2);
        let target = if name.contains("BACKWARD") {
            next.saturating_sub(2 * i32::from(argument))
        } else {
            next.saturating_add(2 * i32::from(argument))
        };
        let taken = destination == target;
        if name.contains("IF_TRUE") {
            Ok(Some(taken))
        } else if name.contains("IF_FALSE") {
            Ok(Some(!taken))
        } else {
            Ok(None)
        }
    }

    pub(super) fn resolve_branch(
        &mut self,
        code: &Bound<'_, PyAny>,
        origin: i32,
        destination: i32,
    ) -> PyResult<Option<(i32, i32)>> {
        if self.opcodes.is_none() {
            self.opcodes = Some(OpcodeSets::new(code.py())?);
        }
        self.prepare_branch_lines(code)?;
        let Some(from_line) = self.branch_line_for_offset(code, origin)? else {
            return Ok(None);
        };
        let first_line = self.first_line(code)?;
        self.cache_bytecode(code)?;
        let code_id = Self::code_id(code);
        let Some(bytecode) = self.bytecodes.get(&code_id) else {
            return Ok(None);
        };
        let Some(opcodes) = self.opcodes.as_ref() else {
            return Ok(None);
        };
        let Some(byte_to_line) = self.offset_lines.get(&code_id) else {
            return Ok(None);
        };
        let mut offset = destination;
        let max_offset = i32::try_from(bytecode.len()).map_err(|_| {
            pyo3::exceptions::PyOverflowError::new_err("Python bytecode is too large")
        })?;
        let mut extended_arg: i32 = 0;
        let mut seen = HashSet::default();
        while (0..max_offset).contains(&offset) && seen.insert(offset) {
            let index = usize::try_from(offset).map_err(|_| {
                pyo3::exceptions::PyValueError::new_err("negative Python bytecode offset")
            })?;
            let Some(&op) = bytecode.get(index) else {
                break;
            };
            if opcodes.cache == Some(op) {
                offset += 2;
                continue;
            }
            if op == opcodes.extended_arg {
                let Some(&argument) = bytecode.get(index + 1) else {
                    break;
                };
                extended_arg = (extended_arg | i32::from(argument)) << 8;
                offset += 2;
                continue;
            }
            if let Some(line) = line_for_word_offset(byte_to_line, offset)
                && let line = self.branch_line(code_id, line)
                && line != 0
                && line != from_line
            {
                return Ok(Some((from_line, line)));
            }
            if opcodes.jumps.contains(&op) {
                if opcodes.always_jumps.contains(&op) {
                    let Some(&argument) = bytecode.get(index + 1) else {
                        break;
                    };
                    let argument = extended_arg | i32::from(argument);
                    let mut next_offset = offset + 2;
                    while (0..max_offset).contains(&next_offset)
                        && opcodes.cache.is_some_and(|cache| {
                            usize::try_from(next_offset)
                                .ok()
                                .and_then(|index| bytecode.get(index))
                                .is_some_and(|&opcode| opcode == cache)
                        })
                    {
                        next_offset += 2;
                    }
                    offset = if opcodes.backward_jumps.contains(&op) {
                        next_offset - 2 * argument
                    } else {
                        next_offset + 2 * argument
                    };
                    extended_arg = 0;
                    continue;
                }
                return Ok(None);
            }
            if opcodes.returns.contains(&op) {
                return Ok(Some((from_line, -first_line)));
            }
            extended_arg = 0;
            offset += 2;
        }
        Ok(None)
    }
}
