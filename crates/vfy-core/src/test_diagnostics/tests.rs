use super::{
    DiagnosticFormat, DiagnosticFrame, DiagnosticLocation, ExceptionDescription, frame_source,
    render_exception,
};

#[test]
fn display_and_crash_keep_their_separately_captured_assertion_text() {
    use super::{
        DiagnosticCrash, DiagnosticException, ExceptionChain, ExceptionNameStyle, TracebackState,
        render_exception_chain,
    };
    let location = DiagnosticLocation {
        path: "test_value.py".to_owned(),
        line: 2,
    };
    let description = |text: &str| {
        ExceptionDescription {
            name: "AssertionError".to_owned(),
            text: text.to_owned(),
        }
        .with_name_style(ExceptionNameStyle::AbbreviatedAssertion)
    };
    let chain = ExceptionChain {
        first: DiagnosticException {
            frames: vec![DiagnosticFrame {
                function: "test_value".to_owned(),
                auto_style: None,
                locals: None,
                location: location.clone(),
                source: frame_source("def test_value():\n    assert 1 == 2\n", 1, 2),
                arguments: Vec::new(),
            }],
            traceback: TracebackState::Visible,
            description: description("AssertionError: assert display\nretained note\n"),
            crash: Some(DiagnosticCrash {
                location,
                text: description("AssertionError: assert crash\nretained note\n").text,
            }),
        },
        following: Vec::new(),
    };
    let actual = render_exception_chain(
        &chain,
        &DiagnosticFormat {
            width: 80,
            entry_separator: "",
        },
    );
    assert!(
        actual
            .detail
            .contains("E       assert display\nE       retained note")
    );
    assert!(!actual.detail.contains("assert crash"));
    assert_eq!(actual.exception, "assert crash\nretained note");
    assert_eq!(actual.crash.map(|location| location.line), Some(2));
    assert_eq!(
        ExceptionDescription {
            name: "CustomAssertion".to_owned(),
            text: "CustomAssertion: assert retained".to_owned(),
        }
        .with_name_style(ExceptionNameStyle::AbbreviatedAssertion)
        .text,
        "CustomAssertion: assert retained"
    );
}

#[test]
fn decorated_fixture_failure_matches_the_retained_pytest_coverage_witness() {
    let source = "import pytest, target\n@pytest.mark.no_cover\ndef test_target(no_cover):\n    assert target.choose(False) == 0\n    assert target.choose(True) == 1\n    assert False, \"deliberate witness failure\"\n";
    let frame = DiagnosticFrame {
        function: "test_target".to_owned(),
        auto_style: None,
        locals: None,
        location: DiagnosticLocation {
            path: "test_target.py".to_owned(),
            line: 6,
        },
        source: frame_source(source, 2, 6),
        arguments: vec![("no_cover".to_owned(), "None".to_owned())],
    };
    let result = render_exception(
        &[frame],
        &ExceptionDescription {
            name: "AssertionError".to_owned(),
            text: "AssertionError: deliberate witness failure\nassert False".to_owned(),
        },
        &DiagnosticFormat {
            width: 80,
            entry_separator: "",
        },
    );
    // Pinned pytest's original early-coverage observer record, not candidate output.
    let expected = "no_cover = None\n\n    @pytest.mark.no_cover\n    def test_target(no_cover):\n        assert target.choose(False) == 0\n        assert target.choose(True) == 1\n>       assert False, \"deliberate witness failure\"\nE       AssertionError: deliberate witness failure\nE       assert False\n\ntest_target.py:6: AssertionError";
    assert_eq!(result.detail, expected);
    assert_eq!(result.terminal_lead, "\n");
    assert_eq!(
        result.exception,
        "AssertionError: deliberate witness failure\nassert False"
    );
}

#[test]
fn source_deindents_class_methods_and_keeps_the_complete_multiline_statement() {
    let source = "class TestValue:\n    @decorator\n    def test_value(self, value):\n        assert (\n            value ==\n            42\n        )\n        unreachable()\n";
    let frame = frame_source(source, 2, 5).expect("the failed expression has source");
    assert_eq!(
        frame.lines,
        [
            "@decorator",
            "def test_value(self, value):",
            "    assert (",
            "        value ==",
            "        42",
            "    )",
        ]
    );
    assert_eq!(frame.exception_indent, 8);
}

#[test]
fn missing_expected_exception_retains_the_actual_context_expression_span() {
    let source = "import pytest\n@pytest.mark.parametrize('value', ['fixture-value'])\ndef test_value(value):\n    with pytest.raises(ValueError):\n        pass\n";
    let frame = DiagnosticFrame {
        function: "test_target".to_owned(),
        auto_style: None,
        locals: None,
        location: DiagnosticLocation {
            path: "test_value.py".to_owned(),
            line: 4,
        },
        source: frame_source(source, 2, 4).map(|source| source.with_position(9..34)),
        arguments: vec![("value".to_owned(), "'fixture-value'".to_owned())],
    };
    let actual = render_exception(
        &[frame],
        &ExceptionDescription {
            name: "Failed".to_owned(),
            text: "Failed: DID NOT RAISE ValueError".to_owned(),
        },
        &DiagnosticFormat {
            width: 80,
            entry_separator: "",
        },
    );
    let expected = "value = 'fixture-value'\n\n    @pytest.mark.parametrize('value', ['fixture-value'])\n    def test_value(value):\n>       with pytest.raises(ValueError):\n             ^^^^^^^^^^^^^^^^^^^^^^^^^\nE       Failed: DID NOT RAISE ValueError\n\ntest_value.py:4: Failed";
    assert_eq!(actual.detail, expected);
}

#[test]
fn causal_exception_keeps_both_sources_and_the_actual_terminal_separator() {
    use super::{DiagnosticException, ExceptionChain, ExceptionRelation, render_exception_chain};
    let source = "def test_value():\n    try:\n        raise ValueError('original')\n    except ValueError as error:\n        raise TypeError('replacement') from error\n";
    let entry = |line, name: &str, text: &str| DiagnosticException {
        traceback: super::TracebackState::Visible,
        frames: vec![DiagnosticFrame {
            function: "test_target".to_owned(),
            auto_style: None,
            locals: None,
            location: DiagnosticLocation {
                path: "test_value.py".to_owned(),
                line,
            },
            source: frame_source(source, 1, line),
            arguments: Vec::new(),
        }],
        description: ExceptionDescription {
            name: name.to_owned(),
            text: text.to_owned(),
        },
        crash: Some(super::DiagnosticCrash {
            location: DiagnosticLocation {
                path: "test_value.py".to_owned(),
                line,
            },
            text: text.to_owned(),
        }),
    };
    let chain = ExceptionChain {
        first: entry(3, "ValueError", "ValueError: original"),
        following: vec![(
            ExceptionRelation::Cause,
            entry(5, "TypeError", "TypeError: replacement"),
        )],
    };
    let actual = render_exception_chain(
        &chain,
        &DiagnosticFormat {
            width: 80,
            entry_separator: "",
        },
    );
    let expected = "def test_value():\n        try:\n>           raise ValueError('original')\nE           ValueError: original\n\ntest_value.py:3: ValueError\n\nThe above exception was the direct cause of the following exception:\n\n    def test_value():\n        try:\n            raise ValueError('original')\n        except ValueError as error:\n>           raise TypeError('replacement') from error\nE           TypeError: replacement\n\ntest_value.py:5: TypeError";
    assert_eq!(actual.detail, expected);
    assert_eq!(actual.exception, "TypeError: replacement");
}
