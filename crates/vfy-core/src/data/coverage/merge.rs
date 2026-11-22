//! Merging, remapping, and tracer validation for coverage data.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};

use super::{CoverageData, Partitions};
use crate::evidence::{SparseArcs, SparseLines};
fn merge_partitions<T>(
    target: &mut Partitions<T>,
    incoming: Partitions<T>,
    union: impl Fn(&mut T, &T),
) {
    for (path, contexts) in incoming {
        match target.entry(path) {
            Entry::Vacant(entry) => {
                entry.insert(contexts);
            }
            Entry::Occupied(mut entry) => {
                for (context, evidence) in contexts {
                    match entry.get_mut().entry(context) {
                        Entry::Vacant(entry) => {
                            entry.insert(evidence);
                        }
                        Entry::Occupied(mut entry) => union(entry.get_mut(), &evidence),
                    }
                }
            }
        }
    }
}

/// Validate a tracer batch against existing names without changing either input.
///
/// # Errors
///
/// Returns an error when a file has two different tracer names, including within
/// the incoming batch.
pub fn validate_file_tracers<'a>(
    existing: &BTreeMap<String, String>,
    incoming: impl IntoIterator<Item = (&'a str, &'a str)>,
) -> Result<(), &'static str> {
    let mut staged = BTreeMap::new();
    for (path, tracer) in incoming {
        if existing
            .get(path)
            .map(String::as_str)
            .is_some_and(|current| current != tracer)
            || staged
                .insert(path, tracer)
                .is_some_and(|current| current != tracer)
        {
            return Err("cannot combine conflicting file tracers");
        }
    }
    Ok(())
}

enum PathSourceHashes<'a> {
    Single(&'a str),
    Mixed,
}

fn changed_semantic_paths<'a, 'b>(
    existing: impl Iterator<Item = (&'a str, &'a str)>,
    incoming: impl Iterator<Item = (&'b str, &'b str)>,
) -> BTreeSet<String> {
    let mut incoming = incoming.peekable();
    if incoming.peek().is_none() {
        return BTreeSet::new();
    }
    let mut hashes = BTreeMap::<&str, PathSourceHashes<'_>>::new();
    for (path, hash) in existing {
        hashes
            .entry(path)
            .and_modify(|current| {
                if let PathSourceHashes::Single(existing) = current
                    && *existing != hash
                {
                    *current = PathSourceHashes::Mixed;
                }
            })
            .or_insert(PathSourceHashes::Single(hash));
    }
    incoming
        .filter(|&(path, hash)| {
            hashes.get(path).is_some_and(|existing| match existing {
                PathSourceHashes::Single(existing) => *existing != hash,
                PathSourceHashes::Mixed => true,
            })
        })
        .map(|(path, _)| path.to_owned())
        .collect()
}

impl CoverageData {
    /// Whether merging `other` into this store would produce `other` unchanged.
    ///
    /// The store holds no evidence, and its metadata and collection mode agree
    /// with `other`, so the merge can share `other` instead of copying it.
    #[must_use]
    pub fn merge_yields(&self, other: &Self) -> bool {
        other.mode_is_known()
            && (!self.has_arcs || other.has_arcs)
            && self.files.is_empty()
            && self.file_order.is_empty()
            && self.contexts.is_empty()
            && self.tracers.is_empty()
            && self.lines.is_empty()
            && self.arcs.is_empty()
            && self.semantic.definitions.is_empty()
            && self.semantic.observations.is_empty()
            && self.semantic.path_definitions.is_empty()
            && self.semantic.path_observations.is_empty()
            && self
                .metadata
                .iter()
                .all(|(key, value)| other.metadata.get(key) == Some(value))
    }

    /// Merge another store, leaving `self` unchanged if validation fails.
    ///
    /// When `replace_changed` is true, replace semantic definitions for changed
    /// source paths; otherwise reject conflicting source revisions.
    ///
    /// # Errors
    ///
    /// Returns an error for incompatible collection modes, tracers, or source revisions.
    pub fn merge_from(&mut self, other: Self, replace_changed: bool) -> Result<(), &'static str> {
        if !other.mode_is_known()
            && other.semantic.definitions.is_empty()
            && other.semantic.path_definitions.is_empty()
        {
            return Ok(());
        }
        if self.mode_is_known() && self.has_arcs != other.has_arcs {
            return Err("cannot combine line and branch coverage data");
        }
        validate_file_tracers(
            &self.tracers,
            other
                .tracers
                .iter()
                .map(|(path, tracer)| (path.as_str(), tracer.as_str())),
        )?;
        let changed_paths = changed_semantic_paths(
            self.semantic.source_revisions(),
            other.semantic.source_revisions(),
        );
        if !replace_changed && !changed_paths.is_empty() {
            return Err("cannot combine semantic data from different source revisions");
        }
        self.semantic
            .validate_merge(&other.semantic, &changed_paths)?;

        self.has_arcs |= other.has_arcs;
        self.metadata.extend(other.metadata);
        for path in other.file_order {
            self.touch_file(&path);
        }
        self.contexts.extend(other.contexts);
        self.tracers.extend(other.tracers);
        merge_partitions(&mut self.lines, other.lines, SparseLines::extend_set);
        merge_partitions(&mut self.arcs, other.arcs, SparseArcs::extend_set);
        if replace_changed && !changed_paths.is_empty() {
            self.semantic
                .definitions
                .retain(|_, definition| !changed_paths.contains(&definition.path));
            self.semantic.observations.retain(|observation| {
                self.semantic
                    .definitions
                    .contains_key(&observation.decision)
            });
            self.semantic
                .path_definitions
                .retain(|_, definition| !changed_paths.contains(&definition.path));
            self.semantic.path_observations.retain(|observation| {
                self.semantic
                    .path_definitions
                    .contains_key(&observation.decision)
            });
        }
        self.semantic.merge_validated(other.semantic);
        Ok(())
    }

    /// Remap every measured file and its related evidence.
    ///
    /// # Errors
    ///
    /// Returns an error for incomplete mappings or collisions with incompatible evidence.
    pub fn remapped_files(self, mapped: &BTreeMap<String, String>) -> Result<Self, &'static str> {
        // Constructors and database reads require every evidence path to be measured.
        // The remapping below relies on that invariant for direct indexing.
        debug_assert!(self.references_measured_files());
        if self.files.iter().any(|path| !mapped.contains_key(path)) {
            return Err("file mapping does not cover every measured file");
        }
        let mut source_hashes = BTreeMap::new();
        for (path, source_hash) in self
            .semantic
            .definitions
            .values()
            .map(|definition| (&definition.path, &definition.source_hash))
            .chain(
                self.semantic
                    .path_definitions
                    .values()
                    .map(|definition| (&definition.path, &definition.source_hash)),
            )
        {
            let target = mapped
                .get(path)
                .ok_or("semantic data refers to an unmeasured file")?;
            if source_hashes
                .insert(target, source_hash)
                .is_some_and(|previous| previous != source_hash)
            {
                return Err("cannot combine semantic data from different source revisions");
            }
        }
        let mut remapped = Self::empty();
        remapped.has_arcs = self.has_arcs;
        remapped.metadata = self.metadata;
        remapped.contexts = self.contexts;
        for path in self.file_order {
            remapped.touch_file(&mapped[&path]);
        }
        for (path, tracer) in self.tracers {
            let target = mapped[&path].clone();
            if remapped
                .tracers
                .get(&target)
                .is_some_and(|existing| existing != &tracer)
            {
                return Err("cannot combine conflicting file tracers");
            }
            remapped.tracers.insert(target, tracer);
        }
        for (path, contexts) in self.lines {
            let target = remapped.lines.entry(mapped[&path].clone()).or_default();
            for (context, lines) in contexts {
                target.entry(context).or_default().extend_set(&lines);
            }
        }
        for (path, contexts) in self.arcs {
            let target = remapped.arcs.entry(mapped[&path].clone()).or_default();
            for (context, arcs) in contexts {
                target.entry(context).or_default().extend_set(&arcs);
            }
        }
        remapped.semantic = self
            .semantic
            .remap_paths(mapped)
            .map_err(|(_, invariant)| invariant)?;
        Ok(remapped)
    }
}
