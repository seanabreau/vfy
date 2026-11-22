mod direct_fnmatch;
mod discovery;
mod exclusions;
pub use discovery::{
    CollectionCandidate, CollectionEntryKind, CollectionVisitor, DirectoryBatch, DirectoryEntry,
    DirectoryLineage, DirectoryPolicy, DiscoveryError, initial_conftest_anchors,
};
pub use exclusions::{CollectionExclusions, CollectionIgnoreSettings, ConftestPathValue};

use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path, PathBuf};

use regex::Regex;

use super::CollectionFile;

const NORECURSEDIRS: &[&str] = &[
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
];

/// Positional target handling before live collector traversal.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum CollectionArgumentPolicy {
    #[default]
    Normalize,
    Keep,
}

/// Resolved configuration values before discovery validation.
#[derive(Debug)]
pub struct CollectionSettings {
    pub argument_policy: CollectionArgumentPolicy,
    pub rootdir: PathBuf,
    pub invocation_dir: PathBuf,
    pub testpaths: Vec<String>,
    pub python_files: Vec<String>,
    pub python_classes: Vec<String>,
    pub python_functions: Vec<String>,
    pub norecursedirs: Vec<String>,
}

impl CollectionSettings {
    pub(super) fn defaults(rootdir: PathBuf, invocation_dir: PathBuf) -> Self {
        Self {
            argument_policy: CollectionArgumentPolicy::Normalize,
            rootdir,
            invocation_dir,
            testpaths: Vec::new(),
            python_files: vec!["test_*.py".to_owned(), "*_test.py".to_owned()],
            python_classes: vec!["Test".to_owned()],
            python_functions: vec!["test".to_owned()],
            norecursedirs: NORECURSEDIRS
                .iter()
                .map(|value| (*value).to_owned())
                .collect(),
        }
    }
}

/// Validated pytest discovery, naming, and deselection policy.
///
/// Targets and ignore paths are relative to the invocation directory. Node IDs
/// are relative to the selected configuration root. Explicit targets bypass
/// ignore and naming checks on the target itself, as they do in pytest.
#[derive(Clone, Debug)]
pub struct CollectionSpecification {
    argument_policy: CollectionArgumentPolicy,
    root: PathBuf,
    targets: Vec<Target>,
    python_files: Vec<Pattern>,
    python_classes: Vec<Pattern>,
    python_functions: Vec<Pattern>,
    exclusions: CollectionExclusions,
    warnings: Vec<String>,
}

#[derive(Clone, Debug)]
struct Target {
    path: PathBuf,
    selector: String,
    parts: Vec<String>,
    parametrization: Option<String>,
    argument: String,
    original_index: usize,
}

/// A validated positional argument, retaining the core's parsed selector policy.
pub struct CollectionTarget<'a> {
    pub path: &'a Path,
    pub argument: &'a str,
    pub original_index: usize,
    pub selector: SelectorCursor<'a>,
}

/// Remaining name components of a positional selector.
#[derive(Clone, Copy)]
pub struct SelectorCursor<'a> {
    parts: &'a [String],
    parametrization: Option<&'a str>,
}

impl<'a> SelectorCursor<'a> {
    #[must_use]
    pub fn is_terminal(self) -> bool {
        self.parts.is_empty()
    }

    #[must_use]
    pub fn parts(self) -> &'a [String] {
        self.parts
    }

    #[must_use]
    pub fn parametrization(self) -> Option<&'a str> {
        self.parametrization
    }

    /// Match one immediate child's name, retaining parameter semantics.
    #[must_use]
    pub fn match_child(self, name: &str) -> Option<Self> {
        let (part, remaining) = self.parts.split_first()?;
        let matches = if remaining.is_empty() {
            self.parametrization.map_or_else(
                || name.split('[').next() == Some(part.as_str()),
                |parameter| {
                    name.strip_prefix(part)
                        .is_some_and(|rest| rest == parameter)
                },
            )
        } else {
            name == part
        };
        matches.then_some(Self {
            parts: remaining,
            parametrization: self.parametrization,
        })
    }
}

impl Target {
    fn matches_selector(&self, suffix: &str) -> bool {
        self.selector.is_empty()
            || suffix == self.selector
            || (self.parametrization.is_none()
                && suffix
                    .strip_prefix(&self.selector)
                    .is_some_and(|rest| rest.starts_with("::") || rest.starts_with('[')))
    }
}

impl CollectionSpecification {
    /// Iterate validated arguments without flattening their collector boundaries.
    pub fn targets(&self) -> impl Iterator<Item = CollectionTarget<'_>> {
        self.targets.iter().map(|target| CollectionTarget {
            path: &target.path,
            argument: &target.argument,
            original_index: target.original_index,
            selector: SelectorCursor {
                parts: &target.parts,
                parametrization: target.parametrization.as_deref(),
            },
        })
    }
    pub(super) fn defaults(root: &Path, targets: &[String]) -> Result<Self, String> {
        let root = absolute(root, Path::new(""))?;
        Self::from_resolved(
            CollectionSettings::defaults(root.clone(), root),
            targets,
            &[],
            &[],
        )
    }

    /// Build a policy from resolved frontend configuration values.
    ///
    /// # Errors
    /// Returns an error for invalid patterns, paths, or node selectors.
    pub fn from_resolved(
        settings: CollectionSettings,
        targets: &[String],
        ignore: &[String],
        ignore_glob: &[String],
    ) -> Result<Self, String> {
        let invocation_dir = absolute(&settings.invocation_dir, Path::new(""))?;
        let root = absolute(&invocation_dir, &settings.rootdir)?;
        if !invocation_dir.is_dir() {
            return Err(format!(
                "invocation directory does not exist: {}",
                invocation_dir.display()
            ));
        }
        if !root.is_dir() {
            return Err(format!(
                "test root directory does not exist: {}",
                root.display()
            ));
        }
        let (selected, missing_testpaths) =
            selected_paths(&root, &invocation_dir, targets, &settings.testpaths)?;
        let warnings = if missing_testpaths {
            vec!["No files were found in testpaths; consider removing or adjusting your testpaths configuration. Searching recursively from the current directory instead.".to_owned()]
        } else {
            Vec::new()
        };
        let targets = selected
            .iter()
            .enumerate()
            .map(|(original_index, target)| {
                if target.ends_with("::") {
                    return Err(format!("empty node selector: {target}"));
                }
                if target
                    .split_once('[')
                    .is_some_and(|(base, _)| !base.contains("::"))
                {
                    return Err(format!("path cannot contain [] parametrization: {target}"));
                }
                let (name, selector) = target.split_once("::").unwrap_or((target, ""));
                let path = absolute(&invocation_dir, Path::new(name))?;
                if !path.exists() {
                    return Err(format!("test target does not exist: {name}"));
                }
                if path.is_dir() && !selector.is_empty() {
                    return Err(format!("node selector requires a Python file: {target}"));
                }
                let (base, parametrization) = selector
                    .split_once('[')
                    .map_or((selector, None), |(base, parameter)| {
                        (base, Some(format!("[{parameter}")))
                    });
                let parts = if base.is_empty() {
                    Vec::new()
                } else {
                    base.split("::").map(str::to_owned).collect()
                };
                Ok(Target {
                    path,
                    selector: selector.to_owned(),
                    parts,
                    parametrization,
                    argument: target.clone(),
                    original_index,
                })
            })
            .collect::<Result<_, String>>()?;
        let targets = match settings.argument_policy {
            CollectionArgumentPolicy::Normalize => normalize_targets(targets),
            CollectionArgumentPolicy::Keep => targets,
        };
        Ok(Self {
            argument_policy: settings.argument_policy,
            root,
            targets,
            python_files: patterns(settings.python_files)?,
            python_classes: patterns(settings.python_classes)?,
            python_functions: patterns(settings.python_functions)?,
            exclusions: CollectionExclusions::from_resolved(CollectionIgnoreSettings {
                invocation_dir,
                ignore: ignore.to_vec(),
                ignore_glob: ignore_glob.to_vec(),
                norecursedirs: settings.norecursedirs,
                collect_in_virtualenv: false,
            })?,
            warnings,
        })
    }

    /// Discover files in positional target order and sorted directory order.
    ///
    /// Symlink spellings remain distinct collection identities. Directory
    /// cycles fail explicitly instead of silently omitting tests.
    ///
    /// # Errors
    /// Returns an error for unreadable directories or a recursive symlink cycle.
    pub fn discover(&self) -> Result<Vec<CollectionFile>, String> {
        struct DefaultVisitor;
        impl CollectionVisitor for DefaultVisitor {
            type Error = std::convert::Infallible;
            fn enter_directory(&mut self, _: &Path) -> Result<(), Self::Error> {
                Ok(())
            }
            fn ignore(&mut self, candidate: &CollectionCandidate<'_>) -> Result<bool, Self::Error> {
                Ok(candidate.default_ignored)
            }
            fn collect_file(&mut self, _: &CollectionFile) -> Result<(), Self::Error> {
                Ok(())
            }
        }
        self.walk(&mut DefaultVisitor)
            .map_err(|error| error.to_string())
    }

    /// Replace live naming settings without changing the already selected targets.
    ///
    /// # Errors
    /// Returns an invalid pattern without changing any naming setting.
    pub fn update_naming(
        &mut self,
        files: Vec<String>,
        classes: Vec<String>,
        functions: Vec<String>,
    ) -> Result<(), String> {
        let files = patterns(files)?;
        let classes = patterns(classes)?;
        let functions = patterns(functions)?;
        self.python_files = files;
        self.python_classes = classes;
        self.python_functions = functions;
        Ok(())
    }

    #[must_use]
    pub fn matches_class(&self, name: &str) -> bool {
        self.python_classes
            .iter()
            .any(|pattern| pattern.matches_name(name))
    }

    #[must_use]
    pub fn matches_function(&self, name: &str) -> bool {
        self.python_functions
            .iter()
            .any(|pattern| pattern.matches_name(name))
    }

    /// Return case indexes in pytest positional target order.
    ///
    /// Bare file and directory targets suppress explicit node selectors for
    /// the same files. Repeated node IDs are emitted only once.
    ///
    /// # Errors
    /// Returns an error when targets require live collector identities.
    pub fn order_nodeids(&self, nodeids: &[String]) -> Result<Vec<usize>, String> {
        if self.argument_policy == CollectionArgumentPolicy::Keep {
            return Err("keep-duplicates requires live collector identities".to_owned());
        }
        let mut selected = Vec::new();
        let mut seen = BTreeSet::new();
        for target in &self.targets {
            let target_id = self.nodeid(&target.path);
            for (index, nodeid) in nodeids.iter().enumerate() {
                if seen.contains(nodeid) {
                    continue;
                }
                let (file, suffix) = nodeid.split_once("::").unwrap_or((nodeid, ""));
                let file_matches = if target.path.is_dir() {
                    target_id.is_empty()
                        || file
                            .strip_prefix(&target_id)
                            .is_some_and(|rest| rest.starts_with('/'))
                } else {
                    file == target_id
                };
                if file_matches && target.matches_selector(suffix) {
                    seen.insert(nodeid);
                    selected.push(index);
                }
            }
        }
        Ok(selected)
    }

    /// Explicit node selectors that match no collected case, before deselection.
    #[must_use]
    pub fn unmatched_selectors<'a>(
        &self,
        nodeids: &(impl Iterator<Item = &'a str> + Clone),
    ) -> Vec<String> {
        self.targets
            .iter()
            .filter(|target| {
                if target.selector.is_empty() {
                    return false;
                }
                let file = self.nodeid(&target.path);
                !nodeids.clone().any(|nodeid| {
                    nodeid
                        .strip_prefix(&file)
                        .and_then(|rest| rest.strip_prefix("::"))
                        .is_some_and(|suffix| target.matches_selector(suffix))
                })
            })
            .map(|target| target.argument.clone())
            .collect()
    }

    /// Return a selected path identity relative to the configured collection root.
    #[must_use]
    pub fn nodeid(&self, path: &Path) -> String {
        let relative = path.strip_prefix(&self.root).ok().or_else(|| {
            if self.targets.iter().any(|target| target.path == path) {
                Some(Path::new(""))
            } else {
                path.ancestors()
                    .find(|parent| self.targets.iter().any(|target| target.path == *parent))
                    .and_then(|parent| path.strip_prefix(parent).ok())
            }
        });
        relative
            .unwrap_or(path)
            .to_string_lossy()
            .replace('\\', "/")
    }

    /// Configuration warnings to emit through the frontend warning policy.
    #[must_use]
    pub fn warnings(&self) -> &[String] {
        &self.warnings
    }
}

fn selected_paths(
    root: &Path,
    invocation_dir: &Path,
    arguments: &[String],
    testpaths: &[String],
) -> Result<(Vec<String>, bool), String> {
    let mut selected = arguments.to_vec();
    if selected.is_empty() && invocation_dir == root {
        for pattern in testpaths {
            selected.extend(expand_testpath(invocation_dir, pattern)?);
        }
    }
    let missing_testpaths = arguments.is_empty()
        && invocation_dir == root
        && !testpaths.is_empty()
        && selected.is_empty();
    if selected.is_empty() {
        selected.push(invocation_dir.to_string_lossy().into_owned());
    }
    Ok((selected, missing_testpaths))
}

/// Match live `python_files` values with the same policy used by resolved discovery.
///
/// Python fnmatch accepts malformed character ranges and brackets.
#[must_use]
pub fn matches_python_file(path: &Path, values: Vec<String>) -> bool {
    if path.extension().is_none_or(|extension| extension != "py") {
        return false;
    }
    let basename = path.file_name().map(|name| name.to_string_lossy());
    let full_path = path.to_string_lossy();
    values.into_iter().any(|pattern| {
        if pattern.contains('/') && !Path::new(&pattern).is_absolute() {
            direct_fnmatch::matches(&format!("*/{pattern}"), &full_path)
        } else if pattern.contains('/') {
            direct_fnmatch::matches(&pattern, &full_path)
        } else {
            basename
                .as_ref()
                .is_some_and(|name| direct_fnmatch::matches(&pattern, name))
        }
    })
}

/// Return whether a Python name matches `python_functions` or `python_classes` values.
///
/// # Errors
/// Returns an error for an invalid naming pattern.
pub fn matches_python_name(name: &str, values: Vec<String>) -> Result<bool, String> {
    Ok(patterns(values)?
        .iter()
        .any(|pattern| pattern.matches_name(name)))
}

fn normalize_targets(targets: Vec<Target>) -> Vec<Target> {
    // Sorting places each broader path or selector immediately before the
    // arguments it covers. Restore invocation order after dropping overlaps.
    let mut ordered: Vec<_> = targets.into_iter().enumerate().collect();
    ordered.sort_by(|(_, left), (_, right)| {
        (&left.path, &left.parts, &left.parametrization).cmp(&(
            &right.path,
            &right.parts,
            &right.parametrization,
        ))
    });
    let mut normalized: Vec<(usize, Target)> = Vec::new();
    for target in ordered {
        let covered = normalized.last().is_some_and(|(_, previous)| {
            if previous.path == target.1.path {
                target.1.parts.starts_with(&previous.parts)
                    && (previous.parametrization.is_none()
                        || previous.parametrization == target.1.parametrization)
            } else {
                previous.parts.is_empty() && target.1.path.starts_with(&previous.path)
            }
        });
        if !covered {
            normalized.push(target);
        }
    }
    normalized.sort_by_key(|(index, _)| *index);
    normalized.into_iter().map(|(_, target)| target).collect()
}

#[derive(Clone, Debug)]
struct Pattern {
    text: String,
    matcher: Regex,
    path_matcher: Option<Regex>,
}

impl Pattern {
    fn new(text: String) -> Result<Self, String> {
        let matcher = fnmatch(&text)?;
        let path_matcher = if text.contains('/') && !Path::new(&text).is_absolute() {
            Some(fnmatch(&format!("*/{text}"))?)
        } else {
            None
        };
        Ok(Self {
            text,
            matcher,
            path_matcher,
        })
    }

    fn matches_name(&self, name: &str) -> bool {
        name.starts_with(&self.text) || self.matcher.is_match(name)
    }

    fn matches_path(&self, path: &Path) -> bool {
        if let Some(matcher) = &self.path_matcher {
            matcher.is_match(&path.to_string_lossy())
        } else if self.text.contains('/') {
            self.matcher.is_match(&path.to_string_lossy())
        } else {
            path.file_name()
                .is_some_and(|name| self.matcher.is_match(&name.to_string_lossy()))
        }
    }
}

fn patterns(values: Vec<String>) -> Result<Vec<Pattern>, String> {
    values.into_iter().map(Pattern::new).collect()
}

fn fnmatch(pattern: &str) -> Result<Regex, String> {
    // Python fnmatch accepts unmatched brackets and descending character
    // ranges. Its stars cross separators, unlike filesystem glob syntax.
    let chars: Vec<_> = pattern.chars().collect();
    let mut expression = String::from("(?s)\\A");
    let mut index = 0;
    while let Some(&character) = chars.get(index) {
        index += 1;
        match character {
            '*' => expression.push_str(".*"),
            '?' => expression.push('.'),
            '[' => {
                let mut end = index;
                if chars.get(end) == Some(&'!') {
                    end += 1;
                }
                if chars.get(end) == Some(&']') {
                    end += 1;
                }
                while end < chars.len() && chars[end] != ']' {
                    end += 1;
                }
                if end == chars.len() {
                    expression.push_str("\\[");
                    continue;
                }
                let mut negated = chars.get(index) == Some(&'!');
                let mut cursor = index + usize::from(negated);
                let mut class = String::new();
                while cursor < end {
                    let first = chars[cursor];
                    if cursor + 2 < end && chars[cursor + 1] == '-' {
                        let last = chars[cursor + 2];
                        if first <= last {
                            class.push_str(&regex::escape(&first.to_string()));
                            class.push('-');
                            class.push_str(&regex::escape(&last.to_string()));
                        }
                        cursor += 3;
                    } else {
                        class.push_str(&regex::escape(&first.to_string()));
                        cursor += 1;
                    }
                }
                // Python applies negation after removing descending ranges,
                // so `[b-a!]` reduces to a negated empty class.
                if !negated && class.starts_with('!') {
                    negated = true;
                    class.remove(0);
                }
                if class.is_empty() {
                    expression.push_str(if negated { "." } else { "[a&&b]" });
                } else {
                    expression.push('[');
                    if negated {
                        expression.push('^');
                    }
                    expression.push_str(&class);
                    expression.push(']');
                }
                index = end + 1;
            }
            value => expression.push_str(&regex::escape(&value.to_string())),
        }
    }
    expression.push_str("\\z");
    Regex::new(&expression)
        .map_err(|error| format!("invalid collection pattern {pattern:?}: {error}"))
}

fn absolute(base: &Path, path: &Path) -> Result<PathBuf, String> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    };
    let path = if path.is_absolute() {
        path
    } else {
        std::env::current_dir()
            .map_err(|error| error.to_string())?
            .join(path)
    };
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            value => normalized.push(value.as_os_str()),
        }
    }
    Ok(normalized)
}

fn entries(directory: &Path) -> Result<Vec<fs::DirEntry>, String> {
    let mut entries = fs::read_dir(directory)
        .map_err(|error| format!("{}: {error}", directory.display()))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("{}: {error}", directory.display()))?;
    entries.sort_by_key(fs::DirEntry::file_name);
    Ok(entries)
}

fn expand_testpath(invocation_dir: &Path, pattern: &str) -> Result<Vec<String>, String> {
    let path = absolute(invocation_dir, Path::new(pattern))?;
    let components: Vec<_> = path
        .components()
        .map(|value| value.as_os_str().to_string_lossy().into_owned())
        .collect();
    let mut matches = Vec::new();
    expand_components(Path::new(""), &components, &mut Vec::new(), &mut matches)?;
    matches.sort();
    Ok(matches
        .into_iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect())
}

fn expand_components(
    base: &Path,
    components: &[String],
    ancestors: &mut Vec<PathBuf>,
    output: &mut Vec<PathBuf>,
) -> Result<(), String> {
    let Some((component, rest)) = components.split_first() else {
        if base.exists() {
            output.push(base.to_path_buf());
        }
        return Ok(());
    };
    if component == "**" {
        expand_components(base, rest, ancestors, output)?;
        if !base.is_dir() {
            return Ok(());
        }
        let identity = fs::canonicalize(base).map_err(|error| error.to_string())?;
        if ancestors.contains(&identity) {
            return Err(format!(
                "recursive symlink directory in testpaths: {}",
                base.display()
            ));
        }
        ancestors.push(identity);
        for entry in entries(base)? {
            if !entry.file_name().to_string_lossy().starts_with('.') {
                if entry.path().is_dir() {
                    expand_components(&entry.path(), components, ancestors, output)?;
                } else if rest.is_empty() {
                    output.push(entry.path());
                }
            }
        }
        ancestors.pop();
    } else if component.contains(['*', '?', '[']) {
        if !base.is_dir() {
            return Ok(());
        }
        let matcher = fnmatch(component)?;
        for entry in entries(base)? {
            let name = entry.file_name();
            if (!name.to_string_lossy().starts_with('.') || component.starts_with('.'))
                && matcher.is_match(&name.to_string_lossy())
            {
                expand_components(&entry.path(), rest, ancestors, output)?;
            }
        }
    } else {
        expand_components(&base.join(component), rest, ancestors, output)?;
    }
    Ok(())
}
