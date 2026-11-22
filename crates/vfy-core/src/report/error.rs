//! Report errors and their conversion at the interpreter boundary.

use std::fmt;

/// Report failures translated by the interpreter boundary.
#[derive(Debug)]
pub enum ReportError {
    /// A report input failed validation.
    InvalidInput(String),
    /// A path or context pattern is malformed.
    InvalidPattern(crate::path::PathPatternError),
    /// A report file could not be written.
    Output(String),
    /// A report record could not be serialized.
    Serialization(serde_json::Error),
}

impl fmt::Display for ReportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidInput(message) | Self::Output(message) => formatter.write_str(message),
            Self::Serialization(error) => error.fmt(formatter),
            Self::InvalidPattern(error) => error.fmt(formatter),
        }
    }
}
impl std::error::Error for ReportError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Serialization(error) => Some(error),
            _ => None,
        }
    }
}
