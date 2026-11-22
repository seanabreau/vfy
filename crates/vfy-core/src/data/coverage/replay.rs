//! Replay of retained coverage across diff hunks.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use super::CoverageData;
use crate::evidence::{SparseArcs, SparseLines};
use crate::path::{absolute_path, glob_path};
fn pytest_test(context: &str) -> Option<&str> {
    let (test, phase) = context.rsplit_once('|')?;
    matches!(phase, "setup" | "run" | "teardown").then_some(test)
}

/// One diff hunk's effect on baseline line numbers.
#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct LineShift {
    /// First removed baseline line, or the line after which lines were inserted.
    pub old_start: i32,
    pub old_count: i32,
    pub new_count: i32,
}

impl LineShift {
    /// Map a baseline line to the current source, or `None` for a removed line.
    ///
    /// `shifts` must be ordered and non-overlapping, as in a unified diff.
    #[must_use]
    pub fn map_line(shifts: &[Self], line: i32) -> Option<i32> {
        let mut delta = 0_i32;
        for shift in shifts {
            let last = if shift.old_count == 0 {
                shift.old_start
            } else {
                let last = shift.old_start.saturating_add(shift.old_count - 1);
                if (shift.old_start..=last).contains(&line) {
                    return None;
                }
                last
            };
            if line > last {
                delta = delta.saturating_add(shift.new_count - shift.old_count);
            }
        }
        Some(line.saturating_add(delta))
    }

    fn ordered(shifts: &[Self]) -> bool {
        shifts
            .iter()
            .all(|shift| shift.old_start >= 0 && shift.old_count >= 0 && shift.new_count >= 0)
            && shifts.windows(2).all(|pair| {
                pair[0].old_start.saturating_add(pair[0].old_count.max(1)) <= pair[1].old_start
            })
    }

    /// Map an arc endpoint, keeping the sign of entry and exit endpoints.
    fn map_endpoint(shifts: &[Self], endpoint: i32) -> Option<i32> {
        if endpoint < 0 {
            Self::map_line(shifts, endpoint.saturating_neg()).map(i32::saturating_neg)
        } else {
            Self::map_line(shifts, endpoint)
        }
    }
}

fn replay_path(path: &str) -> String {
    glob_path(&absolute_path(Path::new(path)))
}

impl CoverageData {
    /// Merge fresh evidence after removing invalidated tests and changed source paths.
    ///
    /// Taking ownership avoids cloning the complete baseline before replay.
    ///
    /// A changed path listed in `scoped_paths` keeps contexts that were not
    /// invalidated, with their lines and arc endpoints moved by the diff hunks.
    /// The caller must establish that these edits are confined to function
    /// bodies which no retained context executed. This includes global evidence
    /// from unchanged helpers called only while collecting a skipped module.
    ///
    /// # Errors
    ///
    /// Returns an error when fresh evidence conflicts with retained coverage data.
    #[expect(
        clippy::too_many_lines,
        reason = "replay removal and retention apply one path policy to each table"
    )]
    pub fn replay_merge(
        self,
        fresh: Self,
        invalidated_tests: Vec<String>,
        changed_paths: &[String],
        scoped_paths: &BTreeMap<String, Vec<LineShift>>,
    ) -> Result<Self, &'static str> {
        if self.has_arcs != fresh.has_arcs {
            return Err("cannot replay line and branch coverage together");
        }
        if !scoped_paths
            .values()
            .all(|shifts| LineShift::ordered(shifts))
        {
            return Err("replay line shifts must be ordered, non-overlapping hunks");
        }
        let invalidated: BTreeSet<String> = invalidated_tests.into_iter().collect();
        let changed: BTreeSet<String> =
            changed_paths.iter().map(|path| replay_path(path)).collect();
        let normalized: BTreeMap<String, String> = self
            .files
            .iter()
            .map(String::as_str)
            .chain(self.tracers.keys().map(String::as_str))
            .chain(self.lines.keys().map(String::as_str))
            .chain(self.arcs.keys().map(String::as_str))
            .chain(
                self.semantic
                    .definitions
                    .values()
                    .map(|definition| definition.path.as_str()),
            )
            .chain(
                self.semantic
                    .path_definitions
                    .values()
                    .map(|definition| definition.path.as_str()),
            )
            .collect::<BTreeSet<_>>()
            .into_iter()
            .map(|path| (path.to_owned(), replay_path(path)))
            .collect();
        let scoped: BTreeMap<String, &[LineShift]> = scoped_paths
            .iter()
            .map(|(path, shifts)| (replay_path(path), shifts.as_slice()))
            .collect();
        let path_changed = |path: &str| {
            normalized
                .get(path)
                .is_some_and(|path| changed.contains(path))
        };
        let path_shifts = |path: &str| {
            normalized
                .get(path)
                .filter(|path| changed.contains(*path))
                .and_then(|path| scoped.get(path).copied())
        };
        let path_removed = |path: &str| path_changed(path) && path_shifts(path).is_none();
        let should_remove = |context: &str, path: &str| {
            let test = pytest_test(context);
            path_removed(path) || test.is_some_and(|test| invalidated.contains(test))
        };

        let mut replayed = self;
        // Declared contexts of invalidated tests return only through fresh evidence.
        replayed
            .contexts
            .retain(|context| !pytest_test(context).is_some_and(|test| invalidated.contains(test)));
        replayed.files.retain(|path| !path_removed(path));
        replayed
            .file_order
            .retain(|path| replayed.files.contains(path));
        replayed.tracers.retain(|path, _| !path_removed(path));
        replayed.lines.retain(|path, contexts| {
            contexts.retain(|context, _| !should_remove(context, path));
            if let Some(shifts) = path_shifts(path) {
                for lines in contexts.values_mut() {
                    let mut moved = SparseLines::default();
                    moved.extend(
                        lines
                            .sorted()
                            .into_iter()
                            .filter_map(|line| LineShift::map_line(shifts, line)),
                    );
                    *lines = moved;
                }
            }
            !contexts.is_empty()
        });
        replayed.arcs.retain(|path, contexts| {
            contexts.retain(|context, _| !should_remove(context, path));
            if let Some(shifts) = path_shifts(path) {
                for arcs in contexts.values_mut() {
                    let mut moved = SparseArcs::default();
                    moved.extend(arcs.iter().filter_map(|(from, to)| {
                        Some((
                            LineShift::map_endpoint(shifts, from)?,
                            LineShift::map_endpoint(shifts, to)?,
                        ))
                    }));
                    *arcs = moved;
                }
            }
            !contexts.is_empty()
        });

        let removed_decisions: BTreeSet<String> = replayed
            .semantic
            .definitions
            .iter()
            .filter(|(_, definition)| path_changed(&definition.path))
            .map(|(key, _)| key.clone())
            .collect();
        replayed
            .semantic
            .definitions
            .retain(|key, _| !removed_decisions.contains(key));
        replayed.semantic.observations.retain(|observation| {
            !removed_decisions.contains(&observation.decision)
                && !pytest_test(&observation.context).is_some_and(|test| invalidated.contains(test))
        });
        let removed_paths: BTreeSet<String> = replayed
            .semantic
            .path_definitions
            .iter()
            .filter(|(_, definition)| path_changed(&definition.path))
            .map(|(key, _)| key.clone())
            .collect();
        replayed
            .semantic
            .path_definitions
            .retain(|key, _| !removed_paths.contains(key));
        replayed.semantic.path_observations.retain(|observation| {
            !removed_paths.contains(&observation.decision)
                && !pytest_test(&observation.context).is_some_and(|test| invalidated.contains(test))
        });

        replayed.merge_from(fresh, false)?;
        Ok(replayed)
    }
}
