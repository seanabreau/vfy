use super::*;
use crate::pytest_config::argument_files::{
    MAX_ARGUMENT_FILE_BYTES, MAX_ARGUMENT_FILE_DEPTH, MAX_ARGUMENT_FILES, MAX_EXPANDED_ARGUMENTS,
};
use crate::pytest_config::config_files::config_contents_fingerprint;
use crate::pytest_config::paths::expand_dollar_vars;
use std::fs;
use tempfile::tempdir;

fn args(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

#[test]
fn mutation_baseline_report_gates_require_original_coverage_intent() {
    use MutationBaselineCoveragePolicy::{Disabled, Implicit, Requested};

    for (arguments, expected) in [
        (args(&["tests"]), Implicit),
        (args(&["--cov", "tests"]), Requested),
        (args(&["--cov=app"]), Requested),
        (args(&["--cov=app", "--cov-reset"]), Implicit),
        (args(&["--cov-reset", "--cov"]), Requested),
        (args(&["--cov-fail-under=0"]), Requested),
        (args(&["--cov-fail-under-decisions", "80"]), Requested),
        (args(&["--cov-fail-under-conditions=80"]), Requested),
        (args(&["--cov-fail-under-mcdc=80"]), Requested),
        (args(&["--no-cov"]), Disabled),
        (args(&["--cov=app", "--no-cov"]), Disabled),
        (args(&["--no-cov", "--cov=app"]), Disabled),
        (args(&["--cov-fail-under=100", "--no-cov"]), Disabled),
        (args(&["--no-cov", "--cov-fail-under=100"]), Disabled),
        (args(&["--cov-reset", "--no-cov"]), Disabled),
        (args(&["--no-cov", "--cov=app", "--cov-reset"]), Disabled),
        (args(&["-k", "--no-cov"]), Implicit),
        (args(&["--", "--no-cov"]), Implicit),
        (args(&["-k", "--cov=app"]), Implicit),
        (args(&["--", "--cov=app"]), Implicit),
    ] {
        assert_eq!(
            mutation_baseline_coverage_policy(&arguments, PytestParserBehavior::Current),
            expected,
            "{arguments:?}"
        );
    }
}

#[test]
fn mutations_override_inherited_workers_and_keep_explicit_serial_precedence() {
    for parser in [PytestParserBehavior::Legacy, PytestParserBehavior::Current] {
        for (effective, cli, expected) in [
            (
                args(&["-n", "auto", "tests"]),
                args(&["tests"]),
                args(&["--numprocesses=0", "--dist=no"]),
            ),
            (
                args(&["--numprocesses=2", "-n0"]),
                args(&["-n0"]),
                args(&["--dist=no"]),
            ),
            (
                args(&["-n2", "--numprocesses", "0"]),
                args(&["--numprocesses", "0"]),
                args(&["--dist=no"]),
            ),
            (
                args(&["-k", "-never", "tests"]),
                args(&["-k", "-never", "tests"]),
                args(&[]),
            ),
            (
                args(&["-qn0", "tests"]),
                args(&["-qn0", "tests"]),
                args(&["--dist=no"]),
            ),
            (args(&["tests", "--", "-n2"]), args(&["-n2"]), args(&[])),
            (
                args(&["-k", "-n2", "tests"]),
                args(&["-n2", "tests"]),
                args(&[]),
            ),
        ] {
            assert_eq!(
                mutation_worker_arguments(&effective, &cli, parser),
                Ok(expected),
                "cli={cli:?}, parser={parser:?}"
            );
        }
        for cli in [
            args(&["-n", "2"]),
            args(&["-qn2"]),
            args(&["--numprocesses=auto"]),
        ] {
            assert!(mutation_worker_arguments(&cli, &cli, parser).is_err());
        }
    }
}

#[test]
fn coverage_reports_keep_cli_sources_reset_and_configuration_fallback() {
    let fallback = args(&["graph-root"]);
    for parser in [PytestParserBehavior::Legacy, PytestParserBehavior::Current] {
        for (arguments, expected) in [
            (args(&[]), Some(fallback.clone())),
            (args(&["--cov=package"]), Some(args(&["package"]))),
            (
                args(&["--cov", "package", "--cov=other"]),
                Some(args(&["package", "other"])),
            ),
            (
                args(&["--cov=old", "--cov-reset", "--cov=package", "--cov-append"]),
                Some(args(&["package"])),
            ),
            (args(&["--cov", "--cov=package"]), None),
            (args(&["--cov="]), Some(args(&[""]))),
            (args(&["--cov=package", "--cov-reset"]), None),
            (
                args(&["test_file.py", "--", "--cov=literal"]),
                Some(fallback.clone()),
            ),
        ] {
            assert_eq!(
                coverage_report_sources(&arguments, &fallback, parser),
                expected
            );
        }
    }
}

#[test]
fn collect_only_projection_uses_effective_options_and_respects_value_boundaries() {
    let temporary = tempdir().expect("create collect-only projection fixture");
    let root = temporary.path();
    fs::write(root.join("collect.txt"), "--collect-only\n").expect("write argument file");
    for (configured, environment, arguments, expected) in [
        ("", None, args(&["--collect-only"]), true),
        ("", None, args(&["--co"]), true),
        ("", None, args(&["--collectonly"]), true),
        ("--co", None, args(&[]), true),
        ("", Some("--collectonly"), args(&[]), true),
        ("", None, args(&["@collect.txt"]), true),
        ("", None, args(&["-o", "addopts=--collect-only"]), true),
        ("", None, args(&["-k=--collect-only"]), false),
        ("", None, args(&["--", "--collect-only"]), false),
        ("", None, args(&[]), false),
    ] {
        fs::write(
            root.join("pytest.ini"),
            format!("[pytest]\naddopts = {configured}\n"),
        )
        .expect("write collect-only configuration");
        let projection = resolve_pytest_configuration(
            &arguments,
            root,
            environment,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("resolve collect-only projection");
        assert_eq!(
            projection.collect_only, expected,
            "{configured:?} {environment:?} {arguments:?}"
        );
    }
}

#[test]
fn filtered_scope_uses_parsed_selector_groups_and_preserves_directory_suites() {
    let temporary = tempdir().expect("create selector fixture");
    let root = temporary.path();
    fs::create_dir(root.join("tests")).expect("create requested test directory");
    fs::write(root.join("pytest.ini"), "[pytest]\ntestpaths = tests\n").expect("write test paths");
    for (arguments, filtered) in [
        (args(&[]), false),
        (args(&["tests"]), false),
        (args(&["-ktest_value"]), true),
        (args(&["-qktest_value"]), true),
        (args(&["-mslow"]), true),
        (args(&["-qmslow"]), true),
        (args(&["--ignore=tests/other"]), true),
        (args(&["--ignore", "tests/other"]), true),
        (args(&["--sw-skip"]), true),
        (args(&["--stepwise-reset"]), true),
        (args(&["--collectonly"]), true),
        (args(&["-rm"]), false),
        (args(&["--junitxml", "report.py"]), false),
        (args(&["--override-ini=python_files=test_*.py"]), false),
        (args(&["-r", "summary.py"]), false),
        (args(&["tests/test_app.py"]), true),
    ] {
        let projection = resolve_pytest_configuration(
            &arguments,
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("project parsed scope");
        assert_eq!(projection.filtered_scope, filtered, "{arguments:?}");
    }
}

#[test]
fn config_addopts_static_selectors_define_the_suite() {
    let temporary = tempdir().expect("create selector fixture");
    let root = temporary.path();
    for (addopts, env_addopts, cli, filtered) in [
        ("-m 'not stress'", None, args(&[]), false),
        ("-k 'not slow' --ignore=tests/other", None, args(&[]), false),
        (
            "--deselect tests/test_app.py::test_slow",
            None,
            args(&[]),
            false,
        ),
        ("tests/test_app.py", None, args(&[]), false),
        ("-m 'not stress'", None, args(&["-m", "stress"]), true),
        ("-m 'not stress'", None, args(&["tests/test_app.py"]), true),
        ("-m 'not stress'", Some("-k value"), args(&[]), true),
        (
            "-m 'not stress'",
            None,
            args(&["-o", "addopts=-k test_value"]),
            true,
        ),
        (
            "-m 'not stress'",
            Some("-o 'addopts=-k test_value'"),
            args(&[]),
            true,
        ),
        ("--lf", None, args(&[]), true),
        ("--stepwise", None, args(&[]), true),
        ("--collect-only", None, args(&[]), true),
    ] {
        fs::write(
            root.join("pytest.ini"),
            format!("[pytest]\naddopts = {addopts}\n"),
        )
        .expect("write addopts");
        let projection = resolve_pytest_configuration(
            &cli,
            root,
            env_addopts,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("project configured scope");
        assert_eq!(
            projection.filtered_scope, filtered,
            "{addopts:?} {env_addopts:?} {cli:?}"
        );
    }
}

#[test]
fn strict_flags_follow_pytest_91_override_order_across_argument_sources() {
    let temporary = tempdir().expect("create strict option fixture");
    let root = temporary.path();
    for (flag, name) in [
        ("--strict", "strict"),
        ("--strict-config", "strict_config"),
        ("--strict-markers", "strict_markers"),
    ] {
        for (arguments, expected) in [
            (
                vec![flag.to_owned(), "-o".to_owned(), format!("{name}=false")],
                "false",
            ),
            (
                vec!["-o".to_owned(), format!("{name}=false"), flag.to_owned()],
                "true",
            ),
        ] {
            fs::write(root.join("pytest.ini"), "[pytest]\n").expect("write strict configuration");
            let projection = resolve_pytest_configuration(
                &arguments,
                root,
                None,
                PytestConfigPolicy::TomlWithAddoptsOverrides,
            )
            .expect("strict projection");
            let setting = projection
                .ini_setting(name, &[])
                .expect("strict flag override");
            assert_eq!(setting.value.as_str(), Some(expected), "{arguments:?}");
            assert!(setting.overridden);
        }
        fs::write(
            root.join("pytest.ini"),
            format!("[pytest]\naddopts = {flag}\n"),
        )
        .expect("write configured strict flag");
        let projection = resolve_pytest_configuration(
            &args(&["-o", &format!("{name}=false")]),
            root,
            Some(flag),
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("strict sources projection");
        assert_eq!(
            projection
                .ini_setting(name, &[])
                .expect("explicit override")
                .value
                .as_str(),
            Some("false")
        );
    }
}

#[test]
fn bare_cov_preserves_following_configuration_options() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::create_dir(root.join("selected")).expect("create selected root");
    fs::write(root.join("pytest.ini"), "[pytest]\n").expect("write default config");
    fs::write(root.join("selected/pytest.ini"), "[pytest]\naddopts = -q\n")
        .expect("write explicit config");

    for following in [
        args(&["--rootdir", "selected"]),
        args(&["--basetemp", "generated"]),
        args(&["-c", "selected/pytest.ini"]),
        args(&["-o", "cache_dir=generated"]),
    ] {
        let mut expected = resolve_pytest_configuration(
            &following,
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("resolve config without coverage option");
        expected
            .effective_args
            .insert(expected.configured_args.len(), "--cov".to_owned());
        expected.cli_args.insert(0, "--cov".to_owned());
        let mut arguments = args(&["--cov"]);
        arguments.extend(following);
        let actual = resolve_pytest_configuration(
            &arguments,
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("resolve config following bare coverage option");
        assert_eq!(actual, expected);
    }
}

#[test]
fn plugin_operands_affect_discovery_before_positive_plugin_activation() {
    let temporary = tempdir().expect("create plugin-option config fixture");
    let root = temporary.path();
    fs::create_dir(root.join("tests")).expect("create test directory");
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = --no-cov\n")
        .expect("write outer config");
    fs::write(
        root.join("tests/pytest.ini"),
        "[pytest]\naddopts = -p unrelated\n",
    )
    .expect("write inner config");
    fs::write(root.join(".coveragerc"), "[run]\n").expect("write coverage config");
    for policy in [
        PytestConfigPolicy::Legacy,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    ] {
        for request in [
            args(&[]),
            args(&["-p", "vfy.pytest_plugin"]),
            args(&["-pvfy.pytest_plugin"]),
            args(&["-qp", "vfy.pytest_plugin"]),
        ] {
            let mut arguments = request;
            arguments.extend(args(&["tests", "--cov-config", ".coveragerc"]));
            let projection = resolve_pytest_configuration(&arguments, root, None, policy)
                .expect("discover config before plugin option registration");
            assert_eq!(projection.config_file, Some(root.join("pytest.ini")));
            assert_eq!(projection.configured_args, args(&["--no-cov"]));
        }
        let attached = resolve_pytest_configuration(
            &args(&["tests", "--cov-config=.coveragerc"]),
            root,
            None,
            policy,
        )
        .expect("attached plugin operand is not an initial positional path");
        assert_eq!(attached.config_file, Some(root.join("tests/pytest.ini")));
    }
}

#[test]
fn option_phases_preserve_plugin_and_builtin_value_boundaries() {
    let arguments = args(&[
        "-vqk",
        "keyword",
        "-p",
        "vfy.pytest_plugin",
        "--cov",
        "source",
        "--cov-config",
        "configuration",
        "--basetemp",
        "generated",
    ]);
    let discovery = parse_arguments(
        &arguments,
        ArgumentPhase::Discovery,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
        PytestParserBehavior::Current,
    );
    assert_eq!(discovery.targets, args(&["source", "configuration"]));
    assert_eq!(discovery.basetemp.as_deref(), Some("generated"));
    let effective = parse_arguments(
        &arguments,
        ArgumentPhase::Effective,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
        PytestParserBehavior::Current,
    );
    assert!(effective.targets.is_empty());
    assert_eq!(effective.basetemp.as_deref(), Some("generated"));
}

#[test]
fn environment_end_of_options_also_applies_to_cli_arguments() {
    let temporary = tempdir().expect("create cross-source positional fixture");
    let root = temporary.path();
    fs::create_dir(root.join("selected")).expect("create selected directory");
    fs::write(root.join("pytest.ini"), "[pytest]\n").expect("write outer config");
    fs::write(root.join("selected/pytest.ini"), "[pytest]\naddopts = -q\n")
        .expect("write selected config");
    let projection = resolve_pytest_configuration(
        &args(&["--rootdir", "selected"]),
        root,
        Some("--"),
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("preserve positional-only boundary across argument sources");
    assert_eq!(projection.rootdir, root.join("selected"));
    assert_eq!(
        projection.config_file,
        Some(root.join("selected/pytest.ini"))
    );
}

#[test]
fn config_identity_changes_when_non_addopts_settings_change() {
    let temporary = tempdir().expect("create config identity fixture");
    let root = temporary.path();
    let path = root.join("pytest.ini");
    fs::write(&path, "[pytest]\ntestpaths = first\n").expect("write initial config");
    let initial = resolve_pytest_configuration(
        &[],
        root,
        None,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("resolve initial config identity");
    fs::write(&path, "[pytest]\ntestpaths = second\n").expect("change config test scope");
    let changed = resolve_pytest_configuration(
        &[],
        root,
        None,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("resolve changed config identity");
    assert_eq!(initial.effective_args, changed.effective_args);
    assert_ne!(
        initial.config_contents_fingerprint,
        changed.config_contents_fingerprint
    );
}

#[test]
fn config_file_hash_retains_consumed_bytes_after_removal() {
    let temporary = tempdir().expect("create config hash fixture");
    let root = temporary.path();
    let path = root.join("pytest.ini");
    fs::write(&path, b"[pytest]\r\nsetting = one\r\n").expect("write exact configuration bytes");
    let projection = resolve_pytest_configuration(
        &[],
        root,
        None,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("resolve configuration hash");
    fs::remove_file(path).expect("remove consumed configuration");
    // Independent oracle: hashlib.sha256 over the CRLF bytes above.
    assert_eq!(
        projection.config_file_sha256.as_deref(),
        Some("9681fe5825af2c7c1b626bb8b85d3833d569ac174006d4b938f7fbe0003bbcd9")
    );
    assert_eq!(
        projection
            .ini_setting("setting", &[])
            .expect("retained setting")
            .value
            .as_str(),
        Some("one")
    );
}

#[test]
fn fallback_pyproject_retains_its_read_content_identity() {
    let temporary = tempdir().expect("create fallback config identity fixture");
    let root = temporary.path();
    let content = "[project]\nname = 'example'\n";
    fs::write(root.join("pyproject.toml"), content).expect("write fallback config");
    let projection = resolve_pytest_configuration(
        &[],
        root,
        None,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("resolve fallback config identity");
    assert_eq!(projection.config_file, Some(root.join("pyproject.toml")));
    assert_eq!(
        projection.config_contents_fingerprint,
        config_contents_fingerprint(content)
    );
}

#[test]
fn recursive_argument_files_resolve_from_invocation_directory_before_config_selection() {
    let temporary = tempdir().expect("create argument-file project");
    let root = temporary.path();
    fs::create_dir(root.join("nested")).expect("create argument-file directory");
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -p unused\n")
        .expect("write unselected config");
    fs::write(
        root.join("selected.ini"),
        "[pytest]\naddopts = @config.txt\n",
    )
    .expect("write selected config");
    fs::write(
        root.join("nested/outer.txt"),
        "@inner.txt\r\n--basetemp\r\nmy temp\r\n",
    )
    .expect("write outer argument file");
    fs::write(root.join("inner.txt"), "-c\nselected.ini\n")
        .expect("write cwd-relative nested argument file");
    fs::write(root.join("env.txt"), "-o\ncache_dir=my cache\n")
        .expect("write environment argument file");
    fs::write(root.join("config.txt"), "--no-cov\n-q\n-n2\n").expect("write config argument file");

    let projection = resolve_pytest_configuration(
        &args(&["@nested/outer.txt"]),
        root,
        Some("@env.txt"),
        PytestConfigPolicy::Legacy,
    )
    .expect("resolve recursive argument files");
    assert_eq!(projection.config_file, Some(root.join("selected.ini")));
    assert_eq!(
        projection.configured_args,
        args(&["@config.txt", "@env.txt"])
    );
    assert_eq!(
        projection.effective_args,
        args(&[
            "--no-cov",
            "-q",
            "-n2",
            "-o",
            "cache_dir=my cache",
            "-c",
            "selected.ini",
            "--basetemp",
            "my temp",
        ])
    );
    assert_eq!(
        projection.cli_args,
        args(&["-c", "selected.ini", "--basetemp", "my temp"])
    );
    assert_eq!(projection.cache_dir, root.join("my cache"));
    assert_eq!(projection.basetemp, Some(root.join("my temp")));
}

#[test]
fn pytest_before_eight_two_does_not_expand_argument_files() {
    let temporary = tempdir().expect("create legacy argument-file project");
    let root = temporary.path();
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -q\n").expect("write legacy config");
    let projection = resolve_pytest_configuration(
        &args(&["@missing.txt"]),
        root,
        None,
        PytestConfigPolicy::LegacyWithoutArgumentFiles,
    )
    .expect("preserve unsupported argument-file token");
    assert_eq!(projection.effective_args, args(&["-q", "@missing.txt"]));
}

#[test]
fn argument_files_preserve_blank_lines_quotes_and_python_line_separators() {
    let temporary = tempdir().expect("create argument-file project");
    let root = temporary.path();
    fs::write(
        root.join("args.txt"),
        "\r\n'quoted value'\u{0085}two words\u{2028}last\u{2029}",
    )
    .expect("write argument lines");
    let projection =
        resolve_pytest_configuration(&args(&["@args.txt"]), root, None, PytestConfigPolicy::Toml)
            .expect("expand literal argument lines");
    assert_eq!(
        projection.effective_args,
        args(&["", "'quoted value'", "two words", "last"])
    );
}

#[test]
fn invalid_or_unbounded_argument_files_fail_before_projection() {
    let temporary = tempdir().expect("create invalid argument-file project");
    let root = temporary.path();
    fs::write(root.join("cycle.txt"), "@cycle.txt\n").expect("write recursive argument file");
    fs::write(root.join("invalid.txt"), [0xff]).expect("write invalid text");
    fs::write(
        root.join("large.txt"),
        vec![
            b'x';
            usize::try_from(MAX_ARGUMENT_FILE_BYTES + 1).expect("argument-file limit fits usize")
        ],
    )
    .expect("write oversized argument file");
    fs::write(
        root.join("many.txt"),
        "-q\n".repeat(MAX_EXPANDED_ARGUMENTS + 1),
    )
    .expect("write excessive argument list");
    for (filename, message) in [
        ("missing.txt", "failed to read pytest argument file"),
        ("cycle.txt", "recursive pytest argument file"),
        ("invalid.txt", "not UTF-8"),
        ("large.txt", "exceeds 8 MiB"),
        ("many.txt", "exceeds 100000 arguments"),
    ] {
        let error = resolve_pytest_configuration(
            &[format!("@{filename}")],
            root,
            None,
            PytestConfigPolicy::Toml,
        )
        .expect_err("reject invalid argument file");
        assert!(error.to_string().contains(message), "{filename}: {error}");
    }
    for index in 0..=MAX_ARGUMENT_FILE_DEPTH {
        fs::write(
            root.join(format!("depth{index}.txt")),
            format!("@depth{}.txt\n", index + 1),
        )
        .expect("write deep argument-file chain");
    }
    let error = resolve_pytest_configuration(
        &args(&["@depth0.txt"]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect_err("bound nested argument files");
    assert!(error.to_string().contains("recursion or file limit"));
    fs::write(root.join("empty.txt"), "").expect("write empty argument file");
    let error = resolve_pytest_configuration(
        &vec!["@empty.txt".to_owned(); MAX_ARGUMENT_FILES + 1],
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect_err("bound repeated argument-file expansion");
    assert!(error.to_string().contains("recursion or file limit"));
}

#[test]
fn chooses_one_preferred_config_at_the_target_ancestor() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::create_dir_all(root.join("pkg/tests")).expect("create nested test directory");
    fs::write(
        root.join("pytest.ini"),
        "[pytest]\naddopts = -q\ncache_dir = cache\n",
    )
    .expect("write preferred config");
    fs::write(root.join("setup.cfg"), "[tool:pytest]\naddopts = -x\n")
        .expect("write lower-priority config");
    let projection =
        resolve_pytest_configuration(&args(&["pkg/tests"]), root, None, PytestConfigPolicy::Toml)
            .expect("resolve pytest config");
    assert_eq!(projection.config_file, Some(root.join("pytest.ini")));
    assert_eq!(projection.configured_args, args(&["-q"]));
    assert_eq!(projection.cache_dir, root.join("cache"));
}

#[test]
fn nested_target_wins_after_value_and_boolean_flags() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let nested = root.join("pkg/tests");
    fs::create_dir_all(&nested).expect("create nested test directory");
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -q\n").expect("write root config");
    fs::write(nested.join("pytest.ini"), "[pytest]\naddopts = -x\n").expect("write nested config");
    fs::write(nested.join("test_sample.py"), "def test_sample(): pass\n")
        .expect("write target test");
    let projection = resolve_pytest_configuration(
        &args(&[
            "--strict-config",
            "-k",
            "sample",
            "-n2",
            "--cov-branch",
            "pkg/tests/test_sample.py::test_sample",
        ]),
        root,
        None,
        PytestConfigPolicy::Legacy,
    )
    .expect("resolve pytest config");
    assert_eq!(projection.config_file, Some(nested.join("pytest.ini")));
    assert_eq!(projection.rootdir, nested);
    assert_eq!(projection.configured_args, args(&["-x"]));
}

#[test]
fn built_in_option_values_do_not_select_a_nested_configuration() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let nested = root.join("nested");
    fs::create_dir(&nested).expect("create nested directory");
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -q\n").expect("write root config");
    fs::write(nested.join("pytest.ini"), "[pytest]\naddopts = -x\n").expect("write nested config");

    for option in [
        "--log-file",
        "--log-cli-format",
        "--doctest-glob",
        "--junit-prefix",
        "-r",
    ] {
        let projection = resolve_pytest_configuration(
            &args(&[option, "nested"]),
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("option value must not become a test target");
        assert_eq!(
            projection.config_file,
            Some(root.join("pytest.ini")),
            "{option}"
        );
        assert_eq!(projection.configured_args, args(&["-q"]), "{option}");
    }
}

#[test]
fn grouped_short_options_preserve_value_and_target_boundaries() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::create_dir(root.join("nested")).expect("create nested directory");
    fs::create_dir(root.join("target")).expect("create actual target directory");
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -q\n").expect("write root config");
    fs::write(
        root.join("nested/pytest.ini"),
        "[pytest]\naddopts = -p unrelated\n",
    )
    .expect("write unrelated nested config");

    for arguments in [
        args(&["-qk", "nested"]),
        args(&["-vqk", "nested"]),
        args(&["-qknested", "target"]),
        args(&["-qk=nested", "target"]),
        args(&["-kqcnested", "target"]),
    ] {
        let projection = resolve_pytest_configuration(
            &arguments,
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("short option value must not become a collection target");
        assert_eq!(projection.config_file, Some(root.join("pytest.ini")));
        assert_eq!(projection.configured_args, args(&["-q"]));
    }
}

#[test]
fn grouped_short_configuration_options_keep_attached_and_separate_values() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -q\n").expect("write root config");
    fs::write(root.join("selected.ini"), "[pytest]\naddopts = -x\n")
        .expect("write explicit config");

    for arguments in [
        args(&["-qc", "selected.ini", "-vo", "cache_dir=custom"]),
        args(&["-vqcselected.ini", "-xocache_dir=custom"]),
        args(&["-qc=selected.ini", "-qo=cache_dir=custom"]),
    ] {
        let projection = resolve_pytest_configuration(
            &arguments,
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("resolve grouped configuration options");
        assert_eq!(projection.config_file, Some(root.join("selected.ini")));
        assert_eq!(projection.configured_args, args(&["-x"]));
        assert_eq!(projection.cache_dir, root.join("custom"));
    }
}

#[test]
fn config_free_target_remains_root_when_invocation_shares_only_filesystem_root() {
    let temporary = tempdir().expect("create external test project");
    let target_root = temporary.path();
    let target = target_root.join("test_sample.py");
    fs::write(&target, "def test_sample(): pass\n").expect("write external test");
    let invocation_dir = target_root
        .ancestors()
        .last()
        .expect("find filesystem root");
    let projection = resolve_pytest_configuration(
        &[target.to_string_lossy().into_owned()],
        invocation_dir,
        None,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("resolve config-free external target");

    assert_eq!(projection.config_file, None);
    assert_eq!(projection.rootdir, target_root);
    assert_eq!(projection.cache_dir, target_root.join(".pytest_cache"));
}

#[test]
fn explicit_rootdir_skips_per_target_configuration_fallback() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::create_dir(root.join("a")).expect("create first target");
    fs::create_dir(root.join("b")).expect("create second target");
    fs::write(
        root.join("a/pytest.ini"),
        "[pytest]\naddopts = -p pytest_cov\ncache_dir = nested-cache\n",
    )
    .expect("write target config");

    for policy in [
        PytestConfigPolicy::Legacy,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    ] {
        let projection =
            resolve_pytest_configuration(&args(&["--rootdir=.", "a", "b"]), root, None, policy)
                .expect("explicit rootdir should skip target configs");
        assert_eq!(projection.config_file, None);
        assert_eq!(projection.rootdir, root);
        assert!(projection.configured_args.is_empty());
        assert_eq!(projection.cache_dir, root.join(".pytest_cache"));
    }
}

#[test]
fn explicit_arbitrary_ini_and_rootdir_override_discovery() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let config_dir = root.join("config");
    let execution_dir = root.join("run");
    fs::create_dir_all(&config_dir).expect("create config directory");
    fs::create_dir_all(&execution_dir).expect("create rootdir");
    fs::write(config_dir.join("arbitrary.ini"), "[pytest]\naddopts = -q\n")
        .expect("write explicit config");
    let projection = resolve_pytest_configuration(
        &args(&["-c", "config/arbitrary.ini", "--rootdir", "run", "tests"]),
        root,
        Some("-v"),
        PytestConfigPolicy::Legacy,
    )
    .expect("resolve explicit config");
    assert_eq!(
        projection.config_file,
        Some(config_dir.join("arbitrary.ini"))
    );
    assert_eq!(projection.rootdir, execution_dir);
    assert_eq!(projection.configured_args, args(&["-q", "-v"]));
}

#[test]
fn cfg_prefers_tool_pytest_when_legacy_section_is_also_present() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(
        root.join("setup.cfg"),
        "[tool:pytest]\naddopts = -q\ncache_dir = selected-cache\n\n[pytest]\naddopts = -v\ncache_dir = ignored-cache\n",
    )
    .expect("write pytest config with both section names");

    let projection = resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Toml)
        .expect("prefer pytest's supported .cfg section");

    assert_eq!(projection.config_file, Some(root.join("setup.cfg")));
    assert_eq!(projection.configured_args, args(&["-q"]));
    assert_eq!(projection.cache_dir, root.join("selected-cache"));
}

#[test]
fn explicit_missing_ini_config_fails_during_config_loading() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let missing = root.join("missing.ini");
    let error = resolve_pytest_configuration(
        &args(&["-c", missing.to_str().expect("format missing config path")]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect_err("reject a missing config");
    assert!(error.to_string().contains("failed to read configuration"));
    let (path, cause) = error.read_error().expect("preserve the actual read cause");
    assert_eq!(path, missing);
    assert_eq!(cause.kind(), std::io::ErrorKind::NotFound);
    assert!(cause.raw_os_error().is_some());
}

#[test]
fn selected_configuration_read_errors_remain_distinct_from_syntax() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let directory = root.join("directory.ini");
    fs::create_dir(&directory).expect("create an unreadable configuration directory");
    let failure = resolve_pytest_configuration(
        &args(&["-c", "directory.ini"]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect_err("reject reading a directory");
    let (path, cause) = failure.read_error().expect("preserve filesystem failure");
    assert_eq!(path, directory);
    assert_eq!(cause.kind(), std::io::ErrorKind::IsADirectory);
    fs::write(root.join("bad.toml"), "[tool.pytest.ini_options\n").expect("write malformed TOML");
    let failure = resolve_pytest_configuration(
        &args(&["-c", "bad.toml"]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect_err("reject malformed TOML");
    assert!(failure.read_error().is_none());
}

#[cfg(unix)]
#[test]
fn explicit_null_device_is_an_empty_configuration() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let null_device = Path::new("/dev/null");
    assert!(null_device.exists(), "Unix null device is required");

    let projection = resolve_pytest_configuration(
        &args(&["-c", null_device.to_str().expect("format null device path")]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect("accept pytest's empty null-device config");

    assert_eq!(projection.config_file.as_deref(), Some(null_device));
    assert_eq!(projection.rootdir, Path::new("/dev"));
    assert!(projection.configured_args.is_empty());
    assert_eq!(projection.cache_dir, Path::new("/dev/.pytest_cache"));
}

#[test]
fn configured_environment_and_cli_options_project_in_pytest_order() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(
        root.join("pytest.ini"),
        "[pytest]\naddopts = -q --basetemp config-temp\ncache_dir = config-cache\n",
    )
    .expect("write pytest config");
    let projection = resolve_pytest_configuration(
        &args(&["-s", "-o", "cache_dir=cli-cache", "--basetemp", "cli-temp"]),
        root,
        Some("-v -o cache_dir=env-cache --basetemp env-temp"),
        PytestConfigPolicy::Toml,
    )
    .expect("resolve config and options");
    assert_eq!(
        projection.configured_args,
        args(&[
            "-q",
            "--basetemp",
            "config-temp",
            "-v",
            "-o",
            "cache_dir=env-cache",
            "--basetemp",
            "env-temp"
        ])
    );
    assert_eq!(projection.cache_dir, root.join("cli-cache"));
    assert_eq!(projection.basetemp, Some(root.join("cli-temp")));
}

#[test]
fn config_addopts_override_affects_ini_values_from_pytest_nine_one() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(
        root.join("pytest.ini"),
        "[pytest]\naddopts: --override-ini cache_dir=addopts-cache\n",
    )
    .expect("write config addopts override");

    for policy in [PytestConfigPolicy::Legacy, PytestConfigPolicy::Toml] {
        let projection = resolve_pytest_configuration(&[], root, None, policy)
            .expect("resolve pre-9.1 pytest configuration");
        assert_eq!(projection.cache_dir, root.join(".pytest_cache"));
        assert_eq!(
            projection.configured_args,
            args(&["--override-ini", "cache_dir=addopts-cache"])
        );
    }

    let projection = resolve_pytest_configuration(
        &[],
        root,
        None,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("resolve pytest 9.1 configuration");
    assert_eq!(projection.cache_dir, root.join("addopts-cache"));

    let projection = resolve_pytest_configuration(
        &[],
        root,
        Some("-o cache_dir=env-cache"),
        PytestConfigPolicy::Legacy,
    )
    .expect("environment override applies before pytest 9.1");
    assert_eq!(projection.cache_dir, root.join("env-cache"));
}

#[test]
fn addopts_override_replaces_config_before_environment_arguments() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = --no-cov -q\n")
        .expect("write pytest config");

    let replaced = resolve_pytest_configuration(
        &args(&["-o", "addopts=-v"]),
        root,
        Some("-o addopts=--cov -s"),
        PytestConfigPolicy::Toml,
    )
    .expect("resolve CLI addopts override");
    assert_eq!(
        replaced.configured_args,
        args(&["-v", "-o", "addopts=--cov", "-s"])
    );

    let cleared = resolve_pytest_configuration(
        &args(&["--override-ini=addopts="]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect("resolve empty addopts override");
    assert!(cleared.configured_args.is_empty());

    let last_wins = resolve_pytest_configuration(
        &args(&["-o", "addopts='unterminated", "-o", "addopts=-q"]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect("ignore an earlier overridden addopts value");
    assert_eq!(last_wins.configured_args, args(&["-q"]));

    fs::write(
        root.join("pytest.ini"),
        "[pytest]\naddopts = 'unterminated\n",
    )
    .expect("write malformed overridden addopts");
    let overridden = resolve_pytest_configuration(
        &args(&["-o", "addopts=-q"]),
        root,
        None,
        PytestConfigPolicy::Toml,
    )
    .expect("ignore invalid addopts replaced by CLI");
    assert_eq!(overridden.configured_args, args(&["-q"]));
    assert!(
        resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Toml).is_err(),
        "malformed addopts without an override must still fail"
    );
}

#[test]
fn unterminated_braced_environment_variables_are_preserved() {
    let expression = "${VFY_UNTERMINATED_EXPANSION_SENTINEL";
    assert_eq!(expand_dollar_vars(expression), expression);
    assert_eq!(expand_dollar_vars("${}"), "${}");
}

#[test]
fn pytest_nine_toml_precedence_and_legacy_pytest_eight_policy_are_distinct() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(
        root.join("pytest.toml"),
        "[pytest]\naddopts = ['-x']\ncache_dir = 'toml-cache'\n",
    )
    .expect("write pytest TOML config");
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -q\n").expect("write legacy config");
    let current = resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Toml)
        .expect("resolve pytest 9 policy");
    assert_eq!(current.config_file, Some(root.join("pytest.toml")));
    assert_eq!(current.configured_args, args(&["-x"]));
    assert_eq!(current.cache_dir, root.join("toml-cache"));
    let legacy = resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Legacy)
        .expect("resolve pytest 8 policy");
    assert_eq!(legacy.config_file, Some(root.join("pytest.ini")));
    assert_eq!(legacy.configured_args, args(&["-q"]));
}

#[test]
fn native_pyproject_settings_accept_an_empty_ini_table_but_reject_nonempty_ini_settings() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let path = root.join("pyproject.toml");
    fs::write(
        &path,
        "[tool.pytest]\naddopts = ['-q']\ncache_dir = 'chosen-cache'\n[tool.pytest.ini_options]\n",
    )
    .expect("write mixed config with an empty INI table");
    let projection = resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Toml)
        .expect("empty INI table leaves native settings authoritative");
    assert_eq!(projection.config_file, Some(path.clone()));
    assert_eq!(projection.configured_args, args(&["-q"]));
    assert_eq!(projection.cache_dir, root.join("chosen-cache"));

    fs::write(
        &path,
        "[tool.pytest]\naddopts = ['-q']\n[tool.pytest.ini_options]\ncache_dir = 'other-cache'\n",
    )
    .expect("write conflicting settings in both tables");
    assert!(
        resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Toml)
            .expect_err("nonempty INI table conflicts with native settings")
            .to_string()
            .contains("cannot use both")
    );
}

#[test]
fn ini_mode_scalar_cache_dir_is_stringified_without_relaxing_native_toml_types() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    let path = root.join("pyproject.toml");
    for (literal, expected) in [
        ("123", "123"),
        ("true", "True"),
        ("false", "False"),
        ("1.25", "1.25"),
        ("1.0", "1.0"),
        ("-0.0", "-0.0"),
        ("1e-5", "1e-05"),
        ("1e20", "1e+20"),
        ("1255208858886835.25", "1255208858886835.2"),
        ("inf", "inf"),
        ("nan", "nan"),
    ] {
        fs::write(
            &path,
            format!("[tool.pytest.ini_options]\ncache_dir = {literal}\n"),
        )
        .expect("write scalar INI-mode cache directory");
        for policy in [PytestConfigPolicy::Legacy, PytestConfigPolicy::Toml] {
            let projection = resolve_pytest_configuration(&[], root, None, policy)
                .expect("INI mode converts the scalar to its Python string value");
            assert_eq!(projection.cache_dir, root.join(expected), "{literal}");
        }
        fs::write(&path, format!("[tool.pytest]\ncache_dir = {literal}\n"))
            .expect("write scalar native TOML cache directory");
        assert!(
            resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Toml)
                .expect_err("native TOML mode retains strict string typing")
                .to_string()
                .contains("cache_dir must be a string")
        );
    }
}

#[test]
fn empty_ini_value_starts_with_its_first_continuation() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(
        root.join("pytest.ini"),
        "[pytest]\ncache_dir =\n    generated-cache\naddopts =\n    -q\n    -x\n",
    )
    .expect("write empty-first-line INI values");
    let projection = resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Legacy)
        .expect("resolve continued INI values");
    assert_eq!(projection.cache_dir, root.join("generated-cache"));
    assert_eq!(projection.configured_args, args(&["-q", "-x"]));
}

#[test]
fn legacy_pyproject_ini_options_are_used_while_pytest_toml_is_ignored() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(root.join("pytest.toml"), "[pytest]\naddopts = ['-x']\n")
        .expect("write pytest TOML config");
    fs::write(
        root.join("pyproject.toml"),
        "[tool.pytest.ini_options]\naddopts = '-q'\n",
    )
    .expect("write pyproject config");
    let projection = resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Legacy)
        .expect("resolve pytest 8 policy");
    assert_eq!(projection.config_file, Some(root.join("pyproject.toml")));
    assert_eq!(projection.configured_args, args(&["-q"]));
}

#[test]
fn malformed_selected_configuration_is_reported_before_projection() {
    let temporary = tempdir().expect("create pytest config fixture");
    let root = temporary.path();
    fs::write(root.join("pytest.ini"), "[pytest\naddopts = -q\n").expect("write malformed config");
    let error = resolve_pytest_configuration(&[], root, None, PytestConfigPolicy::Toml)
        .expect_err("reject malformed selected config");
    assert!(error.to_string().contains("pytest.ini"));
}

#[test]
fn shell_words_preserve_quoted_paths_and_reject_unclosed_quotes() {
    assert_eq!(
        split_shell_words("-o 'cache_dir=some path' -q").expect("split quoted args"),
        args(&["-o", "cache_dir=some path", "-q"])
    );
    assert!(split_shell_words("'unfinished").is_err());
    assert_eq!(
        split_shell_words("a\u{00a0}b").expect("split nonbreaking space"),
        args(&["a\u{00a0}b"])
    );
    assert_eq!(
        split_shell_words("\"a\\$b\" \"c\\`d\"").expect("split retained escapes"),
        args(&["a\\$b", "c\\`d"])
    );
    assert_eq!(
        split_shell_words("\"a\\\nb\"").expect("split quoted newline"),
        args(&["a\\\nb"])
    );
}

#[test]
fn all_ini_settings_preserve_multiline_values_and_final_override_precedence() {
    let temporary = tempdir().expect("create INI settings project");
    let root = temporary.path();
    fs::write(root.join("pytest.ini"), "[pytest]\naddopts = -o feature=from-addopts\nfeature = from-file\nmarkers =\n    slow: slow tests\n    cpu: standard tests\n").expect("write INI settings");
    let config = resolve_pytest_configuration(
        &args(&["-o", "feature=from-cli"]),
        root,
        Some("-o feature=from-environment"),
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("parse INI settings");
    assert_eq!(
        config.ini_values["markers"].value,
        toml::Value::String("slow: slow tests\ncpu: standard tests".to_owned())
    );
    assert_eq!(
        config.ini_values["feature"],
        PytestIniSetting {
            value: toml::Value::String("from-cli".to_owned()),
            mode: PytestIniMode::Ini,
            overridden: true,
        }
    );
}

#[test]
fn native_toml_settings_keep_native_types_while_overrides_use_ini_mode() {
    let temporary = tempdir().expect("create TOML settings project");
    let root = temporary.path();
    fs::write(
        root.join("pytest.toml"),
        "[pytest]\nenabled = true\ncount = 7\nlabels = ['first', 'second']\n",
    )
    .expect("write TOML settings");
    let config = resolve_pytest_configuration(
        &args(&["-o", "count=9"]),
        root,
        None,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
    )
    .expect("parse TOML settings");
    assert_eq!(
        config.ini_values["enabled"].value,
        toml::Value::Boolean(true)
    );
    assert_eq!(config.ini_values["enabled"].mode, PytestIniMode::Toml);
    assert_eq!(config.ini_values["count"].mode, PytestIniMode::Ini);
    assert_eq!(
        config.ini_values["count"].value,
        toml::Value::String("9".to_owned())
    );
}

#[test]
fn independent_result_output_operand_is_not_an_explicit_test_file() {
    let temporary = tempdir().expect("create independent output scope fixture");
    let root = temporary.path();
    fs::write(root.join("pytest.ini"), "[pytest]\n").expect("write scope configuration");
    for (arguments, filtered) in [
        (args(&["--native-result-json", "report.py"]), false),
        (args(&["--native-result-json=report.py"]), false),
        (
            args(&["--native-result-json", "report.py", "test_actual.py"]),
            true,
        ),
    ] {
        let projection = resolve_pytest_configuration(
            &arguments,
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("resolve independent result output scope");
        assert_eq!(projection.filtered_scope, filtered, "{arguments:?}");
    }
}

#[test]
fn independent_result_option_retains_raw_discovery_operand_semantics() {
    let arguments = args(&["--native-result-json", "report.py"]);
    let policy = PytestConfigPolicy::TomlWithAddoptsOverrides;
    let discovery = parse_arguments(
        &arguments,
        ArgumentPhase::Discovery,
        policy,
        PytestParserBehavior::Current,
    );
    let effective = parse_arguments(
        &arguments,
        ArgumentPhase::Effective,
        policy,
        PytestParserBehavior::Current,
    );
    assert_eq!(discovery.targets, args(&["report.py"]));
    assert!(effective.targets.is_empty());
}

#[test]
fn invocation_location_uses_actual_effective_positionals_without_narrowing_line_size() {
    let temporary = tempdir().expect("temporary invocation scope");
    let root = temporary.path();
    for (arguments, expected) in [
        (args(&["test_scope.py:3"]), Some("test_scope.py:3")),
        (args(&["--", "test_scope.py:3"]), Some("test_scope.py:3")),
        (args(&["--", "--native-result-json", "report.py:3"]), None),
        (args(&["--native-result-json", "report.py:3"]), None),
        (args(&["--native-result-json=report.py:3"]), None),
        (
            args(&["--native-result-json", "report.py:3", "test_scope.py:0003"]),
            Some("test_scope.py:0003"),
        ),
        (
            args(&["test_scope.py:999999999999999999999999999999999999999"]),
            Some("test_scope.py:999999999999999999999999999999999999999"),
        ),
        (args(&["test_scope.py:0"]), None),
        (args(&["test_scope.py:000"]), None),
        (args(&["test_scope.py:+3"]), None),
        (args(&["test_scope.py:-3"]), None),
        (args(&["test_scope.py:٣"]), None),
        (args(&["test_scope.py:"]), None),
        (args(&[":3"]), None),
        (args(&["test_scope.py::test_actual"]), None),
        (args(&["test_scope.py:3", "tests"]), None),
        (args(&["tests"]), None),
    ] {
        let projection = resolve_pytest_configuration(
            &arguments,
            root,
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("resolve actual invocation targets");
        assert_eq!(
            projection.invocation_location.as_deref(),
            expected,
            "{arguments:?}"
        );
    }
}

#[test]
fn invocation_location_uses_expanded_environment_and_argument_file_positionals() {
    let temporary = tempdir().expect("temporary argument files");
    let root = temporary.path();
    fs::write(root.join("inner.args"), "test_scope.py:3\n").expect("write inner arguments");
    fs::write(root.join("outer.args"), "@inner.args\n").expect("write outer arguments");
    fs::write(
        root.join("output.args"),
        "--native-result-json\nreport.py:3\n",
    )
    .expect("write output operand arguments");
    for (arguments, environment, expected) in [
        (args(&["@outer.args"]), None, Some("test_scope.py:3")),
        (args(&["@output.args"]), None, None),
        (args(&[]), Some("test_scope.py:3"), Some("test_scope.py:3")),
        (
            args(&["--native-result-json", "report.py:3"]),
            Some("test_scope.py:3"),
            Some("test_scope.py:3"),
        ),
    ] {
        let projection = resolve_pytest_configuration(
            &arguments,
            root,
            environment,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
        )
        .expect("resolve expanded invocation location");
        assert_eq!(projection.invocation_location.as_deref(), expected);
    }
}

#[test]
fn invocation_location_preserves_discovery_and_filter_classification() {
    let arguments = args(&["--native-result-json", "report.py:3"]);
    let policy = PytestConfigPolicy::TomlWithAddoptsOverrides;
    assert_eq!(
        parse_arguments(
            &arguments,
            ArgumentPhase::Discovery,
            policy,
            PytestParserBehavior::Current
        )
        .targets,
        args(&["report.py:3"])
    );
    assert!(
        parse_arguments(
            &arguments,
            ArgumentPhase::Effective,
            policy,
            PytestParserBehavior::Current
        )
        .targets
        .is_empty()
    );
    let temporary = tempdir().expect("temporary selected configuration");
    let root = temporary.path();
    fs::write(
        root.join("pytest.ini"),
        "[pytest]\naddopts = -k configured\n",
    )
    .expect("write selected config");
    let projection = resolve_pytest_configuration(&args(&["test_scope.py:3"]), root, None, policy)
        .expect("resolve same selected configuration");
    assert_eq!(projection.config_file, Some(root.join("pytest.ini")));
    assert_eq!(
        projection.invocation_location.as_deref(),
        Some("test_scope.py:3")
    );
    assert!(!projection.filtered_scope);
}

#[test]
fn final_intermixed_separator_follows_runtime_and_option_value_boundaries() {
    let directory = tempdir().expect("create parser fixture");
    for (arguments, legacy_collect, legacy_filtered) in [
        (args(&["--", "--collect-only"]), true, true),
        (args(&["--", "-k", "wanted"]), false, true),
        (args(&["--", "--ignore=other"]), false, true),
        (args(&["-k", "wanted", "--", "--co"]), true, true),
        (args(&["directory", "--", "--co"]), false, false),
        (args(&["--", "--", "--co"]), false, false),
    ] {
        let legacy = resolve_pytest_configuration_with_parser(
            &arguments,
            directory.path(),
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
            PytestParserBehavior::Legacy,
        )
        .expect("resolve legacy parser arguments");
        assert_eq!(
            (legacy.collect_only, legacy.filtered_scope),
            (legacy_collect, legacy_filtered),
            "{arguments:?}"
        );
        let modern = resolve_pytest_configuration_with_parser(
            &arguments,
            directory.path(),
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
            PytestParserBehavior::Current,
        )
        .expect("resolve modern parser arguments");
        assert!(!modern.collect_only, "{arguments:?}");
    }
}

#[test]
fn configured_positionals_and_separator_affect_invocation_scope() {
    let directory = tempdir().expect("create configured parser fixture");
    for (configured, parser, expected) in [
        ("directory", PytestParserBehavior::Legacy, false),
        ("-q", PytestParserBehavior::Legacy, true),
        ("--", PytestParserBehavior::Current, false),
    ] {
        fs::write(
            directory.path().join("pytest.ini"),
            format!("[pytest]\naddopts = {configured}\n"),
        )
        .expect("write configured arguments");
        let projection = resolve_pytest_configuration_with_parser(
            &args(&["--", "--collect-only"]),
            directory.path(),
            None,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
            parser,
        )
        .expect("resolve configured and invocation arguments");
        assert_eq!(projection.collect_only, expected, "{configured:?}");
        assert_eq!(projection.filtered_scope, expected, "{configured:?}");
    }
}

#[test]
fn discovery_handles_unknown_options_after_separator_like_runtime() {
    let arguments = args(&["test.py", "-q", "--", "--co", "other.py"]);
    for (parser, targets) in [
        (PytestParserBehavior::Legacy, args(&["test.py", "other.py"])),
        (
            PytestParserBehavior::FixedFinal,
            args(&["test.py", "other.py"]),
        ),
        (
            PytestParserBehavior::Current,
            args(&["test.py", "--co", "other.py"]),
        ),
    ] {
        let parsed = parse_arguments(
            &arguments,
            ArgumentPhase::Discovery,
            PytestConfigPolicy::Toml,
            parser,
        );
        assert_eq!(parsed.targets, targets);
    }
}
