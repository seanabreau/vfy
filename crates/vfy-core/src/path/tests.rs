use super::{absolute_path, canonical_path, glob_path};
use super::{discover_python_source_files, is_python_source_file};
use std::path::{Path, PathBuf};

#[test]
fn glob_paths_use_forward_slashes() {
    assert_eq!(
        glob_path(Path::new(r"C:\work\package\module.py")),
        "C:/work/package/module.py"
    );
}

#[test]
fn verbatim_prefixes_are_removed() {
    assert_eq!(
        super::strip_verbatim_prefix(PathBuf::from(r"\\?\C:\work\module.py")),
        PathBuf::from(r"C:\work\module.py")
    );
    assert_eq!(
        super::strip_verbatim_prefix(PathBuf::from(r"\\?\UNC\server\share\module.py")),
        PathBuf::from(r"\\server\share\module.py")
    );
}

#[test]
fn absolute_paths_resolve_existing_parents() {
    let temporary = tempfile::tempdir().expect("temporary path");
    let nested = temporary.path().join("nested");
    std::fs::create_dir(&nested).expect("nested directory");
    assert_eq!(
        absolute_path(&nested.join("..")),
        temporary
            .path()
            .canonicalize()
            .expect("canonical temporary path")
    );
    assert_eq!(
        canonical_path(&nested).expect("canonical path"),
        nested.canonicalize().expect("canonical nested path")
    );
}

#[test]
fn source_filenames_match_coverage_python_import_rules() {
    assert!(is_python_source_file(Path::new("module.py")));
    assert!(is_python_source_file(Path::new("module.pyw")));
    assert!(!is_python_source_file(Path::new("module.pyc")));
    assert!(!is_python_source_file(Path::new("module.test.py")));
    assert!(!is_python_source_file(Path::new("module.py~")));
}

#[test]
fn source_discovery_only_descends_into_importable_packages() {
    let temporary = tempfile::tempdir().expect("temporary source tree");
    let nested = temporary.path().join("nested");
    let not_package = temporary.path().join("not_package");
    let cache = temporary.path().join("__pycache__");
    std::fs::create_dir_all(&nested).expect("nested package");
    std::fs::create_dir_all(&not_package).expect("ordinary directory");
    std::fs::create_dir_all(&cache).expect("bytecode cache");
    std::fs::write(temporary.path().join("__init__.py"), "").expect("root initializer");
    std::fs::write(temporary.path().join("invalid.py"), "def broken(:\n")
        .expect("invalid Python source");
    std::fs::write(temporary.path().join("window.pyw"), "").expect("Python window source");
    std::fs::write(temporary.path().join("module.test.py"), "").expect("editor side file");
    std::fs::write(nested.join("__init__.py"), "").expect("nested initializer");
    std::fs::write(nested.join("module.py"), "").expect("nested module");
    std::fs::write(not_package.join("module.py"), "").expect("non-package module");
    std::fs::write(cache.join("module.py"), "").expect("cache module");

    let files = discover_python_source_files(temporary.path());

    assert!(files.contains(&absolute_path(&temporary.path().join("__init__.py"))));
    assert!(files.contains(&absolute_path(&temporary.path().join("invalid.py"))));
    assert!(files.contains(&absolute_path(&temporary.path().join("window.pyw"))));
    assert!(files.contains(&absolute_path(&nested.join("__init__.py"))));
    assert!(files.contains(&absolute_path(&nested.join("module.py"))));
    assert!(!files.contains(&absolute_path(&temporary.path().join("module.test.py"))));
    assert!(!files.contains(&absolute_path(&not_package.join("module.py"))));
    assert!(!files.contains(&absolute_path(&cache.join("module.py"))));
}

#[cfg(unix)]
#[test]
fn source_discovery_includes_file_symlinks_without_following_directory_symlinks() {
    use std::os::unix::fs::symlink;

    let temporary = tempfile::tempdir().expect("temporary source tree");
    let external = tempfile::tempdir().expect("external source tree");
    let target = external.path().join("target.py");
    std::fs::write(&target, "").expect("symlink target");
    std::fs::write(external.path().join("__init__.py"), "").expect("external initializer");
    std::fs::write(external.path().join("external.py"), "").expect("external module");
    symlink(&target, temporary.path().join("linked.py")).expect("file symlink");
    symlink(external.path(), temporary.path().join("linked_package")).expect("directory symlink");

    let files = discover_python_source_files(temporary.path());

    assert!(files.contains(&absolute_path(&target)));
    assert_eq!(files.len(), 1);
}

#[test]
fn coverage_globs_treat_braces_as_literal_filename_characters() {
    for pattern in ["*a{b,c}.py", "a{b,c}.py", "a{b}.py", "a{.py", "a}.py"] {
        let patterns = super::glob_set(&[pattern.to_owned()])
            .expect("literal braces are supported")
            .expect("nonempty patterns");
        let literal = pattern.trim_start_matches('*');
        assert!(
            patterns.is_match(format!("/project/{literal}")),
            "{pattern}"
        );
        assert!(!patterns.is_match("/project/ab.py"), "{pattern}");
        assert!(!patterns.is_match("/project/ac.py"), "{pattern}");
    }
}

#[test]
fn coverage_globs_preserve_recursive_and_component_wildcards() {
    for (pattern, matching, excluded) in [
        ("*.py", "/project/nested/module.py", "/project/module.txt"),
        (
            "*/pkg/*.py",
            "/project/pkg/module.py",
            "/project/pkg/deep/module.py",
        ),
        ("pkg/*", "pkg/deep/module.py", "pkg"),
        (
            "source/generated/*",
            "source/generated/code.py",
            "source/generated",
        ),
        ("pkg/**", "pkg/deep/module.py", "pkg"),
        ("pkg/**/module.py", "pkg/module.py", "pkg/module.txt"),
        ("pkg/**/module.py", "pkg/deep/module.py", "pkg/module.txt"),
        ("pkg/a?c.py", "pkg/abc.py", "pkg/a/c.py"),
        ("pkg/[a-c].py", "pkg/b.py", "pkg/d.py"),
        ("pkg/[!a].py", "pkg/!.py", "pkg/b.py"),
        ("pkg/[!-a].py", "pkg/@.py", "pkg/z.py"),
        ("pkg/[^a].py", "pkg/b.py", "pkg/a.py"),
        ("pkg/[{}].py", "pkg/{.py", "pkg/a.py"),
        ("pkg/?.py", "pkg/é.py", "pkg/ab.py"),
        ("pkg/[^a].py", "pkg/é.py", "pkg/a.py"),
        ("pkg/[é-ê].py", "pkg/é.py", "pkg/e.py"),
        ("pkg/[a&&b].py", "pkg/&.py", "pkg/c.py"),
        ("pkg/[a||b].py", "pkg/|.py", "pkg/c.py"),
        (r"pkg\*.py", "pkg/module.py", "pkg/deep/module.py"),
    ] {
        let patterns = super::glob_set(&[pattern.to_owned()])
            .expect("supported coverage pattern")
            .expect("nonempty patterns");
        assert!(
            patterns.is_match(matching),
            "{pattern}: {matching}: {}",
            super::coverage_glob(
                &super::tokenize_glob(pattern).expect("validated tokens"),
                super::CaseSensitivity::platform()
            )
        );
        assert!(!patterns.is_match(excluded), "{pattern}: {excluded}");
    }
}

#[test]
fn coverage_globs_follow_python_windows_case_folding() {
    let patterns = super::glob_set_with_case(
        &["PKG/[a-z].PY".to_owned()],
        super::CaseSensitivity::Insensitive,
    )
    .expect("valid coverage pattern")
    .expect("nonempty patterns");
    assert!(patterns.is_match("pkg/İ.py"));
    assert!(patterns.is_match("pkg/ı.py"));
    assert!(patterns.is_match("pkg/ſ.py"));
    assert!(patterns.is_match("pkg/\u{212a}.py"));
    assert!(!patterns.is_match("pkg/é.py"));

    let literal = super::glob_set_with_case(
        &["PKG/I.PY".to_owned()],
        super::CaseSensitivity::Insensitive,
    )
    .expect("valid coverage pattern")
    .expect("nonempty patterns");
    assert!(literal.is_match("pkg/İ.py"));
    assert!(literal.is_match("pkg/ı.py"));
}

#[test]
fn coverage_globs_use_active_python_unicode_case_tables() {
    use super::{CaseSensitivity, UnicodeCaseVersion, glob_set_with_case_and_version};

    assert_eq!(
        UnicodeCaseVersion::for_python_minor(12),
        UnicodeCaseVersion::V15
    );
    assert_eq!(
        UnicodeCaseVersion::for_python_minor(13),
        UnicodeCaseVersion::V15
    );
    assert_eq!(
        UnicodeCaseVersion::for_python_minor(14),
        UnicodeCaseVersion::V16
    );
    assert_eq!(
        UnicodeCaseVersion::for_python_minor(15),
        UnicodeCaseVersion::V17
    );
    assert_eq!(super::unicode_case_pairs(UnicodeCaseVersion::V15).len(), 27);
    assert_eq!(super::unicode_case_pairs(UnicodeCaseVersion::V17).len(), 28);
    for (first, second) in super::unicode_case_pairs(UnicodeCaseVersion::V15) {
        for (pattern, filename) in [(first, second), (second, first)] {
            let class = super::ClassUnicode::new([super::ClassUnicodeRange::new(pattern, pattern)]);
            assert!(!super::class_contains(
                &super::folded_class(&class, UnicodeCaseVersion::V15),
                filename
            ));
            assert!(super::class_contains(
                &super::folded_class(&class, UnicodeCaseVersion::V16),
                filename
            ));
        }
    }
    for (first, second) in super::unicode_case_pairs(UnicodeCaseVersion::V17) {
        for (pattern, filename) in [(first, second), (second, first)] {
            let class = super::ClassUnicode::new([super::ClassUnicodeRange::new(pattern, pattern)]);
            assert!(!super::class_contains(
                &super::folded_class(&class, UnicodeCaseVersion::V16),
                filename
            ));
            assert!(super::class_contains(
                &super::folded_class(&class, UnicodeCaseVersion::V17),
                filename
            ));
        }
    }

    let checks = [
        ("\u{1c89}", "\u{1c8a}", [false, true, true]),
        ("[\u{1c89}]", "\u{1c8a}", [false, true, true]),
        ("[^\u{1c89}]", "\u{1c8a}", [true, false, false]),
        ("\u{019b}", "\u{a7dc}", [false, true, true]),
        ("\u{a7dc}", "\u{019b}", [false, true, true]),
        ("[\u{019b}-\u{0264}]", "\u{a7dc}", [false, true, true]),
        ("[^\u{019b}-\u{0264}]", "\u{a7dc}", [true, false, false]),
        ("[^\u{019b}]", "\u{a7dc}", [true, false, false]),
        ("\u{a7ce}", "\u{a7cf}", [false, false, true]),
        ("[\u{a7ce}-\u{a7ce}]", "\u{a7cf}", [false, false, true]),
        ("[^\u{a7ce}-\u{a7ce}]", "\u{a7cf}", [true, true, false]),
        ("[^\u{a7ce}]", "\u{a7cf}", [true, true, false]),
        ("[^a]", "A", [false, false, false]),
    ];
    for (pattern, filename, expected) in checks {
        for (version, matches) in [
            UnicodeCaseVersion::V15,
            UnicodeCaseVersion::V16,
            UnicodeCaseVersion::V17,
        ]
        .into_iter()
        .zip(expected)
        {
            let compiled = glob_set_with_case_and_version(
                &[format!("pkg/{pattern}.py")],
                CaseSensitivity::Insensitive,
                version,
            )
            .expect("valid Coverage class")
            .expect("nonempty pattern set");
            assert_eq!(
                compiled.is_match(format!("pkg/{filename}.py")),
                matches,
                "{version:?}: {pattern:?} versus {filename:?}"
            );
        }
    }
}

#[test]
fn versioned_glob_compiler_preserves_shared_token_semantics() {
    use super::{CaseSensitivity, UnicodeCaseVersion, glob_set_with_case_and_version};

    let patterns = [
        "*.py",
        "pkg/?.py",
        "pkg/[a-z].py",
        "pkg/[^a].py",
        "pkg/[!a].py",
        "pkg/[a&&é].py",
        "pkg/[a||é].py",
        "pkg/**/module.py",
        "*/pkg/*",
        "**/pkg/*.py",
        "pkg/**",
        r"pkg\*.py",
        "pkg/{ab}.py",
    ];
    let paths = [
        "pkg/a.py",
        "pkg/A.py",
        "pkg/é.py",
        "pkg/!.py",
        "pkg/{ab}.py",
        "pkg/deep/module.py",
        "other/pkg/module.py",
        "other/pkg/deep/module.py",
        "pkg/deep/é.py",
        "pkg/a.rs",
        "pkg/İ.py",
        "pkg/ı.py",
    ];
    for pattern in patterns {
        let regular = glob_set_with_case_and_version(
            &[pattern.to_owned()],
            CaseSensitivity::Insensitive,
            UnicodeCaseVersion::V16,
        )
        .expect("valid pattern")
        .expect("nonempty patterns");
        for version in [UnicodeCaseVersion::V15, UnicodeCaseVersion::V17] {
            let versioned = glob_set_with_case_and_version(
                &[pattern.to_owned()],
                CaseSensitivity::Insensitive,
                version,
            )
            .expect("valid pattern")
            .expect("nonempty patterns");
            for path in paths {
                assert_eq!(
                    versioned.is_match(path),
                    regular.is_match(path),
                    "{version:?}: {pattern:?} versus {path:?}"
                );
            }
        }
    }
}

#[test]
fn coverage_globs_reject_invalid_tokens_with_upstream_diagnostics() {
    for (pattern, fragment) in [
        ("*a**b.py", "*a**"),
        ("pkg/a**b.py", "a**"),
        ("pkg/**a.py", "**a.py"),
        ("pkg/***/a.py", "***"),
        ("pkg/**/**/a.py", "**/**"),
        ("module[.py", "["),
        ("module].py", "]"),
        ("**", "**/**"),
        ("a\n**b.py", "a\\n**"),
    ] {
        let error = super::glob_set(&[pattern.to_owned()]).expect_err("invalid token");
        assert_eq!(
            error.to_string(),
            format!("File pattern can't include '{fragment}'")
        );
    }
}
