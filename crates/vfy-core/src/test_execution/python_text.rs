//! Python text that retains surrogate code points until publication.

use serde::{Deserialize, Serialize};

/// Python text, including code points strict UTF-8 publication must reject.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(
    untagged,
    expecting = "data did not match any variant of untagged enum PythonText"
)]
pub enum PythonText {
    Unicode(String),
    NonScalar(NonScalarText),
}

impl PythonText {
    /// Validate Python Unicode once for semantic and already prepared names.
    ///
    /// # Errors
    /// Returns an error for a code point outside Python's Unicode range.
    pub fn from_codepoints(codepoints: Vec<u32>) -> Result<Self, &'static str> {
        if codepoints.iter().any(|code| *code > 0x10_ffff) {
            return Err("text contains an invalid Python code point");
        }
        if codepoints
            .iter()
            .any(|code| (0xd800..=0xdfff).contains(code))
        {
            return NonScalarText::new(codepoints).map(Self::NonScalar);
        }
        let text = codepoints
            .into_iter()
            .map(char::from_u32)
            .collect::<Option<String>>()
            .ok_or("text contains an invalid Unicode scalar")?;
        Ok(Self::Unicode(text))
    }
}

/// Valid Python Unicode code points containing at least one surrogate.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NonScalarText {
    codepoints: Vec<u32>,
}

impl NonScalarText {
    /// Validate a name that cannot be represented by a Rust string.
    ///
    /// # Errors
    /// Returns an error for invalid Python code points or a scalar-only name.
    pub fn new(codepoints: Vec<u32>) -> Result<Self, &'static str> {
        if codepoints.iter().any(|code| *code > 0x10_ffff)
            || !codepoints
                .iter()
                .any(|code| (0xd800..=0xdfff).contains(code))
        {
            return Err("non-scalar text requires valid Python code points and a surrogate");
        }
        Ok(Self { codepoints })
    }

    /// Return the validated Python Unicode code points in name order.
    #[must_use]
    pub fn codepoints(&self) -> &[u32] {
        &self.codepoints
    }
}

impl<'de> Deserialize<'de> for NonScalarText {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Representation {
            codepoints: Vec<u32>,
        }
        let value = Representation::deserialize(deserializer)?;
        Self::new(value.codepoints).map_err(serde::de::Error::custom)
    }
}

impl Default for PythonText {
    fn default() -> Self {
        Self::Unicode(String::new())
    }
}
impl From<String> for PythonText {
    fn from(value: String) -> Self {
        Self::Unicode(value)
    }
}
impl From<&str> for PythonText {
    fn from(value: &str) -> Self {
        Self::Unicode(value.to_owned())
    }
}
impl PythonText {
    /// Return scalar text without substituting non-scalar code points.
    #[must_use]
    pub fn as_scalar(&self) -> Option<&str> {
        match self {
            Self::Unicode(text) => Some(text),
            Self::NonScalar(_) => None,
        }
    }
    /// Return the exact Python Unicode code points in their original order.
    #[must_use]
    pub fn codepoints(&self) -> Vec<u32> {
        match self {
            Self::Unicode(text) => text.chars().map(u32::from).collect(),
            Self::NonScalar(text) => text.codepoints.clone(),
        }
    }
    /// Whether the text contains no code points.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        matches!(self, Self::Unicode(text) if text.is_empty())
    }
    /// Remove an exact scalar prefix without changing any remaining code point.
    #[must_use]
    pub fn strip_prefix(&self, prefix: &str) -> Option<Self> {
        let points = self.codepoints();
        let prefix: Vec<_> = prefix.chars().map(u32::from).collect();
        points
            .strip_prefix(prefix.as_slice())
            .map(|suffix| Self::from_valid_codepoints(suffix.to_vec()))
    }
    /// Concatenate validated text values.
    #[must_use]
    pub fn joined(parts: &[&Self]) -> Self {
        Self::from_valid_codepoints(parts.iter().flat_map(|part| part.codepoints()).collect())
    }
    // Every caller supplies whole validated text or a slice of validated text.
    fn from_valid_codepoints(points: Vec<u32>) -> Self {
        Self::from_codepoints(points).expect("canonical text slices retain validated code points")
    }
}

#[cfg(test)]
mod tests;
