//! Files and test contexts that define mutation evidence.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::data::CoverageData;
use crate::{absolute_path, glob_path};

const IGNORED: [&str; 5] = [".git", ".vfy", ".venv", "__pycache__", ".pytest_cache"];
const CONFIG_NAMES: [&str; 5] = [
    "pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "conftest.py",
];

/// Distinguish mutation locations from pytest paths and node IDs.
///
/// # Errors
/// Reject a location with an empty path or a nonpositive or invalid line number.
pub fn mutation_location(target: Option<&str>) -> Result<Option<(PathBuf, i32)>, String> {
    let Some(value) = target.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    if value.contains("::") {
        return Ok(None);
    }
    let Some((path, line)) = value.rsplit_once(':') else {
        return Ok(None);
    };
    // A drive prefix is part of a pytest path; a location has another colon.
    if path.len() == 1
        && path.as_bytes()[0].is_ascii_alphabetic()
        && (line.starts_with('\\') || line.starts_with('/'))
    {
        return Ok(None);
    }
    let line = line.parse::<i32>().ok().filter(|line| *line > 0);
    if let Some(line) = line.filter(|_| !path.is_empty()) {
        Ok(Some((absolute_path(Path::new(path)), line)))
    } else {
        Err(format!("invalid mutation location: {value}"))
    }
}

fn ignored(path: &Path) -> bool {
    path.components()
        .any(|part| IGNORED.iter().any(|name| part.as_os_str() == *name))
}

#[derive(Clone, Copy)]
enum SourceFiles {
    Python,
    PythonWithTestAssets,
    All,
}

fn test_root(path: &Path) -> bool {
    path.file_name()
        .is_some_and(|name| name == "test" || name == "tests")
}

fn walk(root: &Path, files: &mut BTreeSet<PathBuf>, scope: SourceFiles) {
    let scope = if matches!(scope, SourceFiles::PythonWithTestAssets) && test_root(root) {
        SourceFiles::All
    } else {
        scope
    };
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        // Judge only the part below the walked root: a checkout stored under a
        // directory named `.venv` or `__pycache__` is still the user's source.
        if entry
            .file_name()
            .to_str()
            .is_some_and(|name| IGNORED.contains(&name))
        {
            continue;
        }
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        if kind.is_dir() {
            walk(&path, files, scope);
        } else if path.is_file()
            && (matches!(scope, SourceFiles::All)
                || path.extension().is_some_and(|extension| extension == "py"))
        {
            files.insert(absolute_path(&path));
        }
    }
}

/// Find Python files under the supplied files and directories.
///
/// Skip `.git`, `.vfy`, `.venv`, `__pycache__`, and `.pytest_cache` paths,
/// unreadable directories, and entries that cannot be inspected. Unless
/// `include_tests` is true, also skip files named `test_*` and files beneath
/// `tests` directories.
#[must_use]
pub fn mutation_sources(roots: &[String], include_tests: bool) -> Vec<String> {
    let base = std::env::current_dir().ok();
    mutation_sources_below(roots, include_tests, base.as_deref())
}

/// Apply the test and generated-directory filters to paths below `base`, so
/// directories that contain the project do not exclude it.
fn mutation_sources_below(
    roots: &[String],
    include_tests: bool,
    base: Option<&Path>,
) -> Vec<String> {
    let mut files = BTreeSet::new();
    for root in roots {
        let root = absolute_path(Path::new(root));
        if root.is_file() && root.extension().is_some_and(|extension| extension == "py") {
            files.insert(root);
        } else if root.is_dir() {
            walk(&root, &mut files, SourceFiles::Python);
        }
    }
    files
        .into_iter()
        .filter(|path| {
            let relative = base
                .and_then(|base| path.strip_prefix(base).ok())
                .unwrap_or(path);
            !ignored(relative)
                && (include_tests
                    || (!path
                        .file_name()
                        .is_some_and(|name| name.to_string_lossy().starts_with("test_"))
                        && !relative
                            .components()
                            .any(|part| part.as_os_str() == "tests")))
        })
        .map(|path| path.to_string_lossy().into_owned())
        .collect()
}

/// Source scope requested by the mutation command.
#[derive(Clone, Copy)]
pub enum MutationScope<'a> {
    All,
    WorkingTree,
    GitBase(&'a str),
}

/// Git identities and current files used by an explicit base comparison.
#[derive(serde::Serialize, serde::Deserialize)]
pub struct MutationScopeEvidence {
    pub requested_base: String,
    pub base_commit: String,
    pub merge_base_commit: String,
    pub selected_files: Vec<String>,
}

pub struct MutationSources {
    pub paths: Vec<String>,
    pub fallback: bool,
    pub scope: Option<MutationScopeEvidence>,
}

fn git(root: &Path, arguments: &[&str]) -> Result<Vec<u8>, String> {
    let output = Command::new("git")
        .current_dir(root)
        .args(arguments)
        .output()
        .map_err(|error| format!("failed to discover changed Python files: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "failed to discover changed Python files: git {} failed: {}",
            arguments.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(output.stdout)
}

fn git_text(root: &Path, arguments: &[&str]) -> Result<String, String> {
    String::from_utf8(git(root, arguments)?)
        .map(|text| text.trim_end_matches(['\r', '\n']).to_owned())
        .map_err(|error| format!("Git returned a non-UTF-8 identity: {error}"))
}

fn changed_sources(directory: &Path, base: Option<&str>) -> Result<MutationSources, String> {
    let root = PathBuf::from(git_text(directory, &["rev-parse", "--show-toplevel"])?);
    let mut scope = if let Some(base) = base {
        let commit = git_text(
            &root,
            &[
                "rev-parse",
                "--verify",
                "--end-of-options",
                &format!("{base}^{{commit}}"),
            ],
        )
        .map_err(|error| {
            format!("cannot resolve mutation base {base:?}; fetch its history: {error}")
        })?;
        let merge_base =
            git_text(&root, &["merge-base", "--all", &commit, "HEAD"]).map_err(|error| {
                format!(
                    "cannot find mutation merge base; fetch complete base and HEAD history: {error}"
                )
            })?;
        if merge_base.is_empty() || merge_base.lines().count() != 1 {
            return Err("mutation scope requires one merge base; fetch complete history and use an unambiguous base".to_owned());
        }
        Some(MutationScopeEvidence {
            requested_base: base.to_owned(),
            base_commit: commit,
            merge_base_commit: merge_base,
            selected_files: Vec::new(),
        })
    } else {
        None
    };
    let revision = scope
        .as_ref()
        .map_or("HEAD", |scope| scope.merge_base_commit.as_str());
    // With rename detection disabled, a rename is a deletion plus its current destination.
    let diff = git(
        &root,
        &[
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=ACMT",
            revision,
            "--",
        ],
    )?;
    let untracked = git(&root, &["ls-files", "--others", "--exclude-standard", "-z"])?;
    let names = diff
        .split(|byte| *byte == 0)
        .chain(untracked.split(|byte| *byte == 0))
        .filter(|name| !name.is_empty())
        .map(|name| {
            let name = std::str::from_utf8(name)
                .map_err(|error| format!("changed Python path is not UTF-8: {error}"))?;
            Ok(root.join(name).to_string_lossy().into_owned())
        })
        .collect::<Result<Vec<_>, String>>()?;
    let paths = mutation_sources(&names, false);
    if let Some(scope) = &mut scope {
        scope.selected_files.clone_from(&paths);
    }
    Ok(MutationSources {
        paths,
        fallback: false,
        scope,
    })
}

/// Select current sources; only implicit working-tree scope permits a Git fallback.
///
/// # Errors
/// Returns invalid explicit base or Git errors before any evidence is written.
pub fn select_mutation_sources(
    roots: &[String],
    scope: MutationScope<'_>,
) -> Result<MutationSources, String> {
    match scope {
        MutationScope::All => Ok(MutationSources {
            paths: mutation_sources(roots, false),
            fallback: false,
            scope: None,
        }),
        MutationScope::GitBase(base) => changed_sources(Path::new("."), Some(base)),
        MutationScope::WorkingTree => match changed_sources(Path::new("."), None) {
            Ok(sources) => Ok(sources),
            Err(error) => {
                let paths = mutation_sources(roots, false);
                if paths.is_empty() {
                    Err(error)
                } else {
                    Ok(MutationSources {
                        paths,
                        fallback: true,
                        scope: None,
                    })
                }
            }
        },
    }
}

/// Digest an artifact, retaining an explicit identity for a missing file.
///
/// # Errors
/// Returns unreadable-file errors for the boundary's platform-specific exception formatting.
pub fn mutation_file_digest(path: &Path) -> std::io::Result<String> {
    crate::file_digest(path)
}

/// Snapshot all local files that can affect pytest semantics.
#[must_use]
pub fn mutation_suite_state(
    root: &Path,
    tests: &[String],
    arguments: &[String],
) -> Vec<(String, std::io::Result<String>)> {
    let root = absolute_path(root);
    let mut files = BTreeSet::new();
    let mut test_roots = BTreeSet::new();
    for node in tests {
        let name = node.split("::").next().unwrap_or_default();
        if name.is_empty() {
            continue;
        }
        let path = absolute_path(&root.join(name));
        files.insert(path.clone());
        for parent in path.ancestors().skip(1) {
            if test_root(parent) {
                test_roots.insert(parent.to_path_buf());
                break;
            }
            if parent == root {
                break;
            }
        }
    }
    for (index, argument) in arguments.iter().enumerate() {
        let candidate = if matches!(argument.as_str(), "-c" | "--config-file" | "--rootdir") {
            arguments.get(index + 1).map(String::as_str)
        } else {
            argument
                .strip_prefix("--config-file=")
                .or_else(|| argument.strip_prefix("--rootdir="))
        };
        if let Some(candidate) = candidate.filter(|value| !value.is_empty()) {
            let path = absolute_path(&root.join(candidate));
            if path.is_file() {
                files.insert(path);
            } else if path.is_dir() {
                test_roots.insert(path);
            }
        }
    }
    for parent in root.ancestors() {
        files.extend(CONFIG_NAMES.iter().map(|name| parent.join(name)));
    }
    for path in test_roots {
        walk(&path, &mut files, SourceFiles::All);
    }
    walk(&root, &mut files, SourceFiles::PythonWithTestAssets);
    files
        .into_iter()
        .map(|path| {
            let digest = mutation_file_digest(&path);
            (path.to_string_lossy().into_owned(), digest)
        })
        .collect()
}

fn test_name(context: &str) -> Option<&str> {
    let (test, phase) = context.rsplit_once('|')?;
    if test.is_empty() || !matches!(phase, "setup" | "run" | "teardown") {
        return None;
    }
    let mut offset = 0;
    for part in test.split('|') {
        if part.contains("::") || part.contains(".py") || part.contains(['/', '\\']) {
            return Some(&test[offset..]);
        }
        offset += part.len() + 1;
    }
    Some(test)
}

/// Return the absolute paths of files with any executed line or arc.
///
/// Evidence from every context counts, including collection and import time,
/// so a module that any test session imported is present even when no test
/// context reaches its definitions.
#[must_use]
pub fn mutation_executed_paths(data: &CoverageData) -> BTreeSet<String> {
    let lines = data
        .line_evidence()
        .filter(|(_, _, lines)| lines.iter().next().is_some())
        .map(|(path, _, _)| path);
    let arcs = data
        .arc_evidence()
        .filter(|(_, _, arcs)| arcs.iter().next().is_some())
        .map(|(path, _, _)| path);
    lines
        .chain(arcs)
        .map(|path| glob_path(&absolute_path(Path::new(path))))
        .collect()
}

/// Files with executed coverage evidence, matched by path or file identity.
///
/// Tests can import a module through a hard link whose path differs from the
/// mutated file; coverage then records the link's path. On Unix the device and
/// inode identify both paths as one file.
#[derive(Debug, Default)]
pub struct ExecutedFiles {
    paths: BTreeSet<String>,
    #[cfg(unix)]
    identities: BTreeSet<(u64, u64)>,
}

impl ExecutedFiles {
    /// Index executed paths, as returned by [`mutation_executed_paths`].
    #[must_use]
    pub fn new(paths: BTreeSet<String>) -> Self {
        #[cfg(unix)]
        let identities = paths
            .iter()
            .filter_map(|path| file_identity(Path::new(path)))
            .collect();
        Self {
            paths,
            #[cfg(unix)]
            identities,
        }
    }

    /// Whether `path` names a file with executed evidence.
    #[must_use]
    pub fn contains(&self, path: &str) -> bool {
        let path = glob_path(&absolute_path(Path::new(path)));
        if self.paths.contains(&path) {
            return true;
        }
        #[cfg(unix)]
        {
            file_identity(Path::new(&path))
                .is_some_and(|identity| self.identities.contains(&identity))
        }
        #[cfg(not(unix))]
        {
            false
        }
    }
}

#[cfg(unix)]
fn file_identity(path: &Path) -> Option<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;

    std::fs::metadata(path)
        .ok()
        .map(|metadata| (metadata.dev(), metadata.ino()))
}

/// Collect source lines executed outside dynamic test contexts, including arc endpoints.
#[must_use]
pub fn mutation_unattributed_lines(data: &CoverageData) -> BTreeMap<String, BTreeSet<i32>> {
    let mut result: BTreeMap<String, BTreeSet<i32>> = BTreeMap::new();
    for (path, context, lines) in data.line_evidence() {
        if context.is_empty() {
            result
                .entry(glob_path(&absolute_path(Path::new(path))))
                .or_default()
                .extend(lines.iter());
        }
    }
    for (path, context, arcs) in data.arc_evidence() {
        if context.is_empty() {
            result
                .entry(glob_path(&absolute_path(Path::new(path))))
                .or_default()
                .extend(
                    arcs.iter()
                        .flat_map(|(start, end)| [start, end])
                        .filter(|line| *line > 0),
                );
        }
    }
    #[cfg(unix)]
    {
        struct UnattributedFile {
            path: String,
            aliases: Vec<String>,
            lines: BTreeSet<i32>,
        }

        let mut files: BTreeMap<(u64, u64), UnattributedFile> = BTreeMap::new();
        for (path, lines) in result {
            let Some(identity) = file_identity(Path::new(&path)) else {
                // Unknown body evidence might belong to an alias of any source.
                return BTreeMap::new();
            };
            files
                .entry(identity)
                .and_modify(|file| {
                    file.aliases.push(path.clone());
                    file.lines.extend(lines.iter().copied());
                })
                .or_insert_with(|| UnattributedFile {
                    path,
                    aliases: Vec::new(),
                    lines,
                });
        }
        result = BTreeMap::new();
        for file in files.into_values() {
            for alias in file.aliases {
                result.insert(alias, file.lines.clone());
            }
            result.insert(file.path, file.lines);
        }
    }
    #[cfg(not(unix))]
    if result.keys().any(|path| !Path::new(path).is_file()) {
        return BTreeMap::new();
    }
    result
}

/// Group test-attributed execution locations and their complete test inventory.
#[must_use]
pub fn mutation_contexts(
    data: &CoverageData,
) -> (BTreeMap<String, BTreeSet<String>>, BTreeSet<String>) {
    // Keep borrowed identities and numeric locations until the public result
    // needs strings. Parameterized tests often share the same line evidence.
    let mut tests: BTreeMap<&str, usize> = BTreeMap::new();
    let mut locations: BTreeMap<&str, BTreeMap<i32, BTreeSet<usize>>> = BTreeMap::new();
    for (path, context, lines) in data.line_evidence() {
        let Some(test) = test_name(context) else {
            continue;
        };
        let next = tests.len();
        let index = *tests.entry(test).or_insert(next);
        let by_line = locations.entry(path).or_default();
        for line in lines.iter() {
            by_line.entry(line).or_default().insert(index);
        }
    }
    for (path, context, arcs) in data.arc_evidence() {
        let Some(test) = test_name(context) else {
            continue;
        };
        let next = tests.len();
        let index = *tests.entry(test).or_insert(next);
        let by_line = locations.entry(path).or_default();
        for line in arcs
            .iter()
            .flat_map(|(start, end)| [start, end])
            .filter(|line| *line > 0)
        {
            by_line.entry(line).or_default().insert(index);
        }
    }
    let mut names = vec![""; tests.len()];
    for (test, index) in &tests {
        names[*index] = test;
    }
    let mut result: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (path, lines) in locations {
        let path = glob_path(&absolute_path(Path::new(path)));
        for (line, indices) in lines {
            result
                .entry(format!("{path}:{line}"))
                .or_default()
                .extend(indices.into_iter().map(|index| names[index].to_owned()));
        }
    }
    (
        result,
        tests.keys().map(|test| (*test).to_owned()).collect(),
    )
}

#[cfg(test)]
mod tests;
