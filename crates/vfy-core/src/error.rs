//! Input, filesystem, and serialization errors from engine operations.

use std::fmt;
use std::path::PathBuf;

/// An engine operation failed.
#[derive(Debug)]
pub enum CoreError {
    /// An input failed validation.
    InvalidInput(String),
    /// An artifact could not be read or committed.
    ArtifactIo {
        /// The file or directory being read or committed.
        path: PathBuf,
        /// The failed operation as a verb phrase, such as `read semantic source`.
        operation: &'static str,
        /// The underlying I/O error.
        source: std::io::Error,
    },
    /// A core record could not be serialized.
    Serialization(serde_json::Error),
}

impl fmt::Display for CoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidInput(message) => formatter.write_str(message),
            Self::ArtifactIo {
                path,
                operation,
                source,
            } => write!(
                formatter,
                "failed to {operation} {}: {source}",
                path.display()
            ),
            Self::Serialization(source) => {
                write!(formatter, "failed to serialize artifact: {source}")
            }
        }
    }
}

impl std::error::Error for CoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::ArtifactIo { source, .. } => Some(source),
            Self::Serialization(source) => Some(source),
            Self::InvalidInput(_) => None,
        }
    }
}
