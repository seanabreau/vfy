//! Cache authority stays with authenticated fresh native results.

use crate::parallel::independent::{ConsumedArguments, InvocationPurpose};
use crate::parallel::{MAX_PARALLEL_NODEIDS, MAX_PLAN_BYTES, io_error};
use crate::results::NativeRunResult;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use vfy_core::mutation::{MutationInput, MutationResult};
use vfy_core::pytest_config::PytestParserBehavior;
use vfy_core::test_execution::ConfigurationObservation;

#[derive(Clone, Eq, PartialEq, Serialize)]
struct InvocationInputs {
    executable: PathBuf,
    directory: PathBuf,
    arguments: Vec<String>,
    effective_arguments: Vec<String>,
    environment: BTreeMap<String, Option<String>>,
    parent_environment: BTreeMap<String, Option<String>>,
    source_state: Vec<(String, String)>,
}

struct ParserVariant {
    parser: PytestParserBehavior,
    effective_arguments: Vec<String>,
    consumed_arguments: Vec<String>,
}

/// Inputs the supervisor captures at spawn time, before application startup.
pub(crate) struct FreshInputs {
    invocation: InvocationInputs,
    parser_variants: Vec<ParserVariant>,
    timeout: Duration,
    route: ScopeRoute,
}

#[derive(Clone, Copy, Eq, PartialEq, Serialize)]
enum ScopeRoute {
    FreshProcess,
    RetainedCollection,
}

#[derive(Clone, Copy)]
enum EvidenceOrigin {
    Fresh(InvocationPurpose),
    PendingCollection,
}

#[derive(Clone, Eq, PartialEq, Serialize)]
struct FullSuiteScope {
    route: ScopeRoute,
    invocation: InvocationInputs,
    parser: PytestParserBehavior,
    configuration: ConfigurationObservation,
    nodeids: Vec<String>,
}

struct CachePartition {
    request: String,
    fingerprint: String,
    fingerprints: BTreeMap<String, String>,
    mutations: BTreeMap<String, MutationInput>,
    plans: BTreeMap<String, Vec<String>>,
    timeout: Duration,
}

/// Proof that a native collection or execution produced a result.
///
/// It has no serializer or public constructor; only the native owners issue it.
pub(crate) struct FreshEvidence {
    pid: u32,
    origin: EvidenceOrigin,
    scope: Option<FullSuiteScope>,
    mutation: Option<MutationResult>,
    timeout: Duration,
    partition: OnceLock<CachePartition>,
    lookup_used: AtomicBool,
}

fn invalid(message: &str) -> PyErr {
    PyValueError::new_err(message.to_owned())
}

const ENVIRONMENT_INPUTS: [&str; 16] = [
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTEST_ADDOPTS",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYTEST_PLUGINS",
    "COVERAGE_PROCESS_START",
    "COVERAGE_RCFILE",
    "VFY_MUTATION_TEST_ISOLATION",
    "VFY_MUTATION_WORKERS",
    "PYTHONOPTIMIZE",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "PYTHONCOERCECLOCALE",
    "LC_ALL",
    "LC_CTYPE",
    "LANG",
];

fn parent_environment() -> BTreeMap<String, Option<String>> {
    ENVIRONMENT_INPUTS
        .into_iter()
        .map(|name| (name.to_owned(), std::env::var(name).ok()))
        .collect()
}

fn source_state(
    directory: &Path,
    nodeids: &[String],
    arguments: &[String],
) -> PyResult<Vec<(String, String)>> {
    vfy_core::mutation::mutation_suite_state(directory, nodeids, arguments)
        .into_iter()
        .map(|(path, digest)| {
            let digest = match digest {
                Ok(digest) => digest,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => "missing".to_owned(),
                Err(error) => return Err(io_error(error)),
            };
            Ok((path, digest))
        })
        .collect()
}

impl FreshInputs {
    pub(crate) fn capture(
        executable: &str,
        directory: &Path,
        arguments: &[String],
        environment: &std::collections::HashMap<String, String>,
        purpose: InvocationPurpose,
        timeout: Duration,
        current_parser: Option<PytestParserBehavior>,
    ) -> PyResult<Self> {
        let directory = directory.canonicalize().map_err(io_error)?;
        let executable = Path::new(executable).canonicalize().map_err(io_error)?;
        let mut executed = vec![
            "-p".to_owned(),
            "vfy.independent.coverage_support".to_owned(),
        ];
        executed.extend_from_slice(arguments);
        executed.push(
            match purpose {
                InvocationPurpose::FreshCollection => "--collect-only",
                InvocationPurpose::FreshSerial => "-x",
                InvocationPurpose::Parallel => {
                    return Err(invalid(
                        "mutation cache requires a fresh invocation purpose",
                    ));
                }
            }
            .to_owned(),
        );
        // A foreign interpreter reports its parser mode in the authenticated
        // completion, so retain each possible discovery result before spawn.
        let candidates: &[PytestParserBehavior] = match current_parser.as_ref() {
            Some(parser) => std::slice::from_ref(parser),
            None => &[PytestParserBehavior::Legacy, PytestParserBehavior::Current],
        };
        let mut parser_variants = Vec::new();
        for &parser in candidates {
            let resolve = |arguments: &[String]| {
                vfy_core::pytest_config::resolve_pytest_configuration_with_parser(
                    arguments,
                    &directory,
                    environment.get("PYTEST_ADDOPTS").map(String::as_str),
                    vfy_core::PytestConfigPolicy::TomlWithAddoptsOverrides,
                    parser,
                )
                .map(|projection| projection.effective_args)
                .map_err(|error| PyValueError::new_err(error.to_string()))
            };
            match resolve(arguments)
                .and_then(|effective| resolve(&executed).map(|consumed| (effective, consumed)))
            {
                Ok((effective_arguments, consumed_arguments)) => {
                    parser_variants.push(ParserVariant {
                        parser,
                        effective_arguments,
                        consumed_arguments,
                    });
                }
                Err(error) if current_parser.is_some() => return Err(error),
                // The worker can still execute and report the right error. It
                // cannot authorize a cache entry without a matching variant.
                Err(_) => {}
            }
        }
        let effective_arguments = parser_variants
            .first()
            .map_or_else(Vec::new, |variant| variant.effective_arguments.clone());
        let environment = ENVIRONMENT_INPUTS
            .into_iter()
            .map(|name| (name.to_owned(), environment.get(name).cloned()))
            .collect();
        let source_state = source_state(&directory, &[], arguments)?;
        Ok(Self {
            invocation: InvocationInputs {
                executable,
                directory,
                arguments: arguments.to_vec(),
                effective_arguments,
                environment,
                parent_environment: parent_environment(),
                source_state,
            },
            parser_variants,
            timeout,
            route: ScopeRoute::FreshProcess,
        })
    }

    pub(super) fn complete(
        self,
        nodeids: &[String],
        result: &NativeRunResult,
        consumed: Option<&ConsumedArguments>,
        purpose: InvocationPurpose,
        mutation: Option<MutationResult>,
        command_status: i64,
    ) -> PyResult<FreshEvidence> {
        if consumed.is_none() {
            return Err(invalid(
                "fresh cache result omitted its consumed argument witness",
            ));
        }
        let configuration = result
            .record()
            .observations
            .as_ref()
            .ok_or_else(|| invalid("fresh cache result has no consumed configuration"))?
            .configuration
            .clone();
        let matching_status = match purpose {
            InvocationPurpose::FreshCollection => {
                command_status == 0 && result.record().exitstatus == Some(0)
            }
            InvocationPurpose::FreshSerial => mutation
                .as_ref()
                .is_some_and(|mutation| mutation.returncode.map(i64::from) == Some(command_status)),
            InvocationPurpose::Parallel => false,
        };
        let timeout = self.timeout;
        let scope = self.observe_scope(nodeids, configuration, consumed, matching_status)?;
        Ok(FreshEvidence {
            pid: std::process::id(),
            origin: EvidenceOrigin::Fresh(purpose),
            scope,
            mutation,
            timeout,
            partition: OnceLock::new(),
            lookup_used: AtomicBool::new(false),
        })
    }

    pub(crate) fn retained(mut self) -> Self {
        self.route = ScopeRoute::RetainedCollection;
        self
    }

    pub(crate) fn pending_collection(
        self,
        nodeids: &[String],
        configuration: ConfigurationObservation,
        consumed: Option<&ConsumedArguments>,
    ) -> PyResult<FreshEvidence> {
        if self.route != ScopeRoute::RetainedCollection {
            return Err(invalid(
                "pending collection requires its retained input owner",
            ));
        }
        let timeout = self.timeout;
        let scope = self.observe_scope(nodeids, configuration, consumed, true)?;
        Ok(FreshEvidence {
            pid: std::process::id(),
            origin: EvidenceOrigin::PendingCollection,
            scope,
            mutation: None,
            timeout,
            partition: OnceLock::new(),
            lookup_used: AtomicBool::new(false),
        })
    }

    fn observe_scope(
        self,
        nodeids: &[String],
        configuration: ConfigurationObservation,
        consumed: Option<&ConsumedArguments>,
        valid: bool,
    ) -> PyResult<Option<FullSuiteScope>> {
        let current = source_state(
            &self.invocation.directory,
            nodeids,
            &self.invocation.arguments,
        )?;
        let matching = match consumed {
            Some(ConsumedArguments::Stable { arguments, parser }) => {
                let discovery = if *parser == PytestParserBehavior::FixedFinal {
                    PytestParserBehavior::Legacy
                } else {
                    *parser
                };
                self.parser_variants.iter().find_map(|variant| {
                    let variant_discovery = if variant.parser == PytestParserBehavior::FixedFinal {
                        PytestParserBehavior::Legacy
                    } else {
                        variant.parser
                    };
                    (discovery == variant_discovery && *arguments == variant.consumed_arguments)
                        .then_some((&variant.effective_arguments, *parser))
                })
            }
            _ => None,
        };
        let stable = matching.is_some()
            && current == self.invocation.source_state
            && parent_environment() == self.invocation.parent_environment
            // Duplicate display names are legitimate collection data. Until a
            // stronger reusable compiled identity is carried, execute them and
            // deny caching rather than deduplicating or rejecting the suite.
            && nodeids.iter().collect::<BTreeSet<_>>().len() == nodeids.len()
            && valid;
        let mut invocation = self.invocation;
        if let Some((effective, _)) = matching {
            invocation.effective_arguments.clone_from(effective);
        }
        Ok(matching
            .filter(|_| stable)
            .map(|(_, parser)| FullSuiteScope {
                route: self.route,
                invocation,
                parser,
                configuration,
                nodeids: nodeids.to_vec(),
            }))
    }
}

impl FullSuiteScope {
    fn current(&self) -> PyResult<bool> {
        Ok(parent_environment() == self.invocation.parent_environment
            && source_state(
                &self.invocation.directory,
                &self.nodeids,
                &self.invocation.arguments,
            )? == self.invocation.source_state)
    }
}

impl FreshEvidence {
    fn clean_scope(&self) -> PyResult<Option<&FullSuiteScope>> {
        if self.pid != std::process::id()
            || !matches!(
                self.origin,
                EvidenceOrigin::Fresh(InvocationPurpose::FreshCollection)
                    | EvidenceOrigin::PendingCollection
            )
            || self.mutation.is_some()
        {
            return Err(invalid(
                "mutation cache requires its actual fresh collection result",
            ));
        }
        self.scope
            .as_ref()
            .map(|scope| scope.current().map(|current| current.then_some(scope)))
            .transpose()
            .map(Option::flatten)
    }

    fn partition(&self, request: &str) -> PyResult<Option<&CachePartition>> {
        if request.len() as u64 > MAX_PLAN_BYTES {
            return Err(invalid(
                "fresh mutation fingerprint request exceeds the supported size",
            ));
        }
        let Some(scope) = self.clean_scope()? else {
            return Ok(None);
        };
        if let Some(partition) = self.partition.get() {
            if partition.request != request {
                return Err(invalid(
                    "fresh collection cache partition was already bound",
                ));
            }
            return Ok(Some(partition));
        }
        let value: serde_json::Value = serde_json::from_str(request)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let arguments: Vec<String> = serde_json::from_value(value["pytest_args"].clone())
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        if arguments != scope.invocation.arguments {
            return Err(invalid(
                "mutation cache request differs from its actual invocation",
            ));
        }
        let mutations: Vec<MutationInput> = serde_json::from_value(value["mutations"].clone())
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let mut plans: BTreeMap<String, Vec<String>> =
            serde_json::from_value(value["evidence"].clone())
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
        for tests in plans.values_mut().filter(|tests| !tests.is_empty()) {
            if let Some(ordered) =
                vfy_core::mutation::mutation_collection_order(&scope.nodeids, tests)
            {
                *tests = ordered;
            }
        }
        let timeout = value["timeout"]
            .as_str()
            .and_then(|value| value.parse::<f64>().ok())
            .and_then(|value| Duration::try_from_secs_f64(value).ok())
            .filter(|value| !value.is_zero())
            .ok_or_else(|| invalid("fresh mutation cache has no valid execution timeout"))?;
        let execution_scope = serde_json::to_string(scope)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let (fingerprint, fingerprints) =
            vfy_core::mutation::fresh_full_suite_mutation_fingerprints(
                request,
                &scope.nodeids,
                &execution_scope,
            )
            .map_err(PyValueError::new_err)?;
        let partition = CachePartition {
            request: request.to_owned(),
            fingerprint,
            fingerprints,
            mutations: mutations
                .into_iter()
                .map(|input| (input.id.clone(), input))
                .collect(),
            plans,
            timeout,
        };
        let _ = self.partition.set(partition);
        match self.partition.get() {
            Some(partition) if partition.request == request => Ok(Some(partition)),
            _ => Err(invalid(
                "fresh collection cache partition was already bound",
            )),
        }
    }

    fn eligible(
        &self,
        clean: &FullSuiteScope,
        expected: &BTreeMap<String, MutationInput>,
        plans: &BTreeMap<String, Vec<String>>,
        timeout: Duration,
    ) -> PyResult<Option<MutationResult>> {
        if self.pid != std::process::id()
            || !matches!(
                self.origin,
                EvidenceOrigin::Fresh(InvocationPurpose::FreshSerial)
            )
        {
            return Err(invalid("cache update has no actual fresh mutant result"));
        }
        let mutation = self
            .mutation
            .as_ref()
            .ok_or_else(|| invalid("cache update has no canonical native mutation result"))?;
        let requested = expected
            .get(&mutation.mutation.id)
            .ok_or_else(|| invalid("cache update belongs to an unrequested mutation"))?;
        let actual = serde_json::to_value(&mutation.mutation)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let requested = serde_json::to_value(requested)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        if actual != requested {
            return Err(invalid(
                "cache update mutation differs from its catalog input",
            ));
        }
        let selected = mutation.tests.is_empty()
            || (mutation.mutation.owner.phase == vfy_core::mutation::MutationPhase::Body
                && plans.get(&mutation.mutation.id) == Some(&mutation.tests)
                && mutation
                    .tests
                    .iter()
                    .all(|test| clean.nodeids.contains(test))
                && mutation.tests.iter().collect::<BTreeSet<_>>().len() == mutation.tests.len());
        let eligible = selected
            && self.timeout == timeout
            && self.scope.as_ref().is_some_and(|scope| scope == clean);
        Ok(eligible.then(|| mutation.clone()))
    }
}

impl FreshEvidence {
    pub(crate) fn fingerprints(
        &self,
        request: &str,
    ) -> PyResult<Option<(String, BTreeMap<String, String>)>> {
        self.partition(request).map(|partition| {
            partition.map(|value| (value.fingerprint.clone(), value.fingerprints.clone()))
        })
    }

    pub(crate) fn read(&self, path: &str, request: &str) -> PyResult<String> {
        let partition = self
            .partition(request)?
            .ok_or_else(|| invalid("collection inputs changed before cache lookup"))?;
        if self.lookup_used.swap(true, Ordering::AcqRel) {
            return Err(invalid("collection already authorized a cache lookup"));
        }
        let cached = vfy_core::mutation::read_mutation_cache(
            Path::new(path),
            &partition.fingerprint,
            Some(&partition.fingerprints),
        )
        .map_err(pyo3::exceptions::PyOSError::new_err)?;
        serde_json::to_string(&cached).map_err(|error| PyValueError::new_err(error.to_string()))
    }

    pub(crate) fn write_inputs(&self, request: &str) -> PyResult<Option<CacheWrite>> {
        let Some(partition) = self.partition(request)? else {
            return Ok(None);
        };
        let scope = self
            .clean_scope()?
            .cloned()
            .ok_or_else(|| invalid("collection inputs changed before cache update"))?;
        Ok(Some(CacheWrite {
            scope,
            fingerprint: partition.fingerprint.clone(),
            fingerprints: partition.fingerprints.clone(),
            mutations: partition.mutations.clone(),
            plans: partition.plans.clone(),
            timeout: partition.timeout,
        }))
    }

    pub(crate) fn catalog_matches(
        &self,
        request: &str,
        expected: &[(MutationInput, Vec<String>)],
    ) -> PyResult<bool> {
        let Some(partition) = self.partition(request)? else {
            return Ok(false);
        };
        if expected.len() != partition.mutations.len() {
            return Err(invalid(
                "retained cache catalog differs from its execution plan",
            ));
        }
        let nodeids = &self
            .scope
            .as_ref()
            .ok_or_else(|| invalid("retained cache has no collection scope"))?
            .nodeids;
        for (input, tests) in expected {
            let ordered = vfy_core::mutation::mutation_collection_order(nodeids, tests);
            let current = partition
                .mutations
                .get(&input.id)
                .ok_or_else(|| invalid("retained cache catalog omitted an execution candidate"))?;
            if serde_json::to_value(current).map_err(|error| invalid(&error.to_string()))?
                != serde_json::to_value(input).map_err(|error| invalid(&error.to_string()))?
                || partition.plans.get(&input.id) != ordered.as_ref()
            {
                return Err(invalid(
                    "retained cache catalog differs from its execution plan",
                ));
            }
        }
        Ok(true)
    }
}

pub(crate) struct CacheWrite {
    scope: FullSuiteScope,
    fingerprint: String,
    fingerprints: BTreeMap<String, String>,
    mutations: BTreeMap<String, MutationInput>,
    plans: BTreeMap<String, Vec<String>>,
    timeout: Duration,
}

impl CacheWrite {
    pub(crate) fn commit(
        self,
        py: Python<'_>,
        path: &str,
        results: Vec<Py<NativeRunResult>>,
    ) -> PyResult<()> {
        if results.len() > MAX_PARALLEL_NODEIDS {
            return Err(invalid(
                "fresh mutation cache batch exceeds the supported size",
            ));
        }
        let mut admitted = BTreeMap::new();
        for result in results {
            let mutation = {
                let result = result.borrow(py);
                let evidence = result.fresh_evidence().ok_or_else(|| {
                    invalid("cache update has no authenticated fresh result evidence")
                })?;
                evidence.eligible(&self.scope, &self.mutations, &self.plans, self.timeout)?
            };
            if let Some(mutation) = mutation
                && admitted
                    .insert(mutation.mutation.id.clone(), mutation)
                    .is_some()
            {
                return Err(invalid("cache update repeats a native mutation result"));
            }
        }
        if admitted.is_empty() {
            return Ok(());
        }
        let report = serde_json::json!({"schema":"vfy.mutation", "fingerprint":self.fingerprint,
            "mutations":admitted.into_values().collect::<Vec<_>>()});
        vfy_core::mutation::write_mutation_cache(
            Path::new(path),
            &report.to_string(),
            Some(&self.fingerprints),
        )
        .map_err(PyValueError::new_err)
    }
}

pub(crate) fn fingerprints(
    result: &NativeRunResult,
    request: &str,
) -> PyResult<Option<(String, BTreeMap<String, String>)>> {
    result
        .fresh_evidence()
        .ok_or_else(|| invalid("result has no authenticated fresh cache evidence"))?
        .fingerprints(request)
}

pub(crate) fn read(result: &NativeRunResult, path: &str, request: &str) -> PyResult<String> {
    result
        .fresh_evidence()
        .ok_or_else(|| invalid("result has no authenticated fresh cache evidence"))?
        .read(path, request)
}

pub(crate) fn write(
    clean: &Bound<'_, NativeRunResult>,
    path: &str,
    request: &str,
    results: Vec<Py<NativeRunResult>>,
) -> PyResult<()> {
    let inputs = {
        let clean = clean.borrow();
        clean
            .fresh_evidence()
            .ok_or_else(|| invalid("result has no authenticated fresh cache evidence"))?
            .write_inputs(request)?
    };
    if let Some(inputs) = inputs {
        inputs.commit(clean.py(), path, results)?;
    }
    Ok(())
}
