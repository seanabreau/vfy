use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use crate::{absolute_path, generated_path, glob_path};

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct GitReplayState {
    pub head: String,
    pub changed: Vec<String>,
    pub untracked: bool,
    pub deleted: bool,
    pub patch: String,
    pub submodule_change: Option<String>,
}

struct GitOutputs {
    root: PathBuf,
    head: Output,
    status: Output,
    patch: Output,
}

struct StatusChanges {
    changed: Vec<String>,
    untracked: bool,
    deleted: bool,
}

fn normal_path(path: &str) -> String {
    glob_path(&absolute_path(Path::new(path)))
}

fn normal_path_at(directory: &Path, path: &str) -> String {
    let path = Path::new(path);
    normal_path(
        &if path.is_absolute() {
            path.to_path_buf()
        } else {
            directory.join(path)
        }
        .to_string_lossy(),
    )
}

fn ignored_path(normalized: &str, ignored: &str) -> bool {
    normalized == ignored
        || normalized.starts_with(&format!("{ignored}/"))
        || ignored.ends_with('.') && normalized.starts_with(ignored)
}

fn output(command: &mut Command, operation: &str) -> Result<Output, String> {
    command
        .output()
        .map_err(|error| format!("failed to {operation}: {error}"))
}

/// Name the failed Git operation and keep Git's first error line; later
/// lines are usage hints that do not fit a one-line diagnostic.
fn git_failure(operation: &str, result: &Output) -> String {
    let stderr = String::from_utf8_lossy(&result.stderr);
    let reason = stderr.lines().map(str::trim).find(|line| !line.is_empty());
    match reason {
        Some(reason) => format!("failed to {operation}: {reason}"),
        None => format!("failed to {operation}: git exited with {}", result.status),
    }
}

pub(super) fn repository_root(directory: &Path) -> Result<PathBuf, String> {
    let result = output(
        Command::new("git")
            .current_dir(directory)
            .args(["rev-parse", "--show-toplevel"]),
        "read Git repository root",
    )?;
    if !result.status.success() {
        return Err(git_failure("read Git repository root", &result));
    }
    let path = String::from_utf8(result.stdout)
        .map_err(|error| format!("Git repository root is not UTF-8: {error}"))?;
    Ok(PathBuf::from(path.trim_end_matches('\n')))
}

fn git_head_output(directory: &Path) -> Result<Output, String> {
    output(
        Command::new("git")
            .current_dir(directory)
            .args(["rev-parse", "HEAD"]),
        "read Git HEAD",
    )
}

fn git_status_output(directory: &Path) -> Result<Output, String> {
    output(
        Command::new("git").current_dir(directory).args([
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ]),
        "read Git status",
    )
}

fn git_outputs_at(directory: &Path) -> Result<GitOutputs, String> {
    let root = repository_root(directory)?;
    let (head, status, patch) = std::thread::scope(|scope| {
        let head = scope.spawn(|| git_head_output(directory));
        let status = scope.spawn(|| git_status_output(directory));
        let patch = scope.spawn(|| {
            output(
                Command::new("git").current_dir(directory).args([
                    "diff",
                    "HEAD",
                    "--no-ext-diff",
                    "--no-relative",
                    "--unified=0",
                    // User configuration must not change the patch grammar.
                    "--no-color",
                    "--no-textconv",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                ]),
                "run git diff",
            )
        });
        (head.join(), status.join(), patch.join())
    });
    Ok(GitOutputs {
        root,
        head: head.map_err(|_| "Git HEAD worker panicked".to_owned())??,
        status: status.map_err(|_| "Git status worker panicked".to_owned())??,
        patch: patch.map_err(|_| "Git diff worker panicked".to_owned())??,
    })
}

/// Read the repository identity and working-tree changes used by replay policy.
///
/// # Errors
/// Returns an error when Git cannot be launched or any command fails.
pub fn read_git_replay_state(ignored: &[String]) -> Result<GitReplayState, String> {
    let directory = std::env::current_dir()
        .map_err(|error| format!("failed to read current directory: {error}"))?;
    read_git_replay_state_at(&directory, ignored)
}

/// Read replay state from the specified repository directory.
///
/// # Errors
/// Returns an error when Git cannot be launched or any command fails.
pub fn read_git_replay_state_at(
    directory: &Path,
    ignored: &[String],
) -> Result<GitReplayState, String> {
    let GitOutputs {
        root,
        head,
        status,
        patch,
    } = git_outputs_at(directory)?;
    replay_state_from_outputs(directory, &root, &head, &status, &patch, ignored)
}

/// Return the current commit only when relevant working-tree inputs are clean.
///
/// Ignore generated paths when checking for changes. This identity check
/// does not compute a revision diff.
///
/// # Errors
/// Returns an error when Git cannot be launched or either command fails.
pub fn read_green_git_head(ignored: &[String]) -> Result<Option<String>, String> {
    read_green_git_head_at(Path::new("."), ignored)
}

/// Return a clean-checkout commit identity from a specific repository.
///
/// # Errors
/// Returns an error when Git cannot be launched or either command fails.
pub fn read_green_git_head_at(
    directory: &Path,
    ignored: &[String],
) -> Result<Option<String>, String> {
    let root = repository_root(directory)?;
    let (head, status) = std::thread::scope(|scope| {
        let head = scope.spawn(|| git_head_output(directory));
        let status = scope.spawn(|| git_status_output(directory));
        (head.join(), status.join())
    });
    let head = head.map_err(|_| "Git HEAD worker panicked".to_owned())??;
    let status = status.map_err(|_| "Git status worker panicked".to_owned())??;
    for (operation, result) in [("read Git HEAD", &head), ("read Git status", &status)] {
        if !result.status.success() {
            return Err(git_failure(operation, result));
        }
    }
    if !status_changes(directory, &root, &status.stdout, ignored)
        .changed
        .is_empty()
        || submodule_change_at_root(&root)?.is_some()
    {
        return Ok(None);
    }
    Ok(Some(
        String::from_utf8_lossy(&head.stdout).trim().to_owned(),
    ))
}

fn replay_state_from_outputs(
    directory: &Path,
    root: &Path,
    head: &Output,
    status: &Output,
    patch: &Output,
    ignored: &[String],
) -> Result<GitReplayState, String> {
    for (operation, result) in [
        ("read Git HEAD", head),
        ("read Git status", status),
        ("read the Git diff", patch),
    ] {
        if !result.status.success() {
            return Err(git_failure(operation, result));
        }
    }
    let changes = status_changes(directory, root, &status.stdout, ignored);
    Ok(GitReplayState {
        head: String::from_utf8_lossy(&head.stdout).trim().to_owned(),
        changed: changes.changed,
        untracked: changes.untracked,
        deleted: changes.deleted,
        patch: String::from_utf8_lossy(&patch.stdout).into_owned(),
        submodule_change: submodule_change_at_root(root)?,
    })
}

fn status_changes(
    directory: &Path,
    root: &Path,
    status: &[u8],
    ignored: &[String],
) -> StatusChanges {
    let normalized_ignored: Vec<String> = ignored
        .iter()
        .map(|path| normal_path_at(directory, path))
        .collect();
    let mut changed = Vec::new();
    let mut untracked = false;
    let mut deleted = false;
    let entries: Vec<&[u8]> = status.split(|byte| *byte == 0).collect();
    let mut index = 0;
    while index < entries.len() {
        let entry = entries[index];
        if entry.len() < 4 {
            index += 1;
            continue;
        }
        let code = &entry[..2];
        let relative = String::from_utf8_lossy(&entry[3..]);
        let normalized = normal_path_at(root, &relative);
        let two_paths = code.contains(&b'R') || code.contains(&b'C');
        // Status paths are relative to the repository root. Judging the
        // absolute path would hide every change in a checkout that lives below
        // a directory such as `target` or `.cache`.
        if generated_path(Path::new(relative.as_ref()))
            || normalized_ignored
                .iter()
                .any(|ignored| ignored_path(&normalized, ignored))
        {
            index += if two_paths { 2 } else { 1 };
            continue;
        }
        untracked |= code == b"??";
        deleted |= code.contains(&b'D');
        changed.push(normalized);
        if two_paths {
            index += 1;
            if let Some(path) = entries.get(index).filter(|path| !path.is_empty()) {
                changed.push(normal_path_at(root, &String::from_utf8_lossy(path)));
            }
        }
        index += 1;
    }
    changed.sort();
    changed.dedup();
    StatusChanges {
        changed,
        untracked,
        deleted,
    }
}

/// What the Git index records for one tracked path.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum IndexEntry {
    /// File content: a regular file, executable, or symbolic link.
    Blob,
    /// A submodule, identified by the commit the superproject records for it.
    Submodule { commit: String },
}

fn read_submodule_index(root: &Path) -> Result<BTreeMap<String, IndexEntry>, String> {
    let result = output(
        Command::new("git")
            .current_dir(root)
            .args(["ls-files", "--stage", "-z"]),
        "read the Git index",
    )?;
    if !result.status.success() {
        return Err(git_failure("read the Git index", &result));
    }
    parse_index_records(
        result
            .stdout
            .split(|byte| *byte == 0)
            .filter(|record| record.starts_with(b"160000 ")),
    )
}

/// Parse `git ls-files --stage -z` records: `<mode> <object> <stage>\t<path>`.
///
/// An unmerged path has one record per stage and keeps its last record;
/// [`submodule_change`] reports a submodule merge conflict separately.
pub(crate) fn parse_index(output: &[u8]) -> Result<BTreeMap<String, IndexEntry>, String> {
    parse_index_records(
        output
            .split(|byte| *byte == 0)
            .filter(|record| !record.is_empty()),
    )
}

fn parse_index_records<'a>(
    records: impl Iterator<Item = &'a [u8]>,
) -> Result<BTreeMap<String, IndexEntry>, String> {
    let mut entries = BTreeMap::new();
    for record in records {
        let invalid = || {
            format!(
                "unexpected Git index record {:?}",
                String::from_utf8_lossy(record)
            )
        };
        let tab = record
            .iter()
            .position(|byte| *byte == b'\t')
            .ok_or_else(invalid)?;
        let mut fields = record[..tab].split(|byte| *byte == b' ');
        let (Some(mode), Some(object), Some(_stage), None) =
            (fields.next(), fields.next(), fields.next(), fields.next())
        else {
            return Err(invalid());
        };
        let path = String::from_utf8(record[tab + 1..].to_vec())
            .map_err(|error| format!("Git index path is not UTF-8: {error}"))?;
        // Mode 160000 is a gitlink: Git stores the submodule's commit, not content.
        let entry = if mode == b"160000" {
            if !matches!(object.len(), 40 | 64) || !object.iter().all(u8::is_ascii_hexdigit) {
                return Err(invalid());
            }
            IndexEntry::Submodule {
                commit: String::from_utf8_lossy(object).into_owned(),
            }
        } else {
            IndexEntry::Blob
        };
        entries.insert(path, entry);
    }
    Ok(entries)
}

/// Explain why submodules require a full run when Git can resolve the working tree.
///
/// # Errors
/// Returns an error if the resolved repository's submodule status is unavailable.
pub fn read_submodule_change(directory: &Path) -> Result<Option<String>, String> {
    let Ok(root) = repository_root(directory) else {
        return Ok(None);
    };
    submodule_change_at_root(&root)
}

fn submodule_change_at_root(root: &Path) -> Result<Option<String>, String> {
    submodule_change(root, &read_submodule_index(root)?)
}

/// Explain why the checked-out submodules cannot stand for their recorded commits.
///
/// Selection identifies a submodule by the commit its superproject records
/// and never reads the submodule's files. That identity holds only while the
/// submodule is initialized, clean, and checked out at that commit; otherwise
/// this returns the reason the full suite must run.
///
/// # Errors
/// Returns an error when Git cannot be launched or the status command fails.
pub(super) fn submodule_change(
    root: &Path,
    index: &BTreeMap<String, IndexEntry>,
) -> Result<Option<String>, String> {
    let submodules: Vec<&str> = index
        .iter()
        .filter(|(_, entry)| matches!(entry, IndexEntry::Submodule { .. }))
        .map(|(path, _)| path.as_str())
        .collect();
    if submodules.is_empty() {
        return Ok(None);
    }
    for path in &submodules {
        // Git status reports an empty, uninitialized submodule directory as unchanged.
        let directory = root.join(path);
        if !directory.join(".git").exists() {
            let state = if directory.is_dir() {
                "is not initialized"
            } else {
                "is missing"
            };
            return Ok(Some(format!("Git submodule {path} {state}")));
        }
    }
    let result = output(
        Command::new("git")
            .current_dir(root)
            .args([
                "status",
                "--porcelain=v2",
                "-z",
                // Configuration such as `submodule.<name>.ignore` can hide changes.
                "--ignore-submodules=none",
                "--untracked-files=normal",
                "--",
            ])
            .args(submodules.iter().map(|path| format!(":(literal){path}"))),
        "read Git submodule status",
    )?;
    if !result.status.success() {
        return Err(git_failure("read Git submodule status", &result));
    }
    Ok(submodule_status_change(&result.stdout))
}

/// Describe the first entry of `git status --porcelain=v2 -z` output limited
/// to submodule paths.
pub(crate) fn submodule_status_change(status: &[u8]) -> Option<String> {
    let mut entries = status.split(|byte| *byte == 0);
    while let Some(entry) = entries.next() {
        let text = String::from_utf8_lossy(entry);
        // Changed, renamed, and unmerged entries have 8, 9, and 10 fields before the path.
        let (count, conflict) = match text.as_bytes().first() {
            Some(b'1') => (9, false),
            Some(b'2') => {
                // A renamed entry is followed by its original path.
                entries.next();
                (10, false)
            }
            Some(b'u') => (11, true),
            _ => continue,
        };
        let fields: Vec<&str> = text.splitn(count, ' ').collect();
        let [_, code, submodule, .., path] = fields.as_slice() else {
            continue;
        };
        if fields.len() != count {
            continue;
        }
        // `S<c><m><u>`: commit changed, tracked changes, untracked changes.
        let state = match submodule.as_bytes() {
            _ if conflict => "has a merge conflict",
            [b'S', b'C', ..] => "is not checked out at its recorded commit",
            [b'S', _, b'M', _] | [b'S', _, _, b'U'] => "has uncommitted changes",
            _ if !code.starts_with('.') => "has a staged change",
            _ => "has changed",
        };
        return Some(format!("Git submodule {path} {state}"));
    }
    None
}
