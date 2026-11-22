use super::*;

#[test]
fn parses_required_plugin_syntax_without_normalizing_names_or_evaluating_markers() {
    let version: Version = "1.5rc1".parse().unwrap();
    for source in [
        "Demo",
        "Demo[extra,Other]>=1.4; python_version < '0'",
        "Demo @ opaque-url",
        "Demo @ x;this-is-still-a-url",
        "Demo()",
        "Demo(>=1.4,)",
        "Demo; os.name == 'unknown' and (extra in 'a' or dependency_groups != 'b')",
        "Demo; platform.python_implementation not in 'unknown'",
        "Demo==1.5rc1",
        "Demo>=\u{1c}1.4",
        "Demo===\u{1c}1.5rc1",
    ] {
        let requirement = PluginRequirement::parse(source).expect(source);
        assert_eq!(requirement.name, "Demo", "{source}");
        assert!(requirement.contains(&version), "{source}");
    }
}

#[test]
fn rejects_malformed_requirements_and_preserves_arbitrary_equality() {
    for source in [
        "",
        "Demo-",
        "Demo.",
        "Demo[extra,]",
        "Demo[,]",
        "Demo>=1,,<2",
        "Demo>=1\n",
        "Demo; unknown == 'x'",
        "Demo; os_name notin 'x'",
        "Demo; (os_name == 'x'",
        "Demo; os_name == 'x' and",
        "Demo; ()",
        "Demo; os_name == 'x\n'",
        "Demo; os_name == '\\xZZ'",
        "Demo @ ",
    ] {
        assert!(PluginRequirement::parse(source).is_none(), "{source:?}");
    }
    for source in ["Demo===", "Demo===not-a-version", "Demo===foo,,>=2"] {
        let requirement = PluginRequirement::parse(source).expect(source);
        assert!(!requirement.contains(&"1.5".parse().unwrap()), "{source}");
    }
    assert!(
        PluginRequirement::parse("Demo===1.5RC1")
            .unwrap()
            .contains(&"1.5rc1".parse().unwrap())
    );
}
