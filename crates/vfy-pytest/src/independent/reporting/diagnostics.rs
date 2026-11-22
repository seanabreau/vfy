//! Capture `CPython` traceback/source values for the shared Rust renderer.

use pyo3::exceptions::{
    PyAssertionError, PyAttributeError, PyException, PyKeyError, PyOverflowError,
};
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PySlice, PyString, PyTuple};
use std::collections::HashSet;
use std::path::Path;
use vfy_core::test_diagnostics::{
    DiagnosticCrash, DiagnosticException, DiagnosticFormat, DiagnosticFrame, DiagnosticFrameStyle,
    DiagnosticLocation, DiagnosticStyle, ExceptionChain, ExceptionDescription, ExceptionNameStyle,
    ExceptionRelation, FrameSource, TracebackState, frame_source, render_exception_chain,
};
use vfy_core::test_execution::PhasePayload;

mod collector;
mod groups;
mod locals;
pub(super) mod value;

/// The failure entrypoint selects traceback policy without inspecting obj.
#[derive(Clone, Copy)]
pub(super) enum TracebackOwner {
    Function,
    Collector,
    /// A test module import failure, rendered from the import call that raised it.
    ModuleImport,
}

fn optional_item<'py>(
    mapping: &Bound<'py, PyAny>,
    name: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    match mapping.get_item(name) {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.is_instance_of::<PyKeyError>(mapping.py()) => Ok(None),
        Err(error) => Err(error),
    }
}

fn is_hidden(frame: &Bound<'_, PyAny>, exception_info: &Bound<'_, PyAny>) -> PyResult<bool> {
    let mut hidden = None;
    for namespace in ["f_locals", "f_globals"] {
        match frame.getattr(namespace)?.get_item("__tracebackhide__") {
            Ok(value) => {
                hidden = Some(value);
                break;
            }
            Err(error) if error.is_instance_of::<PyException>(frame.py()) => {}
            Err(error) => return Err(error),
        }
    }
    let Some(hidden) = hidden else {
        return Ok(false);
    };
    if hidden.is_truthy()? && hidden.is_callable() {
        hidden.call1((exception_info,))?.is_truthy()
    } else {
        hidden.is_truthy()
    }
}

fn internal_frame(frame: &Bound<'_, PyAny>) -> PyResult<bool> {
    let globals = frame.getattr("f_globals")?;
    let name = globals.cast::<PyDict>()?.get_item("__name__")?;
    Ok(name.is_some_and(|name| {
        name.cast::<PyString>().is_ok_and(|name| {
            let name = name.to_string_lossy();
            name.starts_with("vfy.independent.")
        })
    }))
}

fn limit_assertion_representation(value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    if value.len()? <= 240 {
        return Ok(value.clone().unbind());
    }
    let py = value.py();
    let slice = py
        .get_type::<PySlice>()
        .call1((py.None(), 118, py.None()))?;
    let leading = value.get_item(slice)?.add("...")?;
    // A str subclass can change its length while the leading slice is read.
    let length = isize::try_from(value.len()?)
        .map_err(|error| PyOverflowError::new_err(error.to_string()))?;
    let slice = py
        .get_type::<PySlice>()
        .call1((length - 119, py.None(), py.None()))?;
    Ok(leading.add(value.get_item(slice)?)?.unbind())
}

pub(super) fn capture_exception_name(py: Python<'_>, info: &Bound<'_, PyAny>) -> PyResult<()> {
    let exception = info.getattr("value")?;
    let abbreviated = if exception.is_instance_of::<PyAssertionError>() {
        let limit = PyCFunction::new_closure(
            py,
            Some(c"_limit_assertion_representation"),
            None,
            |arguments, _| limit_assertion_representation(&arguments.get_item(0)?),
        )?;
        let expression = py
            .import("vfy.independent.exception_protocol")?
            .getattr("_assertion_expression")?
            .call1((&exception, limit))?;
        // A non-str msg is tested through its own truthiness and startswith method.
        expression.is_truthy()?
            && expression
                .call_method1("startswith", ("AssertionError('assert ",))?
                .is_truthy()?
    } else {
        false
    };
    info.setattr("_vfy_abbreviated_assertion", abbreviated)
}

fn exception_name_style(info: &Bound<'_, PyAny>) -> PyResult<ExceptionNameStyle> {
    match info.getattr("_vfy_abbreviated_assertion") {
        Ok(value) if value.is_truthy()? => Ok(ExceptionNameStyle::AbbreviatedAssertion),
        Ok(_) => Ok(ExceptionNameStyle::Complete),
        Err(error) if error.is_instance_of::<PyAttributeError>(info.py()) => {
            Ok(ExceptionNameStyle::Complete)
        }
        Err(error) => Err(error),
    }
}

fn source(
    py: Python<'_>,
    traceback: &Bound<'_, PyAny>,
    frame: &Bound<'_, PyAny>,
    code: &Bound<'_, PyAny>,
    filename: &str,
    failure_line: i32,
) -> PyResult<Option<FrameSource>> {
    let lines = py
        .import("linecache")?
        .call_method1("getlines", (filename, frame.getattr("f_globals")?))?
        .extract::<Vec<String>>()?;
    let text = lines.concat();
    let first_line = code.getattr("co_firstlineno")?.extract::<i32>()?;
    let Some(source) = frame_source(&text, first_line, failure_line) else {
        return Ok(None);
    };
    let last_instruction = traceback.getattr("tb_lasti")?.extract::<i64>()?;
    let Ok(last_instruction) = usize::try_from(last_instruction) else {
        return Ok(Some(source));
    };
    let positions = code.call_method0("co_positions")?;
    let position = positions
        .try_iter()?
        .nth(last_instruction / 2)
        .transpose()?;
    let Some(position) = position else {
        return Ok(Some(source));
    };
    let (line, end_line, column, end_column) =
        position.extract::<(Option<i32>, Option<i32>, Option<usize>, Option<usize>)>()?;
    Ok(Some(match (line, end_line, column, end_column) {
        (Some(line), Some(end_line), Some(start), Some(end))
            if line == failure_line && end_line == failure_line =>
        {
            source.with_position(start..end)
        }
        _ => source,
    }))
}

#[derive(Clone, Copy)]
enum ArgumentRepresentationLimit {
    Limited,
    Unlimited,
}

#[derive(Clone, Copy)]
struct FrameRepresentation {
    arguments: ArgumentRepresentationLimit,
    locals: Option<locals::LocalRepresentation>,
}

fn verbosity_above(item: &Bound<'_, PyAny>, threshold: i32) -> PyResult<bool> {
    let value = {
        let config = item.getattr("config")?;
        config.call_method0("get_verbosity")?
    };
    value.gt(threshold)
}

fn frame_representation(item: &Bound<'_, PyAny>) -> PyResult<FrameRepresentation> {
    // Both reference queries occur even when locals are not displayed.
    let truncate_locals = !verbosity_above(item, 1)?;
    let arguments = if verbosity_above(item, 2)? {
        ArgumentRepresentationLimit::Unlimited
    } else {
        ArgumentRepresentationLimit::Limited
    };
    let show = item
        .getattr("config")?
        .call_method1("getoption", ("showlocals", false))?
        .is_truthy()?;
    Ok(FrameRepresentation {
        arguments,
        locals: show.then_some(if truncate_locals {
            locals::LocalRepresentation::Truncated
        } else {
            locals::LocalRepresentation::Full
        }),
    })
}

fn arguments(
    py: Python<'_>,
    frame: &Bound<'_, PyAny>,
    code: &Bound<'_, PyAny>,
    representation_limit: ArgumentRepresentationLimit,
) -> PyResult<Vec<(String, String)>> {
    let flags = code.getattr("co_flags")?.extract::<u32>()?;
    let count = code.getattr("co_argcount")?.extract::<usize>()?
        + code.getattr("co_kwonlyargcount")?.extract::<usize>()?
        + usize::from(flags & 0x4 != 0)
        + usize::from(flags & 0x8 != 0);
    let names = code.getattr("co_varnames")?.cast_into::<PyTuple>()?;
    let locals = frame.getattr("f_locals")?;
    let represent = py
        .import("vfy.independent.argument_repr")?
        .getattr("argument_representation")?;
    let maximum =
        matches!(representation_limit, ArgumentRepresentationLimit::Limited).then_some(240);
    let dispatch = PyCFunction::new_closure(
        py,
        Some(c"_dispatch_argument_representation"),
        None,
        |arguments, _| {
            crate::independent::subtests::saferepr::dispatch(
                &arguments.get_item(0)?,
                &arguments.get_item(1)?,
            )?
        },
    )?;
    let limit = PyCFunction::new_closure(
        py,
        Some(c"_limit_argument_representation"),
        None,
        move |arguments, _| {
            let value = arguments.get_item(0)?;
            match representation_limit {
                ArgumentRepresentationLimit::Limited => limit_assertion_representation(&value),
                ArgumentRepresentationLimit::Unlimited => Ok(value.unbind()),
            }
        },
    )?;
    let exception = PyCFunction::new_closure(
        py,
        Some(c"_argument_exception_expression"),
        None,
        |arguments, _| {
            let name = arguments.get_item(0)?.extract::<String>()?;
            let text = arguments.get_item(1)?.extract::<String>()?;
            Ok::<_, PyErr>(format!("{name}(\"{text}\")"))
        },
    )?;
    let unpresentable = PyCFunction::new_closure(
        py,
        Some(c"_unpresentable_argument_expression"),
        None,
        |arguments, _| {
            let text = arguments.get_item(0)?.extract::<String>()?;
            Ok::<_, PyErr>(format!("unpresentable exception ({text})"))
        },
    )?;
    let failure = PyCFunction::new_closure(
        py,
        Some(c"_argument_representation_failure"),
        None,
        |arguments, _| {
            let text = arguments.get_item(0)?.extract::<String>()?;
            let name = arguments.get_item(1)?.extract::<String>()?;
            let address = arguments.get_item(2)?.extract::<usize>()?;
            Ok::<_, PyErr>(format!(
                "<[{text} raised in repr()] {name} object at 0x{address:x}>"
            ))
        },
    )?;
    let mut arguments = Vec::new();
    for name in names.iter().take(count) {
        let name = name.extract::<String>()?;
        if let Some(value) = optional_item(&locals, &name)? {
            let representation = represent.call1((
                value,
                maximum,
                &dispatch,
                &limit,
                &exception,
                &unpresentable,
                &failure,
            ))?;
            arguments.push((name, representation.extract()?));
        }
    }
    Ok(arguments)
}

struct CapturedFrames {
    entries: Vec<DiagnosticFrame>,
    state: TracebackState,
    original: Vec<Py<PyAny>>,
}

struct SelectedTracebacks<'py> {
    original: Vec<Py<PyAny>>,
    selected: Vec<Bound<'py, PyAny>>,
}

fn traceback_entries(traceback: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    let mut traceback = (!traceback.is_none()).then(|| traceback.clone());
    let mut entries = Vec::new();
    while let Some(current) = traceback {
        let next = current.getattr("tb_next")?;
        traceback = (!next.is_none()).then_some(next);
        entries.push(current.unbind());
    }
    Ok(entries)
}

fn selected_tracebacks<'py>(
    py: Python<'py>,
    traceback: &Bound<'py, PyAny>,
    item: &Bound<'py, PyAny>,
    owner: TracebackOwner,
) -> PyResult<SelectedTracebacks<'py>> {
    let original = traceback_entries(traceback)?;
    let selected = match owner {
        // As in pytest's ImportError branch, keep every frame except generated code.
        TracebackOwner::ModuleImport => {
            let hide_generated = !verbosity_above(item, 1)?;
            let mut selected = Vec::new();
            for entry in &original {
                let entry = entry.bind(py);
                if !(hide_generated && generated_frame(entry)?) {
                    selected.push(entry.clone());
                }
            }
            selected
        }
        TracebackOwner::Collector => {
            let start = collector::start(py, item, &original)?;
            original[start..]
                .iter()
                .map(|entry| entry.bind(py).clone())
                .collect()
        }
        TracebackOwner::Function => {
            let entry_code = super::function_code(py, item)?;
            let mut selected = original
                .iter()
                .map(|entry| entry.bind(py).clone())
                .collect::<Vec<_>>();
            let mut first = 0;
            for (index, entry) in selected.iter().enumerate() {
                if entry
                    .getattr("tb_frame")?
                    .getattr("f_code")?
                    .is(&entry_code)
                {
                    first = index;
                    break;
                }
            }
            selected.drain(..first);
            selected
        }
    };
    Ok(SelectedTracebacks { original, selected })
}

/// Generated code has a `<...>` pseudo-filename, such as frozen importlib.
fn generated_frame(entry: &Bound<'_, PyAny>) -> PyResult<bool> {
    let filename = entry
        .getattr("tb_frame")?
        .getattr("f_code")?
        .getattr("co_filename")?
        .extract::<String>()?;
    Ok(filename.contains('<') && filename.contains('>'))
}

/// Pytest prints a path relative to the invocation directory only when that spelling is shorter.
fn display_path(directory: Option<&Path>, filename: &str) -> String {
    let Some(directory) = directory else {
        return filename.to_owned();
    };
    let relative = super::relative_location(directory, Path::new(filename));
    if relative.chars().count() < filename.chars().count() {
        relative
    } else {
        filename.to_owned()
    }
}

/// Pytest drops its own frames unless none remain, then applies `__tracebackhide__`.
///
/// The fallback keeps the frame that a failure raised inside the adapter, such as the
/// `Unexpected success` report, visible as the single entry pytest renders for it.
fn visible_entries<'py>(
    tracebacks: Vec<Bound<'py, PyAny>>,
    exception_info: &Bound<'py, PyAny>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let mut user = Vec::new();
    for traceback in &tracebacks {
        if !internal_frame(&traceback.getattr("tb_frame")?)? {
            user.push(traceback.clone());
        }
    }
    let candidates = if user.is_empty() { tracebacks } else { user };
    let mut visible = Vec::new();
    for traceback in candidates {
        if !is_hidden(&traceback.getattr("tb_frame")?, exception_info)? {
            visible.push(traceback);
        }
    }
    Ok(visible)
}

fn unittest_method(item: &Bound<'_, PyAny>) -> PyResult<bool> {
    match item.getattr("cls") {
        Ok(class) => Ok(!class.is_none() && crate::independent::unittest::is_testcase(&class)?),
        Err(error) if error.is_instance_of::<PyAttributeError>(item.py()) => Ok(false),
        Err(error) => Err(error),
    }
}

fn unittest_frame(frame: &Bound<'_, PyAny>) -> PyResult<bool> {
    match frame
        .getattr("f_globals")?
        .cast::<PyDict>()?
        .get_item("__unittest")?
    {
        Some(value) => value.is_truthy(),
        None => Ok(false),
    }
}

fn frames(
    py: Python<'_>,
    traceback: &Bound<'_, PyAny>,
    item: &Bound<'_, PyAny>,
    exception_info: &Bound<'_, PyAny>,
    representation: FrameRepresentation,
    presentation: DiagnosticStyle,
    owner: TracebackOwner,
) -> PyResult<CapturedFrames> {
    let SelectedTracebacks { original, selected } =
        selected_tracebacks(py, traceback, item, owner)?;
    let unittest = matches!(owner, TracebackOwner::Function) && unittest_method(item)?;
    let had_traceback = !selected.is_empty();
    // Pytest uses absolute paths if a fixture changed cwd since invocation,
    // including when the current directory has disappeared.
    let invocation_dir = item
        .getattr("config")?
        .getattr("_invocation_dir")?
        .extract::<std::path::PathBuf>()?;
    let cwd = std::env::current_dir()
        .ok()
        .filter(|directory| directory == &invocation_dir);
    let visible = visible_entries(selected, exception_info)?;
    let count = visible.len();
    let mut visible = visible
        .into_iter()
        .enumerate()
        .map(|(index, traceback)| {
            let frame = traceback.getattr("tb_frame")?;
            let code = frame.getattr("f_code")?;
            let auto_style = (presentation == DiagnosticStyle::Auto
                && presentation.frame(index, count) == DiagnosticFrameStyle::Short)
                .then_some(DiagnosticFrameStyle::Short);
            Ok((traceback, frame, code, auto_style))
        })
        .collect::<PyResult<Vec<_>>>()?;
    if unittest {
        let mut filtered = Vec::new();
        for entry in &visible {
            if !unittest_frame(&entry.1)? {
                filtered.push(entry.clone());
            }
        }
        if !filtered.is_empty() {
            visible = filtered;
        }
    }
    let mut entries = Vec::new();
    let count = visible.len();
    for (index, (traceback, frame, code, auto_style)) in visible.into_iter().enumerate() {
        let filename = code.getattr("co_filename")?.extract::<String>()?;
        let line = traceback.getattr("tb_lineno")?.extract::<i32>()?;
        let mut entry = DiagnosticFrame {
            function: code.getattr("co_name")?.extract()?,
            location: DiagnosticLocation {
                path: display_path(cwd.as_deref(), &filename),
                line,
            },
            source: source(py, &traceback, &frame, &code, &filename, line)?,
            arguments: Vec::new(),
            locals: None,
            auto_style,
        };
        if entry.style(presentation, index, count) == DiagnosticFrameStyle::Long {
            entry.arguments = arguments(py, &frame, &code, representation.arguments)?;
        }
        entry.locals = locals::capture(&frame, representation.locals)?;
        entries.push(entry);
    }
    let state = if !entries.is_empty() {
        TracebackState::Visible
    } else if had_traceback {
        TracebackState::Hidden
    } else {
        TracebackState::Missing
    };
    Ok(CapturedFrames {
        entries,
        state,
        original,
    })
}

/// The `CPython` snapshot formats the type name, message and notes, including owned outcomes.
fn description(
    exception: &Bound<'_, PyAny>,
    snapshot: &Bound<'_, PyAny>,
    style: ExceptionNameStyle,
) -> PyResult<ExceptionDescription> {
    let name = exception.get_type().name()?.to_str()?.to_owned();
    let text = snapshot
        .call_method0("format_exception_only")?
        .try_iter()?
        .map(|line| line?.extract::<String>())
        .collect::<PyResult<Vec<_>>>()?
        .concat();
    Ok(ExceptionDescription { name, text }.with_name_style(style))
}

fn exception_snapshot<'py>(
    py: Python<'py>,
    exception: &Bound<'py, PyAny>,
    traceback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("compact", true)?;
    py.import("traceback")?
        .getattr("TracebackException")?
        .call((exception.get_type(), exception, traceback), Some(&kwargs))
}

fn capture_crash(
    py: Python<'_>,
    tracebacks: &[Py<PyAny>],
    info: &Bound<'_, PyAny>,
    style: ExceptionNameStyle,
) -> PyResult<Option<DiagnosticCrash>> {
    for traceback in tracebacks.iter().rev() {
        let traceback = traceback.bind(py);
        let frame = traceback.getattr("tb_frame")?;
        if is_hidden(&frame, info)? {
            continue;
        }
        let code = frame.getattr("f_code")?;
        let filename = code.getattr("co_filename")?.extract::<String>()?;
        let location = DiagnosticLocation {
            path: filename,
            line: traceback.getattr("tb_lineno")?.extract()?,
        };
        let exception = info.getattr("value")?;
        let text = if let Some(text) = groups::short_description(py, &exception)? {
            text
        } else {
            let snapshot = exception_snapshot(py, &exception, &info.getattr("tb")?)?;
            description(&exception, &snapshot, style)?.text
        };
        return Ok(Some(DiagnosticCrash { location, text }));
    }
    Ok(None)
}

pub(super) fn exception_crash(
    py: Python<'_>,
    info: &Bound<'_, PyAny>,
) -> PyResult<Option<DiagnosticCrash>> {
    let entries = traceback_entries(&info.getattr("tb")?)?;
    capture_crash(py, &entries, info, exception_name_style(info)?)
}

/// Native protocol sequences accompany their single core capture.
pub(super) struct CapturedChain {
    pub(super) chain: ExceptionChain,
    pub(super) native_lines: Vec<Option<Py<PyAny>>>,
}

#[derive(Clone, Copy)]
struct CapturePresentation {
    owner: TracebackOwner,
    representation: FrameRepresentation,
    style: DiagnosticStyle,
}

fn capture_node(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    exception_info: &Bound<'_, PyAny>,
    exception: &Bound<'_, PyAny>,
    info: &Bound<'_, PyAny>,
    group_type: &Bound<'_, PyAny>,
    presentation: CapturePresentation,
) -> PyResult<(DiagnosticException, Option<Py<PyAny>>)> {
    let grouped = exception.is_instance(group_type)?;
    let missing = info.getattr("tb")?.is_none();
    let (value, native_lines) = if grouped || missing {
        let visible = if grouped {
            value::visible_tracebacks(py, item, exception_info, presentation.owner)?
        } else {
            Vec::new()
        };
        let traceback = visible
            .first()
            .cloned()
            .unwrap_or_else(|| py.None().into_bound(py));
        let target = if grouped {
            exception_info.getattr("value")?
        } else {
            exception.clone()
        };
        let actual_lines = value::native_lines(py, &target, &traceback)?;
        let lines = actual_lines.bind(py).extract::<Vec<String>>()?;
        let crash = if missing {
            None
        } else {
            exception_crash(py, info)?
        };
        let description = ExceptionDescription {
            name: exception.get_type().name()?.to_str()?.to_owned(),
            text: crash
                .as_ref()
                .map_or_else(|| lines.concat(), |crash| crash.text.clone()),
        };
        (
            DiagnosticException {
                frames: Vec::new(),
                traceback: TracebackState::Native {
                    lines,
                    hidden: grouped && visible.is_empty(),
                },
                description,
                crash,
            },
            Some(actual_lines),
        )
    } else {
        let captured = frames(
            py,
            &info.getattr("tb")?,
            item,
            info,
            presentation.representation,
            presentation.style,
            presentation.owner,
        )?;
        let style = exception_name_style(info)?;
        let snapshot = exception_snapshot(py, exception, &info.getattr("tb")?)?;
        let description = description(exception, &snapshot, style)?;
        let crash = capture_crash(py, &captured.original, info, style)?;
        (
            DiagnosticException {
                frames: captured.entries,
                traceback: captured.state,
                description,
                crash,
            },
            None,
        )
    };
    Ok((value, native_lines))
}

/// The exception that pytest's chain renders after `exception`: its cause, else an unsuppressed context.
pub(super) fn chained_exception<'py>(
    exception: &Bound<'py, PyAny>,
) -> PyResult<Option<(ExceptionRelation, Bound<'py, PyAny>)>> {
    let cause = exception.getattr("__cause__")?;
    if !cause.is_none() {
        return Ok(Some((ExceptionRelation::Cause, cause)));
    }
    let context = exception.getattr("__context__")?;
    if context.is_none() || exception.getattr("__suppress_context__")?.is_truthy()? {
        return Ok(None);
    }
    Ok(Some((ExceptionRelation::Context, context)))
}

/// The `ExceptionInfo` pytest attaches to an exception and its traceback.
pub(super) fn filled_info<'py>(
    py: Python<'py>,
    exception: &Bound<'py, PyAny>,
    traceback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let info = py
        .import("vfy.independent.facade")?
        .getattr("ExceptionInfo")?
        .call0()?;
    info.call_method1("_fill", (exception, traceback))?;
    Ok(info)
}

/// A filled `ExceptionInfo` whose exception name style is captured for rendering.
pub(super) fn named_info<'py>(
    py: Python<'py>,
    exception: &Bound<'py, PyAny>,
    traceback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let info = filled_info(py, exception, traceback)?;
    capture_exception_name(py, &info)?;
    Ok(info)
}

pub(super) fn capture_structured(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    info: &Bound<'_, PyAny>,
    style: DiagnosticStyle,
    owner: TracebackOwner,
) -> PyResult<CapturedChain> {
    let group_type = py.import("builtins")?.getattr("BaseExceptionGroup")?;
    let presentation = CapturePresentation {
        owner,
        representation: frame_representation(item)?,
        style,
    };
    let mut exception = info.getattr("value")?;
    let mut current = info.clone();
    let mut seen = HashSet::new();
    let mut nodes = Vec::new();
    loop {
        seen.insert(exception.as_ptr() as usize);
        let (value, native_lines) = capture_node(
            py,
            item,
            info,
            &exception,
            &current,
            &group_type,
            presentation,
        )?;
        let previous = chained_exception(&exception)?
            .filter(|(_, previous)| !seen.contains(&(previous.as_ptr() as usize)));
        let Some((relation, previous)) = previous else {
            nodes.reverse();
            let mut bindings = Vec::with_capacity(nodes.len() + 1);
            bindings.push(native_lines);
            let following = nodes
                .into_iter()
                .map(|(relation, value, binding)| {
                    bindings.push(binding);
                    (relation, value)
                })
                .collect();
            return Ok(CapturedChain {
                chain: ExceptionChain {
                    first: value,
                    following,
                },
                native_lines: bindings,
            });
        };
        nodes.push((relation, value, native_lines));
        current = filled_info(py, &previous, &previous.getattr("__traceback__")?)?;
        if !previous.getattr("__traceback__")?.is_none() {
            capture_exception_name(py, &current)?;
        }
        exception = previous;
    }
}

pub(super) fn apply_captured(
    py: Python<'_>,
    chain: &ExceptionChain,
    payload: &mut PhasePayload,
) -> PyResult<()> {
    let width = py
        .import("shutil")?
        .call_method0("get_terminal_size")?
        .getattr("columns")?
        .extract::<usize>()?;
    let separator = crate::independent::session_hooks::render_terminal_separator(
        "_ ",
        None,
        i64::try_from(width)
            .map_err(|error| pyo3::exceptions::PyOverflowError::new_err(error.to_string()))?,
    )?;
    let rendered = render_exception_chain(
        chain,
        &DiagnosticFormat {
            width,
            entry_separator: &separator,
        },
    );
    payload.exception = Some(rendered.exception.into());
    payload.detail = rendered.detail;
    payload.terminal_lead = rendered.terminal_lead;
    Ok(())
}
