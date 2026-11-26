//! Atomic file replacement, staged multi-file publication, file digests, and Python-compatible JSON.

use std::fmt::Write as _;
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};
use tempfile::NamedTempFile;

/// Bytes read per step while hashing, so memory does not grow with file size.
const DIGEST_CHUNK_BYTES: usize = 64 * 1024;

/// Return the file's SHA-256 hex digest, or `"missing"` if it does not exist.
///
/// The file is hashed in fixed-size chunks; its contents are never held whole.
///
/// # Errors
/// Returns a read error other than `NotFound`.
pub fn file_digest(path: &Path) -> io::Result<String> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok("missing".to_owned()),
        Err(error) => return Err(error),
    };
    let mut hasher = Sha256::new();
    let mut chunk = vec![0_u8; DIGEST_CHUNK_BYTES];
    loop {
        match file.read(&mut chunk) {
            Ok(0) => return Ok(hex(hasher.finalize().as_slice())),
            Ok(count) => hasher.update(&chunk[..count]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
}

#[must_use]
pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    hex(Sha256::digest(bytes).as_slice())
}

fn hex(digest: &[u8]) -> String {
    let mut output = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = write!(output, "{byte:02x}");
    }
    output
}

/// Write a destination as Python's `open(path, "w")` would, but atomically for files.
///
/// A symbolic link is followed and its final target is replaced, so the link
/// survives; a dangling link creates its target. An existing regular file keeps
/// its permission bits, and a new file gets `0o666` less the process umask. A
/// device, FIFO, or socket is written in place, without an atomicity guarantee.
/// The parent directory of `path` is created when missing.
///
/// # Errors
/// Returns the filesystem error. A regular-file destination is unchanged on failure.
pub fn atomic_write(path: &Path, content: &[u8]) -> io::Result<()> {
    atomic_write_with(path, |file| file.write_all(content), |error| error)
}

pub(crate) fn atomic_write_with<E>(
    path: &Path,
    write: impl FnOnce(&mut std::fs::File) -> Result<(), E>,
    io_error: impl Fn(io::Error) -> E,
) -> Result<(), E> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent).map_err(&io_error)?;
    match output_target(path).map_err(&io_error)? {
        OutputTarget::Stream => {
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .truncate(true)
                .open(path)
                .map_err(&io_error)?;
            write(&mut file)
        }
        OutputTarget::File { path, mode } => {
            check_writable(&path, mode).map_err(&io_error)?;
            let mut temporary = replacement_file(&path, mode, NEW_FILE_MODE).map_err(&io_error)?;
            write(temporary.as_file_mut())?;
            temporary
                .persist(&path)
                .map_err(|error| io_error(error.error))?;
            Ok(())
        }
    }
}

/// Mode Python's `open(path, "w")` requests for a new file, before the umask.
pub(crate) const NEW_FILE_MODE: u32 = 0o666;

/// Where a replacement for an output path is written.
#[derive(Debug, Eq, PartialEq)]
pub(crate) enum OutputTarget {
    /// Replace this regular file, or create it. `mode` holds the permission
    /// bits of an existing file.
    File { path: PathBuf, mode: Option<u32> },
    /// The destination is a device, FIFO, or socket; renaming over it would
    /// replace the node instead of writing to it.
    Stream,
}

/// Resolve the file an output path names, following symbolic links.
///
/// # Errors
/// Returns an error when a link cannot be read or forms a cycle.
pub(crate) fn output_target(path: &Path) -> io::Result<OutputTarget> {
    const MAX_LINKS: usize = 40;
    let mut current = path.to_path_buf();
    for depth in 0..=MAX_LINKS {
        if descriptor_path(&current) {
            return Ok(OutputTarget::Stream);
        }
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.is_symlink() => {
                if depth == MAX_LINKS {
                    break;
                }
                let target = std::fs::read_link(&current)?;
                current = match current.parent() {
                    Some(parent) if target.is_relative() => parent.join(target),
                    _ => target,
                };
            }
            Ok(metadata) if metadata.is_file() => {
                return Ok(OutputTarget::File {
                    path: crate::path::canonical_path(&current)?,
                    mode: Some(permission_bits(&metadata)),
                });
            }
            Ok(metadata) if metadata.is_dir() => {
                return Err(io::Error::new(
                    io::ErrorKind::IsADirectory,
                    "Is a directory",
                ));
            }
            Ok(_) => return Ok(OutputTarget::Stream),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return Ok(OutputTarget::File {
                    path: crate::absolute_path(&current),
                    mode: None,
                });
            }
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::other(format!(
        "too many levels of symbolic links: {}",
        path.display()
    )))
}

// A procfs descriptor can refer to a regular file. Renaming its pathname
// would leave the already-open descriptor pointing at the original bytes.
#[cfg(target_os = "linux")]
fn descriptor_path(path: &Path) -> bool {
    if !path
        .file_name()
        .is_some_and(|name| name.as_encoded_bytes().iter().all(u8::is_ascii_digit))
    {
        return false;
    }
    path.parent()
        .and_then(|parent| parent.canonicalize().ok())
        .is_some_and(|parent| parent.starts_with("/proc") && parent.ends_with("fd"))
}

#[cfg(not(target_os = "linux"))]
fn descriptor_path(_path: &Path) -> bool {
    false
}

fn check_writable(path: &Path, mode: Option<u32>) -> io::Result<()> {
    if mode.is_some() {
        // A rename would otherwise bypass the destination file's permissions.
        std::fs::OpenOptions::new().write(true).open(path)?;
    }
    Ok(())
}

#[cfg(unix)]
fn permission_bits(metadata: &std::fs::Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt as _;
    metadata.permissions().mode() & 0o7777
}

#[cfg(not(unix))]
fn permission_bits(_metadata: &std::fs::Metadata) -> u32 {
    NEW_FILE_MODE
}

/// Create a temporary file beside `destination` that can replace it by rename.
///
/// The file gets `mode` when the destination exists, and otherwise
/// `new_mode` less the process umask, which the kernel applies at creation.
///
/// # Errors
/// Returns an error when the file cannot be created or its mode set.
pub(crate) fn replacement_file(
    destination: &Path,
    mode: Option<u32>,
    new_mode: u32,
) -> io::Result<NamedTempFile> {
    let parent = destination
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    temporary_in(parent, mode, new_mode, "")
}

fn temporary_in(
    parent: &Path,
    mode: Option<u32>,
    new_mode: u32,
    suffix: &str,
) -> io::Result<NamedTempFile> {
    let mut builder = tempfile::Builder::new();
    builder.suffix(suffix);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        builder.permissions(std::fs::Permissions::from_mode(new_mode));
    }
    #[cfg(not(unix))]
    let _ = new_mode;
    let temporary = builder.tempfile_in(parent)?;
    #[cfg(unix)]
    if let Some(mode) = mode {
        use std::os::unix::fs::PermissionsExt as _;
        // Unlike creation, an explicit mode change is not reduced by the umask.
        temporary
            .as_file()
            .set_permissions(std::fs::Permissions::from_mode(mode))?;
    }
    #[cfg(not(unix))]
    let _ = mode;
    Ok(temporary)
}

/// Encode JSON with sorted object keys and Python-compatible ASCII escapes.
///
/// `pretty` selects two-space indentation; otherwise use spaces after commas
/// and colons.
///
/// # Errors
/// Returns an error if the value cannot be serialized.
pub fn python_json(value: &serde_json::Value, pretty: bool) -> Result<String, serde_json::Error> {
    encode_json(
        value,
        if pretty {
            JsonLayout::Pretty
        } else {
            JsonLayout::Inline
        },
    )
}

/// Encode compact JSON with Python-compatible ASCII escapes.
///
/// # Errors
/// Returns an error if the value cannot be serialized.
pub fn python_compact_json(value: &serde_json::Value) -> Result<String, serde_json::Error> {
    encode_json(value, JsonLayout::Compact)
}

#[derive(Clone, Copy)]
enum JsonLayout {
    Pretty,
    Inline,
    Compact,
}

fn encode_json(value: &serde_json::Value, layout: JsonLayout) -> Result<String, serde_json::Error> {
    let mut value = value.clone();
    value.sort_all_objects();
    let encoded = if matches!(layout, JsonLayout::Pretty) {
        serde_json::to_string_pretty(&value)?
    } else {
        serde_json::to_string(&value)?
    };
    let mut output = String::with_capacity(encoded.len());
    let mut in_string = false;
    let mut escaped = false;
    for character in encoded.chars() {
        if character == '"' && !escaped {
            in_string = !in_string;
        }
        if character.is_ascii() {
            output.push(character);
        } else {
            for unit in character.encode_utf16(&mut [0; 2]) {
                let _ = write!(output, "\\u{unit:04x}");
            }
        }
        if matches!(layout, JsonLayout::Inline) && !in_string && matches!(character, ':' | ',') {
            output.push(' ');
        }
        escaped = in_string && character == '\\' && !escaped;
    }
    Ok(output)
}

pub(crate) struct PreparedFile {
    pub(crate) destination: PathBuf,
    pub(crate) replacement: NamedTempFile,
    pub(crate) previous: Option<NamedTempFile>,
}

pub(crate) struct PublishedFile {
    pub(crate) destination: PathBuf,
    pub(crate) previous: Option<NamedTempFile>,
}

/// Copy `source`, with its permission bits, into a temporary file in `parent`.
pub(crate) fn prepare(source: &Path, parent: &Path) -> io::Result<NamedTempFile> {
    let mut input = std::fs::File::open(source)?;
    let mode = permission_bits(&input.metadata()?);
    let mut temporary = temporary_in(parent, Some(mode), NEW_FILE_MODE, ".vfy-watch")?;
    io::copy(&mut input, &mut temporary)?;
    Ok(temporary)
}

/// Resolve a target that can be published and rolled back with a rename.
pub(crate) fn publication_target(destination: &Path) -> io::Result<(PathBuf, Option<u32>)> {
    let OutputTarget::File { path, mode } = output_target(destination)? else {
        return Err(io::Error::other(format!(
            "files published together can only replace regular files: {}",
            destination.display()
        )));
    };
    Ok((path, mode))
}

/// Stage replacement bytes and the original file beside the resolved target.
pub(crate) fn prepare_publication(
    path: PathBuf,
    mode: Option<u32>,
    write: impl FnOnce(&mut File) -> io::Result<()>,
) -> io::Result<PreparedFile> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("output requires a parent directory"))?;
    check_writable(&path, mode)?;
    let previous = mode.map(|_| prepare(&path, parent)).transpose()?;
    // Watch ignores this suffix, so staging cannot make its own run obsolete.
    let mut temporary = temporary_in(parent, mode, NEW_FILE_MODE, ".vfy-watch")?;
    write(temporary.as_file_mut())?;
    Ok(PreparedFile {
        destination: path,
        replacement: temporary,
        previous,
    })
}

pub(crate) fn restore(files: Vec<PublishedFile>) -> io::Result<()> {
    let mut failures = Vec::new();
    for file in files.into_iter().rev() {
        let result = match file.previous {
            Some(previous) => match previous.persist(&file.destination) {
                Ok(_) => Ok(()),
                Err(error) => {
                    let failure = error.error;
                    match error.file.keep() {
                        Ok((_, backup)) => Err(io::Error::other(format!(
                            "{failure}; prior evidence retained at {}",
                            backup.display()
                        ))),
                        Err(backup) => Err(io::Error::other(format!(
                            "{failure}; failed to retain backup: {}",
                            backup.error
                        ))),
                    }
                }
            },
            None => match std::fs::remove_file(&file.destination) {
                Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
                result => result,
            },
        };
        if let Err(error) = result {
            failures.push(format!("{}: {error}", file.destination.display()));
        }
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(io::Error::other(format!(
            "failed to restore artifacts: {}",
            failures.join("; ")
        )))
    }
}

#[cfg(test)]
mod tests;
