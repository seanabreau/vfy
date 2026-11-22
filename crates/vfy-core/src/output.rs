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

/// Replace a destination only after the complete content has been written.
///
/// # Errors
/// Returns the filesystem error without replacing the destination on failure.
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
    let mut temporary = NamedTempFile::new_in(parent).map_err(&io_error)?;
    write(temporary.as_file_mut())?;
    temporary
        .persist(path)
        .map_err(|error| io_error(error.error))?;
    Ok(())
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

pub(crate) fn prepare(source: &Path, parent: &Path) -> io::Result<NamedTempFile> {
    let mut input = std::fs::File::open(source)?;
    let mut temporary = tempfile::Builder::new()
        .suffix(".vfy-watch")
        .tempfile_in(parent)?;
    io::copy(&mut input, &mut temporary)?;
    Ok(temporary)
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
