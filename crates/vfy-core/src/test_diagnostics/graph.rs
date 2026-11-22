//! Structured public traceback entries projected from the captured diagnostic.

use super::{
    DiagnosticException, DiagnosticFrame, DiagnosticFrameStyle, DiagnosticLocation, FrameSource,
    HIDDEN_TRACEBACK_MESSAGE, TracebackState, frame_style, python_rstrip, split_python_strip,
};

/// Explicit traceback presentation; automatic mode shortens middle frames only.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiagnosticStyle {
    Auto,
    Long,
    Short,
}

impl DiagnosticStyle {
    /// Choose before capturing frame arguments, whose repr calls are observable.
    #[must_use]
    pub fn frame(self, index: usize, count: usize) -> DiagnosticFrameStyle {
        match self {
            Self::Auto => frame_style(index, count),
            Self::Long => DiagnosticFrameStyle::Long,
            Self::Short => DiagnosticFrameStyle::Short,
        }
    }
}

/// A view of one captured frame, suitable for a mutable frontend representation.
pub struct DiagnosticEntry {
    pub lines: Vec<String>,
    pub arguments: Option<Vec<(String, String)>>,
    pub locals: Option<Vec<String>>,
    pub location: Option<(DiagnosticLocation, String)>,
    pub style: DiagnosticFrameStyle,
}

/// The captured exception's public entries without a second exception model.
pub struct DiagnosticTraceback<'a> {
    pub native: Option<&'a [String]>,
    pub entries: Vec<DiagnosticEntry>,
    pub extra: Option<&'static str>,
}

pub(super) fn source_lines(source: Option<&FrameSource>, short: bool) -> Vec<String> {
    let Some(source) = source else {
        return vec![if short { "    ???" } else { ">   ???" }.to_owned()];
    };
    let mut lines = Vec::new();
    for (offset, text) in source.lines.iter().enumerate() {
        let failed = source
            .first_line
            .saturating_add(i32::try_from(offset).unwrap_or(i32::MAX))
            == source.failure_line;
        if short && !failed {
            continue;
        }
        let body = if short {
            split_python_strip(text).1
        } else {
            text
        };
        lines.push(format!(
            "{}{body}",
            if failed && !short { ">   " } else { "    " }
        ));
        if failed && let Some(highlight) = &source.highlight {
            let leading = if short {
                split_python_strip(text).0.len()
            } else {
                0
            };
            let start = text[..highlight.start]
                .chars()
                .count()
                .saturating_sub(text[..leading].chars().count());
            lines.push(format!(
                "{}{}",
                " ".repeat(4 + start),
                "^".repeat(text[highlight.clone()].chars().count())
            ));
        }
    }
    lines
}

fn entry(
    frame: &DiagnosticFrame,
    style: DiagnosticFrameStyle,
    last: bool,
    exception: &DiagnosticException,
) -> DiagnosticEntry {
    let short = style == DiagnosticFrameStyle::Short;
    let mut lines = source_lines(frame.source.as_ref(), short);
    if last {
        let indent = if short {
            4
        } else {
            frame
                .source
                .as_ref()
                .map_or(4, |source| source.exception_indent)
        };
        for line in python_rstrip(&exception.description.text).split('\n') {
            lines.push(format!("E{}{line}", " ".repeat(indent.saturating_sub(1))));
        }
    }
    DiagnosticEntry {
        lines,
        arguments: (!short).then(|| frame.arguments.clone()),
        locals: frame.locals.clone(),
        location: Some((
            frame.location.clone(),
            if short {
                format!("in {}", frame.function)
            } else if last {
                exception.description.name.clone()
            } else {
                String::new()
            },
        )),
        style,
    }
}

/// Project captured source and messages without parsing rendered text.
#[must_use]
pub fn project_traceback(
    exception: &DiagnosticException,
    style: DiagnosticStyle,
) -> DiagnosticTraceback<'_> {
    if let TracebackState::Native { lines, hidden } = &exception.traceback {
        return DiagnosticTraceback {
            entries: Vec::new(),
            native: Some(lines),
            extra: hidden.then_some(HIDDEN_TRACEBACK_MESSAGE),
        };
    }
    let count = exception.frames.len();
    let entries = if count == 0 {
        let lines = python_rstrip(&exception.description.text)
            .split('\n')
            .enumerate()
            .map(|(index, line)| {
                format!(
                    "{}   {line}",
                    if matches!(exception.traceback, TracebackState::Hidden) && index > 0 {
                        ' '
                    } else {
                        'E'
                    }
                )
            })
            .collect();
        vec![DiagnosticEntry {
            lines,
            arguments: None,
            locals: None,
            location: None,
            style: if style == DiagnosticStyle::Short {
                DiagnosticFrameStyle::Short
            } else {
                DiagnosticFrameStyle::Long
            },
        }]
    } else {
        exception
            .frames
            .iter()
            .enumerate()
            .map(|(index, frame)| {
                entry(
                    frame,
                    frame.style(style, index, count),
                    index + 1 == count,
                    exception,
                )
            })
            .collect()
    };
    DiagnosticTraceback {
        entries,
        native: None,
        extra: matches!(exception.traceback, TracebackState::Hidden)
            .then_some(HIDDEN_TRACEBACK_MESSAGE),
    }
}

#[cfg(test)]
mod tests;
