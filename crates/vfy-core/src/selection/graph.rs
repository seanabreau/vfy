use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::{Path, PathBuf};

use ruff_python_ast::Stmt;
use ruff_python_ast::statement_visitor::{StatementVisitor, walk_stmt};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    CoreError, absolute_path, atomic_write, glob_path, read_python_source, replay_fingerprint,
};

const GRAPH_VERSION: u64 = 7;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
struct ImportReference {
    module: String,
    level: u32,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
struct ImportSource {
    content_digest: String,
    imports: Vec<ImportReference>,
}

type Imports = BTreeMap<String, ImportSource>;

#[derive(Default)]
struct Graph {
    files: Imports,
    reverse: BTreeMap<String, BTreeSet<String>>,
    source_identity: String,
    complete: bool,
    valid: bool,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct PortableGraph {
    files: Imports,
    reverse: BTreeMap<String, BTreeSet<String>>,
    complete: bool,
}

impl PortableGraph {
    pub(super) fn export(history: &Path, root: &Path) -> Result<Self, CoreError> {
        let graph_path = history.with_file_name("import-graph.json");
        let graph = read_graph(&graph_path);
        if !graph.valid {
            return Err(CoreError::InvalidInput(format!(
                "baseline import graph `{}` is missing, malformed, or written by another vfy version; \
                 rerun the baseline before exporting it",
                graph_path.display()
            )));
        }
        let relative = |path: &str| super::ci::relative_path(root, Path::new(path));
        let mut files = BTreeMap::new();
        for (path, source) in graph.files {
            files.insert(relative(&path)?, source);
        }
        let mut reverse = BTreeMap::new();
        for (path, dependents) in graph.reverse {
            reverse.insert(
                relative(&path)?,
                dependents
                    .iter()
                    .map(|path| relative(path))
                    .collect::<Result<_, _>>()?,
            );
        }
        Ok(Self {
            files,
            reverse,
            complete: graph.complete,
        })
    }

    pub(super) fn validate(&self, tracked: &BTreeSet<String>) -> Result<(), CoreError> {
        if self.files.keys().any(|path| !tracked.contains(path))
            || self.reverse.iter().any(|(path, dependents)| {
                !self.files.contains_key(path)
                    || dependents.iter().any(|path| !self.files.contains_key(path))
            })
        {
            return Err(CoreError::InvalidInput(
                "baseline import graph has invalid file references".to_owned(),
            ));
        }
        Ok(())
    }

    pub(super) fn dependents(&self, changed: &BTreeSet<String>) -> ImportGraphResult {
        let mut dependents = BTreeSet::new();
        let mut queue: VecDeque<_> = changed.iter().cloned().collect();
        while let Some(path) = queue.pop_front() {
            for dependent in self.reverse.get(&path).into_iter().flatten() {
                if dependents.insert(dependent.clone()) {
                    queue.push_back(dependent.clone());
                }
            }
        }
        ImportGraphResult {
            dependents,
            reasons: if self.complete {
                Vec::new()
            } else {
                vec!["baseline import graph is incomplete".to_owned()]
            },
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ImportGraphResult {
    pub dependents: BTreeSet<String>,
    pub reasons: Vec<String>,
}

fn normal_path(path: &Path) -> String {
    glob_path(&absolute_path(path))
}

struct Collector {
    names: Vec<ImportReference>,
}
impl<'a> StatementVisitor<'a> for Collector {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        match statement {
            Stmt::Import(value) => {
                self.names
                    .extend(value.names.iter().map(|alias| ImportReference {
                        module: alias.name.as_str().to_owned(),
                        level: 0,
                    }));
            }
            Stmt::ImportFrom(value) => {
                let module = value.module.as_ref().map_or("", |name| name.as_str());
                self.names.push(ImportReference {
                    module: module.to_owned(),
                    level: value.level,
                });
                self.names.extend(
                    value
                        .names
                        .iter()
                        .filter(|alias| alias.name.as_str() != "*")
                        .map(|alias| ImportReference {
                            module: if module.is_empty() {
                                alias.name.to_string()
                            } else {
                                format!("{module}.{}", alias.name)
                            },
                            level: value.level,
                        }),
                );
            }
            _ => {}
        }
        walk_stmt(self, statement);
    }
}

fn import_names(source: &str) -> Option<Vec<ImportReference>> {
    let Ok(parsed) = ruff_python_parser::parse_module(source) else {
        return None;
    };
    if !parsed.errors().is_empty() {
        return None;
    }
    let mut collector = Collector { names: Vec::new() };
    collector.visit_body(parsed.suite());
    Some(collector.names)
}

fn import_source(path: &Path) -> Result<ImportSource, String> {
    let source = read_python_source(path)
        .map_err(|error| format!("failed to read import source {}: {error}", path.display()))?;
    let imports = import_names(&source)
        .ok_or_else(|| format!("failed to parse import source {}", path.display()))?;
    Ok(ImportSource {
        content_digest: replay_fingerprint([&source]),
        imports,
    })
}

fn discover(root: &Path, files: &mut Vec<PathBuf>, reasons: &mut Vec<String>) {
    let entries = match std::fs::read_dir(root) {
        Ok(value) => value,
        Err(error) => {
            reasons.push(format!(
                "failed to inspect source directory {}: {error}",
                root.display()
            ));
            return;
        }
    };
    for entry in entries {
        let entry = match entry {
            Ok(value) => value,
            Err(error) => {
                reasons.push(format!(
                    "failed to inspect source entry in {}: {error}",
                    root.display()
                ));
                continue;
            }
        };
        let path = entry.path();
        let kind = match entry.file_type() {
            Ok(value) => value,
            Err(error) => {
                reasons.push(format!(
                    "failed to inspect source path {}: {error}",
                    path.display()
                ));
                continue;
            }
        };
        if kind.is_symlink() {
            continue;
        }
        if kind.is_dir() {
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or_default();
            if matches!(
                name,
                ".git"
                    | ".venv"
                    | "venv"
                    | "__pycache__"
                    | ".mypy_cache"
                    | ".ruff_cache"
                    | ".vfy"
                    | ".tox"
            ) {
                continue;
            }
            discover(&path, files, reasons);
        } else if kind.is_file() && path.extension().is_some_and(|ext| ext == "py") {
            files.push(path);
        }
    }
}

fn discover_graph(roots: &[PathBuf], reasons: &mut Vec<String>) -> Imports {
    let mut files = Vec::new();
    for root in roots {
        if root.is_dir() {
            discover(root, &mut files, reasons);
        } else if root.extension().is_some_and(|ext| ext == "py") {
            files.push(root.clone());
        } else {
            reasons.push(format!("source root does not exist: {}", root.display()));
        }
    }
    files.sort();
    files.dedup();
    files
        .into_iter()
        .filter_map(|path| match import_source(&path) {
            Ok(source) => Some((normal_path(&path), source)),
            Err(reason) => {
                reasons.push(reason);
                None
            }
        })
        .collect()
}

fn module_name(path: &Path, root: &Path) -> Option<String> {
    let relative = path.strip_prefix(root).ok()?;
    let mut parts: Vec<String> = relative
        .components()
        .filter_map(|part| part.as_os_str().to_str().map(str::to_owned))
        .collect();
    let last = parts.last_mut()?;
    if last == "__init__.py" {
        parts.pop();
    } else if Path::new(last).extension().is_some_and(|ext| ext == "py") {
        *last = last.trim_end_matches(".py").to_owned();
    }
    Some(parts.join("."))
}

fn resolve(reference: &ImportReference, importer: &str, path: &Path) -> Option<String> {
    if reference.level == 0 {
        return (!reference.module.is_empty()).then(|| reference.module.clone());
    }
    let mut parts: Vec<&str> = importer.split('.').collect();
    if path.file_name().is_none_or(|name| name != "__init__.py") {
        parts.pop();
    }
    let parents = usize::try_from(reference.level.saturating_sub(1)).ok()?;
    if parents >= parts.len() {
        return None;
    }
    parts.truncate(parts.len() - parents);
    if !reference.module.is_empty() {
        parts.extend(reference.module.split('.'));
    }
    Some(parts.join("."))
}

fn reverse_imports(graph: &Imports, roots: &[PathBuf]) -> BTreeMap<String, BTreeSet<String>> {
    let mut modules = BTreeMap::new();
    for importer in graph.keys() {
        for root in roots.iter().filter(|root| root.is_dir()) {
            if let Some(name) = module_name(Path::new(importer), root) {
                modules.insert(name, importer.clone());
            }
        }
    }
    let mut reverse: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (importer, source) in graph {
        for root in roots.iter().filter(|root| root.is_dir()) {
            let Some(importer_module) = module_name(Path::new(importer), root) else {
                continue;
            };
            for reference in &source.imports {
                if let Some(target) = resolve(reference, &importer_module, Path::new(importer))
                    .and_then(|name| modules.get(&name))
                {
                    reverse
                        .entry(target.clone())
                        .or_default()
                        .insert(importer.clone());
                }
            }
        }
    }
    reverse
}

fn read_graph(path: &Path) -> Graph {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Graph::default();
    };
    let Ok(Value::Object(root)) = serde_json::from_str::<Value>(&text) else {
        return Graph::default();
    };
    if root.get("version").and_then(Value::as_u64) != Some(GRAPH_VERSION) {
        return Graph::default();
    }
    let Some(source_identity) = root.get("source_identity").and_then(Value::as_str) else {
        return Graph::default();
    };
    let (Some(files), Some(reverse)) = (root.get("files"), root.get("reverse")) else {
        return Graph::default();
    };
    let (Ok(files), Ok(reverse)) = (
        serde_json::from_value::<Imports>(files.clone()),
        serde_json::from_value::<BTreeMap<String, BTreeSet<String>>>(reverse.clone()),
    ) else {
        return Graph::default();
    };
    if reverse.iter().any(|(target, dependents)| {
        !files.contains_key(target) || dependents.iter().any(|path| !files.contains_key(path))
    }) {
        return Graph::default();
    }
    Graph {
        files,
        reverse,
        source_identity: source_identity.to_owned(),
        complete: root.get("complete").and_then(Value::as_bool) == Some(true),
        valid: true,
    }
}

fn write_graph(path: &Path, graph: &Graph) -> Result<(), CoreError> {
    let value = json!({"version": GRAPH_VERSION, "source_identity": graph.source_identity, "complete": graph.complete, "files": graph.files, "reverse": graph.reverse});
    let bytes = serde_json::to_vec_pretty(&value).map_err(CoreError::Serialization)?;
    atomic_write(path, &bytes).map_err(|source| CoreError::ArtifactIo {
        path: path.to_path_buf(),
        operation: "write import graph",
        source,
    })
}

#[must_use]
pub fn static_dependents(
    changed: &BTreeSet<String>,
    roots: &[PathBuf],
    graph_path: &Path,
    source_identity: &str,
) -> ImportGraphResult {
    let mut reasons = Vec::new();
    let cached = read_graph(graph_path);
    let cached_complete = cached.valid
        && cached.complete
        && cached.source_identity == source_identity
        && !changed.is_empty();
    let mut reverse = cached.reverse;
    let mut graph = if cached_complete {
        cached.files
    } else {
        discover_graph(roots, &mut reasons)
    };
    if changed.is_empty() {
        reverse = reverse_imports(&graph, roots);
        let staged = Graph {
            files: graph.clone(),
            reverse: reverse.clone(),
            source_identity: source_identity.to_owned(),
            complete: reasons.is_empty(),
            valid: true,
        };
        if let Err(error) = write_graph(graph_path, &staged) {
            reasons.push(format!("failed to persist import graph: {error}"));
        }
    } else {
        let mut imports_changed = false;
        for path in changed
            .iter()
            .filter(|path| Path::new(path).extension().is_some_and(|ext| ext == "py"))
        {
            match import_source(Path::new(path)) {
                Ok(source) => {
                    imports_changed |= graph
                        .get(path)
                        .is_none_or(|old| old.imports != source.imports);
                    if graph
                        .get(path)
                        .is_none_or(|old| old.content_digest != source.content_digest)
                    {
                        graph.insert(path.clone(), source);
                    }
                }
                Err(reason) => reasons.push(reason),
            }
        }
        if imports_changed || !cached_complete {
            reverse = reverse_imports(&graph, roots);
        }
    }
    let mut dependents = BTreeSet::new();
    let mut queue: VecDeque<String> = changed.iter().cloned().collect();
    while let Some(path) = queue.pop_front() {
        for dependent in reverse.get(&path).into_iter().flatten() {
            if dependents.insert(dependent.clone()) {
                queue.push_back(dependent.clone());
            }
        }
    }
    ImportGraphResult {
        dependents,
        reasons,
    }
}

#[must_use]
pub fn prepare_import_graph(
    history_path: &Path,
    source_roots: &[PathBuf],
    source_identity: &str,
) -> Vec<String> {
    let mut roots: Vec<PathBuf> = source_roots
        .iter()
        .map(|root| absolute_path(root))
        .collect();
    let current = absolute_path(Path::new("."));
    if !roots.contains(&current) {
        roots.push(current);
    }
    let cache = history_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("import-graph.json");
    static_dependents(&BTreeSet::new(), &roots, &cache, source_identity).reasons
}

/// Validate a staged import graph and replace the graph beside the test history.
///
/// # Errors
/// Returns an error when the staged graph is invalid or the target cannot be replaced.
pub fn publish_import_graph(staged_path: &Path, history_path: &Path) -> Result<(), CoreError> {
    let graph = read_graph(staged_path);
    if !graph.valid {
        return Err(CoreError::InvalidInput(
            "staged import graph is invalid".to_owned(),
        ));
    }
    let target = history_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("import-graph.json");
    write_graph(&target, &graph)
}
