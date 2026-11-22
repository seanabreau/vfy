use std::fs;
use std::path::{Component, Path, PathBuf};

/// Resolve a basetemp without allowing it to remove the invocation directory.
///
/// # Errors
/// Returns a diagnostic for an empty path or an ancestor of the invocation directory.
pub fn validate_basetemp(root: &Path, requested: &str) -> Result<PathBuf, String> {
    if requested.is_empty() {
        return Err(
            "basetemp must not be empty, the current working directory or any parent directory of it"
                .to_owned(),
        );
    }
    let root = crate::absolute_path(root);
    let candidate = crate::absolute_path(&root.join(requested));
    let mut normalized = PathBuf::new();
    for component in candidate.components() {
        match component {
            Component::ParentDir => {
                normalized.pop();
            }
            Component::CurDir => (),
            value => normalized.push(value.as_os_str()),
        }
    }
    if root.starts_with(&normalized) {
        return Err(
            "basetemp must not be empty, the current working directory or any parent directory of it"
                .to_owned(),
        );
    }
    Ok(normalized)
}

/// Prepare the already validated explicit temporary root once per execution.
///
/// # Errors
/// Returns the filesystem error without continuing to execute the suite.
pub fn prepare_basetemp(path: &Path) -> std::io::Result<()> {
    if path.exists() {
        fs::remove_dir_all(path)?;
    }
    fs::create_dir_all(path)
}
