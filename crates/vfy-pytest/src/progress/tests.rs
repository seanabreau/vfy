use super::format_duration;

#[test]
fn duration_format_uses_precision_bands_and_unit_transitions() {
    for (seconds, expected) in [
        (0.000_009_999, " 9.999us"),
        (0.000_01, " 10.00us"),
        (0.000_05, " 50.00us"),
        (0.000_1, " 100.0us"),
        (0.000_5, " 500.0us"),
        (0.001, " 1.000ms"),
        (0.01, " 10.00ms"),
        (0.1, " 100.0ms"),
        (1.0, " 1.000s"),
        (60.0, " 1m 0s"),
        (3_600.0, " 1h 0m"),
    ] {
        assert_eq!(format_duration(seconds), expected, "seconds={seconds}");
    }
}
