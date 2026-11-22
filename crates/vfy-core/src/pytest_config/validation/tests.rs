use super::*;

fn policy(
    strict: bool,
    strict_config: Option<bool>,
    strict_markers: Option<bool>,
) -> ConfigurationPolicy {
    StrictnessSettings {
        strict,
        strict_config,
        strict_markers,
        strict_xfail: None,
    }
    .resolve()
}

#[test]
fn strictness_respects_explicit_false_and_unknown_setting_order() {
    let relaxed = policy(true, Some(false), Some(false));
    assert_eq!(
        relaxed.unknown_ini_diagnostics(&["z_unknown".to_owned(), "a_unknown".to_owned()]),
        [
            ConfigurationDiagnostic::ConfigWarning("Unknown config option: a_unknown\n".to_owned()),
            ConfigurationDiagnostic::ConfigWarning("Unknown config option: z_unknown\n".to_owned())
        ]
    );
    let strict = policy(true, None, None);
    assert_eq!(
        strict.unknown_ini_diagnostics(&["z_unknown".to_owned(), "a_unknown".to_owned()]),
        [ConfigurationDiagnostic::UsageError(
            "Unknown config option: a_unknown\n".to_owned()
        )]
    );
    assert!(strict.strict_xfail);
}

#[test]
fn marker_declarations_update_validation_and_preserve_pytest_failures() {
    let strict = policy(false, None, Some(true));
    assert_eq!(
        strict.marker_diagnostic("custom", &[]),
        Some(ConfigurationDiagnostic::MarkerError(
            "'custom' not found in `markers` configuration option".to_owned()
        ))
    );
    assert_eq!(
        strict.marker_diagnostic("custom", &[" custom(reason): description".to_owned()]),
        None
    );
    assert_eq!(
        strict.marker_diagnostic("parameterize", &[]),
        Some(ConfigurationDiagnostic::MarkerError(
            "Unknown 'parameterize' mark, did you mean 'parametrize'?".to_owned()
        ))
    );
    assert_eq!(
        strict.marker_diagnostic("parameterize", &["parameterize: declared".to_owned()]),
        None
    );
    assert!(
        matches!(policy(false, None, None).marker_diagnostic("custom", &[]), Some(ConfigurationDiagnostic::UnknownMarkWarning(message)) if message.starts_with("Unknown pytest.mark.custom"))
    );
}

#[test]
fn required_plugin_validation_matches_names_versions_and_requirement_syntax() {
    let plugins = vec![
        ("demo".to_owned(), "1.5rc1".to_owned()),
        ("other".to_owned(), "2.0.post1".to_owned()),
    ];
    for requirement in [
        "demo>=1.4",
        "demo[extra]>=1.4; python_version < '0'",
        "demo @ arbitrary-url",
        "other==2.0.post1",
    ] {
        assert_eq!(
            required_plugins_diagnostic(&[requirement.to_owned()], &plugins)
                .expect("requirement validation"),
            None,
            "{requirement}"
        );
    }
    assert_eq!(
        required_plugins_diagnostic(
            &[
                "Demo>=1".to_owned(),
                "demo>=2".to_owned(),
                "not a requirement".to_owned(),
                "absent".to_owned()
            ],
            &plugins
        )
        .expect("missing requirement validation"),
        Some(ConfigurationDiagnostic::UsageError(
            "Missing required plugins: Demo>=1, absent, demo>=2, not a requirement".to_owned()
        ))
    );
    assert_eq!(
        required_plugins_diagnostic(&["demo".to_owned()], &[])
            .expect("unloaded installed distribution"),
        Some(ConfigurationDiagnostic::UsageError(
            "Missing required plugins: demo".to_owned()
        ))
    );
}

#[test]
fn minimum_pytest_version_uses_pep440_ordering() {
    assert_eq!(
        minimum_version_diagnostic("9.0", "9.1.1", None).expect("supported minimum"),
        None
    );
    assert_eq!(
        minimum_version_diagnostic("9.2", "9.1.1", None).expect("unsupported minimum"),
        Some(ConfigurationDiagnostic::UsageError(
            "None: 'minversion' requires pytest-9.2, actual pytest-9.1.1'".to_owned()
        ))
    );
    assert!(minimum_version_diagnostic("invalid", "9.1.1", None).is_err());
}
