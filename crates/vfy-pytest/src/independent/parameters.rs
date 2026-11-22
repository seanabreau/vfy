//! Expand `parametrize` marks into parameter rows with pytest-compatible IDs and scopes.

use super::{FixtureBinding, FixtureParameter, metadata};
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyUnicodeEncodeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyComplex, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use vfy_core::test_plan::{FixtureChains, FixturePlan};
use vfy_core::{FixtureId, FixtureScope};
mod ids;
use ids::ParameterIds;

#[derive(Clone, Copy)]
pub(super) enum IdEscaping {
    Ascii,
    Disabled,
}

impl IdEscaping {
    fn text(self, value: &Bound<'_, PyString>) -> PyResult<String> {
        match value.to_str() {
            Ok(text) => Ok(match self {
                Self::Ascii => {
                    escape_identifier(text.chars().map(u32::from), BackslashEscaping::Escape)
                }
                Self::Disabled => text.to_owned(),
            }),
            Err(error)
                if matches!(self, Self::Ascii)
                    && error.is_instance_of::<PyUnicodeEncodeError>(value.py()) =>
            {
                let text = crate::reporting::python_text_from_python(value.as_any())?;
                Ok(escape_identifier(
                    text.codepoints().into_iter(),
                    BackslashEscaping::Escape,
                ))
            }
            Err(error) => Err(error),
        }
    }
}

pub(super) struct ParameterGeneration<'a, 'py> {
    pub(super) callable: &'a Bound<'py, PyAny>,
    pub(super) nodeid: &'a str,
    pub(super) empty_policy: &'a str,
    pub(super) escaping: IdEscaping,
    pub(super) marker_count: usize,
}

#[derive(Default)]
pub(super) struct ParameterRow {
    pub(super) suffix: String,
    pub(super) parameters: BTreeMap<String, Py<PyAny>>,
    pub(super) marks: Vec<Py<PyAny>>,
    pub(super) fixture_parameters: BTreeMap<usize, FixtureParameter>,
    fixture_ids: Vec<String>,
    pub(super) idlist: Vec<String>,
    pub(super) parameter_order: Vec<String>,
    fixture_names: Vec<String>,
    fixture_mark_count: usize,
    indirect: BTreeSet<String>,
    pub(super) indices: BTreeMap<String, usize>,
    pub(super) scopes: BTreeMap<String, FixtureScope>,
    inferred_scope_groups: Vec<Vec<String>>,
}

fn parameter_names(args: &Bound<'_, PyTuple>) -> PyResult<Vec<String>> {
    let names = args.get_item(0)?;
    let names = if let Ok(names) = names.extract::<String>() {
        names
            .split(',')
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .map(str::to_owned)
            .collect::<Vec<_>>()
    } else {
        names.extract::<Vec<String>>()?
    };
    if names.is_empty() || names.iter().any(String::is_empty) {
        return Err(PyValueError::new_err(
            "parametrize requires nonempty argument names",
        ));
    }
    Ok(names)
}

/// Yield the arguments of each parametrize mark in order. Extraction is lazy so errors surface
/// in the same order as the marks are consumed.
fn parametrize_calls<'py>(
    marks: &Bound<'py, PyList>,
) -> impl Iterator<Item = PyResult<(usize, Bound<'py, PyTuple>, Bound<'py, PyDict>)>> {
    marks.iter().enumerate().filter_map(|(index, mark)| {
        match (|| {
            Ok((
                mark.getattr("name")?.extract::<String>()?,
                mark.getattr("args")?.cast_into::<PyTuple>()?,
                mark.getattr("kwargs")?.cast_into::<PyDict>()?,
            ))
        })() {
            Ok((name, args, kwargs)) => {
                (name == "parametrize").then_some(Ok((index, args, kwargs)))
            }
            Err(error) => Some(Err(error)),
        }
    })
}

/// Exclude direct mark arguments from the definition's initial fixture closure.
pub(super) fn direct_parameter_names(marks: &Bound<'_, PyList>) -> PyResult<BTreeSet<String>> {
    let mut direct = BTreeSet::new();
    for call in parametrize_calls(marks) {
        let (_, args, kwargs) = call?;
        let names = parameter_names(&args)?;
        let indirect = indirect_names(&kwargs, &names)?;
        direct.extend(names.into_iter().filter(|name| !indirect.contains(name)));
    }
    Ok(direct)
}

type ParameterChoice<'py> = (Vec<Bound<'py, PyAny>>, Vec<Py<PyAny>>, String);

fn warn_parameter_iterator(
    nodeid: &str,
    values: &Bound<'_, PyAny>,
    caller_depth: usize,
) -> PyResult<()> {
    let py = values.py();
    if values.is_instance(&py.import("collections.abc")?.getattr("Collection")?)? {
        return Ok(());
    }
    let message = format!(
        concat!(
            "Passing a non-Collection iterable to parametrize is deprecated.\n",
            "Test: {}, argvalues type: {}\n",
            "Please convert to a list or tuple.\n",
            "See https://docs.pytest.org/en/stable/deprecations.html#parametrize-iterators"
        ),
        nodeid,
        values.get_type().name()?,
    );
    let warning = py
        .import("vfy.independent.facade")?
        .getattr("PytestRemovedIn10Warning")?
        .call1((message,))?;
    // Only the native marker dispatcher aliases pytest's builtin module. Direct
    // Metafunc callers retain their own source and module-based warning filters.
    let frame = py
        .import("sys")?
        .call_method1("_getframe", (caller_depth,))?;
    let code = frame.getattr("f_code")?;
    let globals = frame.getattr("f_globals")?;
    let module = globals.get_item("__name__")?;
    let registry = globals.call_method1("setdefault", ("__warningregistry__", PyDict::new(py)))?;
    let marker_dispatch =
        module.eq("vfy.independent.protocol")? && code.getattr("co_name")?.eq("perform_collect")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("filename", code.getattr("co_filename")?)?;
    kwargs.set_item("lineno", frame.getattr("f_lineno")?)?;
    kwargs.set_item("registry", registry)?;
    if marker_dispatch {
        kwargs.set_item("module", "_pytest.python")?;
    } else {
        kwargs.set_item("module", module)?;
    }
    py.import("warnings")?
        .call_method("warn_explicit", (warning, py.None()), Some(&kwargs))?;
    Ok(())
}

#[pyfunction]
pub(crate) fn independent_parameter_iterator_warning(
    nodeid: &str,
    values: &Bound<'_, PyAny>,
) -> PyResult<()> {
    warn_parameter_iterator(nodeid, values, 1)
}

fn empty_parameter_choice<'py>(
    generation: &ParameterGeneration<'_, 'py>,
    names: &[String],
) -> PyResult<ParameterChoice<'py>> {
    let py = generation.callable.py();
    let facade = py.import("vfy.independent.facade")?;
    if generation.empty_policy == "fail_at_collect" {
        let (_, line) = super::fixture_lookup::source_location(generation.callable)?;
        let message = PyString::new(py, "Empty parameter set in '{}' at line {}").call_method1(
            "format",
            (generation.callable.getattr("__name__")?, line.add(1)?),
        )?;
        return Err(PyErr::from_value(
            py.import("vfy.independent.protocol")?
                .getattr("Collector")?
                .getattr("CollectError")?
                .call1((message,))?,
        ));
    }
    let values = names
        .iter()
        .map(|_| facade.getattr("NOTSET"))
        .collect::<PyResult<Vec<_>>>()?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(
        "reason",
        format!("got empty parameter set for ({})", names.join(", ")),
    )?;
    let marker = if generation.empty_policy == "xfail" {
        kwargs.set_item("run", false)?;
        "xfail"
    } else {
        "skip"
    };
    let mark = facade
        .getattr("MarkDecorator")?
        .call1((marker, PyTuple::empty(py), kwargs))?
        .unbind();
    // Stock pytest yields one NOTSET parameter set whatever the argument count.
    Ok((values, vec![mark], "NOTSET".to_owned()))
}

fn parameter_choices<'py>(
    generation: &ParameterGeneration<'_, 'py>,
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
    names: &[String],
) -> PyResult<Vec<ParameterChoice<'py>>> {
    let mut parameters = Vec::new();
    let shape = if names.len() == 1 && args.get_item(0)?.is_instance_of::<PyString>() {
        ValueShape::Single
    } else {
        ValueShape::Sequence
    };
    for values in args.get_item(1)?.try_iter()? {
        let values = values?;
        let actual = parameter_values(values.clone(), names.len(), shape)?;
        parameters.push((values, actual));
    }
    let source = kwargs.get_item("ids")?;
    if parameters.is_empty() {
        let choice = empty_parameter_choice(generation, names)?;
        ParameterIds::resolve(source.as_ref(), 1, generation.nodeid)?;
        return Ok(vec![choice]);
    }
    let ids = ParameterIds::resolve(source.as_ref(), parameters.len(), generation.nodeid)?;
    parameters
        .into_iter()
        .enumerate()
        .map(|(index, (values, actual))| {
            let explicit_id = metadata(&values, "id")?.filter(|value| !value.is_none());
            let parameter_marks = parameter_marks(&values)?;
            let id = ids.select(
                &actual,
                names,
                index,
                explicit_id.as_ref(),
                generation.escaping,
            )?;
            Ok((actual, parameter_marks, id))
        })
        .collect()
}

pub(super) fn parameter_rows(
    generation: &ParameterGeneration<'_, '_>,
    marks: &Bound<'_, PyList>,
) -> PyResult<Vec<ParameterRow>> {
    let py = generation.callable.py();
    let mut rows = vec![ParameterRow::default()];
    let mut parameter_order = Vec::new();
    for call in parametrize_calls(marks) {
        let (mark_index, args, kwargs) = call?;
        if mark_index < generation.marker_count {
            warn_parameter_iterator(generation.nodeid, &args.get_item(1)?, 0)?;
        }
        let names = parameter_names(&args)?;
        parameter_order.extend(names.iter().cloned());
        let indirect = indirect_names(&kwargs, &names)?;
        let scope = parameter_scope(&kwargs)?
            .or_else(|| (indirect.len() != names.len()).then_some(FixtureScope::Function));
        let choices = parameter_choices(generation, &args, &kwargs, &names)?;
        let mut identifiers = choices
            .iter()
            .map(|(_, _, id)| id.clone())
            .collect::<Vec<_>>();
        unique_identifiers(&mut identifiers);
        let mut expanded = Vec::new();
        for row in &rows {
            for (index, ((values, parameter_marks, _), id)) in
                choices.iter().zip(&identifiers).enumerate()
            {
                let mut parameters = row
                    .parameters
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone_ref(py)))
                    .collect::<BTreeMap<_, _>>();
                for (name, value) in names.iter().zip(values) {
                    if parameters
                        .insert(name.clone(), value.clone().unbind())
                        .is_some()
                    {
                        return Err(PyValueError::new_err(format!(
                            "duplicate parameter name: {name}"
                        )));
                    }
                }
                let idlist = row
                    .idlist
                    .iter()
                    .cloned()
                    .chain(std::iter::once(id.clone()))
                    .collect::<Vec<_>>();
                let suffix = parameter_suffix(&idlist);
                let row_marks = row
                    .marks
                    .iter()
                    .chain(parameter_marks.iter())
                    .map(|mark| mark.clone_ref(py))
                    .collect();
                let mut indices = row.indices.clone();
                indices.extend(names.iter().cloned().map(|name| (name, index)));
                let mut scopes = row.scopes.clone();
                if let Some(scope) = scope {
                    scopes.extend(names.iter().cloned().map(|name| (name, scope)));
                }
                expanded.push(ParameterRow {
                    suffix,
                    parameters,
                    marks: row_marks,
                    idlist,
                    parameter_order: parameter_order.clone(),
                    indirect: row.indirect.union(&indirect).cloned().collect(),
                    indices,
                    scopes,
                    inferred_scope_groups: row
                        .inferred_scope_groups
                        .iter()
                        .cloned()
                        .chain(scope.is_none().then(|| names.clone()))
                        .collect(),
                    ..ParameterRow::default()
                });
                if expanded.len() > 1_000_000 {
                    return Err(PyValueError::new_err(
                        "parametrization exceeds 1000000 cases",
                    ));
                }
            }
        }
        rows = expanded;
    }
    Ok(rows)
}

/// How each parametrize value maps onto the argument names, as pytest decides
/// from the spelling of the names.
#[derive(Clone, Copy)]
enum ValueShape {
    /// One name given as a string: each value is that argument's value.
    Single,
    /// Names given as a comma-separated string or a sequence: each value is
    /// a sequence of argument values.
    Sequence,
}

fn parameter_values(
    values: Bound<'_, PyAny>,
    name_count: usize,
    shape: ValueShape,
) -> PyResult<Vec<Bound<'_, PyAny>>> {
    let py = values.py();
    let values = if values.is_instance(
        &py.import("vfy.independent.facade")?
            .getattr("ParameterSet")?,
    )? {
        values.getattr("values")?
    } else if matches!(shape, ValueShape::Single) {
        PyTuple::new(py, [&values])?.into_any()
    } else {
        values
    };
    let values = values.try_iter()?.collect::<PyResult<Vec<_>>>()?;
    if values.len() != name_count {
        return Err(PyValueError::new_err(
            "parametrize names and values have different lengths",
        ));
    }
    Ok(values)
}

fn parameter_scope(kwargs: &Bound<'_, PyDict>) -> PyResult<Option<FixtureScope>> {
    let scope = kwargs
        .get_item("scope")?
        .filter(|value| !value.is_none())
        .map(|value| {
            let name = value.extract::<String>()?;
            FixtureScope::parse(&name).ok_or_else(|| {
                PyValueError::new_err(format!("invalid parametrization scope: {name}"))
            })
        })
        .transpose()?;
    Ok(scope)
}

fn parameter_marks(values: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    metadata(values, "marks")?.map_or_else(
        || Ok(Vec::new()),
        |marks| {
            marks
                .try_iter()?
                .map(|mark| {
                    let mark = mark?;
                    Ok(mark.getattr("mark")?.unbind())
                })
                .collect()
        },
    )
}

fn parameter_id(
    values: &[Bound<'_, PyAny>],
    names: &[String],
    index: usize,
    escaping: IdEscaping,
) -> PyResult<String> {
    let mut parts = Vec::new();
    for (part_index, (value, name)) in values.iter().zip(names).enumerate() {
        parts.push(
            id_from_value(value, escaping, part_index)?.unwrap_or_else(|| format!("{name}{index}")),
        );
    }
    Ok(parts.join("-"))
}

/// Convert types pytest accepts as IDs; unsupported objects use the caller's fallback.
fn id_from_value(
    value: &Bound<'_, PyAny>,
    escaping: IdEscaping,
    part_index: usize,
) -> PyResult<Option<String>> {
    let id = if let Ok(text) = value.cast::<PyString>() {
        escaping.text(text)?
    } else if let Ok(value) = value.cast::<PyBytes>() {
        if matches!(escaping, IdEscaping::Disabled) {
            return Err(PyTypeError::new_err(format!(
                "sequence item {part_index}: expected str instance, bytes found"
            )));
        }
        escape_identifier(
            value.as_bytes().iter().copied().map(u32::from),
            BackslashEscaping::Keep,
        )
    } else if value.is_none()
        || value.is_instance_of::<PyInt>()
        || value.is_instance_of::<PyFloat>()
        || value.is_instance_of::<PyComplex>()
    {
        value.str()?.to_string_lossy().into_owned()
    } else if value.is_instance(&value.py().import("re")?.getattr("Pattern")?)? {
        return id_from_value(&value.getattr("pattern")?, IdEscaping::Ascii, part_index);
    } else if value.is(&value
        .py()
        .import("vfy.independent.facade")?
        .getattr("NOTSET")?)
    {
        return Ok(None);
    } else if value.is_instance(&value.py().import("enum")?.getattr("Enum")?)? {
        value.str()?.to_string_lossy().into_owned()
    } else if let Some(name) =
        metadata(value, "__name__")?.filter(|name| name.extract::<String>().is_ok())
    {
        name.extract::<String>()?
    } else {
        return Ok(None);
    };
    Ok(Some(id))
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum BackslashEscaping {
    Escape,
    Keep,
}

fn escape_identifier(value: impl Iterator<Item = u32>, backslashes: BackslashEscaping) -> String {
    let mut output = String::with_capacity(value.size_hint().0);
    for code in value {
        match char::from_u32(code) {
            Some('\t') => output.push_str("\\t"),
            Some('\n') => output.push_str("\\n"),
            Some('\r') => output.push_str("\\r"),
            Some('\\') if backslashes == BackslashEscaping::Escape => output.push_str("\\\\"),
            Some(character) if (' '..='~').contains(&character) => output.push(character),
            _ => {
                if code <= 0xff {
                    let _ = write!(output, "\\x{code:02x}");
                } else if code <= 0xffff {
                    let _ = write!(output, "\\u{code:04x}");
                } else {
                    let _ = write!(output, "\\U{code:08x}");
                }
            }
        }
    }
    output
}

fn indirect_names(kwargs: &Bound<'_, PyDict>, names: &[String]) -> PyResult<BTreeSet<String>> {
    let Some(value) = kwargs.get_item("indirect")? else {
        return Ok(BTreeSet::new());
    };
    let indirect = if let Ok(value) = value.extract::<bool>() {
        if value {
            names.iter().cloned().collect()
        } else {
            BTreeSet::new()
        }
    } else {
        value.extract::<Vec<String>>()?.into_iter().collect()
    };
    if let Some(name) = indirect.iter().find(|name| !names.contains(name)) {
        return Err(PyValueError::new_err(format!(
            "indirect fixture {name} is not a parametrized argument"
        )));
    }
    Ok(indirect)
}

impl ParameterRow {
    fn extend_fixture_marks(&mut self, marks: Vec<Py<PyAny>>) {
        let count = marks.len();
        self.marks
            .splice(self.fixture_mark_count..self.fixture_mark_count, marks);
        self.fixture_mark_count += count;
    }

    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            suffix: self.suffix.clone(),
            parameters: self
                .parameters
                .iter()
                .map(|(key, value)| (key.clone(), value.clone_ref(py)))
                .collect(),
            marks: self.marks.iter().map(|value| value.clone_ref(py)).collect(),
            fixture_parameters: self
                .fixture_parameters
                .iter()
                .map(|(key, value)| {
                    (
                        *key,
                        FixtureParameter {
                            index: value.index,
                            value: value.value.clone_ref(py),
                        },
                    )
                })
                .collect(),
            fixture_ids: self.fixture_ids.clone(),
            idlist: self.idlist.clone(),
            parameter_order: self.parameter_order.clone(),
            fixture_names: self.fixture_names.clone(),
            fixture_mark_count: self.fixture_mark_count,
            indirect: self.indirect.clone(),
            indices: self.indices.clone(),
            scopes: self.scopes.clone(),
            inferred_scope_groups: self.inferred_scope_groups.clone(),
        }
    }
}

#[derive(Default)]
struct RequiredFixtures {
    stack: Vec<FixtureId>,
    definitions: Vec<FixtureId>,
    names: Vec<String>,
}

fn required_fixtures(
    name: &str,
    fixtures: &[FixturePlan],
    chains: &FixtureChains,
    direct: &BTreeMap<String, Py<PyAny>>,
    required: &mut RequiredFixtures,
) {
    if direct.contains_key(name) || name == "request" {
        return;
    }
    if !required.names.iter().any(|value| value == name) {
        required.names.push(name.to_owned());
    }
    let depth = required
        .stack
        .iter()
        .filter(|id| fixtures[id.index()].name == name)
        .count();
    let Some(chain) = chains.by_name.get(name) else {
        return;
    };
    let Some(index) = chain.len().checked_sub(depth + 1) else {
        return;
    };
    let id = chain[index];
    if required.definitions.contains(&id) {
        return;
    }
    if required.stack.contains(&id) {
        return;
    }
    required.stack.push(id);
    for dependency in &fixtures[id.index()].dependencies {
        required_fixtures(dependency, fixtures, chains, direct, required);
    }
    required.stack.pop();
    required.definitions.push(id);
}

// Pytest callspec parameters are keyed by name, shared by all overridden definitions.
fn parameter_source(
    name: &str,
    fixtures: &[FixturePlan],
    bindings: &[FixtureBinding],
    chains: &FixtureChains,
) -> Option<FixtureId> {
    for &id in chains.by_name.get(name)?.iter().rev() {
        if bindings[id.index()].parameters.is_some() {
            return Some(id);
        }
        if !fixtures[id.index()]
            .dependencies
            .iter()
            .any(|dependency| dependency == name)
        {
            break;
        }
    }
    None
}

fn forward_parameter(
    py: Python<'_>,
    row: &mut ParameterRow,
    required: &[FixtureId],
    fixtures: &[FixturePlan],
    name: &str,
    parameter: &FixtureParameter,
) {
    for id in required {
        if fixtures[id.index()].name == name {
            row.fixture_parameters.insert(
                id.index(),
                FixtureParameter {
                    index: parameter.index,
                    value: parameter.value.clone_ref(py),
                },
            );
        }
    }
}

fn expand_fixture_parameter(
    generation: &ParameterGeneration<'_, '_>,
    rows: Vec<ParameterRow>,
    source: FixtureId,
    fixtures: &[FixturePlan],
    bindings: &[FixtureBinding],
    required: &[FixtureId],
) -> PyResult<Vec<ParameterRow>> {
    let py = generation.callable.py();
    let fixture = &fixtures[source.index()];
    let binding = &bindings[source.index()];
    let Some(values) = &binding.parameters else {
        return Ok(rows);
    };
    let attach = |row: &mut ParameterRow, id: String, index: usize, value: Py<PyAny>| {
        row.fixture_ids.push(id);
        row.fixture_names.push(fixture.name.clone());
        row.scopes
            .insert(fixture.name.clone(), fixture.declared_scope);
        forward_parameter(
            py,
            row,
            required,
            fixtures,
            &fixture.name,
            &FixtureParameter { index, value },
        );
    };
    if values.is_empty() {
        let (values, marks, id) =
            empty_parameter_choice(generation, std::slice::from_ref(&fixture.name))?;
        ParameterIds::resolve(
            binding.ids.as_ref().map(|value| value.bind(py)),
            1,
            generation.nodeid,
        )?;
        let missing = values
            .into_iter()
            .next()
            .ok_or_else(|| {
                PyRuntimeError::new_err("empty fixture parameter has no generated value")
            })?
            .unbind();
        let mut next = Vec::new();
        for mut row in rows {
            row.extend_fixture_marks(marks.iter().map(|mark| mark.clone_ref(py)).collect());
            attach(&mut row, id.clone(), 0, missing.clone_ref(py));
            next.push(row);
            validate_case_count(next.len())?;
        }
        return Ok(next);
    }
    let choices = fixture_identifiers(generation, values, binding, &fixture.name)?;
    let mut next = Vec::new();
    for row in rows {
        for (index, value) in values.iter().enumerate() {
            let mut row = row.clone_ref(py);
            let value = value.bind(py);
            let parameter_set = value.is_instance(
                &py.import("vfy.independent.facade")?
                    .getattr("ParameterSet")?,
            )?;
            if parameter_set {
                row.extend_fixture_marks(parameter_marks(value)?);
            }
            let actual = if parameter_set {
                value.getattr("values")?.get_item(0)?
            } else {
                value.clone()
            };
            attach(&mut row, choices[index].clone(), index, actual.unbind());
            next.push(row);
            validate_case_count(next.len())?;
        }
    }
    Ok(next)
}

/// The declared scope of a fixture name's last definition, or function scope when unknown.
fn declared_scope(name: &str, fixtures: &[FixturePlan], chains: &FixtureChains) -> FixtureScope {
    chains
        .by_name
        .get(name)
        .and_then(|chain| chain.last())
        .map_or(FixtureScope::Function, |id| {
            fixtures[id.index()].declared_scope
        })
}

fn prepare_indirect_parameters(
    row: &mut ParameterRow,
    fixtures: &[FixturePlan],
    chains: &FixtureChains,
) -> PyResult<Vec<(String, FixtureParameter)>> {
    for names in &row.inferred_scope_groups {
        let scope = names
            .iter()
            .map(|name| declared_scope(name, fixtures, chains))
            .min_by_key(|scope| scope.rank())
            .unwrap_or(FixtureScope::Function);
        row.scopes
            .extend(names.iter().cloned().map(|name| (name, scope)));
    }
    let mut indirect_parameters = Vec::new();
    for name in &row.indirect {
        let id = chains
            .by_name
            .get(name)
            .and_then(|chain| chain.last())
            .ok_or_else(|| PyValueError::new_err(format!("indirect fixture {name} not found")))?;
        let value = row
            .parameters
            .remove(name)
            .ok_or_else(|| PyValueError::new_err(format!("indirect parameter {name} missing")))?;
        row.scopes
            .entry(name.clone())
            .or_insert(fixtures[id.index()].declared_scope);
        indirect_parameters.push((
            name.clone(),
            FixtureParameter {
                index: row.indices[name],
                value,
            },
        ));
    }
    Ok(indirect_parameters)
}

pub(super) fn expand_fixture_rows(
    generation: &ParameterGeneration<'_, '_>,
    rows: Vec<ParameterRow>,
    fixtures: &[FixturePlan],
    bindings: &[FixtureBinding],
    chains: &FixtureChains,
    roots: &[String],
) -> PyResult<Vec<ParameterRow>> {
    let py = generation.callable.py();
    let mut output = Vec::new();
    let mut fixture_order = Vec::new();
    // The graph depends on direct parameter names, not values. Keep callbacks
    // and parameter forwarding per row; fixtures, chains, and roots are fixed.
    let mut last_required: Option<(Vec<String>, RequiredFixtures)> = None;
    for mut row in rows {
        let indirect_parameters = prepare_indirect_parameters(&mut row, fixtures, chains)?;
        if last_required.as_ref().is_some_and(|(names, _)| {
            names.len() != row.parameters.len()
                || !names.iter().zip(row.parameters.keys()).all(|(a, b)| a == b)
        }) {
            last_required = None;
        }
        let (_, required) = last_required.get_or_insert_with(|| {
            let mut required = RequiredFixtures::default();
            for root in roots {
                required_fixtures(root, fixtures, chains, &row.parameters, &mut required);
            }
            required.names.sort_by_key(|name| {
                std::cmp::Reverse(declared_scope(name, fixtures, chains).rank())
            });
            (row.parameters.keys().cloned().collect(), required)
        });
        for (name, parameter) in indirect_parameters {
            forward_parameter(
                py,
                &mut row,
                &required.definitions,
                fixtures,
                &name,
                &parameter,
            );
        }
        let mut expanded = vec![row];
        for name in &required.names {
            let mut definitions = required
                .definitions
                .iter()
                .copied()
                .filter(|id| fixtures[id.index()].name == *name);
            let Some(first) = definitions.next() else {
                continue;
            };
            if expanded
                .first()
                .is_some_and(|row| row.fixture_parameters.contains_key(&first.index()))
            {
                continue;
            }
            if let Some(source) = parameter_source(name, fixtures, bindings, chains) {
                for id in std::iter::once(first).chain(definitions) {
                    if !fixture_order.contains(&id) {
                        fixture_order.push(id);
                    }
                }
                expanded = expand_fixture_parameter(
                    generation,
                    expanded,
                    source,
                    fixtures,
                    bindings,
                    &required.definitions,
                )?;
            }
        }
        output.extend(expanded);
        validate_case_count(output.len())?;
    }
    finalize_fixture_identifiers(&mut output, &fixture_order);
    Ok(output)
}

fn finalize_fixture_identifiers(output: &mut [ParameterRow], fixture_order: &[FixtureId]) {
    if !fixture_order.is_empty() {
        output.sort_by(|left, right| {
            fixture_order
                .iter()
                .map(|id| {
                    left.fixture_parameters
                        .get(&id.index())
                        .map_or(0, |parameter| parameter.index)
                })
                .cmp(fixture_order.iter().map(|id| {
                    right
                        .fixture_parameters
                        .get(&id.index())
                        .map_or(0, |parameter| parameter.index)
                }))
        });
    }
    for (index, row) in output.iter_mut().enumerate() {
        for name in row.parameters.keys() {
            row.indices.insert(name.clone(), index);
        }
        if !row.fixture_ids.is_empty() {
            row.idlist = row.fixture_ids.iter().chain(&row.idlist).cloned().collect();
            row.suffix = parameter_suffix(&row.idlist);
        }
        if !row.fixture_names.is_empty() {
            row.parameter_order = row
                .fixture_names
                .iter()
                .chain(&row.parameter_order)
                .cloned()
                .collect();
        }
    }
}

fn parameter_suffix(idlist: &[String]) -> String {
    format!("[{}]", idlist.join("-"))
}

fn fixture_identifiers(
    generation: &ParameterGeneration<'_, '_>,
    values: &[Py<PyAny>],
    binding: &FixtureBinding,
    name: &str,
) -> PyResult<Vec<String>> {
    let py = generation.callable.py();
    let ids = ParameterIds::resolve(
        binding.ids.as_ref().map(|value| value.bind(py)),
        values.len(),
        generation.nodeid,
    )?;
    let mut choices = Vec::new();
    for (index, value) in values.iter().enumerate() {
        let value = value.bind(py);
        let parameter_set = value.is_instance(
            &py.import("vfy.independent.facade")?
                .getattr("ParameterSet")?,
        )?;
        let actual = if parameter_set {
            value.getattr("values")?.get_item(0)?
        } else {
            value.clone()
        };
        let explicit_id = if parameter_set {
            metadata(value, "id")?.filter(|value| !value.is_none())
        } else {
            None
        };
        choices.push(ids.select(
            std::slice::from_ref(&actual),
            std::slice::from_ref(&name.to_owned()),
            index,
            explicit_id.as_ref(),
            generation.escaping,
        )?);
    }
    unique_identifiers(&mut choices);
    Ok(choices)
}

fn unique_identifiers(identifiers: &mut [String]) {
    let mut counts = BTreeMap::new();
    for id in identifiers.iter() {
        *counts.entry(id.as_str()).or_insert(0usize) += 1;
    }
    let mut counters = counts
        .into_iter()
        .filter(|(_, count)| *count > 1)
        .map(|(id, _)| (id.to_owned(), 0usize))
        .collect::<BTreeMap<_, _>>();
    if counters.is_empty() {
        return;
    }
    let mut reserved = identifiers.iter().cloned().collect::<BTreeSet<_>>();
    for id in identifiers {
        if let Some(counter) = counters.get_mut(id.as_str()) {
            let separator = if id.ends_with(|character: char| character.is_ascii_digit()) {
                "_"
            } else {
                ""
            };
            loop {
                let candidate = format!("{id}{separator}{counter}");
                *counter += 1;
                if reserved.insert(candidate.clone()) {
                    *id = candidate;
                    break;
                }
            }
        }
    }
}

fn validate_case_count(count: usize) -> PyResult<()> {
    if count > 1_000_000 {
        return Err(PyValueError::new_err(
            "fixture parametrization exceeds 1000000 cases",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests;
