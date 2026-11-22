//! Schema-7 coverage data and packed line numbers.

use std::fmt;
use std::path::PathBuf;

mod coverage;
mod operations;

pub use coverage::{
    Arc, ContextArcs, ContextLines, CoverageData, DataValidationError, LineShift, ReportInput,
    validate_file_tracers,
};
pub use operations::{
    CombineError, CombineOptions, PathAliasMode, combine_transaction, publish_transaction,
};

/// Failure while writing, transporting, or remapping coverage data.
#[derive(Debug)]
pub enum DataError {
    /// A filesystem operation failed.
    Filesystem {
        /// The operation being performed.
        operation: &'static str,
        /// The underlying I/O failure.
        source: std::io::Error,
    },
    /// A `SQLite` operation failed.
    Database {
        /// The operation being performed.
        operation: &'static str,
        /// The underlying `SQLite` failure.
        source: rusqlite::Error,
    },
    /// An existing database could not be validated before an update.
    Validate {
        /// The database path.
        path: PathBuf,
        /// The underlying `SQLite` failure.
        source: rusqlite::Error,
    },
    /// Creating or encoding the in-memory transport database failed.
    Serialize(rusqlite::Error),
    /// Opening or decoding the in-memory transport database failed.
    Deserialize(rusqlite::Error),
    /// The decoded transport database did not contain valid coverage data.
    CorruptTransport(rusqlite::Error),
    /// The payload does not use vfy's transport marker.
    InvalidTransport,
    /// Valid data could not be represented or combined.
    InvalidData {
        /// The operation being performed.
        operation: &'static str,
        /// The stable compatibility message.
        message: String,
    },
}

impl DataError {
    pub(crate) fn database(operation: &'static str, source: rusqlite::Error) -> Self {
        Self::Database { operation, source }
    }

    pub(crate) fn filesystem(operation: &'static str, source: std::io::Error) -> Self {
        Self::Filesystem { operation, source }
    }

    pub(crate) fn invalid(operation: &'static str, message: impl Into<String>) -> Self {
        Self::InvalidData {
            operation,
            message: message.into(),
        }
    }

    /// Return the operation that failed.
    #[must_use]
    pub const fn operation(&self) -> &'static str {
        match self {
            Self::Filesystem { operation, .. }
            | Self::Database { operation, .. }
            | Self::InvalidData { operation, .. } => operation,
            Self::Validate { .. } => "validate coverage database",
            Self::Serialize(_) => "serialize coverage data",
            Self::Deserialize(_) => "deserialize coverage data",
            Self::CorruptTransport(_) => "validate serialized coverage data",
            Self::InvalidTransport => "identify serialized coverage data",
        }
    }
}

impl fmt::Display for DataError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Filesystem { source, .. } => source.fmt(formatter),
            Self::Database { source, .. } => source.fmt(formatter),
            Self::Validate { path, source } => {
                write!(formatter, "failed to validate {}: {source}", path.display())
            }
            Self::Serialize(source) => {
                write!(formatter, "failed to serialize coverage data: {source}")
            }
            Self::Deserialize(source) => {
                write!(
                    formatter,
                    "failed to read serialized coverage data: {source}"
                )
            }
            Self::CorruptTransport(source) => {
                write!(formatter, "serialized coverage data is corrupt: {source}")
            }
            Self::InvalidTransport => {
                formatter.write_str("serialized data was not produced by this vfy format")
            }
            Self::InvalidData { message, .. } => formatter.write_str(message),
        }
    }
}

impl std::error::Error for DataError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Filesystem { source, .. } => Some(source),
            Self::Database { source, .. }
            | Self::Validate { source, .. }
            | Self::Serialize(source)
            | Self::Deserialize(source)
            | Self::CorruptTransport(source) => Some(source),
            Self::InvalidTransport | Self::InvalidData { .. } => None,
        }
    }
}

/// Pack non-negative line numbers into Coverage.py's little-endian bitmap.
///
/// Ignore negative sentinel lines. This encodes bytes without writing to `SQLite`.
#[must_use]
pub fn encode_lines<I>(lines: I) -> Vec<u8>
where
    I: IntoIterator<Item = i32>,
{
    let values: Vec<i32> = lines.into_iter().filter(|line| *line >= 0).collect();
    let Some(maximum) = values.iter().copied().max() else {
        return Vec::new();
    };
    let Ok(maximum) = usize::try_from(maximum) else {
        return Vec::new();
    };
    let mut output = vec![0_u8; maximum / 8 + 1];
    for line in values {
        let Ok(line) = usize::try_from(line) else {
            continue;
        };
        output[line / 8] |= 1 << (line % 8);
    }
    output
}

#[cfg(test)]
mod tests;
