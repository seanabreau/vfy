//! Collection node and fixture owner identities for one compiled suite.

/// A collection node's slot in the suite's native arena.
///
/// The index is valid only in the arena that issued it. Python-facing
/// operations resolve the Python object against that arena before use.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct CollectionNodeId(usize);

impl CollectionNodeId {
    /// Create the identity for an arena slot.
    #[must_use]
    pub const fn from_index(index: usize) -> Self {
        Self(index)
    }

    /// Return the arena slot.
    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// The owner of a fixture scope, as defined by the frontend that compiled the suite.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum FixtureOwner {
    /// The pytest frontend identifies the owner by path.
    Path(String),
    /// The independent frontend identifies the owner by collection node.
    Collection(CollectionNodeId),
}
