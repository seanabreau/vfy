use super::{SupportedPlugin, bounded_version};
use pep440_rs::Version;

#[test]
fn plugin_ranges_use_pep440_release_ordering() {
    for (plugin, candidate, expected) in [
        (SupportedPlugin::Benchmark, "5.2.post999", false),
        (SupportedPlugin::Benchmark, "0!05.003.0+local", true),
        (SupportedPlugin::Benchmark, "5.3.post1", true),
        (SupportedPlugin::Benchmark, "5.3.999999", true),
        (SupportedPlugin::Benchmark, "5.3rc1", false),
        (SupportedPlugin::Benchmark, "5.4.dev1", false),
        (SupportedPlugin::Benchmark, "5.4", false),
        (SupportedPlugin::Mock, "3.12.post999", false),
        (SupportedPlugin::Mock, "v03.013.0+local", true),
        (SupportedPlugin::Mock, "3.99.999999", true),
        (SupportedPlugin::Mock, "3.13.post1.dev1", false),
        (SupportedPlugin::Mock, "3.14.dev1", false),
        (SupportedPlugin::Mock, "4.0rc1", false),
        (SupportedPlugin::Mock, "1!3.13", false),
        (SupportedPlugin::Timeout, "2.4.post1+local", true),
        (SupportedPlugin::Timeout, "2.4.999999", true),
        (SupportedPlugin::Timeout, "2.3.post999", false),
        (SupportedPlugin::Timeout, "2.4rc1", false),
        (SupportedPlugin::Timeout, "2.5.dev1", false),
        (SupportedPlugin::Timeout, "2.5", false),
        (SupportedPlugin::Randomly, "5.0.0", true),
        (SupportedPlugin::Randomly, "5.0.0+local", false),
        (SupportedPlugin::Randomly, "5.0.0.post1", false),
        (SupportedPlugin::Randomly, "5.0.1", false),
        (SupportedPlugin::Randomly, "4.0", false),
    ] {
        let parsed = candidate.parse::<Version>().expect("valid version fixture");
        assert_eq!(plugin.contains(&parsed), expected, "{candidate}");
    }
}

#[test]
fn arbitrary_precision_components_preserve_gate_comparisons() {
    let large = "9".repeat(700);
    for (candidate, expected) in [
        (format!("3.13.{large}"), true),
        (format!("3.{large}"), true),
        (format!("{large}.13"), false),
        (format!("3.13+{large}"), true),
        (format!("{large}!3.13"), false),
        (format!("3.13.post{large}"), true),
    ] {
        let normalized = bounded_version(&candidate);
        let parsed = normalized
            .parse::<Version>()
            .expect("bounded version fixture");
        assert_eq!(
            SupportedPlugin::Mock.contains(&parsed),
            expected,
            "{candidate}"
        );
    }
    assert_eq!(bounded_version(&format!("3.{}13", "0".repeat(700))), "3.13");
}

#[test]
fn trimming_preserves_python_whitespace_and_ascii_grammar() {
    assert_eq!(bounded_version("\u{1c}\u{a0}V03.013\u{85}"), "V3.13");
    for candidate in ["٣.١٣", "3.13poſt1", "3.13+\u{212a}", "3. 13", "3.13+"] {
        assert!(
            bounded_version(candidate).parse::<Version>().is_err(),
            "{candidate}"
        );
    }
}

#[test]
fn version_components_beyond_the_integer_digit_limit_raise_value_error() {
    use pyo3::prelude::*;
    Python::initialize();
    Python::attach(|py| {
        let sys = py.import("sys").expect("sys");
        let previous = sys
            .call_method0("get_int_max_str_digits")
            .expect("current digit limit");
        sys.call_method1("set_int_max_str_digits", (640,))
            .expect("lower digit limit");
        let within = super::pytest_plugin_version_supported(
            py,
            "mock",
            &format!("3.13.{}", "9".repeat(640)),
        );
        let beyond = super::pytest_plugin_version_supported(
            py,
            "mock",
            &format!("3.13.{}", "9".repeat(641)),
        );
        sys.call_method1("set_int_max_str_digits", (previous,))
            .expect("restore digit limit");
        assert!(within.expect("limit-sized component is accepted"));
        let error = beyond.expect_err("oversized component raises");
        assert!(error.is_instance_of::<pyo3::exceptions::PyValueError>(py));
    });
}
