use std::error::Error;
use std::path::Path;

use crate::semantic::SemanticData;

use super::{
    CombineOptions, CoverageData, PathAliasMode, PathAliases, combine_transaction,
    combine_transaction_with_cleanup, publish_transaction,
};

#[test]
fn repeated_shard_paths_are_combined_and_removed_once() {
    let directory = tempfile::tempdir().expect("temporary shard directory");
    let subdirectory = directory.path().join("subdirectory");
    std::fs::create_dir(&subdirectory).expect("create subdirectory");
    let output = directory.path().join(".coverage");
    let shard = directory.path().join("worker.coverage");
    CoverageData::from_trace_records(
        false,
        vec![("ctx".to_owned(), "module.py".to_owned(), vec![3])],
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    )
    .write_path(&shard)
    .expect("write shard");
    let alias = subdirectory.join("..").join("worker.coverage");
    let (_, count) = combine_transaction(
        vec![
            shard.to_string_lossy().into_owned(),
            alias.to_string_lossy().into_owned(),
        ],
        &output,
        None,
        CombineOptions::default(),
    )
    .expect("combine the shard once");
    assert_eq!(count, 1);
    assert!(!shard.exists());
    assert_eq!(
        CoverageData::read_path(&output)
            .expect("read output")
            .lines_for("module.py"),
        Some(vec![3])
    );
}

#[cfg(unix)]
#[test]
fn directory_discovery_combines_symlink_entries_without_removing_targets_or_output_alias() {
    use std::os::unix::fs::symlink;

    let temporary = tempfile::tempdir().expect("temporary shard directory");
    let directory = temporary.path().join("shards");
    std::fs::create_dir(&directory).expect("create shard directory");
    let target = temporary.path().join("target");
    let output = directory.join(".coverage");
    let first = directory.join(".coverage.first");
    let second = directory.join(".coverage.second");
    let output_alias = directory.join(".coverage.output");
    let dangling = directory.join(".coverage.dangling");
    let mut data = CoverageData::new(false);
    data.add_lines("", "example.py", vec![7]);
    data.write_path(&target).expect("write shard target");
    data.write_path(&output).expect("write output");
    let target_bytes = std::fs::read(&target).expect("read target");
    symlink(&target, &first).expect("link first shard");
    symlink(&target, &second).expect("link second shard");
    symlink(&output, &output_alias).expect("link output");
    symlink(temporary.path().join("missing"), &dangling).expect("link missing target");
    let (_, count) = combine_transaction(
        vec![directory.to_string_lossy().into_owned()],
        &output,
        None,
        CombineOptions::default(),
    )
    .expect("combine linked shards");
    assert_eq!(count, 2);
    assert!(!first.is_symlink());
    assert!(!second.is_symlink());
    assert!(output_alias.is_symlink());
    assert!(dangling.is_symlink());
    assert_eq!(
        std::fs::read(&target).expect("retained target"),
        target_bytes
    );
    assert_eq!(
        CoverageData::read_path(&output)
            .expect("read combined data")
            .lines_for("example.py"),
        Some(vec![7])
    );
}

#[test]
fn combine_uses_configured_filename_mode_without_coalescing_distinct_files() {
    let current = std::env::current_dir().expect("working directory");
    let temporary = tempfile::tempdir_in(&current).expect("temporary combined sources");
    let source = temporary.path().join("source/sample.py");
    let other = temporary.path().join("other/sample.py");
    std::fs::create_dir_all(source.parent().expect("source directory"))
        .expect("create source directory");
    std::fs::create_dir_all(other.parent().expect("other directory"))
        .expect("create other directory");
    std::fs::write(&source, "a = 1\nb = 2\n").expect("source file");
    std::fs::write(&other, "different = 3\n").expect("different same-basename file");
    let absolute = crate::path::glob_path(&source);
    let other_absolute = crate::path::glob_path(&other);
    let relative = crate::path::glob_path(source.strip_prefix(&current).expect("relative source"));
    let other_relative =
        crate::path::glob_path(other.strip_prefix(&current).expect("relative other source"));
    let mut parent = CoverageData::new(false);
    parent.add_lines("parent", &relative, vec![1]);
    let mut child = CoverageData::new(false);
    child.add_lines("child", &absolute, vec![2]);
    child.add_lines("other", &other_absolute, vec![1]);
    let shard = temporary.path().join("child.coverage");
    child.write_path(&shard).expect("write child shard");
    for mode in [PathAliasMode::Absolute, PathAliasMode::Relative] {
        let output = temporary.path().join(format!("{mode:?}.coverage"));
        let (combined, count) = combine_transaction(
            vec![shard.to_string_lossy().into_owned()],
            &output,
            Some(parent.clone()),
            CombineOptions {
                keep: true,
                strict: true,
                path_alias_mode: mode,
                ..CombineOptions::default()
            },
        )
        .expect("combine mixed filename shards");
        let combined = combined.expect("combined data");
        assert_eq!(count, 1);
        if mode == PathAliasMode::Relative {
            assert_eq!(combined.measured_files().len(), 2);
            assert_eq!(combined.lines_for(&relative), Some(vec![1, 2]));
            assert_eq!(combined.lines_for(&other_relative), Some(vec![1]));
            assert_eq!(
                combined.line_records(),
                vec![
                    ("child".to_owned(), relative.clone(), vec![2]),
                    ("other".to_owned(), other_relative.clone(), vec![1]),
                    ("parent".to_owned(), relative.clone(), vec![1]),
                ]
            );
        } else {
            assert_eq!(combined.measured_files().len(), 3);
            assert_eq!(combined.lines_for(&relative), Some(vec![1]));
            assert_eq!(combined.lines_for(&absolute), Some(vec![2]));
            assert_eq!(combined.lines_for(&other_absolute), Some(vec![1]));
        }
        assert_eq!(
            CoverageData::read_path(&output)
                .expect("read combined database")
                .line_records(),
            combined.line_records()
        );
    }
}

#[test]
fn relative_combine_collision_keeps_destination_and_shard_bytes_unchanged() {
    let current = std::env::current_dir().expect("working directory");
    let temporary = tempfile::tempdir_in(&current).expect("temporary collision sources");
    let source = temporary.path().join("sample.py");
    std::fs::write(&source, "pass\n").expect("source file");
    let relative = crate::path::glob_path(source.strip_prefix(&current).expect("relative source"));
    let absolute = crate::path::glob_path(&source);
    let output = temporary.path().join(".coverage");
    let shard = temporary.path().join(".coverage.child");
    let mut parent = CoverageData::new(false);
    parent.add_lines("parent", &relative, vec![1]);
    parent.set_tracer(&relative, "parent.plugin");
    parent
        .write_path(&output)
        .expect("write original destination");
    let mut child = CoverageData::new(false);
    child.add_lines("child", &absolute, vec![1]);
    child.set_tracer(&absolute, "child.plugin");
    child.write_path(&shard).expect("write original shard");
    let before_output = std::fs::read(&output).expect("original output bytes");
    let before_shard = std::fs::read(&shard).expect("original shard bytes");
    let error = combine_transaction(
        vec![shard.to_string_lossy().into_owned()],
        &output,
        None,
        CombineOptions {
            strict: true,
            include_output: true,
            path_alias_mode: PathAliasMode::Relative,
            ..CombineOptions::default()
        },
    )
    .err()
    .expect("conflicting mapped tracers must be rejected");
    assert!(error.to_string().contains("conflicting file tracers"));
    assert_eq!(
        std::fs::read(&output).expect("retained output"),
        before_output
    );
    assert_eq!(std::fs::read(&shard).expect("retained shard"), before_shard);
}

#[test]
fn aliases_reject_malformed_groups_and_patterns() {
    assert!(matches!(
        PathAliases::new(vec![Vec::new()]),
        Err(error) if error == "path alias groups must contain a canonical path"
    ));
    assert!(matches!(
        PathAliases::new(vec![vec!["/target".to_owned(), "/source/*".to_owned()]]),
        Err(error) if error == "path alias patterns must not end with wildcards"
    ));
    assert!(matches!(
        PathAliases::new(vec![vec!["/target".to_owned(), "[invalid".to_owned()]]),
        Err(error) if error.starts_with("invalid path alias pattern")
    ));
}

#[test]
fn aliases_map_existing_suffixes_and_leave_unknown_paths_untouched() {
    let temporary = tempfile::tempdir().expect("temporary alias tree");
    let source = temporary.path().join("source");
    let target = temporary.path().join("target");
    std::fs::create_dir_all(source.join("pkg")).expect("source directory");
    std::fs::create_dir_all(target.join("pkg")).expect("target directory");
    let source_file = source.join("pkg/module.py");
    let target_file = target.join("pkg/module.py");
    std::fs::write(&source_file, "pass\n").expect("source file");
    std::fs::write(&target_file, "pass\n").expect("target file");

    let mut aliases = PathAliases::new(vec![vec![
        target.to_string_lossy().into_owned(),
        source.to_string_lossy().into_owned(),
    ]])
    .expect("valid aliases");
    assert_eq!(
        aliases.map(source_file.to_string_lossy().as_ref()),
        crate::path::glob_path(
            &crate::path::canonical_path(&target_file).expect("canonical target"),
        )
    );
    assert_eq!(
        aliases.map("/not-present/module.py"),
        "/not-present/module.py"
    );
}

#[test]
fn absolute_alias_destinations_search_current_directory_before_interpreter_paths() {
    let current = std::env::current_dir().expect("working directory");
    let temporary = tempfile::tempdir_in(&current).expect("temporary alias search tree");
    let relative = temporary
        .path()
        .strip_prefix(&current)
        .expect("relative tree");
    let destination = relative.join("mapped");
    let first = temporary.path().join("first");
    let second = temporary.path().join("second");
    for root in [&current, &first, &second] {
        std::fs::create_dir_all(root.join(&destination)).expect("alias destination");
    }
    for (root, names) in [
        (&current, vec!["local.py"]),
        (&first, vec!["local.py", "ordered.py"]),
        (&second, vec!["ordered.py", "fallback.py"]),
    ] {
        for name in names {
            std::fs::write(root.join(&destination).join(name), "pass\n").expect("mapped source");
        }
    }
    let source = temporary.path().join("source/project/source");
    let groups = vec![vec![
        crate::path::glob_path(&destination),
        "*/source".to_owned(),
    ]];
    let search_paths = vec![first.clone(), second.clone()];
    let mut relative_aliases = PathAliases::with_mode(
        groups.clone(),
        PathAliasMode::Relative,
        search_paths.clone(),
    )
    .expect("relative aliases");
    let ordered_source = crate::path::glob_path(&source.join("ordered.py"));
    let relative_source = crate::path::glob_path(
        &source
            .strip_prefix(&current)
            .expect("relative source")
            .join("ordered.py"),
    );
    assert_eq!(relative_aliases.map(&ordered_source), relative_source);
    let mut aliases = PathAliases::with_mode(groups, PathAliasMode::Absolute, search_paths)
        .expect("absolute aliases");
    for (name, root) in [
        ("local.py", &current),
        ("ordered.py", &first),
        ("fallback.py", &second),
    ] {
        assert_eq!(
            aliases.map(&crate::path::glob_path(&source.join(name))),
            crate::path::glob_path(&crate::path::absolute_path(
                &root.join(&destination).join(name)
            ))
        );
    }
}

#[test]
fn cleanup_failure_after_commit_keeps_the_new_output_readable() {
    let directory = tempfile::tempdir().expect("temporary cleanup directory");
    let output = directory.path().join(".coverage");
    let shard = directory.path().join("worker.coverage");
    CoverageData::from_trace_records(
        false,
        vec![("ctx".to_owned(), "module.py".to_owned(), vec![3])],
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    )
    .write_path(&shard)
    .expect("write shard");
    let failing_shard = shard.clone();
    let Err(error) = combine_transaction_with_cleanup(
        vec![shard.to_string_lossy().into_owned()],
        &output,
        None,
        None,
        CombineOptions {
            strict: true,
            ..CombineOptions::default()
        },
        move |path: &Path| {
            if path == failing_shard {
                Err(std::io::Error::other("injected cleanup failure"))
            } else {
                std::fs::remove_file(path)
            }
        },
    ) else {
        panic!("cleanup failure must be reported");
    };
    assert!(error.to_string().contains("failed to remove shard"));
    let committed = CoverageData::read_path(&output).expect("committed output remains readable");
    assert_eq!(committed.lines_for("module.py"), Some(vec![3]));
    assert!(
        shard.exists(),
        "failed cleanup leaves the shard for inspection"
    );
}

#[test]
fn transport_corruption_preserves_the_sqlite_source() {
    let Err(error) = CoverageData::deserialize(b"vfy-data\0not a database") else {
        panic!("malformed transport database must be rejected");
    };
    assert_eq!(error.operation(), "validate serialized coverage data");
    assert!(error.source().is_some());
    assert!(
        error
            .to_string()
            .starts_with("serialized coverage data is corrupt:")
    );
}

#[test]
fn relative_aliases_merge_evidence_and_retain_cross_platform_root_identity() {
    let current = std::env::current_dir().expect("working directory");
    let temporary = tempfile::tempdir_in(&current).expect("temporary relative source");
    let source = temporary.path().join("sample.py");
    std::fs::write(&source, "a = 1\nb = 2\n").expect("source file");
    let relative = source.strip_prefix(&current).expect("relative source");
    let absolute = crate::path::glob_path(&source);
    let relative = crate::path::glob_path(relative);
    let data = CoverageData::from_trace_records(
        false,
        vec![
            ("absolute".to_owned(), absolute.clone(), vec![1]),
            ("relative".to_owned(), relative.clone(), vec![2]),
        ],
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    );
    let mapped = data
        .remap_paths_with_mode(Vec::new(), super::PathAliasMode::Relative, Vec::new())
        .expect("merged alias records");
    assert_eq!(mapped.measured_files(), vec![relative.clone()]);
    assert_eq!(mapped.lines_for(&relative), Some(vec![1, 2]));
    let mut aliases =
        PathAliases::with_mode(Vec::new(), super::PathAliasMode::Relative, Vec::new())
            .expect("relative aliases");
    assert_eq!(aliases.map("C:/unmapped/file.py"), "C:/unmapped/file.py");
    assert!(aliases.implicit_roots.is_empty());
    assert_eq!(aliases.map(&absolute), relative);
    assert_eq!(aliases.map(&format!("C:/worker/{relative}")), relative);
}

#[test]
fn relative_alias_collision_rejection_preserves_original_data() {
    let current = std::env::current_dir().expect("working directory");
    let temporary = tempfile::tempdir_in(&current).expect("temporary collision source");
    let source = temporary.path().join("sample.py");
    std::fs::write(&source, "pass\n").expect("source file");
    let absolute = crate::path::glob_path(&source);
    let relative = crate::path::glob_path(source.strip_prefix(current).expect("relative source"));
    let data = CoverageData::from_trace_records(
        false,
        vec![
            ("a".to_owned(), absolute.clone(), vec![1]),
            ("b".to_owned(), relative.clone(), vec![1]),
        ],
        Vec::new(),
        vec![
            (absolute, "plugin.a".to_owned()),
            (relative, "plugin.b".to_owned()),
        ],
        SemanticData::default(),
    );
    let before = data.serialize().expect("original data transport");
    assert!(data.clone().remap_paths(Vec::new()).is_ok());
    assert!(
        data.clone()
            .remap_paths_with_mode(Vec::new(), super::PathAliasMode::Relative, Vec::new())
            .is_err()
    );
    assert_eq!(data.serialize().expect("unchanged data transport"), before);
}

#[test]
fn relative_root_inference_retains_file_order_through_schema_round_trip_and_update() {
    let current = std::env::current_dir().expect("working directory");
    let temporary = tempfile::tempdir_in(&current).expect("temporary inferred source");
    let source = temporary.path().join("sample.py");
    std::fs::write(&source, "a = 1\nb = 2\n").expect("source file");
    let relative = crate::path::glob_path(source.strip_prefix(current).expect("relative source"));
    let remote = format!("C:/worker/{relative}");
    for relative_first in [false, true] {
        let first = if relative_first { &relative } else { &remote };
        let second = if relative_first { &remote } else { &relative };
        let mut data = CoverageData::new(false);
        data.add_lines("first", first, vec![1]);
        let mut incoming = CoverageData::new(false);
        incoming.add_lines("second", second, vec![2]);
        data.merge_from(incoming, false).expect("merge evidence");
        let serialized = data.serialize().expect("serialize ordered files");
        let restored = CoverageData::deserialize(&serialized).expect("restore ordered files");
        let mapped = restored
            .remap_paths_with_mode(Vec::new(), super::PathAliasMode::Relative, Vec::new())
            .expect("infer root in creation order");
        assert_eq!(
            mapped.measured_files().len(),
            if relative_first { 1 } else { 2 }
        );
        assert_eq!(
            mapped.lines_for(&relative),
            Some(if relative_first { vec![1, 2] } else { vec![2] })
        );
    }
}

#[test]
fn purged_files_keep_creation_order_and_tracers_through_serialization_and_remapping() {
    let mut data = CoverageData::new(false);
    data.add_lines("ctx", "first.py", vec![1]);
    data.set_tracer("first.py", "plugin.first");
    data.add_lines("ctx", "second.py", vec![2]);
    data.purge_files(vec!["first.py".to_owned()]);
    let restored = CoverageData::deserialize(&data.serialize().expect("serialize purged data"))
        .expect("read purged file identities");
    let mapped = restored.remap_paths(Vec::new()).expect("remap purged data");
    assert_eq!(mapped.measured_files(), vec!["first.py", "second.py"]);
    assert_eq!(mapped.lines_for("first.py"), Some(Vec::new()));
    assert_eq!(
        mapped.tracers.get("first.py"),
        Some(&"plugin.first".to_owned())
    );
    assert_eq!(mapped.file_order, vec!["first.py", "second.py"]);
}

#[test]
fn report_remapping_uses_stored_arcs_without_changing_generic_remapping() {
    for has_evidence in [false, true] {
        let mut data = CoverageData::new(true);
        data.set_metadata("has_arcs", "1");
        data.touch_file("sample.py");
        if has_evidence {
            data.add_arcs("ctx", "sample.py", vec![(1, 2)]);
        }
        assert!(
            data.clone()
                .remap_paths(Vec::new())
                .expect("generic remap")
                .branch_mode()
        );
        let report_data = data
            .remap_paths_for_reporting(Vec::new(), super::PathAliasMode::Absolute, Vec::new())
            .expect("report remap");
        assert_eq!(report_data.branch_mode(), has_evidence);
        assert_eq!(
            report_data.metadata()["has_arcs"],
            if has_evidence { "1" } else { "0" }
        );
        let restored =
            CoverageData::deserialize(&report_data.serialize().expect("serialize report"))
                .expect("restore report mode");
        assert_eq!(restored.branch_mode(), has_evidence);
        assert_eq!(restored.measured_files(), vec!["sample.py"]);
    }
}

#[test]
fn publish_commits_an_incoming_shard_with_the_existing_output_and_aliases() {
    let directory = tempfile::tempdir().expect("temporary publication directory");
    let output = directory.path().join(".coverage");
    let shard = |path: &str, line: i32| {
        CoverageData::from_trace_records(
            false,
            vec![(String::new(), path.to_owned(), vec![line])],
            Vec::new(),
            Vec::new(),
            SemanticData::default(),
        )
    };
    let canonical = directory.path().join("canonical");
    std::fs::create_dir(&canonical).expect("canonical directory");
    std::fs::write(canonical.join("mapped.py"), "x = 1\n").expect("alias target exists");
    let canonical = crate::path::canonical_path(&canonical).expect("canonical root");
    let elsewhere = directory.path().join("elsewhere");
    let moved = elsewhere.join("mapped.py").to_string_lossy().into_owned();
    shard("kept.py", 1)
        .write_path(&output)
        .expect("existing output");
    let child = directory.path().join(".coverage.child");
    shard(&moved, 3).write_path(&child).expect("child shard");
    let options = || CombineOptions {
        include_output: true,
        aliases: vec![vec![
            canonical.to_string_lossy().into_owned(),
            elsewhere.to_string_lossy().into_owned(),
        ]],
        ..CombineOptions::default()
    };
    let (published, count) = publish_transaction(
        shard(&moved, 2),
        vec![directory.path().to_string_lossy().into_owned()],
        &output,
        options(),
    )
    .expect("publish incoming and child shards");
    assert_eq!(count, 1);
    assert!(!child.exists());
    let committed = CoverageData::read_path(&output).expect("read published output");
    assert_eq!(committed.lines_for("kept.py"), Some(vec![1]));
    assert_eq!(
        committed.lines_for(&canonical.join("mapped.py").to_string_lossy()),
        Some(vec![2, 3])
    );
    assert_eq!(published.measured_files(), committed.measured_files());

    let (alone, count) = publish_transaction(
        shard("only.py", 4),
        vec![directory.path().to_string_lossy().into_owned()],
        &output,
        CombineOptions::default(),
    )
    .expect("an incoming shard commits without discovered shards");
    assert_eq!(count, 0);
    assert_eq!(alone.measured_files(), vec!["only.py".to_owned()]);
    assert_eq!(
        CoverageData::read_path(&output)
            .expect("replaced output")
            .measured_files(),
        vec!["only.py".to_owned()]
    );
}

#[test]
fn absolute_aliases_collapse_parents_of_missing_directories_and_keep_root_patterns() {
    let temporary = tempfile::tempdir().expect("temporary alias tree");
    let target = temporary.path().join("target");
    std::fs::create_dir_all(&target).expect("target directory");
    std::fs::write(target.join("module.py"), "pass\n").expect("target file");
    let canonical = crate::path::glob_path(&target.join("missing/.."));
    let expected = crate::path::glob_path(
        &crate::path::canonical_path(&target.join("module.py")).expect("canonical target"),
    );

    let mut aliases = PathAliases::new(vec![vec![canonical.clone(), "/remote".to_owned()]])
        .expect("valid aliases");
    assert_eq!(aliases.map("/remote/module.py"), expected);

    let mut aliases =
        PathAliases::new(vec![vec![canonical, "/".to_owned()]]).expect("root pattern");
    assert_eq!(aliases.map("/module.py"), expected);
}
