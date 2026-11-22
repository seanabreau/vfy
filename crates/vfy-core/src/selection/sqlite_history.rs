//! Incremental bounded test history. Explicit JSON paths retain their JSON contract.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io;
use std::path::Path;
use std::time::Duration;

use rusqlite::{
    Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior, params,
};
use serde::Serialize;
use serde_json::Value;

use super::{TestAttempt, TestHistory, TestOutcome, TestRun};
use crate::CoreError;

#[cfg(test)]
mod tests;

const APPLICATION_ID: i64 = 0x7166_6873;
const VERSION: i64 = 1;
const MAX_OBSERVATIONS: i64 = 20;

// One transaction: the file is unpublished until `initialize_locked` syncs and
// renames it, so per-statement journal commits only cost fsyncs.
const SCHEMA: &str = "
BEGIN;
PRAGMA application_id = 1902536819;
PRAGMA user_version = 1;
CREATE TABLE tests (
    id INTEGER PRIMARY KEY,
    nodeid TEXT NOT NULL UNIQUE,
    duration_ms REAL CHECK(duration_ms >= 0),
    duration_negative_zero INTEGER NOT NULL DEFAULT 0 CHECK(duration_negative_zero IN (0,1)),
    observations_present INTEGER NOT NULL CHECK(observations_present IN (0,1))
);
CREATE TABLE runs (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE);
CREATE TABLE observations (
    test_id INTEGER NOT NULL REFERENCES tests(id),
    position INTEGER NOT NULL CHECK(position >= 0),
    run_id INTEGER REFERENCES runs(id),
    attempt INTEGER CHECK(attempt IN (0,1)),
    outcome INTEGER CHECK(outcome IN (0,1,2)),
    failed_phase INTEGER CHECK(failed_phase IN (0,1,2)),
    duration_ms REAL CHECK(duration_ms >= 0),
    duration_negative_zero INTEGER NOT NULL DEFAULT 0 CHECK(duration_negative_zero IN (0,1)),
    PRIMARY KEY(test_id, position),
    CHECK(run_id IS NOT NULL AND attempt IS NOT NULL AND outcome IS NOT NULL AND duration_ms IS NOT NULL)
) WITHOUT ROWID;
CREATE INDEX observation_runs ON observations(run_id);
CREATE TRIGGER discard_unused_run AFTER DELETE ON observations
WHEN old.run_id IS NOT NULL BEGIN
    DELETE FROM runs WHERE id = old.run_id
        AND NOT EXISTS (SELECT 1 FROM observations WHERE run_id = old.run_id);
END;
COMMIT;
";

pub(super) fn uses_sqlite(path: &Path) -> bool {
    path.extension()
        .is_some_and(|extension| extension == "sqlite3")
}

/// Serialize `SQLite` writers across a watch snapshot and its final publication.
/// Dropping the lease releases its OS lock; explicit JSON paths need no lease.
pub struct TestHistoryLease {
    _file: Option<File>,
}

impl TestHistoryLease {
    /// Acquire the same stable sibling lock used by every `SQLite` history writer.
    ///
    /// # Errors
    /// Returns an error if the lock directory, file, or OS lock is unavailable.
    pub fn acquire(path: &Path) -> Result<Self, CoreError> {
        if !uses_sqlite(path) {
            return Ok(Self { _file: None });
        }
        std::fs::create_dir_all(parent(path)).map_err(|error| io_error(path, error))?;
        let mut lock_path = path.as_os_str().to_os_string();
        lock_path.push(".lock");
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(lock_path)
            .map_err(|error| io_error(path, error))?;
        file.lock().map_err(|error| io_error(path, error))?;
        Ok(Self { _file: Some(file) })
    }
}

/// Capture a consistent history artifact for a staged run.
///
/// # Errors
/// Returns an error for an unreadable source, invalid `SQLite` schema, or failed publication.
pub fn snapshot_test_history(source: &Path, destination: &Path) -> Result<(), CoreError> {
    if source == destination || uses_sqlite(source) != uses_sqlite(destination) {
        return Err(CoreError::InvalidInput(
            "test history snapshot requires distinct paths of the same format".to_owned(),
        ));
    }
    if uses_sqlite(source) {
        let mut connection = open_existing(source, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        // The first read in `snapshot_into` starts a read transaction that sees one
        // committed state, including WAL frames, even when a writer is active
        // outside a watch lease. Keep it open until the copy is written.
        connection
            .execute_batch("BEGIN")
            .map_err(|error| sql_error(source, error))?;
        let copied = snapshot_into(&mut connection, source, destination);
        let released = connection
            .execute_batch("ROLLBACK")
            .map_err(|error| sql_error(source, error));
        copied.and(released)
    } else {
        let bytes = std::fs::read(source).map_err(|error| io_error(source, error))?;
        crate::atomic_write(destination, &bytes).map_err(|error| io_error(destination, error))
    }
}

fn snapshot_into(
    connection: &mut Connection,
    source: &Path,
    destination: &Path,
) -> Result<(), CoreError> {
    connection
        .query_row("SELECT COUNT(*) FROM tests", [], |row| row.get::<_, i64>(0))
        .map_err(|error| sql_error(source, error))?;
    let data = connection
        .serialize("main")
        .map_err(|error| sql_error(source, error))?;
    crate::atomic_write(destination, &data).map_err(|error| io_error(destination, error))
}

fn io_error(path: &Path, source: io::Error) -> CoreError {
    CoreError::ArtifactIo {
        path: path.to_path_buf(),
        operation: "access test history",
        source,
    }
}

fn sql_error(path: &Path, source: rusqlite::Error) -> CoreError {
    io_error(path, io::Error::other(source))
}

fn configure(path: &Path, connection: &Connection) -> Result<(), CoreError> {
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(|error| sql_error(path, error))?;
    connection
        .execute_batch("PRAGMA foreign_keys = ON; PRAGMA synchronous = FULL;")
        .map_err(|error| sql_error(path, error))
}

fn validate_schema(path: &Path, connection: &Connection) -> Result<(), CoreError> {
    let application: i64 = connection
        .pragma_query_value(None, "application_id", |row| row.get(0))
        .map_err(|error| sql_error(path, error))?;
    let version: i64 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .map_err(|error| sql_error(path, error))?;
    if application != APPLICATION_ID || version != VERSION {
        return Err(CoreError::InvalidInput(
            "test history has an unsupported SQLite schema".to_owned(),
        ));
    }
    Ok(())
}

fn open_existing(path: &Path, flags: OpenFlags) -> Result<Connection, CoreError> {
    let connection =
        Connection::open_with_flags(path, flags).map_err(|error| sql_error(path, error))?;
    configure(path, &connection)?;
    validate_schema(path, &connection)?;
    Ok(connection)
}

fn writable(path: &Path) -> Result<(Connection, TestHistoryLease), CoreError> {
    // Open only after acquiring the stable lock: a watch publication may replace
    // the inode while this writer waits for its turn.
    let lease = TestHistoryLease::acquire(path)?;
    initialize_locked(path)?;
    let connection = open_existing(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    Ok((connection, lease))
}

fn parent(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

fn initialize_locked(path: &Path) -> Result<(), CoreError> {
    if path.try_exists().map_err(|error| io_error(path, error))? {
        return Ok(());
    }
    std::fs::create_dir_all(parent(path)).map_err(|error| io_error(path, error))?;
    let temporary =
        tempfile::NamedTempFile::new_in(parent(path)).map_err(|error| io_error(path, error))?;
    let connection = Connection::open(temporary.path()).map_err(|error| sql_error(path, error))?;
    configure(path, &connection)?;
    connection
        .execute_batch(SCHEMA)
        .map_err(|error| sql_error(path, error))?;
    connection
        .close()
        .map_err(|(_, error)| sql_error(path, error))?;
    temporary
        .as_file()
        .sync_all()
        .map_err(|error| io_error(path, error))?;
    temporary
        .persist_noclobber(path)
        .map_err(|error| io_error(path, error.error))?;
    #[cfg(unix)]
    File::open(parent(path))
        .and_then(|directory| directory.sync_all())
        .map_err(|error| io_error(path, error))?;
    Ok(())
}

fn negative_zero(duration: Option<f64>) -> i64 {
    i64::from(duration.is_some_and(|duration| duration == 0.0 && duration.is_sign_negative()))
}

fn restore_zero_sign(duration: f64, negative: bool) -> rusqlite::Result<f64> {
    if negative && duration != 0.0 {
        return Err(rusqlite::Error::InvalidQuery);
    }
    Ok(if negative { -0.0 } else { duration })
}

fn read_duration(row: &rusqlite::Row<'_>, value: usize, sign: usize) -> rusqlite::Result<f64> {
    restore_zero_sign(row.get(value)?, row.get(sign)?)
}

fn intern_run(transaction: &Transaction<'_>, run: &str) -> rusqlite::Result<i64> {
    transaction
        .prepare("INSERT INTO runs(run_id) VALUES (?1) ON CONFLICT(run_id) DO NOTHING")?
        .execute([run])?;
    transaction
        .prepare("SELECT id FROM runs WHERE run_id=?1")?
        .query_row([run], |row| row.get(0))
}

pub(super) fn read_durations(path: &Path) -> Result<TestHistory, CoreError> {
    let connection = open_existing(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let mut statement = connection
        .prepare(
            "SELECT nodeid,duration_ms,duration_negative_zero FROM tests WHERE duration_ms IS NOT NULL ORDER BY nodeid",
        )
        .map_err(|error| sql_error(path, error))?;
    let records = statement
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, read_duration(row, 1, 2)?))
        })
        .map_err(|error| sql_error(path, error))?;
    let mut durations = BTreeMap::new();
    for record in records {
        let (nodeid, value) = record.map_err(|error| sql_error(path, error))?;
        if value.is_finite() && value >= 0.0 {
            durations.insert(nodeid, value);
        }
    }
    Ok(TestHistory { durations })
}

fn history_limit() -> CoreError {
    CoreError::InvalidInput("test history exceeds CI artifact limit".to_owned())
}

// Bound the retained JSON model using canonical compact serialization. Small
// separator allowances are conservative; CI checks the complete pretty bundle.
struct HistoryDocumentBudget {
    remaining: u64,
}

impl HistoryDocumentBudget {
    fn reserve(&mut self, bytes: u64) -> Result<(), CoreError> {
        self.remaining = self
            .remaining
            .checked_sub(bytes)
            .ok_or_else(history_limit)?;
        Ok(())
    }

    fn include<T: Serialize>(&mut self, value: &T) -> Result<(), CoreError> {
        serde_json::to_writer(self, value).map_err(|_| history_limit())
    }

    fn check_text(&self, row: &rusqlite::Row<'_>, columns: &[usize]) -> Result<(), CoreError> {
        let mut bytes = 0_u64;
        for column in columns {
            let value = row.get_ref(*column).map_err(|_| history_limit())?;
            if let rusqlite::types::ValueRef::Text(text) = value {
                bytes =
                    bytes.saturating_add(u64::try_from(text.len()).map_err(|_| history_limit())?);
            }
        }
        if bytes > self.remaining {
            return Err(history_limit());
        }
        Ok(())
    }
}

impl io::Write for HistoryDocumentBudget {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let length = u64::try_from(bytes.len()).map_err(io::Error::other)?;
        self.remaining = self
            .remaining
            .checked_sub(length)
            .ok_or_else(|| io::Error::from(io::ErrorKind::FileTooLarge))?;
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(super) fn read_document(path: &Path, max_bytes: u64) -> Result<Value, CoreError> {
    let mut connection = open_existing(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let transaction = connection
        .transaction()
        .map_err(|error| sql_error(path, error))?;
    let mut budget = HistoryDocumentBudget {
        remaining: max_bytes,
    };
    budget.reserve(12)?; // Empty root and tests objects, including the tests key.
    let mut tests = serde_json::Map::new();
    let mut statement = transaction.prepare("SELECT id,nodeid,duration_ms,observations_present,duration_negative_zero FROM tests ORDER BY nodeid")
        .map_err(|error| sql_error(path, error))?;
    let mut rows = statement
        .query([])
        .map_err(|error| sql_error(path, error))?;
    while let Some(row) = rows.next().map_err(|error| sql_error(path, error))? {
        budget.check_text(row, &[1])?;
        let (id, nodeid, duration, has_observations) = (|| {
            Ok::<_, rusqlite::Error>((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<f64>>(2)?
                    .map(|duration| restore_zero_sign(duration, row.get(4)?))
                    .transpose()?,
                row.get::<_, bool>(3)?,
            ))
        })()
        .map_err(|error| sql_error(path, error))?;
        let mut fields = serde_json::Map::new();
        if let Some(duration) = duration {
            fields.insert("duration_ms".to_owned(), Value::from(duration));
        }
        let mut entry = Value::Object(fields);
        budget.reserve(2)?; // Colon and comma.
        budget.include(&nodeid)?;
        budget.include(&entry)?;
        if has_observations && let Some(fields) = entry.as_object_mut() {
            budget.reserve(4)?; // Colon, comma, and array delimiters.
            budget.include(&"observations")?;
            fields.insert(
                "observations".to_owned(),
                Value::Array(read_observations(path, &transaction, id, &mut budget)?),
            );
        }
        tests.insert(nodeid, entry);
    }
    let mut root = serde_json::Map::new();
    root.insert("tests".to_owned(), Value::Object(tests));
    let mut value = Value::Object(root);
    value.sort_all_objects();
    Ok(value)
}

fn read_observations(
    path: &Path,
    connection: &Connection,
    id: i64,
    budget: &mut HistoryDocumentBudget,
) -> Result<Vec<Value>, CoreError> {
    let mut statement = connection.prepare("SELECT runs.run_id,attempt,outcome,failed_phase,duration_ms,duration_negative_zero FROM observations LEFT JOIN runs ON runs.id=observations.run_id WHERE test_id=?1 ORDER BY position")
        .map_err(|error| sql_error(path, error))?;
    let values = statement
        .query_map([id], |row| {
            let budget_error = |error| rusqlite::Error::ToSqlConversionFailure(Box::new(error));
            budget.check_text(row, &[0]).map_err(budget_error)?;
            let run_id: Option<String> = row.get(0)?;
            let mut fields = serde_json::Map::new();
            fields.insert(
                "attempt".to_owned(),
                Value::from(sql_label(row, 1, &["primary", "confirmation"])?),
            );
            fields.insert(
                "outcome".to_owned(),
                Value::from(sql_label(row, 2, &["passed", "failed", "skipped"])?),
            );
            fields.insert(
                "duration_ms".to_owned(),
                Value::from(read_duration(row, 4, 5)?),
            );
            if row.get::<_, Option<i64>>(3)?.is_some() {
                fields.insert(
                    "failed_phase".to_owned(),
                    Value::from(sql_label(row, 3, &["setup", "call", "teardown"])?),
                );
            }
            let mut value = Value::Object(fields);
            if let Some(run_id) = run_id {
                let Some(fields) = value.as_object_mut() else {
                    return Err(rusqlite::Error::InvalidQuery);
                };
                fields.insert("run_id".to_owned(), Value::from(run_id));
            }
            budget
                .reserve(1)
                .and_then(|()| budget.include(&value))
                .map_err(budget_error)?;
            Ok(value)
        })
        .map_err(|error| sql_error(path, error))?;
    values
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|error| sql_error(path, error))
}

fn sql_label(
    row: &rusqlite::Row<'_>,
    index: usize,
    labels: &[&'static str],
) -> rusqlite::Result<&'static str> {
    let value = usize::from(row.get::<_, u8>(index)?);
    labels
        .get(value)
        .copied()
        .ok_or(rusqlite::Error::InvalidQuery)
}

pub(super) fn write_durations(path: &Path, records: &[(String, f64)]) -> Result<(), CoreError> {
    let (mut connection, _lease) = writable(path)?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| sql_error(path, error))?;
    for (nodeid, value) in records {
        let previous: Option<i64> = transaction
            .query_row("SELECT id FROM tests WHERE nodeid=?1", [nodeid], |row| {
                row.get(0)
            })
            .optional()
            .map_err(|error| sql_error(path, error))?;
        if let Some(id) = previous {
            transaction
                .execute("DELETE FROM observations WHERE test_id=?1", [id])
                .map_err(|error| sql_error(path, error))?;
        }
        transaction.execute("INSERT INTO tests(nodeid,duration_ms,observations_present,duration_negative_zero) VALUES (?1,?2,0,?3)
            ON CONFLICT(nodeid) DO UPDATE SET duration_ms=excluded.duration_ms,observations_present=0,duration_negative_zero=excluded.duration_negative_zero",
            params![nodeid, value, negative_zero(Some(*value))]).map_err(|error| sql_error(path, error))?;
    }
    transaction.commit().map_err(|error| sql_error(path, error))
}

struct PreviousTest {
    id: i64,
    last_position: i64,
    created_in_run: bool,
}

struct RunStatements<'connection> {
    previous: rusqlite::Statement<'connection>,
    create: rusqlite::Statement<'connection>,
    insert: rusqlite::Statement<'connection>,
    update: rusqlite::Statement<'connection>,
    prune: rusqlite::Statement<'connection>,
}

impl<'connection> RunStatements<'connection> {
    fn new(connection: &'connection Connection) -> rusqlite::Result<Self> {
        Ok(Self {
            previous: connection.prepare("SELECT id,COALESCE((SELECT MAX(position) FROM observations WHERE test_id=tests.id),-1) FROM tests WHERE nodeid=?1")?,
            create: connection.prepare("INSERT INTO tests(nodeid,duration_ms,observations_present,duration_negative_zero) VALUES (?1,?2,1,?3)")?,
            insert: connection.prepare("INSERT INTO observations(test_id,position,run_id,attempt,outcome,failed_phase,duration_ms,duration_negative_zero) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)")?,
            update: connection.prepare("UPDATE tests SET observations_present=1,duration_ms=COALESCE(?2,duration_ms),duration_negative_zero=CASE WHEN ?2 IS NULL THEN duration_negative_zero ELSE ?3 END WHERE id=?1")?,
            prune: connection.prepare("DELETE FROM observations WHERE test_id=?1 AND position <= ?2")?,
        })
    }
}

impl PreviousTest {
    fn append(
        &mut self,
        statements: &mut RunStatements<'_>,
        test: &super::TestReport,
        identity: i64,
        attempt: TestAttempt,
    ) -> rusqlite::Result<()> {
        self.last_position = self
            .last_position
            .checked_add(1)
            .ok_or(rusqlite::Error::InvalidQuery)?;
        let attempt_code = match attempt {
            TestAttempt::Primary => 0,
            TestAttempt::Confirmation => 1,
        };
        let outcome = match test.outcome {
            TestOutcome::Passed => 0,
            TestOutcome::Failed => 1,
            TestOutcome::Skipped => 2,
        };
        let phase = test.failed_phase.map(|phase| match phase {
            super::FailedPhase::Setup => 0,
            super::FailedPhase::Call => 1,
            super::FailedPhase::Teardown => 2,
        });
        // Insert before trimming, including when an attempt reuses its run ID:
        // the pruning trigger must still see a reference to the current identity.
        statements.insert.execute(params![
            self.id,
            self.last_position,
            identity,
            attempt_code,
            outcome,
            phase,
            test.duration_ms,
            negative_zero(Some(test.duration_ms))
        ])?;
        if !self.created_in_run || self.last_position != 0 {
            let duration = match attempt {
                TestAttempt::Primary => test.call_duration_ms,
                TestAttempt::Confirmation => None,
            };
            statements
                .update
                .execute(params![self.id, duration, negative_zero(duration)])?;
        }
        // Newly inserted tests have no earlier rows; before position 20 the
        // bounded-history DELETE cannot match anything.
        if !self.created_in_run || self.last_position >= MAX_OBSERVATIONS {
            statements
                .prune
                .execute(params![self.id, self.last_position - MAX_OBSERVATIONS])?;
        }
        Ok(())
    }
}

fn validate_affected_tests<'a>(
    path: &Path,
    statements: &mut RunStatements<'_>,
    run: &'a TestRun,
) -> Result<BTreeMap<&'a str, Option<PreviousTest>>, CoreError> {
    let mut tests = BTreeMap::new();
    // Validate every affected entry before changing any rows. The transaction also
    // prevents another writer from changing those entries between validation and use.
    for test in &run.tests {
        let std::collections::btree_map::Entry::Vacant(slot) = tests.entry(test.nodeid.as_str())
        else {
            continue;
        };
        let previous = statements
            .previous
            .query_row([&test.nodeid], |row| {
                Ok(PreviousTest {
                    id: row.get(0)?,
                    last_position: row.get(1)?,
                    created_in_run: false,
                })
            })
            .optional()
            .map_err(|error| sql_error(path, error))?;
        slot.insert(previous);
    }
    Ok(tests)
}

pub(super) fn record_run(
    path: &Path,
    run: &TestRun,
    run_id: &str,
    attempt: TestAttempt,
) -> Result<(), CoreError> {
    for test in &run.tests {
        if test.nodeid.is_empty()
            || !test.duration_ms.is_finite()
            || test.duration_ms < 0.0
            || test
                .call_duration_ms
                .is_some_and(|duration| !duration.is_finite() || duration < 0.0)
            || (test.outcome == TestOutcome::Failed && test.failed_phase.is_none())
        {
            return Err(CoreError::InvalidInput(
                "test result entry is invalid".to_owned(),
            ));
        }
    }
    let (mut connection, _lease) = writable(path)?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| sql_error(path, error))?;
    let mut statements =
        RunStatements::new(&transaction).map_err(|error| sql_error(path, error))?;
    let mut tests = if run.tests.is_empty() {
        BTreeMap::new()
    } else if transaction
        .query_row("SELECT EXISTS(SELECT 1 FROM tests)", [], |row| {
            row.get::<_, bool>(0)
        })
        .map_err(|error| sql_error(path, error))?
    {
        validate_affected_tests(path, &mut statements, run)?
    } else {
        // The immediate transaction excludes writers, so no selected entry can
        // exist until after this validation pass completes.
        run.tests
            .iter()
            .map(|test| (test.nodeid.as_str(), None))
            .collect()
    };
    if !run.tests.is_empty() {
        let identity = intern_run(&transaction, run_id).map_err(|error| sql_error(path, error))?;
        for test in &run.tests {
            let Some(state) = tests.get_mut(test.nodeid.as_str()) else {
                return Err(CoreError::InvalidInput(
                    "test history entry is missing".to_owned(),
                ));
            };
            if state.is_none() {
                let duration = match attempt {
                    TestAttempt::Primary => test.call_duration_ms,
                    TestAttempt::Confirmation => None,
                };
                statements
                    .create
                    .execute(params![&test.nodeid, duration, negative_zero(duration)])
                    .map_err(|error| sql_error(path, error))?;
                *state = Some(PreviousTest {
                    id: transaction.last_insert_rowid(),
                    last_position: -1,
                    created_in_run: true,
                });
            }
            let Some(state) = state else {
                return Err(CoreError::InvalidInput(
                    "test history entry is missing".to_owned(),
                ));
            };
            state
                .append(&mut statements, test, identity, attempt)
                .map_err(|error| sql_error(path, error))?;
        }
    }
    drop(statements);
    transaction.commit().map_err(|error| sql_error(path, error))
}
