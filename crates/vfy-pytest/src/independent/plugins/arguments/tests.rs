use super::{PluginDirective, plugin_arguments, raw_provider_excluded, validate_exclusion};

#[test]
fn literal_preparse_preserves_operands_and_ignores_argparse_short_option_groups() {
    let args = [
        "-spno:terminal",
        "-p",
        " no:terminal ",
        "-pno:cacheprovider",
        "--",
        "-p",
        "no:runner",
        "-p=no:terminal",
        "-p",
    ]
    .map(str::to_owned);
    let actual = plugin_arguments(&args)
        .map(PluginDirective::parse)
        .collect::<Vec<_>>();
    assert_eq!(
        actual,
        [
            PluginDirective::Disable("terminal"),
            PluginDirective::Disable("cacheprovider"),
            PluginDirective::Disable("runner"),
            PluginDirective::Enable("=no:terminal"),
        ]
    );
}

#[test]
fn essential_exclusions_are_exact_names_and_conftest_blocks_are_usage_errors() {
    for name in ["mark", "main", "runner", "fixtures", "helpconfig"] {
        assert_eq!(
            validate_exclusion(name),
            Err(format!("plugin {name} cannot be disabled"))
        );
        assert!(validate_exclusion(&format!("pytest_{name}")).is_ok());
    }
    assert!(validate_exclusion("path/conftest.py").is_err());
    assert!(validate_exclusion("terminal").is_ok());
}

#[test]
fn programmatic_plugin_directives_keep_the_callers_original_name() {
    assert_eq!(
        PluginDirective::parse(" no:terminal "),
        PluginDirective::Enable(" no:terminal ")
    );
    assert_eq!(
        PluginDirective::parse("no:terminal "),
        PluginDirective::Disable("terminal ")
    );
    let args = ["-p".to_owned(), " no:terminal ".to_owned()];
    assert_eq!(
        plugin_arguments(&args)
            .map(PluginDirective::parse)
            .collect::<Vec<_>>(),
        [PluginDirective::Disable("terminal")]
    );
}

#[test]
fn raw_provider_exclusion_keeps_its_exact_name_and_separate_install_boundary() {
    let arguments = [" no:terminal ", "no:cacheprovider", "no:fixtures"].map(str::to_owned);
    assert!(raw_provider_excluded(&arguments, "cacheprovider"));
    assert!(raw_provider_excluded(&arguments, "fixtures"));
    assert!(!raw_provider_excluded(&arguments, "terminal"));
    assert!(!raw_provider_excluded(&arguments, "doctest"));
}
