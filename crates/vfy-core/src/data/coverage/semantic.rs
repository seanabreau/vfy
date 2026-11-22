//! Schema-7 semantic table reading and validation.

use rusqlite::{Connection, OptionalExtension};

use crate::semantic::{
    DecisionDefinition, PathDecisionDefinition, PathDecisionKind, PathObservation,
    PathOutcomeDefinition, PathOutcomeRole, SemanticData, SemanticObservation,
};
use crate::{parse_logic, valid_observation};

use super::CoverageData;

#[cfg(test)]
mod tests;

pub(super) fn data_read_semantic(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let mut staged = CoverageData::empty();
    staged.files.clone_from(&data.files);
    data_read_semantic_staged(&mut staged, connection)?;
    validate_staged(&mut staged.semantic, &staged.files)?;
    staged.semantic = staged
        .semantic
        .normalize_generated_keys()
        .map_err(|(key, invariant)| corruption(&key, invariant))?;
    let paths = staged
        .semantic
        .definitions
        .values()
        .map(|definition| definition.path.clone())
        .chain(
            staged
                .semantic
                .path_definitions
                .values()
                .map(|definition| definition.path.clone()),
        );
    for path in paths {
        data.touch_file(&path);
    }
    data.semantic.merge_validated(staged.semantic);
    Ok(())
}

fn corruption(key: &str, invariant: &str) -> rusqlite::Error {
    rusqlite::Error::InvalidParameterName(format!("corrupt semantic decision {key:?}: {invariant}"))
}

fn validate_staged(
    semantic: &mut SemanticData,
    files: &std::collections::BTreeSet<String>,
) -> Result<(), rusqlite::Error> {
    let definitions = std::mem::take(&mut semantic.definitions);
    for (key, definition) in definitions {
        if !files.contains(&definition.path) {
            return Err(corruption(&key, "referenced path is absent"));
        }
        let definition = DecisionDefinition::validated(
            definition.key,
            definition.path,
            definition.line,
            definition.end_line,
            definition.kind,
            definition.source_hash,
            definition.expression,
            definition.logic,
            definition.condition_lines,
            definition.condition_ranges,
        )
        .map_err(|error| corruption(&key, error.0))?;
        semantic
            .insert_definition(definition)
            .map_err(|error| corruption(&key, error.0))?;
    }
    let definitions = std::mem::take(&mut semantic.path_definitions);
    for (key, definition) in definitions {
        if !files.contains(&definition.path) {
            return Err(corruption(&key, "referenced path is absent"));
        }
        let definition = PathDecisionDefinition::validated(
            definition.key,
            definition.path,
            definition.line,
            definition.end_line,
            definition.kind,
            definition.source_hash,
            definition.expression,
            definition.body_start,
            definition.body_end,
            definition.range,
            definition.outcomes,
        )
        .map_err(|error| corruption(&key, error.0))?;
        semantic
            .insert_path_definition(definition)
            .map_err(|error| corruption(&key, error.0))?;
    }
    for observation in &semantic.observations {
        if !semantic.definitions.contains_key(&observation.decision) {
            return Err(corruption(
                &observation.decision,
                "observation references an absent decision",
            ));
        }
    }
    Ok(())
}

fn data_read_semantic_staged(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let exists: Option<String> = connection
        .query_row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vfy_decision'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if exists.is_none() {
        return data_read_path_semantic(data, connection);
    }
    let mut statement = connection.prepare(
        "SELECT key, path, line, end_line, kind, source_hash, expression, logic, condition_lines \
         FROM vfy_decision ORDER BY key",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i32>(2)?,
            row.get::<_, i32>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
            row.get::<_, String>(7)?,
            row.get::<_, String>(8)?,
        ))
    })?;
    for row in rows {
        let (key, path, line, end_line, kind, source_hash, expression, logic, condition_lines) =
            row?;
        let logic =
            parse_logic(&logic).map_err(|error| corruption(&key, &format!("logic {error}")))?;
        let condition_lines = if condition_lines.is_empty() {
            Vec::new()
        } else {
            condition_lines
                .split(',')
                .take(logic.condition_count() + 1)
                .map(|value| {
                    value
                        .parse::<i32>()
                        .map_err(|_| rusqlite::Error::InvalidQuery)
                })
                .collect::<Result<Vec<_>, _>>()?
        };
        if condition_lines.len() != logic.condition_count() {
            return Err(corruption(&key, "condition lines have inconsistent arity"));
        }
        data.semantic.definitions.insert(
            key.clone(),
            DecisionDefinition {
                key,
                path,
                line,
                end_line,
                kind,
                source_hash,
                expression,
                logic,
                condition_ranges: condition_lines
                    .iter()
                    .map(|&line| (line, -1, line, -1))
                    .collect(),
                condition_lines,
            },
        );
    }
    data_read_semantic_conditions(data, connection)?;
    data_read_semantic_observations(data, connection)?;
    data_read_path_semantic(data, connection)
}

fn data_read_path_semantic(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let exists: Option<String> = connection
        .query_row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vfy_path_decision'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if exists.is_none() {
        return Ok(());
    }
    data_read_path_definitions(data, connection)?;
    data_read_path_outcomes(data, connection)?;
    data_read_path_observations(data, connection)
}

fn data_read_path_definitions(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let mut statement = connection.prepare(
        "SELECT key, path, line, end_line, kind, source_hash, expression, body_start, body_end \
         FROM vfy_path_decision ORDER BY key",
    )?;
    let rows = statement.query_map([], |row| {
        let kind = row.get::<_, String>(4)?;
        let kind = PathDecisionKind::parse(&kind).ok_or(rusqlite::Error::InvalidQuery)?;
        Ok(PathDecisionDefinition {
            key: row.get(0)?,
            path: row.get(1)?,
            line: row.get(2)?,
            end_line: row.get(3)?,
            kind,
            source_hash: row.get(5)?,
            expression: row.get(6)?,
            body_start: row.get(7)?,
            body_end: row.get(8)?,
            // The path table stores line bounds only, so column offsets are zero.
            range: (row.get(2)?, 0, row.get(3)?, 0),
            outcomes: Vec::new(),
        })
    })?;
    for row in rows {
        let definition = row?;
        data.semantic
            .path_definitions
            .insert(definition.key.clone(), definition);
    }
    Ok(())
}

fn data_read_path_outcomes(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let mut statement = connection.prepare(
        "SELECT decision, index_no, label, start_line, start_col, end_line, end_col, \
                role, body_start, body_end \
         FROM vfy_path_outcome ORDER BY decision, index_no",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i32>(3)?,
            row.get::<_, i32>(4)?,
            row.get::<_, i32>(5)?,
            row.get::<_, i32>(6)?,
            row.get::<_, String>(7)?,
            row.get::<_, Option<i32>>(8)?,
            row.get::<_, Option<i32>>(9)?,
        ))
    })?;
    for row in rows {
        let (
            decision,
            index,
            label,
            start_line,
            start_col,
            end_line,
            end_col,
            role,
            body_start,
            body_end,
        ) = row?;
        let index = usize::try_from(index).map_err(|_| rusqlite::Error::InvalidQuery)?;
        let Some(definition) = data.semantic.path_definitions.get_mut(&decision) else {
            return Err(rusqlite::Error::InvalidQuery);
        };
        if index != definition.outcomes.len() || label.is_empty() {
            return Err(rusqlite::Error::InvalidQuery);
        }
        let body = body_start.zip(body_end);
        let role = PathOutcomeRole::parse(&role, body).ok_or(rusqlite::Error::InvalidQuery)?;
        definition.outcomes.push(PathOutcomeDefinition {
            label,
            range: (start_line, start_col, end_line, end_col),
            role,
        });
    }
    if data
        .semantic
        .path_definitions
        .values()
        .any(|definition| definition.outcomes.len() < 2)
    {
        return Err(rusqlite::Error::InvalidQuery);
    }
    Ok(())
}

fn data_read_path_observations(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let mut statement = connection.prepare(
        "SELECT context, decision, outcome FROM vfy_path_observation \
         ORDER BY context, decision, outcome",
    )?;
    let rows = statement.query_map([], |row| {
        Ok(PathObservation {
            context: row.get(0)?,
            decision: row.get(1)?,
            outcome: usize::try_from(row.get::<_, i64>(2)?)
                .map_err(|_| rusqlite::Error::InvalidQuery)?,
        })
    })?;
    for row in rows {
        let observation = row?;
        if data
            .semantic
            .path_definitions
            .get(&observation.decision)
            .is_none_or(|definition| observation.outcome >= definition.outcomes.len())
        {
            return Err(rusqlite::Error::InvalidQuery);
        }
        data.semantic.path_observations.insert(observation);
    }
    Ok(())
}

fn data_read_semantic_observations(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let mut statement = connection.prepare(
        "SELECT context, decision, vector, outcome FROM vfy_observation \
         ORDER BY context, decision, vector, outcome",
    )?;
    let rows = statement.query_map([], |row| {
        let outcome = row.get::<_, i64>(3)?;
        if !matches!(outcome, 0 | 1) {
            return Err(rusqlite::Error::InvalidQuery);
        }
        Ok(SemanticObservation {
            context: row.get(0)?,
            decision: row.get(1)?,
            vector: row.get(2)?,
            outcome: outcome != 0,
        })
    })?;
    for row in rows {
        let observation = row?;
        if !data
            .semantic
            .definitions
            .contains_key(&observation.decision)
            || observation
                .vector
                .chars()
                .any(|value| !matches!(value, '0' | '1' | '-'))
            || data
                .semantic
                .definitions
                .get(&observation.decision)
                .is_some_and(|definition| {
                    observation.vector.chars().count() != definition.logic.condition_count()
                })
            || data
                .semantic
                .definitions
                .get(&observation.decision)
                .is_some_and(|definition| {
                    !valid_observation(&definition.logic, &observation.vector, observation.outcome)
                })
        {
            return Err(rusqlite::Error::InvalidQuery);
        }
        data.semantic.observations.insert(observation);
    }
    Ok(())
}

fn data_read_semantic_conditions(
    data: &mut CoverageData,
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    let condition_table: Option<String> = connection
        .query_row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vfy_condition'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if condition_table.is_some() {
        let mut next_indices = std::collections::BTreeMap::<String, usize>::new();
        let mut statement = connection.prepare(
            "SELECT decision, index_no, start_line, start_col, end_line, end_col \
             FROM vfy_condition ORDER BY decision, index_no",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i32>(2)?,
                row.get::<_, i32>(3)?,
                row.get::<_, i32>(4)?,
                row.get::<_, i32>(5)?,
            ))
        })?;
        for row in rows {
            let (decision, index, start_line, start_col, end_line, end_col) = row?;
            let index = usize::try_from(index).map_err(|_| rusqlite::Error::InvalidQuery)?;
            let next = next_indices.entry(decision.clone()).or_default();
            if index != *next {
                return Err(corruption(
                    &decision,
                    "condition indices are not contiguous",
                ));
            }
            *next += 1;
            let Some(definition) = data.semantic.definitions.get_mut(&decision) else {
                return Err(rusqlite::Error::InvalidQuery);
            };
            if index >= definition.logic.condition_count() {
                return Err(rusqlite::Error::InvalidQuery);
            }
            if definition.condition_lines[index] != start_line {
                return Err(corruption(
                    &decision,
                    "condition line disagrees with its range",
                ));
            }
            definition.condition_ranges[index] = (start_line, start_col, end_line, end_col);
        }
        for (key, definition) in &data.semantic.definitions {
            if next_indices.get(key).copied().unwrap_or(0) != definition.logic.condition_count() {
                return Err(corruption(key, "condition rows have inconsistent arity"));
            }
        }
    }
    Ok(())
}
