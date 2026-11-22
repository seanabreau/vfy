use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, Seek};
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value};

use super::{HistoryEntryField, serialize_history_fields, sort_history_objects};

pub(super) enum History {
    Compact(CompactHistory),
    Canonical(Map<String, Value>),
}

#[derive(Deserialize)]
pub(super) struct CompactHistory {
    pub(super) tests: BTreeMap<String, CompactEntry>,
    #[serde(flatten)]
    pub(super) custom: BTreeMap<String, Value>,
}

#[derive(Deserialize)]
pub(super) struct CompactEntry {
    #[serde(default, deserialize_with = "deserialize_present")]
    pub(super) duration_ms: Option<Number>,
    #[serde(default, deserialize_with = "deserialize_present")]
    pub(super) observations: Option<Box<[CompactObservation]>>,
    #[serde(flatten)]
    pub(super) custom: BTreeMap<String, Value>,
}

#[derive(Deserialize)]
pub(super) struct CompactObservation {
    attempt: Box<str>,
    duration_ms: Number,
    #[serde(default, deserialize_with = "deserialize_present")]
    failed_phase: Option<Box<str>>,
    outcome: Box<str>,
    run_id: Box<str>,
    #[serde(flatten)]
    custom: BTreeMap<String, Value>,
}

fn deserialize_present<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    // Explicit null stays on the canonical reader; absence may omit the field.
    T::deserialize(deserializer).map(Some)
}

impl Serialize for CompactEntry {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serialize_history_fields(
            serializer,
            self.custom.iter().map(|(key, value)| (key.as_str(), value)),
            [
                self.duration_ms
                    .as_ref()
                    .map(|duration| ("duration_ms", HistoryEntryField::Number(duration))),
                self.observations.as_deref().map(|observations| {
                    (
                        "observations",
                        HistoryEntryField::PreviousObservations(observations),
                    )
                }),
            ],
        )
    }
}

impl Serialize for CompactObservation {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serialize_history_fields(
            serializer,
            self.custom.iter().map(|(key, value)| (key.as_str(), value)),
            [
                Some(("attempt", HistoryEntryField::Text(&self.attempt))),
                Some(("duration_ms", HistoryEntryField::Number(&self.duration_ms))),
                self.failed_phase
                    .as_deref()
                    .map(|phase| ("failed_phase", HistoryEntryField::Text(phase))),
                Some(("outcome", HistoryEntryField::Text(&self.outcome))),
                Some(("run_id", HistoryEntryField::Text(&self.run_id))),
            ],
        )
    }
}

fn normalize(history: &mut CompactHistory) -> bool {
    // Value interprets a first private Number key specially when the workspace
    // enables arbitrary_precision. Preserve that behavior in the canonical reader.
    let reserved = "$serde_json::private::Number";
    if history.custom.contains_key(reserved) || history.tests.contains_key(reserved) {
        return false;
    }
    history.custom.values_mut().for_each(sort_history_objects);
    for entry in history.tests.values_mut() {
        if entry.custom.contains_key(reserved) {
            return false;
        }
        entry.custom.values_mut().for_each(sort_history_objects);
        for observation in entry.observations.as_deref_mut().into_iter().flatten() {
            if observation.custom.contains_key(reserved) {
                return false;
            }
            observation
                .custom
                .values_mut()
                .for_each(sort_history_objects);
        }
    }
    true
}

pub(super) fn read_history(path: &Path) -> History {
    let empty = || History::Canonical(Map::new());
    let Ok(file) = File::open(path) else {
        return empty();
    };
    let ordinary = file.metadata().is_ok_and(|metadata| metadata.is_file());
    let mut reader = BufReader::new(file);
    if ordinary {
        if let Ok(mut history) = serde_json::from_reader::<_, CompactHistory>(&mut reader)
            && normalize(&mut history)
        {
            return History::Compact(history);
        }
        // Keep the same inode if another process atomically replaces the path
        // while the speculative reader encounters an unsupported shape.
        if reader.rewind().is_err() {
            return empty();
        }
    }
    match serde_json::from_reader::<_, Value>(&mut reader) {
        Ok(Value::Object(mut fields)) => {
            fields.values_mut().for_each(sort_history_objects);
            History::Canonical(fields)
        }
        _ => empty(),
    }
}
