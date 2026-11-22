//! Schema-7 `SQLite` reading into validated coverage data.

use std::collections::BTreeMap;
use std::collections::btree_map::Entry;
use std::path::Path;
use std::sync::Arc as Shared;

use rusqlite::{Connection, OptionalExtension};

use super::semantic::data_read_semantic;
use super::{Context, ContextIds, CoverageData, DataIndex, IdMap, SCHEMA_VERSION};
use crate::evidence::{NumbitsTable, SparseArcs, SparseLines};
impl CoverageData {
    /// Read and validate schema-7 coverage data.
    ///
    /// # Errors
    ///
    /// Returns a `SQLite` error for unreadable, incompatible, or corrupt data.
    pub fn read_path(path: &Path) -> Result<Self, rusqlite::Error> {
        let connection = Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        Self::read_connection(&connection)
    }

    /// Read and validate schema-7 coverage data from an open connection.
    ///
    /// # Errors
    ///
    /// Returns a `SQLite` error for incompatible or corrupt data.
    pub fn read_connection(connection: &Connection) -> Result<Self, rusqlite::Error> {
        let schema: Option<i64> = connection
            .query_row("SELECT version FROM coverage_schema", [], |row| row.get(0))
            .optional()?;
        if schema != Some(SCHEMA_VERSION) {
            return Err(rusqlite::Error::InvalidQuery);
        }

        let (mut data, file_ids, contexts) = Self::read_index(connection)?;
        data.read_measurements(connection, &file_ids, &contexts)?;
        Ok(data)
    }

    fn read_index(connection: &Connection) -> Result<DataIndex, rusqlite::Error> {
        let mut data = Self::empty();
        {
            let mut statement = connection.prepare("SELECT key, value FROM meta")?;
            let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
            for row in rows {
                let (key, value) = row?;
                data.metadata.insert(key, value);
            }
        }
        data.has_arcs = match data.metadata.get("has_arcs").map(String::as_str) {
            None | Some("0") => false,
            Some("1") => true,
            Some(_) => {
                return Err(rusqlite::Error::InvalidParameterName(
                    "corrupt coverage data: invalid has_arcs metadata".to_owned(),
                ));
            }
        };
        for (table, allowed) in [
            (
                "line_bits",
                data.metadata.contains_key("has_arcs") && !data.has_arcs,
            ),
            ("arc", data.has_arcs),
        ] {
            if allowed {
                continue;
            }
            let exists: bool = connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?)",
                [table],
                |row| row.get(0),
            )?;
            if exists {
                let sql = format!("SELECT EXISTS(SELECT 1 FROM {table})");
                let populated: bool = connection.query_row(&sql, [], |row| row.get(0))?;
                if populated {
                    return Err(rusqlite::Error::InvalidParameterName(format!(
                        "corrupt coverage data: {table} rows conflict with has_arcs metadata"
                    )));
                }
            }
        }

        let mut file_ids = BTreeMap::new();
        {
            let mut statement = connection.prepare("SELECT id, path FROM file ORDER BY id")?;
            let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
            for row in rows {
                let (id, path): (i64, String) = row?;
                data.touch_file(&path);
                file_ids.insert(id, path);
            }
        }
        let mut contexts = BTreeMap::new();
        {
            let mut statement = connection.prepare("SELECT id, context FROM context")?;
            let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
            for row in rows {
                let (id, context): (i64, String) = row?;
                contexts.insert(id, Context::from(context.as_str()));
                data.contexts.insert(context);
            }
        }
        {
            let mut statement = connection.prepare(
                "SELECT tracer.file_id, tracer.tracer FROM tracer ORDER BY tracer.file_id",
            )?;
            let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
            for row in rows {
                let (file_id, tracer): (i64, String) = row?;
                let path = file_ids.get(&file_id).ok_or_else(|| {
                    rusqlite::Error::InvalidParameterName(format!(
                        "corrupt coverage data: tracer references absent file id {file_id}"
                    ))
                })?;
                data.tracers.insert(path.clone(), tracer);
            }
        }

        Ok((data, file_ids, contexts))
    }

    fn read_measurements(
        &mut self,
        connection: &Connection,
        file_ids: &IdMap,
        contexts: &ContextIds,
    ) -> Result<(), rusqlite::Error> {
        if self.has_arcs {
            let mut statement = connection.prepare(
                "SELECT file_id, context_id, fromno, tono FROM arc \
                 ORDER BY file_id, context_id, fromno, tono",
            )?;
            let rows = statement.query_map([], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })?;
            let mut store_partition = |context: Context, path: String, arcs: SparseArcs| match self
                .arcs
                .entry(path)
                .or_default()
                .entry(context)
            {
                Entry::Vacant(entry) => {
                    entry.insert(arcs);
                }
                Entry::Occupied(mut entry) => entry.get_mut().extend_set(&arcs),
            };
            let mut partition: Option<(i64, i64, Context, String, SparseArcs)> = None;
            for row in rows {
                let (file_id, context_id, start, end): (i64, i64, i32, i32) = row?;
                if let Some((current_file, current_context, _, _, arcs)) = partition.as_mut()
                    && *current_file == file_id
                    && *current_context == context_id
                {
                    arcs.insert((start, end));
                    continue;
                }
                if let Some((_, _, context, path, arcs)) = partition.take() {
                    store_partition(context, path, arcs);
                }
                let path = file_ids
                    .get(&file_id)
                    .ok_or(rusqlite::Error::InvalidQuery)?;
                let context = contexts
                    .get(&context_id)
                    .ok_or(rusqlite::Error::InvalidQuery)?;
                let mut arcs = SparseArcs::default();
                arcs.insert((start, end));
                partition = Some((
                    file_id,
                    context_id,
                    Shared::clone(context),
                    path.clone(),
                    arcs,
                ));
            }
            if let Some((_, _, context, path, arcs)) = partition {
                store_partition(context, path, arcs);
            }
        } else {
            // Rows arrive in file order and equal sets come from contexts of
            // the same file, so a per-file table keeps lookups small.
            let mut line_sets = NumbitsTable::default();
            let mut line_sets_file = None;
            let mut statement = connection.prepare(
                "SELECT file_id, context_id, numbits FROM line_bits \
                 ORDER BY file_id, context_id",
            )?;
            let rows = statement.query_map([], |row| {
                let file_id: i64 = row.get(0)?;
                if line_sets_file != Some(file_id) {
                    line_sets.clear();
                    line_sets_file = Some(file_id);
                }
                let value = row.get_ref(2)?;
                let numbits = value.as_blob().map_err(|_| {
                    rusqlite::Error::InvalidColumnType(2, "numbits".to_owned(), value.data_type())
                })?;
                let lines = line_sets.decode(numbits).map_err(|error| {
                    rusqlite::Error::FromSqlConversionFailure(
                        2,
                        rusqlite::types::Type::Blob,
                        Box::new(error),
                    )
                })?;
                Ok((file_id, row.get(1)?, lines))
            })?;
            for row in rows {
                let (file_id, context_id, lines): (i64, i64, SparseLines) = row?;
                let path = file_ids
                    .get(&file_id)
                    .ok_or(rusqlite::Error::InvalidQuery)?;
                let context = contexts
                    .get(&context_id)
                    .ok_or(rusqlite::Error::InvalidQuery)?;
                match self
                    .lines
                    .entry(path.clone())
                    .or_default()
                    .entry(Shared::clone(context))
                {
                    Entry::Vacant(entry) => {
                        entry.insert(lines);
                    }
                    Entry::Occupied(mut entry) => entry.get_mut().extend_set(&lines),
                }
            }
        }
        data_read_semantic(self, connection)?;
        Ok(())
    }
}
