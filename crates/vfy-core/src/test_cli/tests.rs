use super::parse_test_cli;
use serde_json::json;
use std::collections::BTreeSet;

fn parse(arguments: &[&str]) -> serde_json::Value {
    let args = arguments
        .iter()
        .map(|arg| (*arg).to_owned())
        .collect::<Vec<_>>();
    parse_test_cli(
        &args,
        &BTreeSet::from([
            "-1".to_owned(),
            "-.5".to_owned(),
            "-1.0".to_owned(),
            "-١".to_owned(),
        ]),
    )
    .expect("the test command should parse")
}

#[test]
fn test_controls_after_pytest_options_are_extracted() {
    let parsed = parse(&[
        "test",
        "--cov=src",
        "--all",
        "--source",
        "lib",
        "-k",
        "smoke",
    ]);
    assert_eq!(parsed["fresh"], json!(true));
    assert_eq!(parsed["source"], json!(["lib"]));
    assert_eq!(parsed["target"], json!([]));
    assert_eq!(parsed["pytest_args"], json!(["--cov=src", "-k", "smoke"]));
}

#[test]
fn independent_demand_control_is_extracted_before_forwarded_arguments() {
    let parsed = parse(&[
        "test",
        "--engine=native",
        "--native-scheduling=demand",
        "-j4",
        "-q",
    ]);
    assert_eq!(parsed["native_scheduling"], "demand");
    assert_eq!(parsed["workers"], 4);
    assert_eq!(parsed["pytest_args"], json!(["-q"]));
}

#[test]
fn abbreviated_controls_after_forwarded_pytest_options_remain_forwarded() {
    let parsed = parse(&["test", "-k", "chosen", "--work", "2"]);
    assert_eq!(parsed["workers"], json!(null));
    assert_eq!(
        parsed["pytest_args"],
        json!(["-k", "chosen", "--work", "2"])
    );
}

#[test]
fn repeated_grouped_global_verbosity_accumulates() {
    let parsed = parse(&["-v", "-vv", "test"]);
    assert_eq!(parsed["verbose"], json!(3));
}

#[test]
fn abbreviated_global_color_is_forwarded_to_pytest() {
    let parsed = parse(&["--col=always", "test"]);
    assert_eq!(parsed["color"], json!("always"));
    assert_eq!(parsed["pytest_args"], json!(["--color=yes"]));
}

#[test]
fn serial_xdist_override_is_forwarded_at_its_original_position() {
    for engine in ["native", "pytest-native"] {
        for arguments in [vec!["-n0"], vec!["-n=0"], vec!["-n", "0"]] {
            let mut command = vec!["test", "--engine", engine];
            command.extend(arguments);
            command.extend(["one.py", "--all"]);
            let parsed = parse(&command);
            assert_eq!(parsed["workers"], json!(1));
            assert_eq!(parsed["target"], json!([]));
            assert_eq!(parsed["pytest_args"], json!(["--numprocesses=0", "one.py"]));
        }
    }
}

#[test]
fn abbreviated_options_after_a_target_are_forwarded() {
    let parsed = parse(&["test", "one.py", "--work", "2"]);
    assert_eq!(parsed["target"], json!(["one.py"]));
    assert_eq!(parsed["workers"], json!(null));
    assert_eq!(parsed["pytest_args"], json!(["--work", "2"]));
}

#[test]
fn negative_numeric_targets_follow_argparse_decimal_rules() {
    let parsed = parse(&["test", "-1", "-.5"]);
    assert_eq!(parsed["target"], json!(["-1", "-.5"]));
    let parsed = parse(&["test", "-1e3"]);
    assert_eq!(parsed["target"], json!([]));
    assert_eq!(parsed["pytest_args"], json!(["-1e3"]));
}

#[test]
fn color_consumes_negative_numeric_values_as_invalid_choices() {
    for value in ["-1", "-.5", "-1.0", "-", "-١"] {
        let args = ["test".to_owned(), "--color".to_owned(), value.to_owned()];
        assert!(
            parse_test_cli(
                &args,
                &BTreeSet::from([
                    "-1".to_owned(),
                    "-.5".to_owned(),
                    "-1.0".to_owned(),
                    "-١".to_owned()
                ])
            )
            .is_err(),
            "color must reject {value}"
        );
    }
}

#[test]
fn exponent_notation_is_an_option_where_a_value_is_required() {
    let args = ["test".to_owned(), "--source".to_owned(), "-1e3".to_owned()];
    assert!(
        parse_test_cli(
            &args,
            &BTreeSet::from([
                "-1".to_owned(),
                "-.5".to_owned(),
                "-1.0".to_owned(),
                "-١".to_owned()
            ])
        )
        .is_err()
    );
}

#[test]
fn ambiguity_is_checked_until_the_first_positional() {
    let args = [
        "test".to_owned(),
        "-q".to_owned(),
        "--plan-o".to_owned(),
        "out".to_owned(),
    ];
    assert!(
        parse_test_cli(
            &args,
            &BTreeSet::from([
                "-1".to_owned(),
                "-.5".to_owned(),
                "-1.0".to_owned(),
                "-١".to_owned()
            ])
        )
        .is_err()
    );
    let parsed = parse(&["test", "-k", "chosen", "--plan-o", "out"]);
    assert_eq!(
        parsed["pytest_args"],
        json!(["-k", "chosen", "--plan-o", "out"])
    );
}

#[test]
fn decimal_option_lexing_uses_the_interpreter_digit_policy() {
    for digit in ['\u{10d40}', '\u{11de0}'] {
        let args = [
            "test".to_owned(),
            "--source".to_owned(),
            format!("-{digit}"),
        ];
        assert!(parse_test_cli(&args, &BTreeSet::new()).is_err());
        let parsed = parse_test_cli(&args, &BTreeSet::from([format!("-{digit}")]))
            .expect("the interpreter recognizes this decimal digit");
        assert_eq!(parsed["source"], json!([format!("-{digit}")]));
    }
}

#[test]
fn negative_token_policy_tracks_the_active_interpreter() {
    let token = "-1e3".to_owned();
    let args = ["test".to_owned(), "--source".to_owned(), token.clone()];
    assert!(parse_test_cli(&args, &BTreeSet::new()).is_err());
    let parsed = parse_test_cli(&args, &BTreeSet::from([token.clone()]))
        .expect("the interpreter accepts a negative-number prefix");
    assert_eq!(parsed["source"], json!([token]));
}

#[test]
fn unknown_dash_arguments_containing_spaces_are_positional() {
    let parsed = parse(&["test", "--source", "-z has space"]);
    assert_eq!(parsed["source"], json!(["-z has space"]));
    for option in ["-n has space", "--all=has space"] {
        let args = ["test".to_owned(), "--source".to_owned(), option.to_owned()];
        assert!(parse_test_cli(&args, &BTreeSet::new()).is_err());
    }
}
