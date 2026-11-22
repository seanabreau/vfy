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
    let GitOutputs {
        root,
        head,
        status,
        patch,
    } = git_outputs_at(&directory)?;
    replay_state_from_outputs(&directory, &root, &head, &status, &patch, ignored)
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
    Ok(status_changes(directory, &root, &status.stdout, ignored)
        .changed
        .is_empty()
        .then(|| String::from_utf8_lossy(&head.stdout).trim().to_owned()))
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
