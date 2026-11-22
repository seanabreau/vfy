use std::path::Path;

use serde_json::{Map, json};

use super::{
    Kind, TomlOwnership, discover, normalize, normalize_paths, normalize_value, parse_ini,
    parse_toml, select_toml_config, selected_toml_config, substitute_environment,
};

fn initialize_python() {
    pyo3::Python::initialize();
}

#[test]
fn configuration_and_api_reject_the_same_unsupported_patch_choices() {
    initialize_python();
    let path = Path::new(".coveragerc");
    for patches in [vec![], vec!["_exit"], vec!["subprocess", "_exit"]] {
        let config = Map::from_iter([("run".to_owned(), json!({"patch": patches}))]);
        normalize(config, path).expect("supported configuration patches");
        super::validate_run_patch(patches.into_iter().map(str::to_owned).collect())
            .expect("supported API patches");
    }
    for patch in ["fork", "execv", "unknown"] {
        let config = Map::from_iter([("run".to_owned(), json!({"patch": ["_exit", patch]}))]);
        let config_error = normalize(config, path).expect_err("unsupported configuration patch");
        let api_error = super::validate_run_patch(vec!["_exit".to_owned(), patch.to_owned()])
            .expect_err("unsupported API patch");
        assert_eq!(
            config_error.to_string(),
            format!("{api_error} (.coveragerc)")
        );
        assert!(api_error.to_string().contains(patch));
    }
}

#[test]
fn scalar_and_list_normalization_accepts_coverage_style_values() {
    assert_eq!(
        normalize_value(Kind::Bool, &json!("yes")),
        Some(json!(true))
    );
    assert_eq!(
        normalize_value(Kind::Bool, &json!("OFF")),
        Some(json!(false))
    );
    assert_eq!(
        normalize_value(Kind::Integer, &json!(" 7 ")),
        Some(json!(7))
    );
    assert_eq!(
        normalize_value(Kind::Number, &json!("99.5")),
        Some(json!(99.5))
    );
    assert_eq!(
        normalize_value(Kind::List, &json!("one, two\nthree")),
        Some(json!(["one", "two", "three"]))
    );
    assert_eq!(normalize_value(Kind::Bool, &json!("perhaps")), None);
    assert_eq!(normalize_value(Kind::Number, &json!("NaN")), None);
}

#[test]
fn vfy_settings_validate_domains_before_execution() {
    initialize_python();
    let path = Path::new("pyproject.toml");
    for (section, key, invalid) in [
        ("test", "selection", json!("sfae")),
        ("test", "workers", json!("0")),
        ("test", "workers", json!(0)),
        ("test", "workers", json!(1.5)),
        ("test", "workers", json!(true)),
        ("mutate", "timeout", json!(-1)),
        ("mutate", "timeout", json!("NaN")),
        ("mutate", "timeout_policy", json!("disabled")),
        ("check", "format", json!("yaml")),
        ("check", "fail_under_mcdc", json!(101)),
    ] {
        let options = json!({key: invalid});
        let config = Map::from_iter([("vfy".to_owned(), json!({section: options}))]);
        let error = normalize(config, path).expect_err("invalid setting must fail validation");
        assert!(error.to_string().contains(key), "{error}");
        assert!(super::validate_vfy_options_json(section, &options.to_string()).is_err());
    }
    let valid = json!({
        "test": {"selection": "balanced", "workers": "logical"},
        "mutate": {"timeout": 0.1, "timeout_policy": "fixed", "workers": 2},
        "check": {"format": "json", "fail_under_decisions": 0, "fail_under_mcdc": 100}
    });
    normalize(Map::from_iter([("vfy".to_owned(), valid)]), path).expect("valid boundary values");
}

#[test]
fn environment_substitution_handles_escaping_defaults_and_strict_failures() {
    initialize_python();
    let path = Path::new(".coveragerc");
    let path_value = std::env::var("PATH").expect("PATH is present in test environment");
    assert_eq!(
        substitute_environment(
            "$$PATH:$PATH:${VFY_MISSING-fallback}",
            path,
            "run",
            "data_file"
        )
        .expect("environment substitution"),
        format!("$PATH:{path_value}:fallback")
    );
    let error = substitute_environment("${VFY_MISSING?}", path, "run", "data_file")
        .expect_err("strict substitution must fail");
    assert!(error.to_string().contains("VFY_MISSING"));
}

#[test]
fn ini_parser_resolves_defaults_multiline_values_and_namespaced_sections() {
    initialize_python();
    let directory = tempfile::tempdir().expect("temporary config directory");
    let path = directory.path().join("setup.cfg");
    std::fs::write(
        &path,
        "[DEFAULT]\nsource =\n    package\n    helpers\n\n[coverage:run]\nbranch = yes\n",
    )
    .expect("write setup.cfg");
    let parsed = parse_ini(&path).expect("parse setup.cfg");
    assert_eq!(parsed["run"]["branch"], "yes");
    assert_eq!(parsed["run"]["source"], "\npackage\nhelpers");

    let coveragerc = directory.path().join(".coveragerc");
    std::fs::write(
        &coveragerc,
        "[run]\nbranch = true\n[run]\nparallel = true\n",
    )
    .expect("write duplicate config");
    assert!(parse_ini(&coveragerc).is_err());
}

#[test]
fn toml_selection_keeps_coverage_and_vfy_tables_from_pyproject() {
    let directory = tempfile::tempdir().expect("temporary config directory");
    let path = directory.path().join("pyproject.toml");
    std::fs::write(
        &path,
        "[tool.coverage.run]\nbranch = true\n[tool.vfy.run]\ndecisions = true\n",
    )
    .expect("write pyproject.toml");
    let selected =
        selected_toml_config(&path, TomlOwnership::Shared).expect("select pyproject config");
    assert_eq!(selected["run"]["branch"], true);
    assert_eq!(selected["vfy"]["run"]["decisions"], true);

    let raw = parse_toml(&path).expect("parse pyproject.toml");
    assert_eq!(raw["tool"]["coverage"]["run"]["branch"], true);
}

#[test]
fn own_toml_reads_bare_sections_per_section_and_shared_toml_does_not() {
    let path = Path::new("coverage.toml");
    let value = json!({
        "tool": {"coverage": {"run": {"patch": ["subprocess"]}}},
        "run": {"branch": true},
        "report": {"fail_under": 100},
        "coverage": {"html": {"directory": "ignored"}}
    });
    let own = select_toml_config(&value, path, TomlOwnership::Own).expect("own TOML");
    assert_eq!(own["run"], json!({"patch": ["subprocess"]}));
    assert_eq!(own["report"]["fail_under"], 100);
    assert!(own.get("coverage").is_none());
    let shared = select_toml_config(&value, path, TomlOwnership::Shared).expect("shared TOML");
    assert!(shared.get("report").is_none());
}

#[test]
fn normalize_rejects_unknown_options_and_invalid_patch_values() {
    initialize_python();
    let path = Path::new("pyproject.toml");
    let mut unknown = Map::new();
    unknown.insert("run".to_owned(), json!({"not_an_option": true}));
    let error = normalize(unknown, path).expect_err("unknown option must fail");
    assert!(error.to_string().contains("unsupported option"));

    let invalid_patch = Map::from_iter([("run".to_owned(), json!({"patch": ["threads"]}))]);
    let error = normalize(invalid_patch, path).expect_err("unsupported patch must fail");
    assert!(error.to_string().contains("unsupported [run] patch"));

    let report = Map::from_iter([("report".to_owned(), json!({"ignore_errors": true}))]);
    let normalized = normalize(report, path).expect("ignore_errors is a supported report option");
    assert_eq!(normalized["report"]["ignore_errors"], true);
}

#[test]
fn normalize_accepts_measured_concurrency_and_rejects_greenlet_libraries() {
    initialize_python();
    let path = Path::new("pyproject.toml");
    let supported = Map::from_iter([(
        "run".to_owned(),
        json!({"concurrency": "thread,\nmultiprocessing"}),
    )]);
    let normalized = normalize(supported, path).expect("thread and multiprocessing are measured");
    assert_eq!(
        normalized["run"]["concurrency"],
        json!(["thread", "multiprocessing"])
    );

    let unknown = Map::from_iter([("run".to_owned(), json!({"concurrency": ["threads"]}))]);
    let error = normalize(unknown, path).expect_err("unknown concurrency must fail");
    assert!(
        error
            .to_string()
            .contains("unknown [run] concurrency choice(s): threads")
    );

    let greenlet = Map::from_iter([(
        "run".to_owned(),
        json!({"concurrency": ["thread", "gevent"]}),
    )]);
    let error = normalize(greenlet, path).expect_err("greenlet libraries are not measured");
    assert!(
        error
            .to_string()
            .contains("unsupported [run] concurrency value(s): gevent")
    );
}

#[test]
fn normalize_accepts_known_cores_and_warning_slugs() {
    initialize_python();
    let path = Path::new("pyproject.toml");
    let supported = Map::from_iter([(
        "run".to_owned(),
        json!({"core": "sysmon", "disable_warnings": "no-data-collected,\nmodule-not-imported"}),
    )]);
    let normalized = normalize(supported, path).expect("sysmon and warning slugs are accepted");
    assert_eq!(normalized["run"]["core"], "sysmon");
    assert_eq!(
        normalized["run"]["disable_warnings"],
        json!(["no-data-collected", "module-not-imported"])
    );

    for core in ["ctrace", "pytrace"] {
        let config = Map::from_iter([("run".to_owned(), json!({"core": core}))]);
        assert!(
            normalize(config, path).is_ok(),
            "{core} is a tracer preference"
        );
    }
    let config = Map::from_iter([("run".to_owned(), json!({"core": "sysmonitor"}))]);
    let error = normalize(config, path).expect_err("unknown cores are rejected");
    assert!(
        error
            .to_string()
            .contains("unknown [run] core value: sysmonitor")
    );
}

#[test]
fn normalize_paths_requires_nonempty_aliases_and_rejects_terminal_wildcards() {
    initialize_python();
    let path = Path::new("pyproject.toml");
    assert!(normalize_paths(json!({"src": ["src", "build/src"]}), path).is_ok());
    assert!(normalize_paths(json!({"src": []}), path).is_err());
    assert!(normalize_paths(json!({"src": ["src/*"]}), path).is_err());
    assert!(normalize_paths(json!("src"), path).is_err());
}

#[test]
fn discovery_prefers_explicit_names_and_skips_unrelated_tox_sections() {
    initialize_python();
    let directory = tempfile::tempdir().expect("temporary config directory");
    let root = directory.path();
    std::fs::write(
        root.join("tox.ini"),
        "[tox]\nenvlist = py314\n[testenv]\ncommands = pytest\n",
    )
    .expect("write tox.ini");
    std::fs::write(
        root.join("pyproject.toml"),
        "[tool.coverage.run]\nbranch = true\n",
    )
    .expect("write pyproject.toml");
    assert_eq!(
        discover(root.to_str()).expect("discover config"),
        Some(vfy_core::canonical_path(&root.join("pyproject.toml")).expect("canonical path"))
    );

    std::fs::write(root.join(".coveragerc"), "[run]\nbranch = true\n").expect("write .coveragerc");
    assert_eq!(
        discover(root.to_str()).expect("discover config"),
        Some(vfy_core::canonical_path(&root.join(".coveragerc")).expect("canonical path"))
    );
}

#[test]
fn resolved_project_config_preserves_discovery_precedence_and_explicit_errors() {
    initialize_python();
    let directory = tempfile::tempdir().expect("temporary config directory");
    let root = directory.path();
    let nested = root.join("package");
    std::fs::create_dir(&nested).expect("create nested directory");
    let project = root.join("pyproject.toml");
    let local = nested.join("pyproject.toml");
    std::fs::write(
        &project,
        "[tool.coverage.run]\nbranch = true\n[tool.vfy.test]\nselection = 'safe'\n",
    )
    .expect("write project config");
    std::fs::write(&local, "[tool.coverage.run]\nbranch = false\n").expect("write local config");

    let resolved: serde_json::Value = serde_json::from_str(
        &super::resolve_config_json("project", None, nested.to_str())
            .expect("resolve parent vfy config"),
    )
    .expect("decode resolved config");
    let canonical_project = vfy_core::canonical_path(&project).expect("canonical project config");
    assert_eq!(
        resolved["source"],
        canonical_project.to_string_lossy().as_ref()
    );
    assert_eq!(resolved["discovery"], "nearest [tool.vfy]");
    assert_eq!(resolved["settings"]["run"]["branch"], true);

    std::fs::write(&project, "[tool.coverage.run]\nbranch = true\n").expect("remove vfy settings");
    let fallback: serde_json::Value = serde_json::from_str(
        &super::resolve_config_json("project", None, nested.to_str())
            .expect("resolve local Coverage.py fallback"),
    )
    .expect("decode fallback config");
    let canonical_local = vfy_core::canonical_path(&local).expect("canonical local config");
    assert_eq!(
        fallback["source"],
        canonical_local.to_string_lossy().as_ref()
    );
    assert_eq!(fallback["discovery"], "Coverage.py fallback");
    assert_eq!(fallback["settings"]["run"]["branch"], false);

    std::fs::write(&local, "[tool.coverage.run\n").expect("write invalid local TOML");
    let missing: serde_json::Value = serde_json::from_str(
        &super::resolve_config_json("project", None, nested.to_str())
            .expect("skip malformed implicit candidate"),
    )
    .expect("decode defaults");
    assert_eq!(missing["discovery"], "defaults");
    assert_eq!(missing["settings"], serde_json::json!({}));
    let error = super::resolve_config_json("project", local.to_str(), nested.to_str())
        .expect_err("explicit malformed TOML must fail");
    assert!(error.to_string().contains("failed to read TOML"));
}
