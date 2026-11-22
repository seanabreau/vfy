//! Pytest-compatible allocation below an already acquired temporary root.

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyInt, PySlice, PyString};
use std::fs;
#[cfg(unix)]
use std::fs::DirBuilder;
use std::io;
#[cfg(unix)]
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

fn create_directory(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        DirBuilder::new().mode(0o700).create(path)
    }
    #[cfg(not(unix))]
    {
        fs::create_dir(path)
    }
}

fn filesystem_error(py: Python<'_>, error: io::Error, path: &Path) -> PyErr {
    let Some(code) = error.raw_os_error() else {
        return error.into();
    };
    match py
        .import("os")
        .and_then(|os| os.call_method1("strerror", (code,)))
        .and_then(|message| {
            let filename = path.as_os_str().into_pyobject(py)?;
            Ok((message.unbind(), filename.unbind()))
        }) {
        Ok((message, filename)) => PyOSError::new_err((code, message, filename)),
        Err(error) => error,
    }
}

fn update_current(current: &Path, path: &Path) {
    // Pytest treats its latest-directory link as best effort, including collisions.
    let _ = fs::remove_file(current);
    #[cfg(unix)]
    let _ = std::os::unix::fs::symlink(path, current);
    #[cfg(windows)]
    let _ = std::os::windows::fs::symlink_dir(path, current);
}

#[pyfunction]
pub(super) fn independent_temporary_path(
    py: Python<'_>,
    base: &Bound<'_, PyAny>,
    basename: &Bound<'_, PyAny>,
    numbered: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    // Preserve CPython path normalization, PathLike conversion and symlink resolution.
    let normalized = py
        .import("os.path")?
        .call_method1("normpath", (basename,))?;
    let candidate = base.call_method1("joinpath", (&normalized,))?;
    if !candidate
        .call_method0("resolve")?
        .getattr("parent")?
        .eq(base)?
    {
        return Err(PyValueError::new_err(format!(
            "{} is not a normalized and relative path",
            normalized.str()?
        )));
    }
    let root = base.extract::<PathBuf>()?;
    if !numbered.is_truthy()? {
        let path = candidate.extract::<PathBuf>()?;
        py.detach(|| create_directory(&path))
            .map_err(|error| filesystem_error(py, error, &path))?;
        return Ok(candidate.unbind());
    }

    let folded_prefix = normalized.call_method0("lower")?;
    let prefix_length = normalized.call_method0("__len__")?.extract::<isize>()?;
    for _ in 0..10 {
        let mut maximum = PyInt::new(py, -1).into_any();
        let entries = py
            .detach(|| fs::read_dir(&root))
            .map_err(|error| filesystem_error(py, error, &root))?;
        for entry in entries {
            let entry = entry.map_err(|error| filesystem_error(py, error, &root))?;
            let name = entry.file_name().into_pyobject(py)?;
            if !name
                .call_method0("lower")?
                .call_method1("startswith", (&folded_prefix,))?
                .is_truthy()?
            {
                continue;
            }
            let suffix = name.call_method1(
                "__getitem__",
                (PySlice::new(py, prefix_length, isize::MAX, 1),),
            )?;
            // int() includes Python's Unicode digits, signs, whitespace and digit limit.
            let number = match py.get_type::<PyInt>().call1((suffix,)) {
                Ok(number) => number,
                Err(error) if error.is_instance_of::<PyValueError>(py) => continue,
                Err(error) => return Err(error),
            };
            if number.gt(&maximum)? {
                maximum = number;
            }
        }
        let number = maximum.add(1)?;
        let filename = normalized.add(number.str()?)?;
        let result = base.call_method1("joinpath", (filename,))?;
        let path = result.extract::<PathBuf>()?;
        if py.detach(|| create_directory(&path)).is_ok() {
            let current = base
                .call_method1("joinpath", (normalized.add("current")?,))?
                .extract::<PathBuf>()?;
            py.detach(|| update_current(&current, &path));
            return Ok(result.unbind());
        }
    }
    Err(PyOSError::new_err(
        PyString::new(
            py,
            "could not create numbered dir with prefix {} in {} after 10 tries",
        )
        .call_method1("format", (normalized, base))?
        .unbind(),
    ))
}

/// How long a numbered directory's lock protects it from another session's cleanup.
const LOCK_TIMEOUT: Duration = Duration::from_hours(72);

/// pytest's default base temporary directory, `{temproot}/pytest-of-{user}/pytest-{N}`.
///
/// Numbered directories beyond the `keep` most recent are removed unless a
/// live session's lock protects them. Returns the directory and the lock that
/// marks it in use, which the session removes when it finishes.
#[pyfunction]
fn independent_default_basetemp(
    py: Python<'_>,
    keep: usize,
) -> PyResult<(PathBuf, Option<PathBuf>)> {
    let configured = py
        .import("os")?
        .getattr("environ")?
        .call_method1("get", ("PYTEST_DEBUG_TEMPROOT",))?;
    let temproot = if configured.is_truthy()? {
        configured.extract::<PathBuf>()?
    } else {
        py.import("tempfile")?
            .call_method0("gettempdir")?
            .extract::<PathBuf>()?
    };
    let temproot = fs::canonicalize(&temproot).unwrap_or(temproot);
    let user = match py.import("getpass")?.call_method0("getuser") {
        Ok(user) => user.extract::<String>()?,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyException>(py) => {
            "unknown".to_owned()
        }
        Err(error) => return Err(error),
    };
    let mut root = temproot.join(format!("pytest-of-{user}"));
    if create_directory(&root).is_err() && !root.is_dir() {
        root = temproot.join("pytest-of-unknown");
        if let Err(error) = create_directory(&root)
            && !root.is_dir()
        {
            return Err(filesystem_error(py, error, &root));
        }
    }
    restrict_owner(py, &root)?;
    let base = py.import("pathlib")?.getattr("Path")?.call1((&root,))?;
    let current = independent_temporary_path(
        py,
        &base,
        &PyString::new(py, "pytest-").into_any(),
        &pyo3::types::PyBool::new(py, true).to_owned().into_any(),
    )?
    .extract::<PathBuf>(py)?;
    let lock = if keep == 0 {
        None
    } else {
        let lock = current.join(".lock");
        #[expect(
            clippy::disallowed_methods,
            reason = "pytest's tmp_path lock is a plain file holding the owner's process ID"
        )]
        fs::write(&lock, std::process::id().to_string())
            .map_err(|error| filesystem_error(py, error, &lock))?;
        Some(lock)
    };
    let dead_before = fs::metadata(&current)
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| modified.checked_sub(LOCK_TIMEOUT));
    py.detach(|| remove_old_numbered(&root, "pytest-", keep, &current, dead_before));
    Ok((current, lock))
}

/// Reject a shared root another user owns, and remove group and other access.
fn restrict_owner(py: Python<'_>, root: &Path) -> PyResult<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let metadata = fs::metadata(root).map_err(|error| filesystem_error(py, error, root))?;
        let user = py.import("os")?.call_method0("getuid")?.extract::<u32>()?;
        if metadata.uid() != user {
            return Err(PyOSError::new_err(format!(
                "The temporary directory {} is not owned by the current user. Fix this and try again.",
                root.display()
            )));
        }
        let mode = metadata.mode();
        if mode & 0o077 != 0 {
            fs::set_permissions(root, fs::Permissions::from_mode(mode & !0o077))
                .map_err(|error| filesystem_error(py, error, root))?;
        }
    }
    #[cfg(not(unix))]
    let _ = (py, root);
    Ok(())
}

/// Remove numbered directories older than the `keep` most recent, as pytest's
/// exit cleanup does, skipping any that a live session has locked.
fn remove_old_numbered(
    root: &Path,
    prefix: &str,
    keep: usize,
    current: &Path,
    dead_before: Option<SystemTime>,
) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    let numbered = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            let number = name
                .get(..prefix.len())
                .filter(|start| start.eq_ignore_ascii_case(prefix))
                .and_then(|_| name[prefix.len()..].parse::<u64>().ok())?;
            Some((number, entry.path()))
        })
        .collect::<Vec<_>>();
    let Some(newest) = numbered.iter().map(|(number, _)| *number).max() else {
        return;
    };
    let Some(oldest_kept) = newest.checked_sub(keep as u64) else {
        return;
    };
    for (number, path) in numbered {
        // This session's directory stays until the session finishes.
        if number > oldest_kept || path == current || path.is_symlink() {
            continue;
        }
        let lock = path.join(".lock");
        if lock.is_file() {
            let locked_at = fs::metadata(&lock).and_then(|metadata| metadata.modified());
            match (locked_at, dead_before) {
                (Ok(locked_at), Some(dead_before)) if locked_at < dead_before => {}
                _ => continue,
            }
        }
        let _ = fs::remove_dir_all(&path);
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(independent_temporary_path, module)?)?;
    module.add_function(wrap_pyfunction!(independent_default_basetemp, module)?)
}
