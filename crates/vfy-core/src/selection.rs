//! Test duration history and replay input fingerprints.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

use serde::Deserialize;
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{CoreError, atomic_write};

pub mod ci;
mod evidence;
mod git;
mod graph;
mod plan;
mod results;
mod scope;
mod sqlite_history;

pub use sqlite_history::{TestHistoryLease, snapshot_test_history};

pub use evidence::{green_test_run_valid, write_green_test_run};
pub use git::{
    GitReplayState, read_git_replay_state, read_git_replay_state_at, read_green_git_head,
    read_green_git_head_at, read_submodule_change,
};
pub use graph::{ImportGraphResult, prepare_import_graph, publish_import_graph, static_dependents};
pub use plan::{
    ArcSelectionRecord, ChangedFile, FileSelectionEvidence, LineSelectionRecord, SelectionEvidence,
    SelectionInput, SelectionMode, SelectionPlan, SelectionRequest, TestObservation,
    build_selection_plan, build_selection_plan_from_data, parse_diff, plan_selection,
    rank_test_contexts, sort_tests, test_context,
};
pub use results::{
    FailedPhase, RecordedTestRun, TestAttempt, TestOutcome, TestReport, TestReportAccumulator,
    TestRun, normalize_test_phases, normalize_test_results, read_test_run,
    record_test_execution_artifact, record_test_run, record_test_run_artifact, write_test_phases,
    write_test_reports, write_test_run_result,
};
pub use scope::DiffHunk;

/// Recorded test durations used by test-selection policy.
#[derive(Clone, Default)]
pub struct TestHistory {
    /// Last observed duration in milliseconds, ordered by node id.
    pub durations: BTreeMap<String, f64>,
}

/// Read finite, non-negative test durations from the artifact's `tests` object.
///
/// Missing, unreadable, or malformed artifacts return empty history; unusable
/// duration entries are skipped.
#[must_use]
pub fn read_test_history(path: &Path) -> TestHistory {
    if sqlite_history::uses_sqlite(path) {
        return sqlite_history::read_durations(path).unwrap_or_default();
    }
    let Ok(file) = File::open(path) else {
        return TestHistory::default();
    };
    serde_json::from_reader::<_, HistoryDurations>(BufReader::new(file)).map_or_else(
        |_| TestHistory::default(),
        |history| TestHistory {
            durations: history.durations,
        },
    )
}

#[derive(Default)]
struct HistoryDurations {
    durations: BTreeMap<String, f64>,
    duration: Option<f64>,
}

impl<'de> Deserialize<'de> for HistoryDurations {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        DurationProjection::Root.deserialize(deserializer)
    }
}

#[derive(Clone, Copy)]
enum DurationProjection {
    Root,
    Tests,
    Entry,
    Discard,
}

impl DurationProjection {
    fn project_value(self, value: &Value) -> HistoryDurations {
        let mut result = HistoryDurations::default();
        let tests = match self {
            Self::Root => value.get("tests"),
            Self::Tests => Some(value),
            Self::Entry => {
                result.duration = value
                    .get("duration_ms")
                    .and_then(Value::as_f64)
                    .filter(|duration| duration.is_finite() && *duration >= 0.0);
                None
            }
            Self::Discard => None,
        };
        if let Some(tests) = tests.and_then(Value::as_object) {
            for (nodeid, entry) in tests {
                if let Some(duration) = entry.get("duration_ms").and_then(Value::as_f64)
                    && duration.is_finite()
                    && duration >= 0.0
                {
                    result.durations.insert(nodeid.clone(), duration);
                }
            }
        }
        result
    }
}

impl<'de> DeserializeSeed<'de> for DurationProjection {
    type Value = HistoryDurations;

    fn deserialize<D: serde::Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for DurationProjection {
    type Value = HistoryDurations;

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a JSON test history value")
    }

    fn visit_bool<E: de::Error>(self, _: bool) -> Result<Self::Value, E> {
        Ok(HistoryDurations::default())
    }

    fn visit_i64<E: de::Error>(self, _: i64) -> Result<Self::Value, E> {
        Ok(HistoryDurations::default())
    }

    fn visit_u64<E: de::Error>(self, _: u64) -> Result<Self::Value, E> {
        Ok(HistoryDurations::default())
    }

    fn visit_f64<E: de::Error>(self, _: f64) -> Result<Self::Value, E> {
        Ok(HistoryDurations::default())
    }

    fn visit_str<E: de::Error>(self, _: &str) -> Result<Self::Value, E> {
        Ok(HistoryDurations::default())
    }

    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(HistoryDurations::default())
    }

    fn visit_seq<S: SeqAccess<'de>>(self, mut sequence: S) -> Result<Self::Value, S::Error> {
        while sequence
            .next_element_seed(DurationProjection::Discard)?
            .is_some()
        {}
        Ok(HistoryDurations::default())
    }

    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
        let mut result = HistoryDurations::default();
        let Some(first_key) = map.next_key::<String>()? else {
            return Ok(result);
        };
        if first_key == "$serde_json::private::Number" {
            // Value interprets this first key as a Number when arbitrary_precision
            // is enabled, including inside otherwise discarded custom fields.
            // Replay its map visitor to keep that validation and feature behavior.
            let value = Value::deserialize(de::value::MapAccessDeserializer::new(FirstKeyMap {
                first_key: Some(first_key),
                map: &mut map,
            }))?;
            return Ok(self.project_value(&value));
        }
        let mut key = Some(first_key);
        while let Some(name) = key {
            match self {
                Self::Root if name == "tests" => {
                    result = map.next_value_seed(Self::Tests)?;
                }
                Self::Tests => {
                    let entry = map.next_value_seed(Self::Entry)?;
                    if let Some(duration) = entry.duration {
                        result.durations.insert(name, duration);
                    } else {
                        result.durations.remove(&name);
                    }
                }
                Self::Entry if name == "duration_ms" => {
                    result.duration = map
                        .next_value::<Value>()?
                        .as_f64()
                        .filter(|duration| duration.is_finite() && *duration >= 0.0);
                }
                _ => {
                    map.next_value_seed(Self::Discard)?;
                }
            }
            key = map.next_key()?;
        }
        Ok(result)
    }
}

struct FirstKeyMap<'a, M> {
    first_key: Option<String>,
    map: &'a mut M,
}

impl<'de, M: MapAccess<'de>> MapAccess<'de> for FirstKeyMap<'_, M> {
    type Error = M::Error;

    fn next_key_seed<K: DeserializeSeed<'de>>(
        &mut self,
        seed: K,
    ) -> Result<Option<K::Value>, Self::Error> {
        match self.first_key.take() {
            Some(key) => seed
                .deserialize(de::value::StringDeserializer::<M::Error>::new(key))
                .map(Some),
            None => self.map.next_key_seed(seed),
        }
    }

    fn next_value_seed<V: DeserializeSeed<'de>>(
        &mut self,
        seed: V,
    ) -> Result<V::Value, Self::Error> {
        self.map.next_value_seed(seed)
    }
}

/// Merge finite, non-negative durations into a selection artifact.
///
/// Skip invalid input durations, then atomically replace the destination.
/// A missing, unreadable, or malformed existing artifact starts empty history.
///
/// # Errors
/// Returns an error if the existing tests field is not an object, serialization
/// fails, or the artifact cannot be committed. The destination is unchanged on failure.
pub fn write_test_history<I, S>(path: &Path, records: I) -> Result<(), CoreError>
where
    I: IntoIterator<Item = (S, f64)>,
    S: Into<String>,
{
    let records: Vec<(String, f64)> = records
        .into_iter()
        .map(|(nodeid, duration)| (nodeid.into(), duration))
        .filter(|(_, duration)| duration.is_finite() && *duration >= 0.0)
        .collect();

    if sqlite_history::uses_sqlite(path) {
        return sqlite_history::write_durations(path, &records);
    }

    let mut root = match std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
    {
        Some(Value::Object(root)) => root,
        _ => serde_json::Map::new(),
    };
    root.remove("version");
    let tests = root
        .entry("tests".to_owned())
        .or_insert_with(|| Value::Object(serde_json::Map::new()));
    let Value::Object(tests) = tests else {
        return Err(CoreError::InvalidInput(
            "test history has an invalid tests object".to_owned(),
        ));
    };
    for (nodeid, duration) in records {
        tests.insert(nodeid, json!({"duration_ms": duration}));
    }

    let mut value = Value::Object(root);
    value.sort_all_objects();
    let payload = serde_json::to_vec_pretty(&value).map_err(CoreError::Serialization)?;
    atomic_write(path, &payload).map_err(|source| CoreError::ArtifactIo {
        path: path.to_path_buf(),
        operation: "write test history",
        source,
    })?;
    Ok(())
}

/// Hash length-prefixed replay inputs, distinguishing `['ab', 'c']` from `['a', 'bc']`.
#[must_use]
pub fn replay_fingerprint<I, S>(values: I) -> String
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut digest = Sha256::new();
    for value in values {
        let value = value.as_ref();
        digest.update(value.len().to_le_bytes());
        digest.update(value.as_bytes());
    }
    let mut output = String::with_capacity(64);
    for byte in digest.finalize() {
        let _ = write!(output, "{byte:02x}");
    }
    output
}

#[cfg(test)]
mod tests;
