//! Portable CI baselines, selection plans, and audits.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::io::Write as _;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tempfile::NamedTempFile;

use super::graph::PortableGraph;
use super::plan::node_file;
use super::{SelectionMode, SelectionPlan, read_git_replay_state, sort_tests, test_context};
use crate::data::CoverageData;
use crate::{CoreError, absolute_path, atomic_write, file_digest, glob_path, replay_fingerprint};

const VERSION: u32 = 1;
const MAX_ARTIFACT_BYTES: u64 = 128 * 1024 * 1024;
const MAX_RECORDS: usize = 1_000_000;

fn bytes_digest(bytes: &[u8]) -> String {
    let mut result = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        let _ = write!(result, "{byte:02x}");
    }
    result
}

fn invalid(message: impl std::fmt::Display) -> CoreError {
    CoreError::InvalidInput(format!("CI selection: {message}"))
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, CoreError> {
    read_json_bounded(path, MAX_ARTIFACT_BYTES)
}

fn read_json_bounded<T: serde::de::DeserializeOwned>(
    path: &Path,
    max_bytes: u64,
) -> Result<T, CoreError> {
    let metadata = std::fs::metadata(path).map_err(invalid)?;
    if metadata.len() > max_bytes {
        return Err(invalid("artifact exceeds 128 MiB"));
    }
    serde_json::from_slice(&std::fs::read(path).map_err(invalid)?).map_err(invalid)
}

fn read_history(path: &Path, max_bytes: u64) -> Result<serde_json::Value, CoreError> {
    if super::sqlite_history::uses_sqlite(path) {
        super::sqlite_history::read_document(path, max_bytes).map_err(invalid)
    } else {
        read_json_bounded(path, max_bytes)
    }
}

fn write_json(path: &Path, value: &impl Serialize) -> Result<(), CoreError> {
    let bytes = serde_json::to_vec_pretty(value).map_err(CoreError::Serialization)?;
    if bytes.len() as u64 > MAX_ARTIFACT_BYTES {
        return Err(invalid("artifact exceeds 128 MiB"));
    }
    atomic_write(path, &bytes).map_err(|source| CoreError::ArtifactIo {
        path: path.to_path_buf(),
        operation: "write CI artifact",
        source,
    })
}

fn git(root: &Path, args: &[&str]) -> Result<Vec<u8>, CoreError> {
    let output = Command::new("git")
        .current_dir(root)
        .args(args)
        .output()
        .map_err(invalid)?;
    if !output.status.success() {
        return Err(invalid(String::from_utf8_lossy(&output.stderr)));
    }
    Ok(output.stdout)
}

fn git_text(root: &Path, args: &[&str]) -> Result<String, CoreError> {
    String::from_utf8(git(root, args)?)
        .map(|value| value.trim().to_owned())
        .map_err(invalid)
}

/// Resolve the repository root for portable CI identities.
///
/// # Errors
/// Rejects missing Git state or invocation below the repository root.
pub fn repository_root() -> Result<PathBuf, CoreError> {
    let root = PathBuf::from(git_text(Path::new("."), &["rev-parse", "--show-toplevel"])?);
    if absolute_path(Path::new(".")) != absolute_path(&root) {
        return Err(invalid("invoke CI selection from the repository root"));
    }
    Ok(root)
}

fn checked_relative(path: &str) -> Result<(), CoreError> {
    if path.is_empty()
        || path.contains('\\')
        || path.split('/').any(|part| matches!(part, "" | "." | ".."))
        || Path::new(path)
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(invalid(format!("invalid repository path {path:?}")));
    }
    Ok(())
}

pub(super) fn relative_path(root: &Path, path: &Path) -> Result<String, CoreError> {
    let path = absolute_path(path);
    let relative = path
        .strip_prefix(root)
        .map_err(|_| invalid("external source roots are unsupported for CI baselines"))?;
    let result = glob_path(relative);
    checked_relative(&result)?;
    Ok(result)
}

/// Whether a tracked path can change what the tests run.
///
/// The interpreter imports bytecode from `__pycache__` only for an existing source file
/// and rewrites it when it no longer matches that source (PEP 3147), as vfy's
/// assertion rewriting does for test modules. Tracked bytecode there is a
/// cache of other inputs, and importing tests can rewrite it during a run.
fn source_input(path: &str) -> bool {
    !path.split('/').any(|part| part == "__pycache__")
}

fn tracked_files(root: &Path) -> Result<BTreeSet<String>, CoreError> {
    let bytes = git(root, &["ls-files", "-z"])?;
    let mut files: BTreeSet<String> = bytes
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8(part.to_vec()).map_err(invalid))
        .collect::<Result<_, _>>()?;
    files.retain(|path| source_input(path));
    if files.len() > MAX_RECORDS {
        return Err(invalid("too many tracked files"));
    }
    for path in &files {
        checked_relative(path)?;
    }
    Ok(files)
}

pub(super) fn collection_inventory(nodeids: &[String]) -> Result<BTreeSet<String>, CoreError> {
    if nodeids.len() > MAX_RECORDS || nodeids.iter().any(String::is_empty) {
        return Err(invalid("invalid collected test identities"));
    }
    Ok(nodeids.iter().cloned().collect())
}

fn checked_collection(nodeids: &[String]) -> Result<BTreeSet<String>, CoreError> {
    let result = collection_inventory(nodeids)?;
    if result.len() != nodeids.len() {
        return Err(invalid("duplicate collected test identities"));
    }
    Ok(result)
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BaselineBundle {
    version: u32,
    commit: String,
    fingerprint: String,
    source_digests: BTreeMap<String, String>,
    source_roots: Vec<String>,
    collected: Vec<String>,
    history: serde_json::Value,
    import_graph: PortableGraph,
    coverage: Vec<u8>,
    coverage_digest: String,
}

fn read_coverage(bytes: &[u8]) -> Result<CoverageData, CoreError> {
    if bytes.len() as u64 > MAX_ARTIFACT_BYTES {
        return Err(invalid("coverage exceeds artifact limit"));
    }
    let mut temporary = NamedTempFile::new().map_err(invalid)?;
    temporary.write_all(bytes).map_err(invalid)?;
    temporary.flush().map_err(invalid)?;
    CoverageData::read_path(temporary.path()).map_err(invalid)
}

fn coverage_bytes(data: &CoverageData) -> Result<Vec<u8>, CoreError> {
    let temporary = NamedTempFile::new().map_err(invalid)?.into_temp_path();
    data.write_path(&temporary).map_err(invalid)?;
    std::fs::read(&temporary).map_err(invalid)
}

#[derive(Clone, Copy)]
pub struct BaselineExport<'a> {
    pub destination: &'a Path,
    pub coverage_path: &'a Path,
    pub history_path: &'a Path,
    pub collection_path: &'a Path,
    pub fingerprint: &'a str,
    pub source_roots: &'a [String],
    pub ignored: &'a [String],
}

fn export_digests(root: &Path) -> Result<BTreeMap<String, String>, CoreError> {
    let tracked = tracked_files(root)?;
    let mut source_digests = BTreeMap::new();
    for path in &tracked {
        if root
            .join(path)
            .symlink_metadata()
            .map_err(invalid)?
            .file_type()
            .is_symlink()
        {
            return Err(invalid(
                "symlinked tracked inputs are unsupported for CI baselines",
            ));
        }
        source_digests.insert(
            path.clone(),
            file_digest(&root.join(path)).map_err(invalid)?,
        );
    }
    Ok(source_digests)
}

/// Export a passing full run from a clean checkout into one baseline artifact.
///
/// # Errors
/// Rejects source races, incomplete evidence, and external file identities.
pub fn export_baseline(request: BaselineExport<'_>) -> Result<(), CoreError> {
    let root = repository_root()?;
    let state = read_git_replay_state(request.ignored).map_err(invalid)?;
    if !state.changed.is_empty() {
        return Err(invalid("baseline export requires a clean checkout"));
    }
    let source_digests = export_digests(&root)?;
    let tracked: BTreeSet<_> = source_digests.keys().cloned().collect();
    let collected: Vec<String> = read_json(request.collection_path)?;
    let inventory = checked_collection(&collected)?;
    let data = CoverageData::read_path(request.coverage_path).map_err(invalid)?;
    if data
        .metadata_records()
        .iter()
        .find(|(key, _)| key == "vfy_replay_commit")
        .map(|(_, value)| value)
        != Some(&state.head)
    {
        return Err(invalid(
            "coverage does not belong to the current passing-baseline commit",
        ));
    }
    let mut mapping = BTreeMap::new();
    let mut targets = BTreeSet::new();
    for path in data.measured_files() {
        let relative = relative_path(&root, Path::new(&path))?;
        if !tracked.contains(&relative) {
            return Err(invalid("coverage contains an untracked source"));
        }
        if !targets.insert(relative.clone()) {
            return Err(invalid(
                "coverage paths have an ambiguous repository mapping",
            ));
        }
        mapping.insert(path, relative);
    }
    for context in data
        .line_measurements()
        .map(|(context, _, _)| context)
        .chain(data.arc_measurements().map(|(context, _, _)| context))
    {
        if !context.is_empty() && test_context(context).is_none_or(|test| !inventory.contains(test))
        {
            return Err(invalid(
                "coverage context does not match the collected inventory",
            ));
        }
    }
    let mut portable = data.remapped_files(&mapping).map_err(invalid)?;
    // Local cache metadata contains absolute paths; the bundle owns portable identity.
    for (key, _) in portable.metadata_records() {
        if !matches!(key.as_str(), "has_arcs" | "vfy_optimize" | "version") {
            portable.set_metadata(&key, "");
        }
    }
    portable.set_metadata("vfy_replay_commit", &state.head);
    let coverage = coverage_bytes(&portable)?;
    let coverage_digest = bytes_digest(&coverage);
    let source_roots = request
        .source_roots
        .iter()
        .map(|path| {
            let absolute = absolute_path(Path::new(path));
            if absolute == root {
                Ok(".".to_owned())
            } else {
                relative_path(&root, &absolute)
            }
        })
        .collect::<Result<_, CoreError>>()?;
    let bundle = BaselineBundle {
        version: VERSION,
        commit: state.head.clone(),
        fingerprint: request.fingerprint.to_owned(),
        source_digests,
        source_roots,
        collected,
        history: read_history(request.history_path, MAX_ARTIFACT_BYTES)?,
        import_graph: PortableGraph::export(request.history_path, &root)?,
        coverage,
        coverage_digest,
    };
    bundle.import_graph.validate(&tracked)?;
    // Source may change while the bundle is assembled.
    for (path, digest) in &bundle.source_digests {
        if file_digest(&root.join(path)).map_err(invalid)? != *digest {
            return Err(invalid("source changed during baseline export"));
        }
    }
    let current = read_git_replay_state(request.ignored).map_err(invalid)?;
    if current.head != state.head || !current.changed.is_empty() {
        return Err(invalid("checkout changed during baseline export"));
    }
    write_json(request.destination, &bundle)
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CiPlan {
    pub version: u32,
    pub baseline_commit: Option<String>,
    pub target_commit: String,
    pub source_identity: String,
    pub selection: SelectionPlan,
    pub collected: Vec<String>,
    pub audit: Option<AuditResult>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuditResult {
    pub complete: bool,
    pub omitted_failures: Vec<String>,
}

pub struct ImportedBaseline {
    pub data: Option<CoverageData>,
    pub plan: CiPlan,
}

fn full_plan(reason: &str) -> SelectionPlan {
    SelectionPlan {
        mode: SelectionMode::Safe,
        selected: Vec::new(),
        all_tests: Vec::new(),
        durations: BTreeMap::new(),
        affected_files: Vec::new(),
        affected_functions: 0,
        affected_branches: 0,
        selected_seconds: None,
        full_seconds: None,
        fallback_full: true,
        reasons: vec![reason.to_owned()],
        location: None,
        scoped_files: BTreeMap::new(),
        collection_files: None,
    }
}

fn validate_bundle(bundle: &BaselineBundle) -> Result<CoverageData, CoreError> {
    if bundle.version != VERSION
        || bundle.source_digests.len() > MAX_RECORDS
        || bundle.commit.len() != 40 && bundle.commit.len() != 64
        || !bundle.commit.bytes().all(|byte| byte.is_ascii_hexdigit())
        || bundle.fingerprint.is_empty()
    {
        return Err(invalid("invalid baseline version or identity"));
    }
    let inventory = checked_collection(&bundle.collected)?;
    for (path, digest) in &bundle.source_digests {
        checked_relative(path)?;
        if digest.len() != 64 || !digest.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(invalid("invalid source digest"));
        }
    }
    for root in &bundle.source_roots {
        if root != "." {
            checked_relative(root)?;
        }
    }
    let tracked = bundle.source_digests.keys().cloned().collect();
    bundle.import_graph.validate(&tracked)?;
    if bytes_digest(&bundle.coverage) != bundle.coverage_digest {
        return Err(invalid("coverage digest mismatch"));
    }
    let data = read_coverage(&bundle.coverage)?;
    if data
        .metadata_records()
        .iter()
        .find(|(key, _)| key == "vfy_replay_commit")
        .map(|(_, value)| value)
        != Some(&bundle.commit)
    {
        return Err(invalid("coverage belongs to a different baseline commit"));
    }
    for path in data.measured_files() {
        checked_relative(&path)?;
        if !bundle.source_digests.contains_key(&path) {
            return Err(invalid("coverage source is absent from baseline digests"));
        }
    }
    for context in data
        .line_measurements()
        .map(|(context, _, _)| context)
        .chain(data.arc_measurements().map(|(context, _, _)| context))
    {
        if !context.is_empty() && test_context(context).is_none_or(|test| !inventory.contains(test))
        {
            return Err(invalid("coverage refers to an unknown test"));
        }
    }
    let Some(tests) = bundle
        .history
        .get("tests")
        .and_then(serde_json::Value::as_object)
    else {
        return Err(invalid("invalid baseline history"));
    };
    if tests.len() > MAX_RECORDS
        || tests.values().any(|value| {
            value
                .get("duration_ms")
                .and_then(serde_json::Value::as_f64)
                .is_none_or(|duration| !duration.is_finite() || duration < 0.0)
        })
    {
        return Err(invalid("invalid baseline timing history"));
    }
    Ok(data)
}

fn eligible_changes(
    root: &Path,
    bundle: &BaselineBundle,
    fingerprint: &str,
    state: &super::GitReplayState,
) -> Result<Result<BTreeSet<String>, String>, CoreError> {
    if bundle.fingerprint != fingerprint {
        return Ok(Err(
            "the Python, configuration, environment, or pytest invocation changed".to_owned(),
        ));
    }
    let ancestor = Command::new("git")
        .current_dir(root)
        .args(["merge-base", "--is-ancestor", &bundle.commit, &state.head])
        .status()
        .map_err(invalid)?;
    if !ancestor.success() {
        return Ok(Err(
            "baseline commit is unavailable or is not an ancestor of HEAD".to_owned(),
        ));
    }
    let tracked = tracked_files(root)?;
    if tracked != bundle.source_digests.keys().cloned().collect()
        || state.untracked
        || state.deleted
    {
        return Ok(Err(
            "added, deleted, renamed, or untracked inputs require a full run".to_owned(),
        ));
    }
    let statuses = git(
        root,
        &[
            "diff",
            "--name-status",
            "--no-renames",
            &bundle.commit,
            "--",
        ],
    )?;
    if statuses
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .any(|line| !line.starts_with(b"M\t"))
    {
        return Ok(Err(
            "added, deleted, or renamed inputs require a full run".to_owned()
        ));
    }
    let mut changed = BTreeSet::new();
    for (path, digest) in &bundle.source_digests {
        let absolute = root.join(path);
        if absolute
            .symlink_metadata()
            .map_err(invalid)?
            .file_type()
            .is_symlink()
        {
            return Ok(Err("symlinked inputs require a full run".to_owned()));
        }
        if file_digest(&absolute).map_err(invalid)? != *digest {
            changed.insert(path.clone());
        }
    }
    if changed.iter().any(|path| {
        Path::new(path)
            .extension()
            .is_none_or(|extension| extension != "py")
            || path.ends_with("conftest.py")
    }) {
        return Ok(Err(
            "configuration or unsupported runtime inputs changed".to_owned()
        ));
    }
    Ok(Ok(changed))
}

fn affected_paths(
    root: &Path,
    bundle: &BaselineBundle,
    changed: &BTreeSet<String>,
) -> Result<Result<BTreeSet<String>, Vec<String>>, CoreError> {
    let closure = bundle.import_graph.dependents(changed);
    if !closure.reasons.is_empty() {
        return Ok(Err(closure.reasons));
    }
    let mut affected = changed.clone();
    affected.extend(closure.dependents);
    let absolute_changed: BTreeSet<_> = changed
        .iter()
        .map(|path| glob_path(&root.join(path)))
        .collect();
    let roots: Vec<_> = bundle
        .source_roots
        .iter()
        .map(|path| root.join(path))
        .chain(std::iter::once(root.to_path_buf()))
        .collect();
    if !changed.is_empty() {
        let current_graph = super::static_dependents(
            &absolute_changed,
            &roots,
            &root.join(".vfy/ci-unused-graph.json"),
            "",
        );
        if !current_graph.reasons.is_empty() {
            return Ok(Err(current_graph.reasons));
        }
        for path in current_graph.dependents {
            affected.insert(relative_path(root, Path::new(&path))?);
        }
    }
    Ok(Ok(affected))
}

fn selected_tests(
    data: &CoverageData,
    collected: &[String],
    affected: &BTreeSet<String>,
) -> BTreeSet<String> {
    let observed: BTreeSet<_> = data
        .measured_contexts()
        .into_iter()
        .filter_map(|context| test_context(&context).map(str::to_owned))
        .collect();
    // Tests without attributed evidence cannot be proven independent of an edit.
    let mut selected: BTreeSet<_> = collected
        .iter()
        .filter(|test| !observed.contains(*test))
        .cloned()
        .collect();
    for (context, path, _) in data.line_measurements() {
        if affected.contains(path)
            && let Some(test) = test_context(context)
        {
            selected.insert(test.to_owned());
        }
    }
    for (context, path, _) in data.arc_measurements() {
        if affected.contains(path)
            && let Some(test) = test_context(context)
        {
            selected.insert(test.to_owned());
        }
    }
    selected
}

fn empty_import(target_commit: String) -> ImportedBaseline {
    ImportedBaseline {
        data: None,
        plan: CiPlan {
            version: VERSION,
            baseline_commit: None,
            target_commit,
            source_identity: String::new(),
            selection: full_plan("no compatible CI baseline exists"),
            collected: Vec::new(),
            audit: None,
        },
    }
}

/// Validate ancestor evidence and select at file granularity across revisions.
///
/// # Errors
/// Rejects malformed artifacts. Missing or incompatible evidence selects the full suite.
pub fn import_baseline(
    path: &Path,
    fingerprint: &str,
    ignored: &[String],
) -> Result<ImportedBaseline, CoreError> {
    let state = read_git_replay_state(ignored).ok();
    let mut result = empty_import(
        state
            .as_ref()
            .map_or_else(String::new, |state| state.head.clone()),
    );
    if !path.exists() {
        result.plan.selection.reasons = vec!["CI baseline is missing".to_owned()];
        return Ok(result);
    }
    let bundle: BaselineBundle = read_json(path)?;
    let data = validate_bundle(&bundle)?;
    result.plan.baseline_commit = Some(bundle.commit.clone());
    let Ok(root) = repository_root() else {
        result.plan.selection.reasons =
            vec!["Git state or repository-root invocation is unavailable".to_owned()];
        return Ok(result);
    };
    let Some(state) = state else {
        return Ok(result);
    };

    let changed = match eligible_changes(&root, &bundle, fingerprint, &state)? {
        Ok(changed) => changed,
        Err(reason) => {
            result.plan.selection.reasons = vec![reason];
            return Ok(result);
        }
    };
    result.plan.source_identity = source_identity(&root, &bundle.source_digests)?;
    let affected = match affected_paths(&root, &bundle, &changed)? {
        Ok(affected) => affected,
        Err(reasons) => {
            result.plan.selection.reasons = reasons;
            return Ok(result);
        }
    };
    // Without prior reachability, newly executable code cannot justify a subset.
    let measured_files = data.measured_files();
    if changed
        .iter()
        .any(|path| measured_files.binary_search(path).is_err())
    {
        result.plan.selection.reasons = vec!["changed source has no recorded coverage".to_owned()];
        return Ok(result);
    }
    let selected = selected_tests(&data, &bundle.collected, &affected);
    let durations: BTreeMap<_, _> = bundle.history["tests"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(test, value)| {
            value["duration_ms"]
                .as_f64()
                .map(|duration| (test.clone(), duration))
        })
        .collect();
    let mut selected: Vec<_> = selected.into_iter().collect();
    sort_tests(&mut selected, &durations);
    result.plan.selection = SelectionPlan {
        mode: SelectionMode::Safe,
        selected,
        all_tests: bundle.collected,
        durations,
        affected_files: changed
            .iter()
            .map(|path| glob_path(&root.join(path)))
            .collect(),
        affected_functions: 0,
        affected_branches: 0,
        selected_seconds: None,
        full_seconds: None,
        fallback_full: false,
        reasons: vec![
            "CI selection includes changed-file coverage and transitive import dependents"
                .to_owned(),
        ],
        location: None,
        scoped_files: BTreeMap::new(),
        collection_files: None,
    };
    let mapping = measured_files
        .into_iter()
        .map(|path| {
            let target = glob_path(&root.join(&path));
            (path, target)
        })
        .collect();
    result.data = Some(data.remapped_files(&mapping).map_err(invalid)?);
    Ok(result)
}

fn source_identity(root: &Path, files: &BTreeMap<String, String>) -> Result<String, CoreError> {
    let mut values = Vec::new();
    for path in files.keys() {
        values.push(path.clone());
        values.push(file_digest(&root.join(path)).map_err(invalid)?);
    }
    Ok(replay_fingerprint(values))
}

pub struct PlanRequest<'a> {
    pub path: &'a Path,
    pub baseline_commit: Option<String>,
    pub target_commit: String,
    pub source_identity: String,
    pub selection: SelectionPlan,
}

/// Save the selection plan consumed by collection hooks.
///
/// # Errors
/// Returns an error without replacing the destination on serialization or IO failure.
pub fn write_plan_request(request: PlanRequest<'_>) -> Result<(), CoreError> {
    let identity = if request.source_identity.len() == 64
        && request
            .source_identity
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit())
    {
        request.source_identity
    } else if let Ok(root) = repository_root() {
        let files = tracked_files(&root)?
            .into_iter()
            .map(|path| (path, String::new()))
            .collect();
        source_identity(&root, &files)?
    } else {
        request.source_identity
    };
    write_json(
        request.path,
        &CiPlan {
            version: VERSION,
            baseline_commit: request.baseline_commit,
            target_commit: request.target_commit,
            source_identity: identity,
            selection: request.selection,
            collected: Vec::new(),
            audit: None,
        },
    )
}

/// Finalize a plan after pytest's project collection hooks have finished.
///
/// # Errors
/// Rejects malformed identities before changing any persisted plan.
pub fn finalize_plan(
    request_path: &Path,
    collected: Vec<String>,
    output_path: &Path,
) -> Result<CiPlan, CoreError> {
    let plan = finalize_collection_plan(request_path, collected, CollectionIdentity::Unique)?;
    write_json(output_path, &plan)?;
    Ok(plan)
}

/// Baseline test identities a scoped selected run keeps instead of collecting their files.
pub struct CollectionScope {
    retained_files: BTreeSet<String>,
    retained: Vec<String>,
}

impl CollectionScope {
    /// Whether collection skips the file with this node-ID file part.
    #[must_use]
    pub fn skips(&self, file: &str) -> bool {
        self.retained_files.contains(file)
    }
}

/// Read the collection scope a prepared plan request allows, if any.
///
/// # Errors
/// Returns an error for an unreadable or unsupported request.
pub fn collection_scope(request_path: &Path) -> Result<Option<CollectionScope>, CoreError> {
    let plan: CiPlan = read_json(request_path)?;
    if plan.version != VERSION {
        return Err(invalid("unsupported plan version"));
    }
    let selection = plan.selection;
    let Some(files) = selection
        .collection_files
        .filter(|_| !selection.fallback_full)
    else {
        return Ok(None);
    };
    let retained: Vec<String> = selection
        .all_tests
        .into_iter()
        .filter(|test| !files.contains(node_file(test)))
        .collect();
    let retained_files = retained
        .iter()
        .map(|test| node_file(test).to_owned())
        .collect();
    Ok(Some(CollectionScope {
        retained_files,
        retained,
    }))
}

/// The result of binding a plan to the final independent collection.
pub enum IndependentCollection {
    /// The plan was finalized and both staged artifacts were published.
    Finalized(Box<CiPlan>),
    /// A scoped collection disagreed with its baseline identities. The scope was
    /// removed from the request so the run can be repeated with complete collection;
    /// nothing was published.
    ScopeChanged,
}

/// Finalize independent collection, retaining repeated display IDs as a full run.
///
/// With a `scope`, `collected` covers only the files the plan collects and the
/// scope supplies every other baseline identity. A scoped collection cannot run
/// as a full suite, so an inventory change removes the scope instead.
///
/// # Errors
/// Rejects invalid evidence before publishing either staged artifact.
pub fn finalize_independent_collection(
    request_path: &Path,
    mut collected: Vec<String>,
    scope: Option<&CollectionScope>,
    output_path: &Path,
    collection_path: &Path,
) -> Result<IndependentCollection, CoreError> {
    if let Some(scope) = scope {
        collected.extend(scope.retained.iter().cloned());
    }
    let plan =
        finalize_collection_plan(request_path, collected, CollectionIdentity::FullOnRepeated)?;
    if scope.is_some() && plan.selection.fallback_full {
        let mut request: CiPlan = read_json(request_path)?;
        request.selection.collection_files = None;
        write_json(request_path, &request)?;
        return Ok(IndependentCollection::ScopeChanged);
    }
    write_json(collection_path, &plan.collected)?;
    write_json(output_path, &plan)?;
    Ok(IndependentCollection::Finalized(Box::new(plan)))
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum CollectionIdentity {
    Unique,
    FullOnRepeated,
}

fn finalize_collection_plan(
    request_path: &Path,
    collected: Vec<String>,
    identity: CollectionIdentity,
) -> Result<CiPlan, CoreError> {
    let mut plan: CiPlan = read_json(request_path)?;
    let inventory = collection_inventory(&collected)?;
    let repeated = inventory.len() != collected.len();
    if repeated && identity == CollectionIdentity::Unique {
        return Err(invalid("duplicate collected test identities"));
    }
    if plan.version != VERSION {
        return Err(invalid("unsupported plan version"));
    }
    if repeated {
        plan.selection.fallback_full = true;
        plan.selection.reasons.push(
            "collected tests have repeated display identities; running the full suite".to_owned(),
        );
    }
    if !plan.selection.fallback_full
        && inventory != plan.selection.all_tests.iter().cloned().collect()
    {
        plan.selection.fallback_full = true;
        plan.selection
            .reasons
            .push("collected test inventory changed; running the full suite".to_owned());
    }
    plan.selection.all_tests = if repeated {
        collected.clone()
    } else {
        inventory.into_iter().collect()
    };
    if plan.selection.fallback_full {
        plan.selection.select_full();
    } else {
        plan.selection.selected_seconds =
            super::plan::seconds(&plan.selection.selected, &plan.selection.durations);
        plan.selection.full_seconds =
            super::plan::seconds(&plan.selection.all_tests, &plan.selection.durations);
    }
    plan.collected = collected;
    Ok(plan)
}

/// Read and validate a finalized selection plan.
///
/// # Errors
/// Returns an error for invalid evidence or failed filesystem operations.
pub fn read_plan(path: &Path) -> Result<CiPlan, CoreError> {
    read_json(path)
}

/// Check that cross-commit input bytes still match the prepared plan.
///
/// # Errors
/// Returns an error for invalid evidence or failed filesystem operations.
pub fn plan_source_current(path: &Path, ignored: &[String]) -> Result<bool, CoreError> {
    let plan = read_plan(path)?;
    if plan.source_identity.is_empty()
        || plan.source_identity.starts_with("git:")
        || plan.source_identity.starts_with("content:")
    {
        return Ok(true);
    }
    let root = repository_root()?;
    let files = tracked_files(&root)?
        .into_iter()
        .map(|path| (path, String::new()))
        .collect();
    let state = match read_git_replay_state(ignored) {
        Ok(state) => state,
        // A repository without commits has no HEAD. A plan prepared there has
        // no target commit and stays current while the source bytes match.
        Err(_) if plan.target_commit.is_empty() => {
            return Ok(source_identity(&root, &files)? == plan.source_identity);
        }
        Err(error) => return Err(invalid(error)),
    };
    Ok(source_identity(&root, &files)? == plan.source_identity
        && state.head == plan.target_commit
        && (plan.selection.fallback_full || !state.untracked))
}

/// Persist an explicit full-suite decision.
///
/// # Errors
/// Returns an error for invalid evidence or failed filesystem operations.
pub fn write_fallback_plan(
    path: &Path,
    target_commit: &str,
    reason: &str,
) -> Result<(), CoreError> {
    write_plan_request(PlanRequest {
        path,
        baseline_commit: None,
        target_commit: target_commit.to_owned(),
        source_identity: String::new(),
        selection: full_plan(reason),
    })
}

/// Persist the complete pytest collection.
///
/// # Errors
/// Returns an error for invalid evidence or failed filesystem operations.
pub fn write_collection(path: &Path, nodeids: &[String]) -> Result<(), CoreError> {
    checked_collection(nodeids)?;
    write_json(path, &nodeids)
}

/// Publish an audit of the proposed subset without changing the primary outcome.
///
/// # Errors
/// Rejects incomplete or invalid result artifacts before replacing the plan.
pub fn audit_plan(
    plan_path: &Path,
    result_path: &Path,
    status: i32,
    output: &Path,
) -> Result<AuditResult, CoreError> {
    let mut plan: CiPlan = read_json(plan_path)?;
    let run = if result_path.exists() {
        super::read_test_run(result_path)?
    } else {
        super::TestRun {
            infrastructure_failed: true,
            tests: Vec::new(),
        }
    };
    let observed: BTreeSet<_> = run.tests.iter().map(|test| test.nodeid.clone()).collect();
    let expected: BTreeSet<_> = plan.collected.iter().cloned().collect();
    let selected: BTreeSet<_> = plan.selection.selected.iter().cloned().collect();
    let result = AuditResult {
        complete: !run.infrastructure_failed && matches!(status, 0 | 1) && observed == expected,
        omitted_failures: run
            .tests
            .iter()
            .filter(|test| {
                test.outcome == super::TestOutcome::Failed && !selected.contains(&test.nodeid)
            })
            .map(|test| test.nodeid.clone())
            .collect(),
    };
    plan.audit = Some(result.clone());
    write_json(output, &plan)?;
    Ok(result)
}

fn portable_text(value: &str, root: &str) -> String {
    let mut result = String::new();
    let mut remaining = value;
    while let Some(index) = remaining.find(root) {
        result.push_str(&remaining[..index]);
        let suffix = &remaining[index + root.len()..];
        if suffix.is_empty()
            || suffix.starts_with('/')
            || suffix.starts_with(std::path::MAIN_SEPARATOR)
            || suffix.starts_with(if cfg!(windows) { ';' } else { ':' })
        {
            result.push_str("$REPOSITORY");
        } else {
            result.push_str(root);
        }
        remaining = suffix;
    }
    result.push_str(remaining);
    result
}

fn portable_value(value: &mut serde_json::Value, root: &str) {
    match value {
        serde_json::Value::String(text) => *text = portable_text(text, root),
        serde_json::Value::Array(values) => {
            for value in values {
                portable_value(value, root);
            }
        }
        serde_json::Value::Object(values) => {
            for value in values.values_mut() {
                portable_value(value, root);
            }
        }
        _ => {}
    }
}

/// Normalize repository-local paths in execution identity without changing other inputs.
#[must_use]
pub fn portable_fingerprint(values: &[String], root: &str) -> String {
    replay_fingerprint(values.iter().map(|text| {
        if let Ok(mut value) = serde_json::from_str::<serde_json::Value>(text) {
            portable_value(&mut value, root);
            value.to_string()
        } else {
            portable_text(text, root)
        }
    }))
}

#[cfg(test)]
mod tests;

/// Publish the finalized plan unchanged.
///
/// # Errors
/// Rejects invalid plans or failed output replacement.
pub fn publish_plan(source: &Path, output: &Path) -> Result<(), CoreError> {
    write_json(output, &read_plan(source)?)
}

/// Validate CI artifact destinations before launching pytest or publishing evidence.
///
/// # Errors
/// Rejects collisions with source files, input bundles, and local evidence stores.
pub fn validate_artifact_paths(
    data: &str,
    history: &str,
    baseline: Option<&str>,
    export: Option<&str>,
    plan: Option<&str>,
) -> Result<(), CoreError> {
    let mut paths = vec![
        absolute_path(Path::new(data)),
        absolute_path(Path::new(history)),
    ];
    paths.extend(
        [baseline, export, plan]
            .into_iter()
            .flatten()
            .map(|path| absolute_path(Path::new(path))),
    );
    let normalized: BTreeSet<_> = paths.iter().collect();
    if normalized.len() != paths.len() {
        return Err(invalid(
            "CI artifact paths must be distinct from coverage, history, and each other",
        ));
    }
    let directory = absolute_path(Path::new(history))
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| invalid("history has no parent directory"))?;
    for path in [baseline, export, plan].into_iter().flatten() {
        let path = absolute_path(Path::new(path));
        if ["replay.coverage", "test-run.json", "import-graph.json"]
            .iter()
            .any(|name| path == directory.join(name))
        {
            return Err(invalid("CI artifacts cannot replace the local replay data"));
        }
    }
    if let Ok(root) = repository_root() {
        let tracked = tracked_files(&root)?;
        for path in [Some(data), Some(history), export, plan]
            .into_iter()
            .flatten()
        {
            let absolute = absolute_path(Path::new(path));
            if absolute
                .strip_prefix(&root)
                .is_ok_and(|relative| tracked.contains(&glob_path(relative)))
            {
                return Err(invalid("CI outputs cannot replace tracked source files"));
            }
        }
    }
    Ok(())
}
