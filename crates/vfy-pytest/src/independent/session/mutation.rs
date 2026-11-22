//! Isolated body mutations consume a pristine retained suite only in fork children.

use super::super::{NativeIndependentInvocation, global_capture, runtime_providers};
use super::{NativeIndependentSession, State};
use crate::parallel::{NativeParallelSession, NativeParallelWorker};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyTuple};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::rc::Rc;
use vfy_core::mutation::{FreshMutation, MutationInput, MutationPhase};

#[cfg(test)]
mod tests;

fn fork_admitted(py: Python<'_>, descriptors: &[i32]) -> PyResult<bool> {
    let sys = py.import("sys")?;
    let gil_enabled = match sys.getattr("_is_gil_enabled") {
        Ok(check) => check.call0()?.extract::<bool>()?,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => true,
        Err(error) => return Err(error),
    };
    let safe_runtime = gil_enabled
        && sys.call_method0("gettrace")?.is_none()
        && sys.call_method0("getprofile")?.is_none();
    Ok(safe_runtime && crate::parallel::validate_fork_resources(descriptors, &[]).is_ok())
}

fn selected_request(
    request: &ValidatedRequest,
    nodeids: &[String],
    lines: &BTreeMap<String, BTreeSet<i32>>,
) -> PyResult<bool> {
    Ok(request.scope == Scope::Selected
        && !request.tests.is_empty()
        && vfy_core::mutation::mutation_collection_order(nodeids, &request.tests).is_some()
        && body_isolated(request.mutation.input(), lines)?)
}

#[derive(Clone, Copy, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Scope {
    Selected,
    FullSuite,
    Unreached,
}

#[derive(Deserialize)]
struct Request {
    mutation: MutationInput,
    tests: Vec<String>,
    scope: Scope,
}

struct ValidatedRequest {
    mutation: FreshMutation,
    tests: Vec<String>,
    scope: Scope,
}

fn parse_requests(encoded: &str) -> PyResult<Vec<ValidatedRequest>> {
    if encoded.len() as u64 > crate::parallel::MAX_PLAN_BYTES {
        return Err(PyValueError::new_err(
            "mutation batch exceeds the supported metadata size",
        ));
    }
    let requests: Vec<Request> =
        serde_json::from_str(encoded).map_err(|error| PyValueError::new_err(error.to_string()))?;
    if requests.len() > crate::parallel::MAX_PARALLEL_NODEIDS {
        return Err(PyValueError::new_err(
            "mutation batch exceeds the supported candidate count",
        ));
    }
    let mut seen = BTreeSet::new();
    requests
        .into_iter()
        .map(|request| {
            let mutation = FreshMutation::new(request.mutation).map_err(PyValueError::new_err)?;
            if !seen.insert(mutation.input().id.clone())
                || request.tests.len() > crate::parallel::MAX_PARALLEL_NODEIDS
                || request.tests.iter().any(|test| {
                    test.is_empty()
                        || test.len() > crate::parallel::MAX_NODEID_BYTES
                        || test.contains('\0')
                })
                || (request.scope == Scope::Selected) == request.tests.is_empty()
                || (request.scope == Scope::Selected
                    && mutation.input().owner.phase != MutationPhase::Body)
            {
                return Err(PyValueError::new_err(
                    "mutation batch has invalid test plans or repeated candidates",
                ));
            }
            Ok(ValidatedRequest {
                mutation,
                tests: request.tests,
                scope: request.scope,
            })
        })
        .collect()
}

fn collection_lines(path: &str) -> PyResult<BTreeMap<String, BTreeSet<i32>>> {
    let data = vfy_core::data::CoverageData::read_path(Path::new(path))
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(vfy_core::mutation::mutation_unattributed_lines(&data))
}

fn source_line(prefix: &[u8]) -> PyResult<i32> {
    i32::try_from(prefix.split(|byte| *byte == b'\n').count())
        .map_err(|_| PyValueError::new_err("mutation source exceeds the supported line count"))
}

fn body_isolated(input: &MutationInput, lines: &BTreeMap<String, BTreeSet<i32>>) -> PyResult<bool> {
    if !input.owner.fast_eligible {
        return Ok(false);
    }
    let source = std::fs::read(&input.path)?;
    vfy_core::mutation::mutation_body_isolated(input, &source, lines).map_err(PyValueError::new_err)
}

#[pyfunction]
pub(crate) fn has_collectable_mutations(requests: &str, context_data: &str) -> PyResult<bool> {
    let requests = parse_requests(requests)?;
    let lines = collection_lines(context_data)?;
    for request in requests {
        if request.scope == Scope::Selected
            && !request.tests.is_empty()
            && body_isolated(request.mutation.input(), &lines)?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

#[pyfunction]
pub(crate) fn fresh_mutation_test_plans(
    requests: &str,
    context_data: &str,
) -> PyResult<Vec<Vec<String>>> {
    let requests = parse_requests(requests)?;
    let lines = collection_lines(context_data)?;
    requests
        .into_iter()
        .map(|request| {
            Ok(
                if request.scope == Scope::Selected
                    && body_isolated(request.mutation.input(), &lines)?
                {
                    request.tests
                } else {
                    Vec::new()
                },
            )
        })
        .collect()
}

fn live_function<'py>(
    py: Python<'py>,
    input: &MutationInput,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let target = std::fs::canonicalize(&input.path)?;
    let target_metadata = std::fs::metadata(&target).ok();
    let modules = py
        .import("sys")?
        .getattr("modules")?
        .cast_into::<PyDict>()?;
    let mut seen = BTreeSet::new();
    let mut found = None;
    for module in modules.values().iter() {
        if !seen.insert(module.as_ptr() as usize) {
            continue;
        }
        let Ok(filename) = module
            .getattr("__file__")
            .and_then(|value| value.extract::<String>())
        else {
            continue;
        };
        // Distinct file identities cannot resolve to the same canonical path.
        // Matching identities still need canonicalization: hard links share an
        // inode without being aliases of the same path.
        if let (Some(target), Ok(candidate)) = (&target_metadata, std::fs::metadata(&filename))
            && (target.dev(), target.ino()) != (candidate.dev(), candidate.ino())
        {
            continue;
        }
        if std::fs::canonicalize(filename).ok().as_ref() == Some(&target) {
            if found.is_some() {
                return Ok(None);
            }
            found = Some(module);
        }
    }
    let Some(mut value) = found else {
        return Ok(None);
    };
    if input.owner.qualname.contains("<locals>") {
        return Ok(None);
    }
    for name in input.owner.qualname.split('.') {
        let Ok(next) = value.getattr(name) else {
            return Ok(None);
        };
        value = next;
    }
    if let Ok(function) = value.getattr("__func__") {
        value = function;
    }
    let function_type = py.import("types")?.getattr("FunctionType")?;
    Ok(value.is_instance(&function_type)?.then_some(value))
}

fn matching_codes<'py>(
    code: &Bound<'py, PyAny>,
    qualname: &str,
    line: i32,
    found: &mut Vec<Bound<'py, PyAny>>,
) -> PyResult<()> {
    if code.getattr("co_qualname")?.extract::<String>()? == qualname
        && code.getattr("co_firstlineno")?.extract::<i32>()? == line
    {
        found.push(code.clone());
    }
    let code_type = code.py().import("types")?.getattr("CodeType")?;
    for value in code.getattr("co_consts")?.cast_into::<PyTuple>()?.iter() {
        if value.is_instance(&code_type)? {
            matching_codes(&value, qualname, line, found)?;
        }
    }
    Ok(())
}

fn compiled_owner<'py>(
    py: Python<'py>,
    input: &MutationInput,
    source: &[u8],
    line: i32,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("dont_inherit", true)?;
    let compiled = py.import("builtins")?.getattr("compile")?.call(
        (PyBytes::new(py, source), &input.path, "exec"),
        Some(&kwargs),
    )?;
    let mut candidates = Vec::new();
    matching_codes(&compiled, &input.owner.qualname, line, &mut candidates)?;
    Ok(if candidates.len() == 1 {
        candidates.pop()
    } else {
        None
    })
}

struct CodeReplacement<'py> {
    function: Bound<'py, PyAny>,
    current: Bound<'py, PyAny>,
    changed: Bound<'py, PyAny>,
}

fn replacement<'py>(
    py: Python<'py>,
    mutation: &FreshMutation,
) -> PyResult<Option<CodeReplacement<'py>>> {
    let input = mutation.input();
    if !input.owner.fast_eligible || input.owner.phase != MutationPhase::Body {
        return Ok(None);
    }
    let Some(function) = live_function(py, input)? else {
        return Ok(None);
    };
    let source = std::fs::read(&input.path)?;
    if input.owner.start as usize >= source.len() || input.owner.end as usize > source.len() {
        return Ok(None);
    }
    let patched = mutation.patch(&source).map_err(PyValueError::new_err)?;
    let line = source_line(&source[..input.owner.start as usize])?;
    let Some(clean) = compiled_owner(py, input, &source, line)? else {
        return Ok(None);
    };
    let Some(changed) = compiled_owner(py, input, &patched, line)? else {
        return Ok(None);
    };
    let current = function.getattr("__code__")?;
    if !current.eq(&clean)?
        || current.getattr("co_qualname")?.extract::<String>()? != input.owner.qualname
        || current.getattr("co_firstlineno")?.extract::<i32>()? != line
    {
        return Ok(None);
    }
    for name in [
        "co_freevars",
        "co_cellvars",
        "co_argcount",
        "co_posonlyargcount",
        "co_kwonlyargcount",
    ] {
        if !current.getattr(name)?.eq(changed.getattr(name)?)? {
            return Ok(None);
        }
    }
    let flags = |code: &Bound<'_, PyAny>| code.getattr("co_flags")?.extract::<u32>();
    if flags(&current)? & 0x2a0 != flags(&changed)? & 0x2a0 {
        return Ok(None);
    }
    Ok(Some(CodeReplacement {
        function,
        current,
        changed,
    }))
}

fn retained_replacement<'py>(
    py: Python<'py>,
    mutation: &FreshMutation,
    observed: Option<&[Py<PyAny>]>,
) -> PyResult<Option<CodeReplacement<'py>>> {
    let Some(observed) = observed else {
        return Ok(None);
    };
    let Some(binding) = replacement(py, mutation)? else {
        return Ok(None);
    };
    Ok((!observed
        .iter()
        .any(|function| function.bind(py).is(&binding.function)))
    .then_some(binding))
}

#[pyfunction]
pub(crate) fn install_mutation_code(py: Python<'_>, payload: &str) -> PyResult<Option<Py<PyAny>>> {
    let input =
        serde_json::from_str(payload).map_err(|error| PyValueError::new_err(error.to_string()))?;
    let mutation = FreshMutation::new(input).map_err(PyValueError::new_err)?;
    let Some(CodeReplacement {
        function, changed, ..
    }) = replacement(py, &mutation)?
    else {
        return Ok(None);
    };
    function.setattr("__code__", changed)?;
    Ok(Some(function.unbind()))
}

#[pyclass(unsendable)]
struct MutationWorker {
    session: Py<NativeIndependentSession>,
    mutation: FreshMutation,
    tests: Vec<String>,
}

#[pymethods]
impl MutationWorker {
    fn __call__(
        &self,
        py: Python<'_>,
        directory: &str,
        index: usize,
        worker: &Bound<'_, NativeParallelWorker>,
    ) -> PyResult<i32> {
        let session = self.session.bind(py);
        let lifecycle = match &session.borrow().state {
            State::Pending(suite) => {
                global_capture::rebind_fork(suite.context.config.bind(py))?;
                runtime_providers::rebind_fork(suite.context.config.bind(py))?;
                suite.context.invocation.retain_session.set(true);
                *suite.context.invocation.session.borrow_mut() =
                    Some(suite.context.session.clone_ref(py));
                Rc::clone(&suite.context.invocation)
            }
            _ => {
                return Err(PyRuntimeError::new_err(
                    "retained mutation requires its pending suite",
                ));
            }
        };
        let owner = NativeIndependentInvocation::from_collected(
            py,
            lifecycle,
            directory,
            index,
            worker.clone().unbind(),
        )?;
        let result = (|| {
            worker.call_method1("prepare_independent", (&self.tests,))?;
            let observed = session
                .borrow()
                .mutation_observed_code
                .as_ref()
                .map(|functions| {
                    functions
                        .iter()
                        .map(|function| function.clone_ref(py))
                        .collect::<Vec<_>>()
                });
            let Some(CodeReplacement {
                function,
                current,
                changed,
            }) = retained_replacement(py, &self.mutation, observed.as_deref())?
            else {
                return Err(PyValueError::new_err(
                    "collected mutation callable identity changed",
                ));
            };
            let source = std::fs::read(&self.mutation.input().path)?;
            owner
                .borrow_mut(py)
                .patch_collected_source(&self.mutation.input().path, &source)?;
            if !function.getattr("__code__")?.is(&current) {
                return Err(PyValueError::new_err(
                    "collected mutation callable changed before installation",
                ));
            }
            function.setattr("__code__", changed)?;
            NativeIndependentSession::run(session, py, Some(self.tests.clone()))
        })();
        NativeIndependentInvocation::complete_collected(owner.bind(py), result, worker)
    }
}

#[derive(Clone, Copy)]
pub(super) struct MutationBatch<'a, 'py> {
    pub(super) requests: &'a str,
    pub(super) arguments: &'a [String],
    pub(super) timeout: f64,
    pub(super) context_data: &'a str,
    pub(super) fresh: &'a Bound<'py, PyAny>,
    pub(super) record: &'a Bound<'py, PyAny>,
}

impl NativeIndependentSession {
    pub(super) fn pending_mutation_evidence(&self) -> PyResult<&crate::parallel::FreshEvidence> {
        if !matches!(self.state, State::Pending(_)) {
            return Err(PyRuntimeError::new_err(
                "mutation cache requires its pending collection",
            ));
        }
        self.mutation_evidence
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection has no mutation cache authority"))
    }

    pub(super) fn prepare_mutation_cache_owned(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        request: &str,
        plans: &str,
        context_data: &str,
    ) -> PyResult<Option<(String, BTreeMap<String, String>)>> {
        let requests = parse_requests(plans)?;
        let lines = collection_lines(context_data)?;
        let (suite, inputs, observed) = {
            let mut owner = slf.borrow_mut();
            if owner.mutation_evidence.is_some() {
                return Err(PyRuntimeError::new_err(
                    "retained mutation cache was already prepared",
                ));
            }
            let inputs = owner.mutation_inputs.take().ok_or_else(|| {
                PyRuntimeError::new_err("mutation cache requires its original collection inputs")
            })?;
            let observed = owner.mutation_observed_code.as_ref().map(|functions| {
                functions
                    .iter()
                    .map(|function| function.clone_ref(py))
                    .collect::<Vec<_>>()
            });
            (owner.take_pending()?, inputs, observed)
        };
        let prepared = (|| -> PyResult<Option<crate::parallel::FreshEvidence>> {
            if suite
                .collection_reports
                .iter()
                .any(|report| report.outcome == vfy_core::test_execution::CollectionOutcome::Failed)
            {
                return Ok(None);
            }
            let nodeids = suite
                .plan
                .tests()
                .iter()
                .map(|test| test.nodeid.clone())
                .collect::<Vec<_>>();
            let mut descriptors = global_capture::fork_descriptors(suite.context.config.bind(py))?;
            descriptors.extend(runtime_providers::fork_descriptors(
                suite.context.config.bind(py),
            )?);
            descriptors.extend([0, 1, 2]);
            if !fork_admitted(py, &descriptors)? {
                return Ok(None);
            }
            let mut eligible = false;
            for candidate in &requests {
                if selected_request(candidate, &nodeids, &lines)?
                    && retained_replacement(py, &candidate.mutation, observed.as_deref())?.is_some()
                {
                    eligible = true;
                    break;
                }
            }
            if !eligible {
                return Ok(None);
            }
            let configuration = super::super::observations::configuration(py, &suite.context)?;
            let consumed = suite.context.invocation.consumed_arguments.borrow().clone();
            let evidence = inputs.pending_collection(&nodeids, configuration, consumed.as_ref())?;
            let expected = requests
                .iter()
                .map(|candidate| (candidate.mutation.input().clone(), candidate.tests.clone()))
                .collect::<Vec<_>>();
            if !evidence.catalog_matches(request, &expected)? {
                return Ok(None);
            }
            Ok(Some(evidence))
        })();
        slf.borrow_mut().state = State::Pending(Box::new(suite));
        let Some(evidence) = prepared? else {
            return Ok(None);
        };
        let partition = evidence.fingerprints(request)?;
        slf.borrow_mut().mutation_evidence = Some(evidence);
        Ok(partition)
    }

    pub(super) fn run_mutations_owned(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        batch: MutationBatch<'_, '_>,
    ) -> PyResult<usize> {
        let MutationBatch {
            requests,
            arguments,
            timeout,
            context_data,
            fresh,
            record,
        } = batch;
        let requests = parse_requests(requests)?;
        let lines = collection_lines(context_data)?;
        let (nodeids, mut descriptors, observed) = {
            let session = slf.borrow();
            let State::Pending(suite) = &session.state else {
                return Err(PyRuntimeError::new_err(
                    "mutation batch requires its pristine collection",
                ));
            };
            let mut descriptors = global_capture::fork_descriptors(suite.context.config.bind(py))?;
            descriptors.extend(runtime_providers::fork_descriptors(
                suite.context.config.bind(py),
            )?);
            (
                suite
                    .plan
                    .tests()
                    .iter()
                    .map(|test| test.nodeid.clone())
                    .collect::<Vec<_>>(),
                descriptors,
                session.mutation_observed_code.as_ref().map(|functions| {
                    functions
                        .iter()
                        .map(|function| function.clone_ref(py))
                        .collect::<Vec<_>>()
                }),
            )
        };
        descriptors.extend([0, 1, 2]);
        let mut fork_safe = fork_admitted(py, &descriptors)?;
        let mut retained = 0;
        for (index, mut request) in requests.into_iter().enumerate() {
            py.check_signals()?;
            let selected = selected_request(&request, &nodeids, &lines)?;
            if selected {
                request.tests =
                    vfy_core::mutation::mutation_collection_order(&nodeids, &request.tests)
                        .ok_or_else(|| {
                            PyValueError::new_err("validated mutation selection changed")
                        })?;
            }
            let mutation = request.mutation;
            if !selected
                || !fork_safe
                || retained_replacement(py, &mutation, observed.as_deref())?.is_none()
            {
                fresh.call1((index, if selected { request.tests } else { Vec::new() }))?;
                continue;
            }
            // Recheck before every fork: collection callbacks and earlier fresh
            // fallback processes can have changed the controller's resources.
            fork_safe = crate::parallel::validate_fork_resources(&descriptors, &[]).is_ok();
            if !fork_safe {
                fresh.call1((index, &request.tests))?;
                continue;
            }
            let input = serde_json::to_string(mutation.input())
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
            let bootstrap = Py::new(
                py,
                MutationWorker {
                    session: slf.clone().unbind(),
                    mutation,
                    tests: request.tests.clone(),
                },
            )?;
            let (result, outcome) = NativeParallelSession::run_retained_mutation(
                py,
                bootstrap.bind(py).as_any(),
                crate::parallel::mutation::RetainedMutation {
                    mutation: &input,
                    tests: request.tests,
                    collection: nodeids.clone(),
                    arguments: arguments.to_owned(),
                    timeout,
                    allowed_fds: &descriptors,
                },
            )?;
            let outcome = outcome.ok_or_else(|| {
                PyRuntimeError::new_err("retained mutation omitted its canonical result")
            })?;
            let result = result.map(|result| Py::new(py, result)).transpose()?;
            record.call1((index, outcome, result))?;
            retained += 1;
        }
        Ok(retained)
    }
}
