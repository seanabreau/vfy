use std::collections::{BTreeMap, BTreeSet};

use rusqlite::Connection;
use tempfile::NamedTempFile;

use crate::semantic::{DecisionDefinition, Logic, SemanticData};

use super::{CoverageData, LineShift};

#[test]
fn adding_file_tracers_rejects_the_whole_conflicting_batch() {
    let mut coverage = CoverageData::from_trace_records(
        false,
        vec![("ctx".to_owned(), "module.py".to_owned(), vec![1])],
        Vec::new(),
        vec![("module.py".to_owned(), "observed.Plugin".to_owned())],
        SemanticData::default(),
    );
    let state = |data: &CoverageData| {
        (
            data.branch_mode(),
            data.measured_files(),
            data.tracer_records(),
            data.line_records(),
            data.arc_records(),
        )
    };
    let before = state(&coverage);
    for tracers in [
        vec![
            ("virtual.py".to_owned(), "first.Plugin".to_owned()),
            ("virtual.py".to_owned(), "second.Plugin".to_owned()),
        ],
        vec![("module.py".to_owned(), "discovered.Plugin".to_owned())],
    ] {
        assert_eq!(
            coverage.add_file_tracers(tracers),
            Err("cannot combine conflicting file tracers")
        );
        assert_eq!(state(&coverage), before);
    }
}

#[test]
fn empty_tracer_does_not_block_a_later_plugin() {
    let mut coverage = CoverageData::empty();
    coverage
        .add_file_tracers(vec![("source.py".into(), String::new())])
        .expect("empty tracer is accepted");
    coverage
        .add_file_tracers(vec![("source.py".into(), "plugin".into())])
        .expect("plugin can replace an empty tracer");
    assert_eq!(
        coverage.tracer_records(),
        vec![("source.py".into(), "plugin".into())]
    );
}

#[test]
fn merge_rejects_any_conflicting_hash_on_a_shared_semantic_path_atomically() {
    let mut existing_semantic = SemanticData::default();
    for (key, hash) in [("first", "revision-a"), ("second", "revision-b")] {
        existing_semantic
            .insert_definition(
                DecisionDefinition::validated(
                    key.to_owned(),
                    "source.py".to_owned(),
                    1,
                    1,
                    "if".to_owned(),
                    hash.to_owned(),
                    "value".to_owned(),
                    Logic::Condition(0),
                    vec![1],
                    vec![(1, 0, 1, 5)],
                )
                .expect("valid existing semantic definition"),
            )
            .expect("distinct existing semantic key");
    }
    let mut incoming_semantic = SemanticData::default();
    incoming_semantic
        .insert_definition(
            DecisionDefinition::validated(
                "first".to_owned(),
                "source.py".to_owned(),
                1,
                1,
                "if".to_owned(),
                "revision-a".to_owned(),
                "value".to_owned(),
                Logic::Condition(0),
                vec![1],
                vec![(1, 0, 1, 5)],
            )
            .expect("valid incoming semantic definition"),
        )
        .expect("distinct incoming semantic key");
    let mut existing = CoverageData::from_trace_records(
        true,
        Vec::new(),
        vec![("ctx".to_owned(), "source.py".to_owned(), vec![(1, 2)])],
        Vec::new(),
        existing_semantic,
    );
    let incoming = CoverageData::from_trace_records(
        true,
        Vec::new(),
        vec![("ctx".to_owned(), "source.py".to_owned(), vec![(2, 3)])],
        Vec::new(),
        incoming_semantic,
    );
    let before = existing.semantic_data().records();
    let before_arcs = existing.arc_records();
    let incoming_records = incoming.semantic_data().records();
    assert_eq!(
        existing.merge_from(incoming.clone(), false),
        Err("cannot combine semantic data from different source revisions")
    );
    assert_eq!(existing.semantic_data().records(), before);
    assert_eq!(existing.arc_records(), before_arcs);

    existing
        .merge_from(incoming, true)
        .expect("explicit replacement accepts the incoming revision");
    assert_eq!(existing.semantic_data().records(), incoming_records);
    assert_eq!(
        existing.arc_records(),
        vec![(
            "ctx".to_owned(),
            "source.py".to_owned(),
            vec![(1, 2), (2, 3)]
        )]
    );
}

#[test]
fn reads_schema_seven_line_bits_and_contexts() {
    let file = NamedTempFile::new().expect("temporary database");
    let connection = Connection::open(file.path()).expect("open database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);\
             INSERT INTO coverage_schema VALUES (7);\
             CREATE TABLE meta (key text, value text);\
             INSERT INTO meta VALUES ('has_arcs', '0');\
             CREATE TABLE file (id integer, path text);\
             INSERT INTO file VALUES (1, 'source.py');\
             CREATE TABLE context (id integer, context text);\
             INSERT INTO context VALUES (1, 'unit');\
             CREATE TABLE tracer (file_id integer, tracer text);\
             CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);\
             INSERT INTO line_bits VALUES (1, 1, X'16');",
        )
        .expect("create schema");
    drop(connection);

    let data = CoverageData::read_path(file.path()).expect("read coverage data");
    assert_eq!(data.measured_files(), vec!["source.py"]);
    assert_eq!(
        data.line_records(),
        vec![("unit".to_owned(), "source.py".to_owned(), vec![1, 2, 4])]
    );
}

#[test]
fn rejects_non_schema_seven_data() {
    let file = NamedTempFile::new().expect("temporary database");
    let connection = Connection::open(file.path()).expect("open database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);\
             INSERT INTO coverage_schema VALUES (6);",
        )
        .expect("create schema");
    drop(connection);

    assert!(CoverageData::read_path(file.path()).is_err());
}

/// Return whether every file partition of each context holds one shared name.
fn context_names_are_shared(data: &CoverageData) -> bool {
    let mut first: BTreeMap<&str, &super::Context> = BTreeMap::new();
    data.lines
        .values()
        .flat_map(BTreeMap::keys)
        .chain(data.arcs.values().flat_map(BTreeMap::keys))
        .all(|name| std::sync::Arc::ptr_eq(first.entry(name).or_insert(name), name))
}

#[test]
fn each_context_name_is_stored_once_across_file_partitions() {
    let directory = tempfile::tempdir().expect("temporary data directory");
    for branch_mode in [false, true] {
        let record = |context: &str, path: &str| (context.to_owned(), path.to_owned());
        let measured = [
            record("tests/test_a.py::test_a|run", "a.py"),
            record("tests/test_a.py::test_a|run", "b.py"),
            record("tests/test_b.py::test_b|run", "a.py"),
            record("tests/test_b.py::test_b|run", "b.py"),
        ];
        let lines = measured
            .iter()
            .map(|(context, path)| (context.clone(), path.clone(), vec![1, 2]))
            .collect();
        let arcs = measured
            .iter()
            .map(|(context, path)| (context.clone(), path.clone(), vec![(-1, 1), (1, -1)]))
            .collect();
        let (lines, arcs) = if branch_mode {
            (Vec::new(), arcs)
        } else {
            (lines, Vec::new())
        };
        let traced = CoverageData::from_trace_records(
            branch_mode,
            lines,
            arcs,
            Vec::new(),
            SemanticData::default(),
        );
        let path = directory
            .path()
            .join(format!("branch-{branch_mode}.coverage"));
        traced.write_path(&path).expect("write coverage data");
        let stored = CoverageData::read_path(&path).expect("read coverage data");
        let materialized = CoverageData::from_materialized_records(
            branch_mode,
            stored.report_inputs(),
            BTreeMap::new(),
            BTreeMap::new(),
            None,
        )
        .expect("materialized records");

        for data in [&traced, &stored, &materialized] {
            assert_eq!(data.context_path_identities().count(), 4);
            assert!(context_names_are_shared(data));
            assert_eq!(data.line_records(), traced.line_records());
            assert_eq!(data.arc_records(), traced.arc_records());
        }
    }
}

#[test]
fn rust_write_read_round_trip_preserves_measurements_and_tracers() {
    let directory = tempfile::tempdir().expect("temporary data directory");
    let path = directory.path().join("nested/coverage.sqlite");
    let mut original = CoverageData::from_trace_records(
        true,
        vec![
            (
                "context-a".to_owned(),
                "source.py".to_owned(),
                vec![1, 4, 4],
            ),
            ("context-b".to_owned(), "source.py".to_owned(), vec![9]),
        ],
        vec![(
            "context-a".to_owned(),
            "source.py".to_owned(),
            vec![(-1, 1), (1, 4), (4, -1)],
        )],
        vec![("source.py".to_owned(), "plugin.FileTracer".to_owned())],
        SemanticData::default(),
    );
    original
        .metadata
        .insert("has_arcs".to_owned(), "1".to_owned());
    original.write_path(&path).expect("write Rust data");
    let connection = Connection::open(&path).expect("open written database");
    let line_rows: i64 = connection
        .query_row("SELECT count(*) FROM line_bits", [], |row| row.get(0))
        .expect("count line rows");
    let arc_rows: i64 = connection
        .query_row("SELECT count(*) FROM arc", [], |row| row.get(0))
        .expect("count arc rows");
    assert_eq!((line_rows, arc_rows), (0, 3));
    let loaded = CoverageData::read_path(&path).expect("read Rust data");
    assert!(loaded.branch_mode());
    assert_eq!(loaded.measured_files(), vec!["source.py".to_owned()]);
    assert!(loaded.line_records().is_empty());
    assert_eq!(loaded.lines_for("source.py"), Some(vec![1, 4]));
    assert_eq!(
        loaded
            .arcs
            .values()
            .flat_map(|contexts| contexts.values())
            .flat_map(crate::evidence::SparseArcs::iter)
            .flat_map(|(start, end)| [start, end])
            .filter(|line| *line > 0)
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([1, 4])
    );
    assert_eq!(loaded.arcs, original.arcs);
    assert_eq!(
        loaded.tracers,
        [("source.py".to_owned(), "plugin.FileTracer".to_owned())]
            .into_iter()
            .collect()
    );
}

#[test]
fn rust_line_mode_round_trip_preserves_context_partitioning() {
    let directory = tempfile::tempdir().expect("temporary data directory");
    let path = directory.path().join("coverage.sqlite");
    let original = CoverageData::from_trace_records(
        false,
        vec![
            (
                "context-b".to_owned(),
                "source.py".to_owned(),
                vec![9, 3, 3],
            ),
            ("context-a".to_owned(), "source.py".to_owned(), vec![1]),
        ],
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    );
    original.write_path(&path).expect("write Rust data");
    let loaded = CoverageData::read_path(&path).expect("read Rust data");
    assert!(!loaded.branch_mode());
    assert_eq!(loaded.line_records(), original.line_records());
    assert_eq!(
        loaded
            .line_records()
            .into_iter()
            .flat_map(|(_, _, lines)| lines)
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([1, 3, 9])
    );
}

#[test]
fn line_read_shares_repeated_sets_and_preserves_unique_sparse_partitions() {
    let connection = Connection::open_in_memory().expect("in-memory database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);
             INSERT INTO coverage_schema VALUES (7);
             CREATE TABLE meta (key text, value text);
             INSERT INTO meta VALUES ('has_arcs', '0');
             CREATE TABLE file (id integer, path text);
             INSERT INTO file VALUES (1, 'a.py'), (2, 'b.py');
             CREATE TABLE context (id integer, context text);
             INSERT INTO context VALUES (1, 'first'), (2, 'padded'), (3, 'other-file'),
                                        (4, 'unique'), (5, 'sparse'), (6, 'empty'),
                                        (7, 'other-file-repeat');
             CREATE TABLE tracer (file_id integer, tracer text);
             CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);
             INSERT INTO line_bits VALUES (1, 1, X'0208'), (1, 2, X'020800'),
                                          (2, 3, X'0208'), (2, 4, X'0408'),
                                          (2, 5, X'000000000000000001'), (1, 6, X''),
                                          (2, 7, X'020800');",
        )
        .expect("create schema with repeated and sparse line sets");
    let mut data = CoverageData::read_connection(&connection).expect("read line data");
    let first = &data.lines["a.py"]["first"];
    assert!(first.shares_allocation(&data.lines["a.py"]["padded"]));
    let other_file = &data.lines["b.py"]["other-file"];
    assert!(other_file.shares_allocation(&data.lines["b.py"]["other-file-repeat"]));
    assert_eq!(other_file.sorted(), first.sorted());
    assert!(!first.shares_allocation(other_file));
    assert!(!first.shares_allocation(&data.lines["b.py"]["unique"]));
    assert_eq!(data.lines["b.py"]["sparse"].sorted(), [64]);
    assert!(data.lines["a.py"]["empty"].sorted().is_empty());

    let directory = tempfile::tempdir().expect("temporary data directory");
    let path = directory.path().join("coverage.sqlite");
    data.write_path(&path).expect("write shared data");
    let reloaded = CoverageData::read_path(&path).expect("read written data");
    assert_eq!(reloaded.line_records(), data.line_records());

    data.lines
        .get_mut("a.py")
        .expect("measured file")
        .get_mut("first")
        .expect("measured context")
        .insert(99);
    assert_eq!(data.lines["a.py"]["first"].sorted(), [1, 11, 99]);
    assert_eq!(data.lines["a.py"]["padded"].sorted(), [1, 11]);
}

#[test]
fn arc_rows_round_trip_across_insert_batch_boundaries() {
    let directory = tempfile::tempdir().expect("temporary data directory");
    let path = directory.path().join("coverage.sqlite");
    let batch = i32::try_from(super::ARC_INSERT_BATCH).expect("batch size fits i32");
    // Two full batches span a context boundary; the remainder uses single rows.
    let arcs = |count: i32| (1..=count).map(|line| (line, line + 1)).collect::<Vec<_>>();
    let mut original = CoverageData::from_trace_records(
        true,
        vec![],
        vec![
            ("context-a".to_owned(), "a.py".to_owned(), arcs(batch + 3)),
            ("context-b".to_owned(), "a.py".to_owned(), arcs(batch - 1)),
            ("context-a".to_owned(), "b.py".to_owned(), arcs(5)),
        ],
        Vec::new(),
        SemanticData::default(),
    );
    original
        .metadata
        .insert("has_arcs".to_owned(), "1".to_owned());
    original.write_path(&path).expect("write Rust data");
    let loaded = CoverageData::read_path(&path).expect("read Rust data");
    assert_eq!(loaded.arcs, original.arcs);
    let rows: i64 = Connection::open(&path)
        .expect("open written data")
        .query_row("SELECT COUNT(*) FROM arc", [], |row| row.get(0))
        .expect("count arc rows");
    assert_eq!(rows, i64::from(2 * batch + 7));
}

#[test]
fn read_rejects_measurements_that_reference_unknown_ids() {
    let file = NamedTempFile::new().expect("temporary database");
    let connection = Connection::open(file.path()).expect("open database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);
             INSERT INTO coverage_schema VALUES (7);
             CREATE TABLE meta (key text, value text);
             CREATE TABLE file (id integer, path text);
             CREATE TABLE context (id integer, context text);
             CREATE TABLE tracer (file_id integer, tracer text);
             CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);
             INSERT INTO line_bits VALUES (99, 99, X'01');",
        )
        .expect("create malformed schema");
    drop(connection);
    assert!(CoverageData::read_path(file.path()).is_err());
}

#[test]
fn schema_mode_validation_applies_to_reads_and_transport() {
    for (mode, insert) in [
        (Some("0"), "INSERT INTO arc VALUES (1, 1, 1, 2)"),
        (Some("1"), "INSERT INTO line_bits VALUES (1, 1, X'02')"),
        (None, "INSERT INTO line_bits VALUES (1, 1, X'02')"),
        (Some("invalid"), "SELECT 1"),
    ] {
        let mut connection = Connection::open_in_memory().expect("in-memory database");
        CoverageData::new(false)
            .write_connection(&mut connection)
            .expect("write schema");
        connection
            .execute("DELETE FROM meta WHERE key = 'has_arcs'", [])
            .expect("clear mode");
        if let Some(mode) = mode {
            connection
                .execute("INSERT INTO meta VALUES ('has_arcs', ?)", [mode])
                .expect("set mode");
        }
        connection
            .execute_batch(insert)
            .expect("insert inconsistent data");
        assert!(CoverageData::read_connection(&connection).is_err());
        let mut payload = b"vfy-data\0".to_vec();
        payload.extend_from_slice(&connection.serialize("main").expect("serialize fixture"));
        assert!(CoverageData::deserialize(&payload).is_err());
    }
    let mut empty = Connection::open_in_memory().expect("in-memory database");
    CoverageData::empty()
        .write_connection(&mut empty)
        .expect("write empty schema");
    assert!(CoverageData::read_connection(&empty).is_ok());
}

#[test]
fn branch_read_unions_rows_when_distinct_ids_share_a_path_and_context() {
    let connection = Connection::open_in_memory().expect("in-memory database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);
             INSERT INTO coverage_schema VALUES (7);
             CREATE TABLE meta (key text, value text);
             INSERT INTO meta VALUES ('has_arcs', '1');
             CREATE TABLE file (id integer, path text);
             INSERT INTO file VALUES (1, 'source.py'), (2, 'source.py');
             CREATE TABLE context (id integer, context text);
             INSERT INTO context VALUES (1, 'test|run'), (2, 'test|run');
             CREATE TABLE tracer (file_id integer, tracer text);
             CREATE TABLE arc (file_id integer, context_id integer, fromno integer, tono integer);
             INSERT INTO arc VALUES (1, 1, 1, 2), (2, 2, 2, 3);",
        )
        .expect("create schema with duplicate identities");
    let data = CoverageData::read_connection(&connection).expect("read arc data");
    assert_eq!(
        data.arc_records(),
        vec![(
            "test|run".to_owned(),
            "source.py".to_owned(),
            vec![(1, 2), (2, 3)]
        )]
    );
}

#[test]
fn line_read_unions_rows_when_distinct_ids_share_a_path_and_context() {
    let connection = Connection::open_in_memory().expect("in-memory database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);
             INSERT INTO coverage_schema VALUES (7);
             CREATE TABLE meta (key text, value text);
             INSERT INTO meta VALUES ('has_arcs', '0');
             CREATE TABLE file (id integer, path text);
             INSERT INTO file VALUES (1, 'source.py'), (2, 'source.py');
             CREATE TABLE context (id integer, context text);
             INSERT INTO context VALUES (1, 'test|run'), (2, 'test|run'), (3, 'empty');
             CREATE TABLE tracer (file_id integer, tracer text);
             CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);
             INSERT INTO line_bits VALUES (1, 1, X'02'), (2, 2, X'04'), (1, 3, X'');",
        )
        .expect("create schema with duplicate identities");
    let data = CoverageData::read_connection(&connection).expect("read line data");
    assert_eq!(
        data.line_records(),
        vec![
            ("empty".to_owned(), "source.py".to_owned(), Vec::new()),
            ("test|run".to_owned(), "source.py".to_owned(), vec![1, 2],),
        ]
    );
}

#[test]
fn measurement_records_remain_ordered_by_context_then_path() {
    let data = CoverageData::from_trace_records(
        true,
        vec![
            ("z-context".to_owned(), "a.py".to_owned(), vec![3]),
            ("a-context".to_owned(), "z.py".to_owned(), vec![1]),
        ],
        vec![
            ("z-context".to_owned(), "a.py".to_owned(), vec![(3, 4)]),
            ("a-context".to_owned(), "z.py".to_owned(), vec![(1, 2)]),
        ],
        Vec::new(),
        SemanticData::default(),
    );

    assert_eq!(
        data.line_records(),
        vec![
            ("a-context".to_owned(), "z.py".to_owned(), vec![1]),
            ("z-context".to_owned(), "a.py".to_owned(), vec![3]),
        ]
    );
    assert_eq!(
        data.arc_records(),
        vec![
            ("a-context".to_owned(), "z.py".to_owned(), vec![(1, 2)],),
            ("z-context".to_owned(), "a.py".to_owned(), vec![(3, 4)],),
        ]
    );
}

#[test]
fn branch_read_rejects_an_arc_with_an_unknown_context() {
    let connection = Connection::open_in_memory().expect("in-memory database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);
             INSERT INTO coverage_schema VALUES (7);
             CREATE TABLE meta (key text, value text);
             INSERT INTO meta VALUES ('has_arcs', '1');
             CREATE TABLE file (id integer, path text);
             INSERT INTO file VALUES (1, 'source.py');
             CREATE TABLE context (id integer, context text);
             CREATE TABLE tracer (file_id integer, tracer text);
             CREATE TABLE arc (file_id integer, context_id integer, fromno integer, tono integer);
             INSERT INTO arc VALUES (1, 99, 1, 2);",
        )
        .expect("create schema with dangling context");
    assert!(CoverageData::read_connection(&connection).is_err());
}

#[test]
fn read_rejects_a_tracer_that_references_an_unknown_file() {
    let file = NamedTempFile::new().expect("temporary database");
    let connection = Connection::open(file.path()).expect("open database");
    connection
        .execute_batch(
            "CREATE TABLE coverage_schema (version integer);
             INSERT INTO coverage_schema VALUES (7);
             CREATE TABLE meta (key text, value text);
             CREATE TABLE file (id integer, path text);
             CREATE TABLE context (id integer, context text);
             CREATE TABLE tracer (file_id integer, tracer text);
             CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);
             INSERT INTO tracer VALUES (99, 'plugin.name');",
        )
        .expect("create malformed schema");
    drop(connection);
    let Err(error) = CoverageData::read_path(file.path()) else {
        panic!("accepted dangling tracer");
    };
    assert!(
        error
            .to_string()
            .contains("tracer references absent file id 99")
    );
}

#[test]
fn report_inputs_include_arc_endpoints_but_not_negative_sentinels() {
    let data = CoverageData::from_trace_records(
        true,
        Vec::new(),
        vec![(
            "ctx".to_owned(),
            "source.py".to_owned(),
            vec![(-7, 3), (3, -7)],
        )],
        Vec::new(),
        SemanticData::default(),
    );
    assert_eq!(
        data.report_inputs(),
        vec![(
            "source.py".to_owned(),
            vec![3],
            vec![(-7, 3), (3, -7)],
            vec![("ctx".to_owned(), vec![3])],
            vec![("ctx".to_owned(), vec![(-7, 3), (3, -7)])],
        )]
    );
}

#[test]
fn report_input_filter_omits_unselected_context_evidence_before_materialization() {
    let data = CoverageData::from_trace_records(
        true,
        vec![
            ("selected".to_owned(), "source.py".to_owned(), vec![2]),
            ("ignored".to_owned(), "source.py".to_owned(), vec![8]),
        ],
        vec![
            ("selected".to_owned(), "source.py".to_owned(), vec![(2, 3)]),
            ("ignored".to_owned(), "source.py".to_owned(), vec![(8, 9)]),
        ],
        Vec::new(),
        SemanticData::default(),
    );

    let inputs = data
        .report_inputs_filtered(
            |_| true,
            |_, context| Ok::<_, std::convert::Infallible>(context == "selected"),
        )
        .unwrap_or_else(|error| match error {});
    assert_eq!(
        inputs,
        vec![(
            "source.py".to_owned(),
            vec![2, 3],
            vec![(2, 3)],
            vec![("selected".to_owned(), vec![2, 3])],
            vec![("selected".to_owned(), vec![(2, 3)])],
        )]
    );
}

#[test]
fn cloned_evidence_is_isolated_by_copy_on_write() {
    let original_lines = CoverageData::from_trace_records(
        false,
        vec![("ctx".to_owned(), "source.py".to_owned(), vec![1, 65])],
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    );
    let mut changed_lines = original_lines.clone();
    changed_lines.add_lines("ctx", "source.py", vec![2, 130]);
    assert_eq!(original_lines.line_records()[0].2, vec![1, 65]);
    assert_eq!(changed_lines.line_records()[0].2, vec![1, 2, 65, 130]);

    let original_arcs = CoverageData::from_trace_records(
        true,
        Vec::new(),
        vec![(
            "ctx".to_owned(),
            "source.py".to_owned(),
            vec![(-1, 1), (1, -1)],
        )],
        Vec::new(),
        SemanticData::default(),
    );
    let mut changed_arcs = original_arcs.clone();
    changed_arcs.add_arcs("ctx", "source.py", vec![(1, 2), (2, -1)]);
    assert_eq!(original_arcs.arc_records()[0].2, vec![(-1, 1), (1, -1)]);
    assert_eq!(
        changed_arcs.arc_records()[0].2,
        vec![(-1, 1), (1, -1), (1, 2), (2, -1)]
    );
}

#[test]
fn replay_remaps_global_helper_evidence_only_on_scoped_paths() {
    let path = "/project/src.py".to_owned();
    for (old_count, new_count, helper_definition, helper_line) in [(1, 2, 6, 7), (2, 1, 4, 5)] {
        for branch_mode in [false, true] {
            let evidence = |lines: Vec<i32>, arcs: Vec<(i32, i32)>| {
                CoverageData::from_trace_records(
                    branch_mode,
                    if branch_mode {
                        Vec::new()
                    } else {
                        vec![(String::new(), path.clone(), lines)]
                    },
                    if branch_mode {
                        vec![(String::new(), path.clone(), arcs)]
                    } else {
                        Vec::new()
                    },
                    Vec::new(),
                    SemanticData::default(),
                )
            };
            let baseline = evidence(
                vec![1, 5, 6],
                vec![(-1, 1), (1, 5), (5, -1), (-5, 6), (6, -5)],
            );
            // Selected collection imports the module, but never calls its helper.
            let fresh = evidence(
                vec![1, helper_definition],
                vec![(-1, 1), (1, helper_definition), (helper_definition, -1)],
            );
            let expected = evidence(
                vec![1, helper_definition, helper_line],
                vec![
                    (-1, 1),
                    (1, helper_definition),
                    (helper_definition, -1),
                    (-helper_definition, helper_line),
                    (helper_line, -helper_definition),
                ],
            );
            let shifts = BTreeMap::from([(
                path.clone(),
                vec![LineShift {
                    old_start: 2,
                    old_count,
                    new_count,
                }],
            )]);
            let replayed = baseline
                .clone()
                .replay_merge(
                    fresh.clone(),
                    Vec::new(),
                    std::slice::from_ref(&path),
                    &shifts,
                )
                .expect("replay global helper after a scoped body edit");
            assert_eq!(replayed.line_records(), expected.line_records());
            assert_eq!(replayed.arc_records(), expected.arc_records());

            let unscoped = baseline
                .replay_merge(
                    fresh.clone(),
                    Vec::new(),
                    std::slice::from_ref(&path),
                    &BTreeMap::new(),
                )
                .expect("replace global evidence after an unscoped change");
            assert_eq!(unscoped.line_records(), fresh.line_records());
            assert_eq!(unscoped.arc_records(), fresh.arc_records());
        }
    }
}

#[test]
fn replay_keeps_unselected_tests_on_scoped_paths_at_shifted_lines() {
    let path = "/project/src.py".to_owned();
    let arcs = |records: &[(&str, &[(i32, i32)])]| {
        CoverageData::from_trace_records(
            true,
            Vec::new(),
            records
                .iter()
                .map(|(context, arcs)| ((*context).to_owned(), path.clone(), arcs.to_vec()))
                .collect(),
            Vec::new(),
            SemanticData::default(),
        )
    };
    let baseline = arcs(&[
        ("", &[(-1, 1), (1, 5), (5, -1)]),
        ("tests/test_src.py::test_area|run", &[(-1, 2), (2, -1)]),
        (
            "tests/test_src.py::test_perimeter|run",
            &[(-5, 6), (6, 7), (7, -5)],
        ),
    ]);
    let fresh = arcs(&[
        ("", &[(-1, 1), (1, 6), (6, -1)]),
        (
            "tests/test_src.py::test_area|run",
            &[(-1, 2), (2, 3), (3, -1)],
        ),
    ]);
    let shifts = vec![LineShift {
        old_start: 2,
        old_count: 1,
        new_count: 2,
    }];
    let replayed = baseline
        .clone()
        .replay_merge(
            fresh.clone(),
            vec!["tests/test_src.py::test_area".to_owned()],
            std::slice::from_ref(&path),
            &BTreeMap::from([(path.clone(), shifts)]),
        )
        .expect("replay scoped path");
    let mut expected = fresh.arc_records();
    expected.push((
        "tests/test_src.py::test_perimeter|run".to_owned(),
        path.clone(),
        vec![(-6, 7), (7, 8), (8, -6)],
    ));
    expected.sort();
    assert_eq!(replayed.arc_records(), expected);

    let overlapping = vec![
        LineShift {
            old_start: 2,
            old_count: 2,
            new_count: 1,
        },
        LineShift {
            old_start: 3,
            old_count: 1,
            new_count: 1,
        },
    ];
    assert!(
        baseline
            .replay_merge(
                fresh,
                Vec::new(),
                std::slice::from_ref(&path),
                &BTreeMap::from([(path.clone(), overlapping)]),
            )
            .is_err()
    );
}

fn selection_fixture() -> CoverageData {
    let mut data = CoverageData::from_trace_records(
        true,
        vec![("ctx".to_owned(), "lines.py".to_owned(), vec![1, 2])],
        vec![
            ("ctx".to_owned(), "keep.py".to_owned(), vec![(1, 2)]),
            ("other".to_owned(), "drop.py".to_owned(), vec![(3, 4)]),
            ("other".to_owned(), "keep.py".to_owned(), vec![(2, -1)]),
        ],
        vec![("keep.py".to_owned(), "plugin.Tracer".to_owned())],
        SemanticData::default(),
    );
    data.touch_file("touched.py");
    data.set_metadata("has_arcs", "1");
    data
}

#[test]
fn moving_file_selection_matches_copying_selection() {
    let data = selection_fixture();
    let paths = vec![
        "touched.py".to_owned(),
        "keep.py".to_owned(),
        "missing.py".to_owned(),
    ];

    let copied = data.select_files(paths.clone());
    let moved = data.into_selected_files(paths);

    assert_eq!(
        moved.serialize().expect("moved payload"),
        copied.serialize().expect("copied payload")
    );
    assert_eq!(moved.measured_files(), vec!["keep.py", "touched.py"]);
}

#[test]
fn merge_yields_the_incoming_store_only_for_an_empty_compatible_destination() {
    let incoming = selection_fixture();
    let mut destination = CoverageData::empty();
    assert!(destination.merge_yields(&incoming));
    destination
        .merge_from(incoming.clone(), true)
        .expect("merge into empty store");
    assert_eq!(
        destination.serialize().expect("merged payload"),
        incoming.serialize().expect("incoming payload")
    );

    let mut matching = CoverageData::empty();
    matching.set_metadata("has_arcs", "1");
    assert!(matching.merge_yields(&incoming));

    let mut conflicting = CoverageData::empty();
    conflicting.set_metadata("note", "kept");
    assert!(!conflicting.merge_yields(&incoming));
    let mut measured = CoverageData::new(true);
    measured.touch_file("other.py");
    assert!(!measured.merge_yields(&incoming));
    let mut line_mode = CoverageData::empty();
    line_mode.set_metadata("has_arcs", "0");
    assert!(!line_mode.merge_yields(&incoming));
    assert!(!CoverageData::empty().merge_yields(&CoverageData::empty()));
}

#[test]
fn write_time_metadata_overrides_the_file_without_changing_the_store() {
    let data = selection_fixture();
    let output = NamedTempFile::new().expect("output file").into_temp_path();
    let overrides = BTreeMap::from([
        ("has_arcs".to_owned(), "1".to_owned()),
        ("when".to_owned(), "now".to_owned()),
    ]);

    data.write_path_with_metadata(&output, &overrides)
        .expect("write with metadata");

    let written = CoverageData::read_path(&output).expect("read written data");
    assert_eq!(
        written.metadata_records(),
        vec![
            ("has_arcs".to_owned(), "1".to_owned()),
            ("when".to_owned(), "now".to_owned()),
        ]
    );
    assert_eq!(
        data.metadata_records(),
        vec![("has_arcs".to_owned(), "1".to_owned())]
    );
    assert_eq!(written.arc_records(), data.arc_records());
}

fn single_family_coverage(source: &str, branch: bool, line: i32) -> CoverageData {
    let (definitions, paths) = crate::semantic::analyze_semantic_source_data("module.py", source)
        .expect("valid semantic test source");
    let mut semantic = SemanticData::default();
    for definition in definitions {
        let key = definition.key().to_owned();
        semantic
            .insert_definition(definition)
            .expect("discovered Boolean definition");
        semantic
            .insert_observation("test".to_owned(), key, "1".to_owned(), true)
            .expect("Boolean observation");
    }
    for definition in paths {
        let key = definition.key().to_owned();
        semantic
            .insert_path_definition(definition)
            .expect("discovered path definition");
        semantic
            .insert_path_observation("test".to_owned(), key, 0)
            .expect("path observation");
    }
    CoverageData::from_trace_records(
        branch,
        vec![("test".to_owned(), "module.py".to_owned(), vec![line])],
        if branch {
            vec![(
                "test".to_owned(),
                "module.py".to_owned(),
                vec![(-1, line), (line, -1)],
            )]
        } else {
            Vec::new()
        },
        Vec::new(),
        semantic,
    )
}

#[test]
fn cross_family_source_revisions_reject_atomically_and_replace_both_families() {
    for branch in [false, true] {
        let boolean = single_family_coverage("left = True\nif left:\n    pass\n", branch, 2);
        let path = single_family_coverage("left = True\nvalue = left or False\n", branch, 3);
        assert_eq!(boolean.semantic.definitions.len(), 1);
        assert!(boolean.semantic.path_definitions.is_empty());
        assert!(path.semantic.definitions.is_empty());
        assert_eq!(path.semantic.path_definitions.len(), 1);
        for (mut existing, incoming) in [(boolean.clone(), path.clone()), (path, boolean)] {
            let before = existing.serialize().expect("original complete state");
            assert_eq!(
                existing.merge_from(incoming.clone(), false),
                Err("cannot combine semantic data from different source revisions")
            );
            assert_eq!(
                existing.serialize().expect("rejected complete state"),
                before
            );
            existing
                .merge_from(incoming.clone(), true)
                .expect("replace changed source");
            assert_eq!(existing.semantic.records(), incoming.semantic.records());
            assert_eq!(
                existing.semantic.path_records(),
                incoming.semantic.path_records()
            );
            assert_eq!(
                existing.semantic.observation_records(),
                incoming.semantic.observation_records()
            );
            assert_eq!(
                existing.semantic.path_observation_records(),
                incoming.semantic.path_observation_records()
            );
            assert_eq!(existing.lines_for("module.py"), Some(vec![2, 3]));
        }
    }
}

#[test]
fn writes_persist_authoritative_mode_without_metadata_and_match_transport() {
    let mut lines = CoverageData::new(false);
    lines.add_lines("test", "module.py", vec![1]);
    let mut arcs = CoverageData::new(true);
    arcs.add_arcs("test", "module.py", vec![(-1, 1), (1, -1)]);
    for data in [CoverageData::empty(), lines, CoverageData::new(true), arcs] {
        let expected_known = data.mode_is_known();
        let mut connection = Connection::open_in_memory().expect("in-memory database");
        data.write_connection(&mut connection)
            .expect("write connection");
        let from_connection = CoverageData::read_connection(&connection).expect("read connection");
        let output = NamedTempFile::new().expect("output file").into_temp_path();
        data.write_path(&output).expect("write path");
        let from_path = CoverageData::read_path(&output).expect("read path");
        let from_transport =
            CoverageData::deserialize(&data.serialize().expect("serialize")).expect("deserialize");
        for restored in [from_connection, from_path, from_transport] {
            assert_eq!(restored.mode_is_known(), expected_known);
            assert_eq!(restored.branch_mode(), data.branch_mode());
            assert_eq!(restored.arc_records(), data.arc_records());
            assert_eq!(restored.line_records(), data.line_records());
        }
        assert!(data.metadata_records().is_empty());
    }
}

#[test]
fn conflicting_mode_override_preserves_existing_destination() {
    let output = NamedTempFile::new()
        .expect("destination file")
        .into_temp_path();
    let mut original = CoverageData::new(false);
    original.add_lines("original", "saved.py", vec![7]);
    original.write_path(&output).expect("original destination");
    let before = std::fs::read(&output).expect("original bytes");
    let mut branch = CoverageData::new(true);
    branch.add_arcs("test", "module.py", vec![(-1, 1), (1, -1)]);
    let overrides = BTreeMap::from([("has_arcs".to_owned(), "0".to_owned())]);
    let error = branch
        .write_path_with_metadata(&output, &overrides)
        .expect_err("conflicting collection mode");
    assert!(
        error
            .to_string()
            .contains("has_arcs metadata conflicts with collection mode")
    );
    assert_eq!(std::fs::read(&output).expect("retained bytes"), before);
    assert_eq!(
        CoverageData::read_path(&output)
            .expect("valid destination")
            .line_records(),
        original.line_records()
    );
}

#[test]
fn incomplete_file_mapping_returns_error_for_touched_and_measured_files() {
    for branch in [false, true] {
        let mut data = CoverageData::new(branch);
        data.touch_file("touched.py");
        if branch {
            data.add_arcs("test", "module.py", vec![(-1, 1), (1, -1)]);
        } else {
            data.add_lines("test", "module.py", vec![1]);
        }
        for mapping in [
            BTreeMap::new(),
            BTreeMap::from([("module.py".to_owned(), "mapped.py".to_owned())]),
            BTreeMap::from([("touched.py".to_owned(), "mapped.py".to_owned())]),
        ] {
            assert!(matches!(
                data.clone().remapped_files(&mapping),
                Err("file mapping does not cover every measured file")
            ));
        }
    }
}

#[test]
fn cross_family_combine_conflict_preserves_destination_and_shards() {
    let temporary = tempfile::tempdir().expect("temporary combine directory");
    let output = temporary.path().join(".coverage");
    let first = temporary.path().join(".coverage.first");
    let second = temporary.path().join(".coverage.second");
    let boolean = single_family_coverage("left = True\nif left:\n    pass\n", false, 2);
    let path = single_family_coverage("left = True\nvalue = left or False\n", false, 3);
    boolean.write_path(&output).expect("original destination");
    boolean.write_path(&first).expect("Boolean shard");
    path.write_path(&second).expect("path shard");
    let before: Vec<_> = [&output, &first, &second]
        .into_iter()
        .map(|path| std::fs::read(path).expect("original database bytes"))
        .collect();
    for inputs in [[&first, &second], [&second, &first]] {
        let error = crate::data::combine_transaction(
            inputs
                .into_iter()
                .map(|path| path.to_string_lossy().into_owned())
                .collect(),
            &output,
            None,
            crate::data::CombineOptions::default(),
        )
        .err()
        .expect("cross-family source conflict");
        assert!(
            error.to_string().contains("different source revisions"),
            "{error}"
        );
        let after: Vec<_> = [&output, &first, &second]
            .into_iter()
            .map(|path| std::fs::read(path).expect("retained database bytes"))
            .collect();
        assert_eq!(after, before);
        CoverageData::read_path(&output).expect("original destination remains valid");
    }
}

#[test]
fn internally_mixed_source_revisions_reject_before_merge_or_transport() {
    let mut mixed = single_family_coverage("value = left or right\nif left:\n    pass\n", false, 2);
    mixed
        .semantic
        .path_definitions
        .values_mut()
        .next()
        .expect("path definition")
        .source_hash = "another revision".to_owned();
    for replace_changed in [false, true] {
        for mut destination in [
            CoverageData::empty(),
            single_family_coverage("if left:\n    pass\n", false, 1),
        ] {
            let before = destination.serialize().expect("original state");
            assert!(
                destination
                    .merge_from(mixed.clone(), replace_changed)
                    .is_err()
            );
            assert_eq!(destination.serialize().expect("retained state"), before);
        }
    }
    let payload = mixed.serialize().expect("encode malformed fixture");
    assert!(CoverageData::deserialize(&payload).is_err());
    assert!(
        CoverageData::from_materialized_records(
            false,
            mixed.report_inputs(),
            BTreeMap::new(),
            BTreeMap::new(),
            Some(&mixed),
        )
        .is_err()
    );
}

#[test]
fn replay_replaces_invalidated_contexts_and_changed_files() {
    let changed = "/project/changed.py".to_owned();
    let untouched = "/project/untouched.py".to_owned();
    let mut baseline = CoverageData::new(false);
    baseline.add_lines("tests/test_a.py::test_a|run", &changed, vec![1, 2]);
    baseline.add_lines("tests/test_b.py::test_b|run", &untouched, vec![1, 2]);
    let mut fresh = CoverageData::new(false);
    fresh.add_lines("tests/test_a.py::test_a|run", &changed, vec![1, 3]);

    let replayed = baseline
        .clone()
        .replay_merge(
            fresh,
            vec!["tests/test_a.py::test_a".to_owned()],
            std::slice::from_ref(&changed),
            &BTreeMap::new(),
        )
        .expect("replay invalidated test");

    assert_eq!(replayed.lines_for(&changed), Some(vec![1, 3]));
    assert_eq!(replayed.lines_for(&untouched), Some(vec![1, 2]));
    assert_eq!(
        replayed.line_records(),
        [
            (
                "tests/test_a.py::test_a|run".to_owned(),
                changed,
                vec![1, 3]
            ),
            (
                "tests/test_b.py::test_b|run".to_owned(),
                untouched,
                vec![1, 2]
            ),
        ]
    );
    assert_eq!(
        baseline
            .replay_merge(CoverageData::new(true), Vec::new(), &[], &BTreeMap::new())
            .err(),
        Some("cannot replay line and branch coverage together")
    );
}

#[test]
fn replay_drops_contexts_of_invalidated_tests_that_produce_no_fresh_evidence() {
    let changed = "/project/changed.py".to_owned();
    let directory = tempfile::tempdir().expect("temporary data directory");
    let mut baseline = CoverageData::new(false);
    baseline.add_lines("tests/test_a.py::test_a|run", &changed, vec![1, 2]);
    baseline.add_lines("tests/test_b.py::test_b|run", &changed, vec![1]);
    let stored = directory.path().join("baseline.coverage");
    baseline.write_path(&stored).expect("write baseline");
    let baseline = CoverageData::read_path(&stored).expect("read baseline");

    let replayed = baseline
        .replay_merge(
            CoverageData::new(false),
            vec!["tests/test_a.py::test_a".to_owned()],
            &[],
            &BTreeMap::new(),
        )
        .expect("replay removed test");

    assert_eq!(
        replayed.measured_contexts(),
        vec!["tests/test_b.py::test_b|run".to_owned()]
    );
}

#[test]
fn empty_arc_set_registers_no_file() {
    let mut data = CoverageData::new(true);
    data.add_arcs("", "empty.py", Vec::new());
    assert!(data.measured_files().is_empty());
    data.add_arcs("", "module.py", vec![(1, 2)]);
    assert_eq!(data.measured_files(), ["module.py"]);
}

#[test]
fn traced_line_and_arc_partitions_share_context_names() {
    let context = || super::Context::from("tests/test_a.py::test_a|run");
    let lines = crate::TraceLines(vec![
        (
            context(),
            super::Context::from("a.py"),
            crate::evidence::SparseLines::from_numbits(&[0b0000_0110]).expect("valid line bits"),
        ),
        (
            context(),
            super::Context::from("b.py"),
            crate::evidence::SparseLines::from_numbits(&[0b0000_0110]).expect("valid line bits"),
        ),
    ]);
    let arcs = vec![(
        "tests/test_a.py::test_a|run".to_owned(),
        "a.py".to_owned(),
        vec![(-1, 1), (1, -1)],
    )];
    let data = CoverageData::from_trace(true, lines, arcs, Vec::new(), SemanticData::default());
    assert!(context_names_are_shared(&data));
}
