//! Schema-7 `SQLite` writing of coverage data.

use std::collections::BTreeMap;
use std::path::Path;

use crate::output::{OutputTarget, output_target, replacement_file};
use rusqlite::{Connection, params, params_from_iter};

use super::CoverageData;
use crate::data::DataError;
impl CoverageData {
    /// Atomically update the watch identity in an existing schema-7 database.
    ///
    /// # Errors
    ///
    /// Returns an error when the database cannot be read, validated, or replaced.
    pub fn stamp_watch_identity(path: &Path, identity: &str) -> Result<(), DataError> {
        let mut data = Self::read_path(path).map_err(|source| DataError::Validate {
            path: path.to_path_buf(),
            source,
        })?;
        data.metadata
            .insert("vfy_watch_input_identity".to_owned(), identity.to_owned());
        data.write_path(path)
    }

    /// Write schema-7 records, then replace the destination file.
    ///
    /// A write or replacement failure leaves an existing destination unchanged.
    ///
    /// # Errors
    ///
    /// Returns an error when the destination cannot be created or replaced.
    pub fn write_path(&self, path: &Path) -> Result<(), DataError> {
        self.write_path_with_metadata(path, &BTreeMap::new())
    }

    /// Atomically write this store with metadata values replaced by `overrides`.
    ///
    /// The store itself is unchanged, so a shared store is written without a
    /// copy.
    ///
    /// # Errors
    ///
    /// Returns an error for conflicting collection-mode metadata or when the destination
    /// cannot be created or replaced.
    pub fn write_path_with_metadata(
        &self,
        path: &Path,
        overrides: &BTreeMap<String, String>,
    ) -> Result<(), DataError> {
        let parent = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(parent)
            .map_err(|source| DataError::filesystem("create coverage directory", source))?;
        let OutputTarget::File { path, .. } = output_target(path)
            .map_err(|source| DataError::filesystem("inspect coverage database", source))?
        else {
            return Err(DataError::invalid(
                "write coverage database",
                "the data file must be a regular file",
            ));
        };
        let path = path.as_path();
        // Coverage.py erases the data file and SQLite recreates it with mode
        // 0o644 less the umask, so an earlier mode is not kept.
        let temporary = replacement_file(path, None, 0o644).map_err(|source| {
            DataError::filesystem("create temporary coverage database", source)
        })?;
        let mut connection = Connection::open(temporary.path())
            .map_err(|source| DataError::database("open temporary coverage database", source))?;
        // The rename below replaces the destination atomically, and a failed
        // write discards the temporary file, so this private file needs no
        // rollback journal or per-commit fsync. Coverage.py writes its data
        // files with the same pragmas.
        connection
            .execute_batch("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;")
            .map_err(|source| {
                DataError::database("configure temporary coverage database", source)
            })?;
        self.write_connection_with_metadata(&mut connection, overrides)?;
        drop(connection);
        temporary
            .persist(path)
            .map_err(|error| DataError::filesystem("replace coverage database", error.error))?;
        Ok(())
    }

    /// Write this store into an empty `SQLite` connection in one transaction.
    ///
    /// # Errors
    ///
    /// Returns an error when schema creation or record insertion fails.
    pub fn write_connection(&self, connection: &mut Connection) -> Result<(), DataError> {
        self.write_connection_with_metadata(connection, &BTreeMap::new())
    }

    fn write_connection_with_metadata(
        &self,
        connection: &mut Connection,
        overrides: &BTreeMap<String, String>,
    ) -> Result<(), DataError> {
        let mut metadata: BTreeMap<&str, &str> = self
            .metadata
            .iter()
            .map(|(key, value)| (key.as_str(), value.as_str()))
            .collect();
        metadata.extend(
            overrides
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str())),
        );
        if self.mode_is_known() {
            let mode = if self.has_arcs { "1" } else { "0" };
            if overrides.get("has_arcs").is_some_and(|value| value != mode) {
                return Err(DataError::invalid(
                    "write coverage database",
                    "has_arcs metadata conflicts with collection mode",
                ));
            }
            metadata.insert("has_arcs", mode);
        }
        let transaction = connection
            .transaction()
            .map_err(|source| DataError::database("start coverage transaction", source))?;
        transaction
            .execute_batch(
                "CREATE TABLE coverage_schema (version integer);\
                 INSERT INTO coverage_schema VALUES (7);\
                 CREATE TABLE meta (key text, value text, unique (key));\
                 CREATE TABLE file (id integer primary key, path text, unique (path));\
                 CREATE TABLE context (id integer primary key, context text, unique (context));\
                 CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob,\
                     unique (file_id, context_id));\
                 CREATE TABLE arc (file_id integer, context_id integer, fromno integer,\
                     tono integer, unique (file_id, context_id, fromno, tono));\
                 CREATE TABLE tracer (file_id integer primary key, tracer text);",
            )
            .map_err(|source| DataError::database("create coverage schema", source))?;

        for (key, value) in metadata {
            transaction
                .execute("INSERT INTO meta VALUES (?, ?)", params![key, value])
                .map_err(|source| DataError::database("write coverage database", source))?;
        }
        let mut file_ids = BTreeMap::new();
        for path in &self.file_order {
            transaction
                .execute("INSERT INTO file (path) VALUES (?)", [path])
                .map_err(|source| DataError::database("write coverage database", source))?;
            file_ids.insert(path, transaction.last_insert_rowid());
        }
        let contexts = self.context_names();
        let mut context_ids = BTreeMap::new();
        for context in contexts {
            transaction
                .execute("INSERT INTO context (context) VALUES (?)", [context])
                .map_err(|source| DataError::database("write coverage database", source))?;
            context_ids.insert(context, transaction.last_insert_rowid());
        }

        self.write_measurements(&transaction, &file_ids, &context_ids)?;
        self.write_semantic_tables(&transaction)?;
        transaction
            .commit()
            .map_err(|source| DataError::database("write coverage database", source))
    }

    fn write_semantic_tables(
        &self,
        transaction: &rusqlite::Transaction<'_>,
    ) -> Result<(), DataError> {
        self.write_boolean_semantic_tables(transaction)?;
        self.write_path_semantic_tables(transaction)
    }

    fn write_boolean_semantic_tables(
        &self,
        transaction: &rusqlite::Transaction<'_>,
    ) -> Result<(), DataError> {
        if !self.semantic.definitions.is_empty() {
            transaction
                .execute_batch(
                    "CREATE TABLE vfy_decision (key text primary key, path text, line integer, end_line integer, kind text, source_hash text, expression text, logic text, condition_lines text);\
                     CREATE TABLE vfy_condition (decision text, index_no integer, start_line integer, start_col integer, end_line integer, end_col integer, unique (decision, index_no));\
                     CREATE TABLE vfy_observation (context text, decision text, vector text, outcome integer, unique (context, decision, vector, outcome));",
                )
                .map_err(|source| DataError::database("write coverage database", source))?;
            for definition in self.semantic.definitions.values() {
                let condition_lines = definition
                    .condition_lines
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(",");
                transaction
                    .execute(
                        "INSERT INTO vfy_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        params![
                            definition.key,
                            definition.path,
                            definition.line,
                            definition.end_line,
                            definition.kind.as_str(),
                            definition.source_hash,
                            definition.expression,
                            definition.logic.render(),
                            condition_lines,
                        ],
                    )
                    .map_err(|source| DataError::database("write coverage database", source))?;
            }
            self.write_semantic_conditions(transaction)?;
            for observation in &self.semantic.observations {
                transaction
                    .execute(
                        "INSERT INTO vfy_observation VALUES (?, ?, ?, ?)",
                        params![
                            observation.context,
                            observation.decision,
                            observation.vector,
                            i64::from(observation.outcome),
                        ],
                    )
                    .map_err(|source| DataError::database("write coverage database", source))?;
            }
        }
        Ok(())
    }

    fn write_path_semantic_tables(
        &self,
        transaction: &rusqlite::Transaction<'_>,
    ) -> Result<(), DataError> {
        if !self.semantic.path_definitions.is_empty() {
            transaction
                .execute_batch(
                    "CREATE TABLE vfy_path_decision (key text primary key, path text, line integer, end_line integer, kind text, source_hash text, expression text, body_start integer, body_end integer);\
                     CREATE TABLE vfy_path_outcome (decision text, index_no integer, label text, start_line integer, start_col integer, end_line integer, end_col integer, role text, body_start integer, body_end integer, unique (decision, index_no));\
                     CREATE TABLE vfy_path_observation (context text, decision text, outcome integer, unique (context, decision, outcome));",
                )
                .map_err(|source| DataError::database("write coverage database", source))?;
            for definition in self.semantic.path_definitions.values() {
                transaction
                    .execute(
                        "INSERT INTO vfy_path_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        params![
                            definition.key,
                            definition.path,
                            definition.line,
                            definition.end_line,
                            definition.kind.as_str(),
                            definition.source_hash,
                            definition.expression,
                            definition.body_start,
                            definition.body_end,
                        ],
                    )
                    .map_err(|source| DataError::database("write coverage database", source))?;
                for (index, outcome) in definition.outcomes.iter().enumerate() {
                    let index = i64::try_from(index).map_err(|_| {
                        DataError::invalid("encode semantic outcome", "outcome index overflow")
                    })?;
                    transaction
                        .execute(
                            "INSERT INTO vfy_path_outcome VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            params![
                                definition.key,
                                index,
                                outcome.label,
                                outcome.range.0,
                                outcome.range.1,
                                outcome.range.2,
                                outcome.range.3,
                                outcome.role.as_str(),
                                outcome.role.body().map(|body| body.0),
                                outcome.role.body().map(|body| body.1),
                            ],
                        )
                        .map_err(|source| DataError::database("write coverage database", source))?;
                }
            }
            for observation in &self.semantic.path_observations {
                transaction
                    .execute(
                        "INSERT INTO vfy_path_observation VALUES (?, ?, ?)",
                        params![
                            observation.context,
                            observation.decision,
                            i64::try_from(observation.outcome).map_err(|_| DataError::invalid(
                                "encode semantic outcome",
                                "outcome index overflow"
                            ))?,
                        ],
                    )
                    .map_err(|source| DataError::database("write coverage database", source))?;
            }
        }
        Ok(())
    }

    fn write_semantic_conditions(
        &self,
        transaction: &rusqlite::Transaction<'_>,
    ) -> Result<(), DataError> {
        for definition in self.semantic.definitions.values() {
            for (index, &(start_line, start_col, end_line, end_col)) in
                definition.condition_ranges.iter().enumerate()
            {
                let index = i64::try_from(index).map_err(|_| {
                    DataError::invalid("encode semantic condition", "condition index overflow")
                })?;
                transaction
                    .execute(
                        "INSERT INTO vfy_condition VALUES (?, ?, ?, ?, ?, ?)",
                        params![
                            definition.key,
                            index,
                            start_line,
                            start_col,
                            end_line,
                            end_col,
                        ],
                    )
                    .map_err(|source| DataError::database("write coverage database", source))?;
            }
        }
        Ok(())
    }

    fn write_measurements(
        &self,
        transaction: &rusqlite::Transaction<'_>,
        file_ids: &BTreeMap<&String, i64>,
        context_ids: &BTreeMap<&str, i64>,
    ) -> Result<(), DataError> {
        for (path, tracer) in &self.tracers {
            transaction
                .execute(
                    "INSERT INTO tracer VALUES (?, ?)",
                    params![file_ids[path], tracer],
                )
                .map_err(|source| DataError::database("write coverage database", source))?;
        }
        if !self.has_arcs && !self.lines.is_empty() {
            let mut statement = transaction
                .prepare("INSERT INTO line_bits VALUES (?, ?, ?)")
                .map_err(|source| DataError::database("write coverage database", source))?;
            for (path, contexts) in &self.lines {
                for (context, lines) in contexts {
                    let numbits = lines.to_numbits();
                    statement
                        .execute(params![
                            file_ids[path],
                            context_ids[context.as_ref()],
                            numbits
                        ])
                        .map_err(|source| DataError::database("write coverage database", source))?;
                }
            }
        }
        if self.has_arcs && !self.arcs.is_empty() {
            let mut rows = ArcRowWriter::new(transaction)?;
            for (path, contexts) in &self.arcs {
                for (context, arcs) in contexts {
                    let file_id = file_ids[path];
                    let context_id = context_ids[context.as_ref()];
                    for (start, end) in arcs.sorted() {
                        rows.push([file_id, context_id, i64::from(start), i64::from(end)])?;
                    }
                }
            }
            rows.finish()?;
        }
        Ok(())
    }
}

/// Rows per multi-row `arc` insert; four values each stays far below
/// `SQLite`'s bound-parameter limit.
pub(super) const ARC_INSERT_BATCH: usize = 256;

/// Insert `arc` rows in caller order, binding full batches to one statement.
struct ArcRowWriter<'connection> {
    batch: rusqlite::Statement<'connection>,
    single: rusqlite::Statement<'connection>,
    pending: Vec<i64>,
}

impl<'connection> ArcRowWriter<'connection> {
    fn new(transaction: &'connection rusqlite::Transaction<'_>) -> Result<Self, DataError> {
        let values = vec!["(?, ?, ?, ?)"; ARC_INSERT_BATCH].join(", ");
        let prepare = |sql: &str| {
            transaction
                .prepare(sql)
                .map_err(|source| DataError::database("write coverage database", source))
        };
        Ok(Self {
            batch: prepare(&format!("INSERT INTO arc VALUES {values}"))?,
            single: prepare("INSERT INTO arc VALUES (?, ?, ?, ?)")?,
            pending: Vec::with_capacity(ARC_INSERT_BATCH * 4),
        })
    }

    fn push(&mut self, row: [i64; 4]) -> Result<(), DataError> {
        self.pending.extend(row);
        if self.pending.len() == ARC_INSERT_BATCH * 4 {
            self.batch
                .execute(params_from_iter(&self.pending))
                .map_err(|source| DataError::database("write coverage database", source))?;
            self.pending.clear();
        }
        Ok(())
    }

    fn finish(mut self) -> Result<(), DataError> {
        for row in self.pending.chunks_exact(4) {
            self.single
                .execute(params_from_iter(row))
                .map_err(|source| DataError::database("write coverage database", source))?;
        }
        self.pending.clear();
        Ok(())
    }
}
