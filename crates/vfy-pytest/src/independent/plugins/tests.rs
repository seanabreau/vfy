use super::hypothesis_version_supported;

#[test]
fn hypothesis_admission_accepts_final_6x_releases_from_the_adapter_floor() {
    for (version, expected) in [
        ("6.152.0", false),
        ("6.152.1", true),
        ("6.170.1", true),
        ("6.170.9", true),
        ("6.200.0", true),
        ("6.170.5rc1", false),
        ("6.170", false),
        ("7.0.0", false),
        ("5.999.0", false),
    ] {
        assert_eq!(hypothesis_version_supported(version), expected, "{version}");
    }
}
