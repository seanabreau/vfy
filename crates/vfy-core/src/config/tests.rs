use super::resolve_cli_options;
use serde_json::json;

#[test]
fn pytest_explicit_zero_thresholds_override_configuration() {
    let config = json!({
        "report": {"precision": 2, "fail_under": 95},
        "vfy": {
            "run": {"decisions": true},
            "check": {"fail_under_decisions": 90, "fail_under_conditions": 80}
        }
    });
    let arguments = json!({
        "command": "pytest",
        "cov_decisions": false,
        "cov_precision": 0,
        "cov_fail_under": 0,
        "cov_fail_under_decisions": 0,
        "cov_fail_under_conditions": null,
        "cov_fail_under_mcdc": null
    });
    let resolved = resolve_cli_options(
        arguments.as_object().expect("test arguments").clone(),
        &config,
        None,
    )
    .expect("valid pytest options");
    assert_eq!(resolved["cov_decisions"], true);
    assert_eq!(resolved["cov_precision"], 0);
    assert_eq!(resolved["cov_fail_under"], 0);
    assert_eq!(resolved["cov_fail_under_decisions"], 0);
    assert_eq!(resolved["cov_fail_under_conditions"], 80);
    assert!(resolved["cov_fail_under_mcdc"].is_null());
}

#[test]
fn native_test_defaults_to_serial_and_keeps_explicit_worker_settings() {
    let arguments = json!({
        "command": "test",
        "engine": "pytest-native",
        "workers": null,
        "fresh": false,
        "history": null,
        "data_file": null,
        "source": null
    });
    let resolved = resolve_cli_options(
        arguments.as_object().expect("test arguments").clone(),
        &json!({}),
        None,
    )
    .expect("valid native test options");
    assert_eq!(resolved["workers"], 1);

    let configured = resolve_cli_options(
        arguments.as_object().expect("test arguments").clone(),
        &json!({"vfy": {"test": {"workers": "2"}}}),
        None,
    )
    .expect("valid configured worker count");
    assert_eq!(configured["workers"], 2);

    let cli_override = json!({
        "command": "test",
        "engine": "pytest-native",
        "workers": 1,
        "fresh": false,
        "history": null,
        "data_file": null,
        "source": null
    });
    let configured = resolve_cli_options(
        cli_override.as_object().expect("test arguments").clone(),
        &json!({"vfy": {"test": {"workers": "2"}}}),
        None,
    )
    .expect("valid CLI worker override");
    assert_eq!(configured["workers"], 1);
}

#[test]
fn test_mode_is_safe_selection_unless_configured_and_all_forces_safe() {
    let resolve = |fresh: bool, config: serde_json::Value| {
        let arguments = json!({
            "command": "test",
            "engine": "pytest-native",
            "workers": null,
            "fresh": fresh,
            "history": null,
            "data_file": null,
            "source": null
        });
        resolve_cli_options(
            arguments.as_object().expect("test arguments").clone(),
            &config,
            None,
        )
        .expect("valid test options")["mode"]
            .clone()
    };
    let balanced = json!({"vfy": {"test": {"selection": "balanced"}}});

    assert_eq!(resolve(false, json!({})), "safe");
    assert_eq!(resolve(false, balanced.clone()), "balanced");
    assert_eq!(resolve(true, balanced), "safe");
}

#[test]
fn stock_pytest_test_keeps_auto_worker_default() {
    let arguments = json!({
        "command": "test",
        "engine": "pytest",
        "workers": null,
        "fresh": false,
        "history": null,
        "data_file": null,
        "source": null
    });
    let resolved = resolve_cli_options(
        arguments.as_object().expect("test arguments").clone(),
        &json!({}),
        None,
    )
    .expect("valid stock pytest options");
    assert_eq!(resolved["workers"], "auto");
}

#[test]
fn report_omit_ignores_the_measurement_omit_setting() {
    let config = json!({
        "run": {"omit": ["*/measured_only.py"]},
        "report": {"omit": ["*/reported_only.py"]}
    });
    let resolve = |config: &serde_json::Value| {
        let arguments = json!({"command": "report", "omit": null});
        resolve_cli_options(
            arguments.as_object().expect("test arguments").clone(),
            config,
            None,
        )
        .expect("valid report options")
    };
    assert_eq!(resolve(&config)["omit"], json!(["*/reported_only.py"]));
    assert!(resolve(&json!({"run": {"omit": ["*/measured_only.py"]}}))["omit"].is_null());
}
