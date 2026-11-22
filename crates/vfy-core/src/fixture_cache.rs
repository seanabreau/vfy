//! Typed fixture cache identities.

use crate::FixtureOwner;

/// The scope that owns a cached fixture instance.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum FixtureScope {
    Function,
    Class,
    Module,
    Package,
    Session,
}

impl FixtureScope {
    /// Parse a pytest-compatible fixture scope.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "function" => Some(Self::Function),
            "class" => Some(Self::Class),
            "module" => Some(Self::Module),
            "package" => Some(Self::Package),
            "session" => Some(Self::Session),
            _ => None,
        }
    }

    /// Return the public spelling of this scope.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Function => "function",
            Self::Class => "class",
            Self::Module => "module",
            Self::Package => "package",
            Self::Session => "session",
        }
    }

    /// Return the nesting order used to validate fixture dependencies.
    #[must_use]
    pub const fn rank(self) -> u8 {
        match self {
            Self::Function => 0,
            Self::Class => 1,
            Self::Module => 2,
            Self::Package => 3,
            Self::Session => 4,
        }
    }
}

/// Stable fixture-definition identity within one compiled suite.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct FixtureId(usize);

impl FixtureId {
    /// Construct an identity from its index in the compiled fixture table.
    #[must_use]
    pub const fn from_index(index: usize) -> Self {
        Self(index)
    }

    /// Return the fixture table index.
    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// Generation distinguishes successive parameter instances of one fixture identity.
#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct FixtureGeneration(u64);

impl FixtureGeneration {
    /// Return the next generation, or `None` if the counter is exhausted.
    #[must_use]
    pub const fn checked_next(self) -> Option<Self> {
        match self.0.checked_add(1) {
            Some(value) => Some(Self(value)),
            None => None,
        }
    }
}

/// Scope and owner distinguish fixture instances that have the same definition.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct FixtureCacheIdentity {
    /// Definition index in the compiled suite.
    pub fixture: FixtureId,
    /// Collection node (independent frontend) or path (pytest frontend).
    pub owner: FixtureOwner,
    /// Effective cache scope, including any parametrization override.
    pub scope: FixtureScope,
}

/// Complete identity for one cached fixture instance.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct FixtureCacheKey {
    /// The logical fixture identity independent of its parameter generation.
    pub identity: FixtureCacheIdentity,
    /// Distinguishes successive instances of the same fixture identity.
    pub generation: FixtureGeneration,
}
