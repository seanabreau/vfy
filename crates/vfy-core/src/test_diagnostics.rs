//! Source and terminal projections for exceptions captured by a test frontend.

use crate::{SourceLocator, analysis, test_execution::split_python_strip};
use std::fmt::Write;
use std::ops::Range;

mod graph;
pub use graph::{DiagnosticEntry, DiagnosticStyle, DiagnosticTraceback, project_traceback};

/// Explain the absence of visible frames in either traceback presentation.
pub const HIDDEN_TRACEBACK_MESSAGE: &str =
    "All traceback entries are hidden. Pass `--full-trace` to see hidden and internal frames.";

/// Source belonging to the failing code object, with one-based physical lines.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FrameSource {
    pub first_line: i32,
    pub failure_line: i32,
    pub lines: Vec<String>,
    pub exception_indent: usize,
    indent_bytes: usize,
    highlight: Option<Range<usize>>,
}

impl FrameSource {
    /// Retain a single-line `CPython` bytecode span when it narrows the statement.
    #[must_use]
    pub fn with_position(mut self, columns: Range<usize>) -> Self {
        let Some(offset) = self.failure_line.checked_sub(self.first_line) else {
            return self;
        };
        let Ok(offset) = usize::try_from(offset) else {
            return self;
        };
        let Some(line) = self.lines.get(offset) else {
            return self;
        };
        let Some(start) = columns.start.checked_sub(self.indent_bytes) else {
            return self;
        };
        let Some(end) = columns.end.checked_sub(self.indent_bytes) else {
            return self;
        };
        if start >= end || !line.is_char_boundary(start) || !line.is_char_boundary(end) {
            return self;
        }
        let body_start = split_python_strip(line).0.len();
        if start == body_start && end == line.len() {
            return self;
        }
        self.highlight = Some(start..end);
        self
    }
}

fn python_rstrip(text: &str) -> &str {
    let (leading, body) = split_python_strip(text);
    if body.is_empty() {
        ""
    } else {
        &text[..leading.len() + body.len()]
    }
}

/// Locate a complete failing statement using logical-line analysis.
/// The code object's first line includes its decorators, as `CPython` records it.
#[must_use]
pub fn frame_source(source: &str, first_line: i32, failure_line: i32) -> Option<FrameSource> {
    if first_line < 1 || failure_line < first_line {
        return None;
    }
    let locator = SourceLocator::new(source);
    locator.line_start(failure_line)?;
    let multiline = analysis::multiline_map(source);
    let statement_start = multiline
        .get(&failure_line)
        .copied()
        .unwrap_or(failure_line);
    let last_line = multiline
        .range(failure_line..)
        .take_while(|(_, start)| **start == statement_start)
        .map(|(line, _)| *line)
        .last()
        .unwrap_or(failure_line);
    let mut lines = (first_line..=last_line)
        .map(|line| python_rstrip(locator.line_text(line)).to_owned())
        .collect::<Vec<_>>();
    let indent = lines
        .iter()
        .filter(|line| !line.trim_matches([' ', '\t']).is_empty())
        .map(|line| &line[..line.len() - line.trim_start_matches([' ', '\t']).len()])
        .reduce(|left, right| {
            let common = left
                .bytes()
                .zip(right.bytes())
                .take_while(|(left, right)| left == right)
                .count();
            &left[..common]
        })
        .unwrap_or("")
        .len();
    for line in &mut lines {
        if line.trim_matches([' ', '\t']).is_empty() {
            line.clear();
        } else {
            line.drain(..indent);
        }
    }
    let statement_offset = usize::try_from(statement_start.saturating_sub(first_line)).ok()?;
    let exception_indent = 4 + lines
        .get(statement_offset)
        .map_or(0, |line| split_python_strip(line).0.chars().count());
    Some(FrameSource {
        first_line,
        failure_line,
        lines,
        exception_indent,
        indent_bytes: indent,
        highlight: None,
    })
}

/// The automatic traceback keeps full context at its first and last visible frames.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiagnosticFrameStyle {
    Long,
    Short,
}

/// Select a frame style before the frontend asks user objects for representations.
#[must_use]
pub fn frame_style(index: usize, frame_count: usize) -> DiagnosticFrameStyle {
    if index > 0 && index < frame_count.saturating_sub(1) {
        DiagnosticFrameStyle::Short
    } else {
        DiagnosticFrameStyle::Long
    }
}

/// A display location already resolved by the owning frontend's path policy.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DiagnosticLocation {
    pub path: String,
    pub line: i32,
}

/// Captured frame values are rendered once by `CPython` before entering this model.
#[derive(Clone, Debug)]
pub struct DiagnosticFrame {
    pub function: String,
    pub location: DiagnosticLocation,
    pub source: Option<FrameSource>,
    pub arguments: Vec<(String, String)>,
    pub locals: Option<Vec<String>>,
    pub auto_style: Option<DiagnosticFrameStyle>,
}

impl DiagnosticFrame {
    /// Keep automatic entry choices through later filtering; explicit styles win.
    #[must_use]
    pub fn style(
        &self,
        presentation: DiagnosticStyle,
        index: usize,
        count: usize,
    ) -> DiagnosticFrameStyle {
        match presentation {
            DiagnosticStyle::Auto => self.auto_style.unwrap_or_else(|| frame_style(index, count)),
            other => other.frame(index, count),
        }
    }
}

/// `CPython`'s exception-only text, including any PEP 678 notes in their order.
#[derive(Clone, Debug)]
pub struct ExceptionDescription {
    pub name: String,
    pub text: String,
}

/// Whether the captured assertion representation selects pytest's short name.
#[derive(Clone, Copy, Debug)]
pub enum ExceptionNameStyle {
    Complete,
    AbbreviatedAssertion,
}

impl ExceptionDescription {
    /// Abbreviate only the exact built-in name selected when the error was captured.
    #[must_use]
    pub fn with_name_style(mut self, style: ExceptionNameStyle) -> Self {
        if matches!(style, ExceptionNameStyle::AbbreviatedAssertion)
            && let Some(text) = self.text.strip_prefix("AssertionError: ")
        {
            self.text = text.to_owned();
        }
        self
    }
}

/// The independently captured crash message can differ from displayed text.
#[derive(Clone, Debug)]
pub struct DiagnosticCrash {
    pub location: DiagnosticLocation,
    pub text: String,
}

/// Distinguish hidden source entries from an exception without a traceback.
#[derive(Clone, Debug)]
pub enum TracebackState {
    Native { lines: Vec<String>, hidden: bool },
    Visible,
    Hidden,
    Missing,
}

/// A captured exception retains whether user frames were hidden or absent.
#[derive(Clone, Debug)]
pub struct DiagnosticException {
    pub traceback: TracebackState,
    pub frames: Vec<DiagnosticFrame>,
    pub description: ExceptionDescription,
    pub crash: Option<DiagnosticCrash>,
}

/// The relation `CPython` records between two displayed exceptions.
#[derive(Clone, Copy, Debug)]
pub enum ExceptionRelation {
    Cause,
    Context,
}

/// A nonempty chain, ordered from the original exception to its replacement.
#[derive(Clone, Debug)]
pub struct ExceptionChain {
    pub first: DiagnosticException,
    pub following: Vec<(ExceptionRelation, DiagnosticException)>,
}

/// Terminal geometry and the owning terminal renderer's entry separator.
pub struct DiagnosticFormat<'a> {
    pub width: usize,
    pub entry_separator: &'a str,
}

/// Terminal and stored text share one rendering of the captured frame values.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RenderedDiagnostic {
    pub detail: String,
    pub terminal_lead: String,
    pub exception: String,
    pub crash: Option<DiagnosticLocation>,
}

/// Honor an explicit outcome's request to display only its supplied message.
#[must_use]
pub fn render_message_only(exception: &ExceptionDescription, message: &str) -> RenderedDiagnostic {
    let (lead, detail) = split_python_strip(message);
    RenderedDiagnostic {
        detail: detail.to_owned(),
        terminal_lead: lead.to_owned(),
        exception: python_rstrip(&exception.text).to_owned(),
        crash: None,
    }
}

fn arguments(output: &mut String, values: &[(String, String)], width: usize) {
    if values.is_empty() {
        return;
    }
    let mut line = String::new();
    for (name, value) in values {
        let argument = format!("{name} = {value}");
        if !line.is_empty() && line.chars().count() + argument.chars().count() + 2 > width {
            output.push_str(&line);
            output.push('\n');
            line.clear();
        }
        if !line.is_empty() {
            line.push_str(", ");
        }
        line.push_str(&argument);
    }
    output.push_str(&line);
    output.push_str("\n\n");
}

fn frame(output: &mut String, entry: &DiagnosticFrame, width: usize) {
    arguments(output, &entry.arguments, width);
    for line in graph::source_lines(entry.source.as_ref(), false) {
        output.push_str(&line);
        output.push('\n');
    }
}

fn locals(output: &mut String, entry: &DiagnosticFrame, short: bool) {
    if let Some(lines) = &entry.locals {
        if !short {
            output.push('\n');
        }
        for line in lines {
            if short {
                output.push_str("        ");
            }
            output.push_str(line);
            output.push('\n');
        }
    }
}

/// Render long source entries from traceback frames in their original order.
#[must_use]
pub fn render_exception(
    entries: &[DiagnosticFrame],
    exception: &ExceptionDescription,
    format: &DiagnosticFormat<'_>,
) -> RenderedDiagnostic {
    let crash = entries.last().map(|entry| DiagnosticCrash {
        location: entry.location.clone(),
        text: exception.text.clone(),
    });
    render_diagnostic(
        entries,
        exception,
        if entries.is_empty() {
            &TracebackState::Missing
        } else {
            &TracebackState::Visible
        },
        crash.as_ref(),
        format,
    )
}

fn exception_lines(
    output: &mut String,
    description: &str,
    traceback: &TracebackState,
    indent: usize,
) {
    for (index, line) in description.split('\n').enumerate() {
        output.push(
            if matches!(traceback, TracebackState::Hidden) && index > 0 {
                ' '
            } else {
                'E'
            },
        );
        output.extend(std::iter::repeat_n(' ', indent.saturating_sub(1)));
        output.push_str(line);
        output.push('\n');
    }
}

fn exception_tail(
    output: &mut String,
    entries: &[DiagnosticFrame],
    exception: &ExceptionDescription,
    traceback: &TracebackState,
) {
    let last_short = entries.last().is_some_and(|entry| {
        entry.style(DiagnosticStyle::Auto, entries.len() - 1, entries.len())
            == DiagnosticFrameStyle::Short
    });
    let indent = if last_short {
        4
    } else {
        entries
            .last()
            .and_then(|entry| entry.source.as_ref())
            .map_or(4, |source| source.exception_indent)
    };
    exception_lines(output, python_rstrip(&exception.text), traceback, indent);
    if let Some(entry) = entries.last() {
        locals(output, entry, last_short);
    }
    if matches!(traceback, TracebackState::Hidden) {
        output.push_str(HIDDEN_TRACEBACK_MESSAGE);
        output.push('\n');
    }
    if let Some(entry) = entries.last()
        && !last_short
    {
        let _ = writeln!(
            output,
            "\n{}:{}: {}",
            entry.location.path, entry.location.line, exception.name
        );
    }
}

fn render_diagnostic(
    entries: &[DiagnosticFrame],
    exception: &ExceptionDescription,
    traceback: &TracebackState,
    crash: Option<&DiagnosticCrash>,
    format: &DiagnosticFormat<'_>,
) -> RenderedDiagnostic {
    if let TracebackState::Native { lines, hidden } = traceback {
        let mut text = lines.concat();
        if *hidden {
            text.push_str(HIDDEN_TRACEBACK_MESSAGE);
            text.push('\n');
        }
        let (lead, detail) = split_python_strip(&text);
        return RenderedDiagnostic {
            detail: detail.to_owned(),
            terminal_lead: lead.to_owned(),
            exception: python_rstrip(crash.map_or(&exception.text, |crash| &crash.text)).to_owned(),
            crash: crash.map(|crash| crash.location.clone()),
        };
    }
    let description = python_rstrip(&exception.text);
    if matches!(traceback, TracebackState::Missing) {
        return RenderedDiagnostic {
            detail: description.to_owned(),
            terminal_lead: String::new(),
            exception: description.to_owned(),
            crash: None,
        };
    }
    let mut output = String::from("\n");
    for (index, entry) in entries.iter().enumerate() {
        let middle =
            entry.style(DiagnosticStyle::Auto, index, entries.len()) == DiagnosticFrameStyle::Short;
        if middle {
            let _ = writeln!(
                output,
                "{}:{}: in {}",
                entry.location.path, entry.location.line, entry.function
            );
            let line = entry
                .source
                .as_ref()
                .and_then(|source| {
                    usize::try_from(source.failure_line - source.first_line)
                        .ok()
                        .and_then(|offset| source.lines.get(offset))
                })
                .map_or("???", |line| line.trim());
            let _ = writeln!(output, "    {line}");
        } else {
            frame(&mut output, entry, format.width);
        }
        if index + 1 < entries.len() {
            locals(&mut output, entry, middle);
        }
        if let Some(next) = entries.get(index + 1)
            && (!middle
                || next.style(DiagnosticStyle::Auto, index + 1, entries.len())
                    == DiagnosticFrameStyle::Long)
        {
            if !middle {
                let _ = writeln!(
                    output,
                    "\n{}:{}: ",
                    entry.location.path, entry.location.line
                );
            }
            output.push_str(format.entry_separator);
            output.push('\n');
            if index + 2 == entries.len()
                && next.style(DiagnosticStyle::Auto, index + 1, entries.len())
                    == DiagnosticFrameStyle::Long
            {
                output.push('\n');
            }
        }
    }
    exception_tail(&mut output, entries, exception, traceback);
    let (terminal_lead, detail) = split_python_strip(&output);
    RenderedDiagnostic {
        detail: detail.to_owned(),
        terminal_lead: terminal_lead.to_owned(),
        exception: crash.map_or_else(
            || detail.to_owned(),
            |crash| python_rstrip(&crash.text).to_owned(),
        ),
        crash: crash.map(|crash| crash.location.clone()),
    }
}

/// Preserve causal/context separators while rendering each source entry once.
#[must_use]
pub fn render_exception_chain(
    chain: &ExceptionChain,
    format: &DiagnosticFormat<'_>,
) -> RenderedDiagnostic {
    let mut rendered = render_diagnostic(
        &chain.first.frames,
        &chain.first.description,
        &chain.first.traceback,
        chain.first.crash.as_ref(),
        format,
    );
    let mut output = rendered.terminal_lead.clone();
    output.push_str(&rendered.detail);
    for (relation, exception) in &chain.following {
        output.push_str("\n\n");
        output.push_str(match relation {
            ExceptionRelation::Cause => {
                "The above exception was the direct cause of the following exception:"
            }
            ExceptionRelation::Context => {
                "During handling of the above exception, another exception occurred:"
            }
        });
        output.push('\n');
        rendered = render_diagnostic(
            &exception.frames,
            &exception.description,
            &exception.traceback,
            exception.crash.as_ref(),
            format,
        );
        output.push_str(&rendered.terminal_lead);
        output.push_str(&rendered.detail);
    }
    let (lead, detail) = split_python_strip(&output);
    RenderedDiagnostic {
        detail: detail.to_owned(),
        terminal_lead: lead.to_owned(),
        exception: rendered.exception,
        crash: rendered.crash,
    }
}

#[cfg(test)]
mod tests;
