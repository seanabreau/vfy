use super::*;
use crate::test_diagnostics::{DiagnosticCrash, ExceptionDescription, frame_source};

#[test]
fn same_captured_frame_projects_long_and_short_without_losing_crash_or_arguments() {
    let frame = DiagnosticFrame {
        function: "test_body".to_owned(),
        auto_style: None,
        locals: None,
        location: DiagnosticLocation {
            path: "test_body.py".to_owned(),
            line: 2,
        },
        source: frame_source(
            "def test_body(value):\n    raise ValueError('actual')\n",
            1,
            2,
        ),
        arguments: vec![("value".to_owned(), "7".to_owned())],
    };
    let exception = DiagnosticException {
        traceback: TracebackState::Visible,
        frames: vec![frame],
        description: ExceptionDescription {
            name: "ValueError".to_owned(),
            text: "ValueError: actual\n".to_owned(),
        },
        crash: Some(DiagnosticCrash {
            location: DiagnosticLocation {
                path: "/actual/test_body.py".to_owned(),
                line: 2,
            },
            text: "ValueError: actual".to_owned(),
        }),
    };
    let long = project_traceback(&exception, DiagnosticStyle::Long);
    assert_eq!(
        long.entries[0].lines,
        [
            "    def test_body(value):",
            ">       raise ValueError('actual')",
            "E       ValueError: actual"
        ]
    );
    assert_eq!(
        long.entries[0].arguments.as_ref().expect("long arguments")[0],
        ("value".to_owned(), "7".to_owned())
    );
    let short = project_traceback(&exception, DiagnosticStyle::Short);
    assert_eq!(
        short.entries[0].lines,
        ["    raise ValueError('actual')", "E   ValueError: actual"]
    );
    assert!(short.entries[0].arguments.is_none());
    assert_eq!(
        short.entries[0].location.as_ref().expect("location").1,
        "in test_body"
    );
    assert_eq!(
        exception.crash.as_ref().expect("same crash").location.path,
        "/actual/test_body.py"
    );
}

#[test]
fn native_projection_borrows_the_actual_captured_line_sequence() {
    let exception = DiagnosticException {
        traceback: TracebackState::Native {
            lines: vec![
                "actual native header\n".to_owned(),
                "actual detail\n".to_owned(),
            ],
            hidden: true,
        },
        frames: Vec::new(),
        description: ExceptionDescription {
            name: "ExceptionGroup".to_owned(),
            text: "group".to_owned(),
        },
        crash: None,
    };
    let projected = project_traceback(&exception, DiagnosticStyle::Long);
    assert!(projected.entries.is_empty());
    let TracebackState::Native { lines, .. } = &exception.traceback else {
        panic!("native source model")
    };
    assert_eq!(
        projected.native.expect("native view").as_ptr(),
        lines.as_ptr()
    );
    assert_eq!(projected.extra, Some(HIDDEN_TRACEBACK_MESSAGE));
}

#[test]
fn automatic_short_entry_survives_filtering_with_explicit_style_overrides() {
    let frame = DiagnosticFrame {
        function: "helper".to_owned(),
        location: DiagnosticLocation {
            path: "helpers.py".to_owned(),
            line: 2,
        },
        source: frame_source("def helper(value):\n    raise ValueError('actual')\n", 1, 2),
        arguments: vec![("value".to_owned(), "retained".to_owned())],
        auto_style: Some(DiagnosticFrameStyle::Short),
        locals: None,
    };
    let exception = DiagnosticException {
        frames: vec![frame],
        traceback: TracebackState::Visible,
        description: ExceptionDescription {
            name: "ValueError".to_owned(),
            text: "ValueError: actual".to_owned(),
        },
        crash: None,
    };
    let automatic = project_traceback(&exception, DiagnosticStyle::Auto);
    assert_eq!(automatic.entries[0].style, DiagnosticFrameStyle::Short);
    assert!(automatic.entries[0].arguments.is_none());
    assert_eq!(
        automatic.entries[0].lines,
        ["    raise ValueError('actual')", "E   ValueError: actual"]
    );
    let rendered = crate::test_diagnostics::render_exception(
        &exception.frames,
        &exception.description,
        &crate::test_diagnostics::DiagnosticFormat {
            width: 80,
            entry_separator: "",
        },
    );
    assert_eq!(
        rendered.detail,
        "helpers.py:2: in helper\n    raise ValueError('actual')\nE   ValueError: actual"
    );
    let long = project_traceback(&exception, DiagnosticStyle::Long);
    assert_eq!(long.entries[0].style, DiagnosticFrameStyle::Long);
    assert_eq!(
        long.entries[0].arguments.as_ref().expect("long args").len(),
        1
    );
    let short = project_traceback(&exception, DiagnosticStyle::Short);
    assert_eq!(short.entries[0].style, DiagnosticFrameStyle::Short);

    let mut first = exception.frames[0].clone();
    first.auto_style = None;
    first.function = "test_body".to_owned();
    first.source = frame_source("def test_body():\n    helper('value')\n", 1, 2);
    first.arguments.clear();
    let frames = [first, exception.frames[0].clone()];
    let rendered = crate::test_diagnostics::render_exception(
        &frames,
        &exception.description,
        &crate::test_diagnostics::DiagnosticFormat {
            width: 80,
            entry_separator: "---",
        },
    );
    assert_eq!(
        rendered.detail,
        "def test_body():\n>       helper('value')\n\nhelpers.py:2: \n---\nhelpers.py:2: in helper\n    raise ValueError('actual')\nE   ValueError: actual"
    );
}

#[test]
fn captured_locals_survive_both_frame_styles_and_follow_the_exception() {
    for style in [DiagnosticFrameStyle::Long, DiagnosticFrameStyle::Short] {
        let frame = DiagnosticFrame {
            function: "test_body".to_owned(),
            location: DiagnosticLocation {
                path: "test_body.py".to_owned(),
                line: 2,
            },
            source: frame_source("def test_body():\n    raise ValueError('actual')\n", 1, 2),
            arguments: Vec::new(),
            locals: Some(vec!["value      = 7".to_owned()]),
            auto_style: Some(style),
        };
        let exception = DiagnosticException {
            frames: vec![frame],
            traceback: TracebackState::Visible,
            description: ExceptionDescription {
                name: "ValueError".to_owned(),
                text: "ValueError: actual".to_owned(),
            },
            crash: None,
        };
        let projected = project_traceback(&exception, DiagnosticStyle::Auto);
        assert_eq!(projected.entries[0].locals, exception.frames[0].locals);
        let rendered = crate::test_diagnostics::render_exception(
            &exception.frames,
            &exception.description,
            &crate::test_diagnostics::DiagnosticFormat {
                width: 80,
                entry_separator: "",
            },
        );
        let expected = match style {
            DiagnosticFrameStyle::Long => {
                "def test_body():\n>       raise ValueError('actual')\nE       ValueError: actual\n\nvalue      = 7\n\ntest_body.py:2: ValueError"
            }
            DiagnosticFrameStyle::Short => {
                "test_body.py:2: in test_body\n    raise ValueError('actual')\nE   ValueError: actual\n        value      = 7"
            }
        };
        assert_eq!(rendered.detail, expected);
    }
}
