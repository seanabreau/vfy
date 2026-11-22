//! Filesystem watching, run invalidation, and recovery for tests.
//! Callers provide source roots, generated-output paths, and run outcomes.

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant, UNIX_EPOCH};

use notify::event::ModifyKind;
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use sha2::{Digest, Sha256};

mod publication;

const DEFAULT_DEBOUNCE_MS: u64 = 200;
const DEFAULT_IDLE_RECONCILE_MS: u64 = 5_000;
const GENERATED_DIRECTORY_NAMES: &[&str] = &[
    ".git",
    ".vfy",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    ".cache",
    ".venv",
    "venv",
    "__pycache__",
    "target",
];

/// Lifecycle state for one long-lived watch session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WatchState {
    Idle,
    Debouncing,
    Running,
    RecoveryRequired,
    Stopping,
}

impl WatchState {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Debouncing => "debouncing",
            Self::Running => "running",
            Self::RecoveryRequired => "recovery-required",
            Self::Stopping => "stopping",
        }
    }
}

/// A coalesced set of filesystem changes observed after the debounce window.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchBatch {
    pub generation: u64,
    pub paths: Vec<String>,
    pub full_run: bool,
    pub reasons: Vec<String>,
}

/// The requested execution scope, resolved before handing control to pytest.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchRun {
    pub input_identity: String,
    pub full_run: bool,
}

/// A completed pytest invocation, including threshold failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunOutcome {
    Failed,
    PassedSubset,
    PassedFull,
}

/// Whether a completion still describes the watched inputs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunCurrency {
    Current,
    Obsolete,
}

struct ActiveRun {
    generation: u64,
    identity: String,
    obsolete: bool,
}

#[derive(Default)]
struct PendingEventBatch {
    generation_start: Option<u64>,
    recovery_start: bool,
    observed_during_active: EventObservation,
    identity_check: bool,
    direct_file_modify: bool,
    directory_paths: BTreeSet<String>,
}

#[derive(Default, PartialEq, Eq)]
enum EventObservation {
    #[default]
    Idle,
    Active,
}

/// A watch session with at most one active run.
/// The notify callback sends events through a channel; filtering, coalescing,
/// and state transitions run on the caller's thread.
pub struct WatchSession {
    watcher: RecommendedWatcher,
    receiver: Receiver<notify::Result<Event>>,
    roots: Vec<PathBuf>,
    selection_root: Option<PathBuf>,
    ignored: Vec<PathBuf>,
    resolved_ignored: Vec<PathBuf>,
    debounce: Duration,
    generation: u64,
    pending: BTreeSet<String>,
    pending_full_run: bool,
    pending_reasons: BTreeSet<String>,
    pending_event_batch: PendingEventBatch,
    last_run_identity: Option<String>,
    last_metadata_identity: Option<String>,
    last_idle_reconcile: Instant,
    last_event: Option<Instant>,
    state: WatchState,
    failure: Option<String>,
    recovery_required: bool,
    active: Option<ActiveRun>,
}

impl WatchSession {
    /// Watch roots recursively and coalesce events until the trailing debounce expires.
    ///
    /// `None` uses the 200 ms default; `Some(0)` disables the debounce wait.
    ///
    /// # Errors
    ///
    /// Returns the backend error when a root cannot be registered.
    pub fn new(
        roots: &[PathBuf],
        ignored: &[PathBuf],
        debounce_ms: Option<u64>,
    ) -> notify::Result<Self> {
        let (sender, receiver) = mpsc::channel();
        let roots = normalized_roots(roots);
        let mut watcher = notify::recommended_watcher(move |event| {
            // A disconnected receiver means the command is stopping.
            let _ = sender.send(event);
        })?;
        for root in &roots {
            watcher.watch(root, RecursiveMode::Recursive)?;
        }
        Ok(Self {
            watcher,
            receiver,
            selection_root: roots
                .iter()
                .find(|root| root.join(".git").exists())
                .cloned(),
            roots,
            resolved_ignored: Vec::new(),
            ignored: ignored.iter().map(|path| normalize(path)).collect(),
            debounce: Duration::from_millis(debounce_ms.unwrap_or(DEFAULT_DEBOUNCE_MS)),
            generation: 0,
            pending: BTreeSet::new(),
            pending_full_run: false,
            pending_reasons: BTreeSet::new(),
            pending_event_batch: PendingEventBatch::default(),
            last_run_identity: None,
            last_metadata_identity: None,
            last_idle_reconcile: Instant::now(),
            last_event: None,
            state: WatchState::Idle,
            failure: None,
            recovery_required: false,
            active: None,
        })
    }

    #[must_use]
    pub const fn state(&self) -> WatchState {
        self.state
    }

    #[must_use]
    pub const fn generation(&self) -> u64 {
        self.generation
    }

    #[must_use]
    pub const fn requires_full_run(&self) -> bool {
        self.recovery_required
    }

    #[must_use]
    pub fn failure(&self) -> Option<&str> {
        self.failure.as_deref()
    }

    /// Drain events already delivered by notify and return the generation.
    pub fn observe(&mut self) -> u64 {
        self.drain_ready();
        self.generation
    }

    /// Capture inputs and reserve the sole active execution slot.
    ///
    /// # Errors
    /// Rejects overlapping runs, stopped watchers, and unreadable inputs.
    pub fn begin_run(&mut self) -> io::Result<WatchRun> {
        self.observe();
        if self.active.is_some() {
            return Err(io::Error::other("a watch run is already active"));
        }
        if self.state == WatchState::Stopping {
            return Err(io::Error::other(
                self.failure
                    .as_deref()
                    .unwrap_or("watch session has stopped"),
            ));
        }
        let generation = self.generation;
        let (identity, metadata_identity) =
            source_tree_identities(&self.roots, &self.ignored_paths())?;
        self.pending.clear();
        self.pending_reasons.clear();
        self.pending_full_run = false;
        self.pending_event_batch = PendingEventBatch::default();
        self.last_event = None;
        self.last_run_identity = Some(identity.clone());
        self.last_metadata_identity = Some(metadata_identity);
        self.last_idle_reconcile = Instant::now();
        self.active = Some(ActiveRun {
            generation,
            identity: identity.clone(),
            obsolete: false,
        });
        self.state = WatchState::Running;
        Ok(WatchRun {
            input_identity: identity,
            full_run: self.recovery_required,
        })
    }

    /// Return the active run's identity for staged results.
    ///
    /// # Errors
    /// Rejects access without an active run.
    pub fn active_run_identity(&self) -> io::Result<&str> {
        self.active
            .as_ref()
            .map(|run| run.identity.as_str())
            .ok_or_else(|| io::Error::other("no watch run is active"))
    }

    /// Reconcile the active execution with both content and delivered events.
    /// Once invalidated, a run cannot become current again after an edit/revert.
    ///
    /// # Errors
    /// Rejects checks without an active run and propagates unreadable inputs.
    pub fn is_current(&mut self) -> io::Result<bool> {
        let identity = self.input_identity();
        self.observe();
        let active = self
            .active
            .as_mut()
            .ok_or_else(|| io::Error::other("no watch run is active"))?;
        active.obsolete |= self.failure.is_some()
            || self.state == WatchState::Stopping
            || active.generation != self.generation
            || identity
                .as_ref()
                .map_or(true, |identity| identity != &active.identity);
        identity?;
        Ok(!active.obsolete)
    }

    /// Consume a completion. Only a current, passing full suite clears recovery.
    ///
    /// # Errors
    /// Rejects duplicate completions and propagates unreadable inputs while
    /// leaving recovery required.
    pub fn finish_run(&mut self, outcome: RunOutcome) -> io::Result<RunCurrency> {
        let current = self.is_current();
        let currency = if current.as_ref().is_ok_and(|current| *current) {
            RunCurrency::Current
        } else {
            RunCurrency::Obsolete
        };
        let result = self.complete(outcome, currency);
        current?;
        result
    }

    fn complete(&mut self, outcome: RunOutcome, currency: RunCurrency) -> io::Result<RunCurrency> {
        let Some(active) = self.active.take() else {
            return Err(io::Error::other("no watch run is active"));
        };
        if currency == RunCurrency::Current {
            self.last_run_identity = Some(active.identity);
        }
        let obsolete = currency == RunCurrency::Obsolete;
        if obsolete && self.pending.is_empty() && !self.pending_full_run {
            self.pending_full_run = true;
            self.last_event = Some(Instant::now());
        }
        if obsolete || outcome == RunOutcome::Failed {
            self.recovery_required = true;
        } else if outcome == RunOutcome::PassedFull {
            self.recovery_required = false;
        }
        if self.state != WatchState::Stopping {
            self.state = if self.recovery_required {
                WatchState::RecoveryRequired
            } else {
                WatchState::Idle
            };
        }
        Ok(currency)
    }

    pub fn stop(&mut self) {
        self.state = WatchState::Stopping;
        self.active = None;
    }

    /// Replace configured watch exclusions while retaining pytest-resolved
    /// output paths until the next child reports its destinations.
    pub fn set_ignored(&mut self, paths: &[PathBuf]) {
        self.ignored = normalized_paths(paths);
    }

    /// Replace pytest-resolved output exclusions for this run. Destinations
    /// removed from the set can be watched again when no other exclusion applies.
    pub fn set_resolved_ignored(&mut self, paths: &[PathBuf]) {
        self.resolved_ignored = normalized_paths(paths);
    }

    /// Replace source roots after reloading configuration.
    ///
    /// # Errors
    /// Registration failures stop the session: incomplete watching cannot
    /// establish whether a run's results are current.
    pub fn set_roots(&mut self, roots: &[PathBuf]) -> notify::Result<()> {
        let roots = normalized_roots(roots);
        // Overlapping inotify registrations share descriptors. Remove old roots
        // before adding replacements so unwatch cannot remove their descriptors.
        for root in &self.roots {
            if !roots.contains(root)
                && let Err(error) = self.watcher.unwatch(root)
                && !already_unwatched(&error)
            {
                self.failure = Some(format!(
                    "failed to stop watching {}: {error}",
                    root.display()
                ));
                self.state = WatchState::Stopping;
                return Err(error);
            }
        }
        for root in &roots {
            if !self.roots.contains(root)
                && let Err(error) = self.watcher.watch(root, RecursiveMode::Recursive)
            {
                self.failure = Some(format!("failed to watch {}: {error}", root.display()));
                self.state = WatchState::Stopping;
                return Err(error);
            }
        }
        self.selection_root = roots
            .iter()
            .find(|root| root.join(".git").exists())
            .cloned();
        self.roots = roots;
        Ok(())
    }

    /// Hash the current watched inputs, excluding the same generated paths as
    /// event filtering. Content checks detect writes whose backend events have
    /// not yet arrived.
    ///
    /// # Errors
    /// Returns an IO error if any input cannot be read completely.
    pub fn input_identity(&self) -> std::io::Result<String> {
        source_tree_identity(&self.roots, &self.ignored_paths())
    }

    /// Hash watched path names, file sizes, and modification times without
    /// reading file contents.
    ///
    /// This is public so the repository benchmark can measure the exact idle
    /// reconciliation scan. Product callers should use [`Self::wait_for_batch`].
    ///
    /// # Errors
    /// Returns the first filesystem error encountered while reading metadata.
    #[doc(hidden)]
    pub fn input_metadata_identity(&self) -> std::io::Result<String> {
        source_tree_metadata_identity(&self.roots, &self.ignored_paths())
    }

    fn ignored_paths(&self) -> Vec<PathBuf> {
        self.ignored
            .iter()
            .chain(&self.resolved_ignored)
            .cloned()
            .collect()
    }

    /// Wait at most `timeout_ms`, including trailing debounce, so the lifecycle
    /// adapter can handle interrupts even under continuous filesystem activity.
    pub fn wait_for_batch(&mut self, timeout_ms: u64) -> Option<WatchBatch> {
        let deadline = Instant::now() + Duration::from_millis(timeout_ms);
        loop {
            self.drain_ready();
            if self.active.is_none() && self.pending_event_batch.identity_check {
                match self.input_identity() {
                    Ok(identity) => self.reconcile_pending_identity(&identity, Instant::now()),
                    Err(error) => {
                        self.failure = Some(format!("failed to reconcile watched inputs: {error}"));
                        self.state = WatchState::Stopping;
                    }
                }
            }
            if self.active.is_none()
                && let Err(error) = self.reconcile_idle_inputs(Instant::now())
            {
                self.failure = Some(format!("failed to reconcile watched inputs: {error}"));
                self.state = WatchState::Stopping;
            }
            if self.state == WatchState::Stopping {
                return None;
            }
            let now = Instant::now();
            if let Some(batch) = self.take_batch(now) {
                return Some(batch);
            }
            if now >= deadline {
                return None;
            }
            let remaining = self.last_event.map_or(deadline - now, |last| {
                self.debounce
                    .saturating_sub(now.saturating_duration_since(last))
                    .min(deadline - now)
            });
            match self.receiver.recv_timeout(remaining) {
                Ok(event) => self.accept(event),
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => {
                    self.failure = Some("watcher event channel disconnected".to_owned());
                    self.state = WatchState::Stopping;
                    return None;
                }
            }
        }
    }

    fn take_batch(&mut self, now: Instant) -> Option<WatchBatch> {
        if self.state == WatchState::Running || self.state == WatchState::Stopping {
            return None;
        }
        let last = self.last_event?;
        if now.saturating_duration_since(last) < self.debounce {
            self.state = WatchState::Debouncing;
            return None;
        }
        self.last_event = None;
        self.pending_event_batch = PendingEventBatch::default();
        let paths = std::mem::take(&mut self.pending).into_iter().collect();
        let full_run = std::mem::take(&mut self.pending_full_run);
        self.recovery_required |= full_run;
        self.state = if self.recovery_required {
            WatchState::RecoveryRequired
        } else {
            WatchState::Idle
        };
        Some(WatchBatch {
            generation: self.generation,
            paths,
            full_run,
            reasons: std::mem::take(&mut self.pending_reasons)
                .into_iter()
                .collect(),
        })
    }

    fn reconcile_pending_identity(&mut self, identity: &str, now: Instant) {
        let batch = std::mem::take(&mut self.pending_event_batch);
        if !batch.identity_check {
            self.pending_event_batch = batch;
            return;
        }
        let Some(baseline) = self.last_run_identity.as_deref() else {
            self.pending_event_batch = batch;
            return;
        };

        if identity == baseline
            && !batch.direct_file_modify
            && !batch.recovery_start
            && batch.observed_during_active == EventObservation::Idle
        {
            // Backends can deliver initial-scan notifications after a run has
            // covered those contents. Discard this unchanged idle batch.
            self.pending.clear();
            self.pending_full_run = false;
            self.pending_reasons.clear();
            self.recovery_required = false;
            self.last_event = None;
            self.state = WatchState::Idle;
            return;
        }

        if identity != baseline && !batch.directory_paths.is_empty() {
            self.pending.extend(batch.directory_paths.iter().cloned());
            self.pending_full_run = true;
            self.pending_reasons
                .insert("directory changes require a full run".to_owned());
            self.recovery_required = true;
            if self.generation == batch.generation_start.unwrap_or(self.generation) {
                self.generation = self.generation.saturating_add(1);
            }
            self.last_event.get_or_insert(now);
        }
    }

    fn reconcile_idle_inputs(&mut self, now: Instant) -> io::Result<()> {
        if now.saturating_duration_since(self.last_idle_reconcile)
            < Duration::from_millis(DEFAULT_IDLE_RECONCILE_MS)
        {
            return Ok(());
        }
        self.last_idle_reconcile = now;

        let metadata_identity = self.input_metadata_identity()?;
        if self.last_metadata_identity.as_deref() == Some(&metadata_identity) {
            return Ok(());
        }
        self.last_metadata_identity = Some(metadata_identity);

        if self.pending_event_batch.identity_check
            || self.last_event.is_some()
            || !self.pending.is_empty()
            || self.pending_full_run
        {
            return Ok(());
        }
        let Some(baseline) = self.last_run_identity.as_deref() else {
            return Ok(());
        };
        if self.input_identity()? == baseline {
            return Ok(());
        }

        self.generation = self.generation.saturating_add(1);
        self.pending_full_run = true;
        self.pending_reasons
            .insert("watched inputs changed without a filesystem event".to_owned());
        self.recovery_required = true;
        self.last_event = Some(now);
        Ok(())
    }

    fn drain_ready(&mut self) {
        loop {
            match self.receiver.try_recv() {
                Ok(event) => self.accept(event),
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.failure = Some("watcher event channel disconnected".to_owned());
                    self.state = WatchState::Stopping;
                    break;
                }
            }
        }
    }

    fn accept(&mut self, result: notify::Result<Event>) {
        self.accept_at(result, Instant::now());
    }

    fn accept_at(&mut self, result: notify::Result<Event>, now: Instant) {
        let event = match result {
            Ok(event) => event,
            Err(error) => {
                self.generation = self.generation.saturating_add(1);
                self.recovery_required = true;
                self.failure = Some(format!("filesystem watcher failed: {error}"));
                self.state = WatchState::Stopping;
                return;
            }
        };
        if matches!(event.kind, EventKind::Access(_)) && !event.need_rescan() {
            return;
        }
        let uncertain = event.need_rescan() || event.paths.is_empty();
        let create_event = matches!(event.kind, EventKind::Create(_));
        let conservative = uncertain
            || create_event
            || matches!(event.kind, EventKind::Remove(_))
            || matches!(
                event.kind,
                EventKind::Modify(ModifyKind::Name(_) | ModifyKind::Any | ModifyKind::Other)
            )
            || matches!(event.kind, EventKind::Other | EventKind::Any);
        let reason = match event.kind {
            EventKind::Create(_) => "created inputs require a full run",
            EventKind::Remove(_) => "deleted inputs require a full run",
            EventKind::Modify(ModifyKind::Name(_)) => "renamed inputs require a full run",
            _ => "uncertain filesystem changes require a full run",
        };
        let mut accepted = uncertain;
        for path in event.paths {
            let path = normalize(&path);
            let is_directory = path.is_dir();
            if self.is_ignored(&path) {
                continue;
            }
            if create_event || is_directory {
                self.note_pending_event();
                self.pending_event_batch.identity_check = true;
            }
            if is_directory {
                self.pending_event_batch
                    .directory_paths
                    .insert(path.to_string_lossy().into_owned());
            } else if matches!(event.kind, EventKind::Modify(_)) {
                self.note_pending_event();
                self.pending_event_batch.direct_file_modify = true;
            }
            if !conservative && is_directory {
                continue;
            }
            self.note_pending_event();
            accepted = true;
            if self
                .selection_root
                .as_ref()
                .is_some_and(|root| !path.starts_with(root))
            {
                self.pending_full_run = true;
                self.pending_reasons
                    .insert("inputs outside the Git worktree changed".to_owned());
            }
            if conservative {
                self.pending_reasons.insert(reason.to_owned());
            } else if path.extension().is_none_or(|ext| ext != "py") {
                self.pending_reasons
                    .insert("configuration or runtime inputs changed".to_owned());
            }
            // Only edits to Python files can use mapped test selection. Runtime
            // inputs and configuration require the complete requested scope.
            self.pending_full_run |= conservative || path.extension().is_none_or(|ext| ext != "py");
            self.pending.insert(path.to_string_lossy().into_owned());
        }
        if accepted {
            self.generation = self.generation.saturating_add(1);
            self.last_event = Some(now);
            self.pending_full_run |= uncertain;
            self.recovery_required |= self.pending_full_run;
        }
    }

    fn note_pending_event(&mut self) {
        let batch = &mut self.pending_event_batch;
        if self.active.is_some() {
            batch.observed_during_active = EventObservation::Active;
        }
        if batch.generation_start.is_none() {
            batch.generation_start = Some(self.generation);
            batch.recovery_start = self.recovery_required;
        }
    }

    fn is_ignored(&self, path: &Path) -> bool {
        is_ignored(path, &self.ignored, &self.roots)
            || is_ignored(path, &self.resolved_ignored, &self.roots)
    }
}

/// Hash the complete contents and identities of a set of source roots.
///
/// # Errors
/// Returns the first filesystem error encountered while reading an input.
pub fn source_tree_identity(roots: &[PathBuf], ignored: &[PathBuf]) -> std::io::Result<String> {
    let digests = tree_identity_digests(roots, ignored, TreeIdentityMode::Content)?;
    digest_hex(digests.content)
}

fn already_unwatched(error: &notify::Error) -> bool {
    if matches!(error.kind, notify::ErrorKind::WatchNotFound) {
        return true;
    }
    #[cfg(target_os = "linux")]
    if let notify::ErrorKind::Io(io_error) = &error.kind
        && io_error.raw_os_error() == Some(22)
        && let [path] = error.paths.as_slice()
    {
        // Linux removes inotify descriptors when their directories disappear.
        // notify can unwatch before processing that deletion and receive EINVAL.
        return std::fs::symlink_metadata(path)
            .is_err_and(|error| error.kind() == io::ErrorKind::NotFound);
    }
    false
}

fn normalized_roots(paths: &[PathBuf]) -> Vec<PathBuf> {
    let roots: BTreeSet<_> = paths.iter().map(|path| normalize(path)).collect();
    roots
        .iter()
        .filter(|path| {
            !path
                .ancestors()
                .skip(1)
                .any(|parent| roots.contains(parent))
        })
        .cloned()
        .collect()
}

fn normalized_paths(paths: &[PathBuf]) -> Vec<PathBuf> {
    paths
        .iter()
        .map(|path| normalize(path))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn normalize(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map_or_else(|_| path.to_path_buf(), |directory| directory.join(path))
    };
    let mut probe = absolute;
    let mut suffix = Vec::new();
    while !probe.exists() {
        let Some(name) = probe.file_name().map(ToOwned::to_owned) else {
            break;
        };
        suffix.push(name);
        if !probe.pop() {
            break;
        }
    }
    let mut normalized = std::fs::canonicalize(&probe).unwrap_or(probe);
    for component in suffix.iter().rev() {
        normalized.push(component);
    }
    normalized
}

fn hash_field(digest: &mut Sha256, bytes: &[u8]) {
    digest.update((bytes.len() as u64).to_le_bytes());
    digest.update(bytes);
}

#[derive(Clone, Copy)]
enum TreeIdentityMode {
    Content,
    Metadata,
    Both,
}

struct TreeIdentityDigests {
    content: Option<Sha256>,
    metadata: Option<Sha256>,
}

impl TreeIdentityDigests {
    fn new(mode: TreeIdentityMode) -> Self {
        Self {
            content: matches!(mode, TreeIdentityMode::Content | TreeIdentityMode::Both)
                .then(Sha256::new),
            metadata: matches!(mode, TreeIdentityMode::Metadata | TreeIdentityMode::Both)
                .then(Sha256::new),
        }
    }
}

fn source_tree_metadata_identity(
    roots: &[PathBuf],
    ignored: &[PathBuf],
) -> std::io::Result<String> {
    let digests = tree_identity_digests(roots, ignored, TreeIdentityMode::Metadata)?;
    digest_hex(digests.metadata)
}

fn source_tree_identities(
    roots: &[PathBuf],
    ignored: &[PathBuf],
) -> std::io::Result<(String, String)> {
    let digests = tree_identity_digests(roots, ignored, TreeIdentityMode::Both)?;
    Ok((digest_hex(digests.content)?, digest_hex(digests.metadata)?))
}

fn tree_identity_digests(
    roots: &[PathBuf],
    ignored: &[PathBuf],
    mode: TreeIdentityMode,
) -> std::io::Result<TreeIdentityDigests> {
    let mut digests = TreeIdentityDigests::new(mode);
    for root in normalized_roots(roots) {
        hash_tree(&root, &root, ignored, &mut digests)?;
    }
    Ok(digests)
}

fn digest_hex(digest: Option<Sha256>) -> std::io::Result<String> {
    let Some(digest) = digest else {
        return Err(io::Error::other(
            "requested source-tree digest was not initialized",
        ));
    };
    let mut output = String::with_capacity(64);
    for byte in digest.finalize() {
        let _ = write!(output, "{byte:02x}");
    }
    Ok(output)
}

fn hash_metadata(digest: &mut Sha256, metadata: &std::fs::Metadata) {
    digest.update(metadata.len().to_le_bytes());
    let Ok(modified) = metadata.modified() else {
        digest.update([2_u8]);
        return;
    };
    let (sign, duration) = match modified.duration_since(UNIX_EPOCH) {
        Ok(duration) => (1_u8, duration),
        Err(error) => (0_u8, error.duration()),
    };
    digest.update([sign]);
    digest.update(duration.as_secs().to_le_bytes());
    digest.update(duration.subsec_nanos().to_le_bytes());
}

fn hash_tree(
    root: &Path,
    path: &Path,
    ignored: &[PathBuf],
    digests: &mut TreeIdentityDigests,
) -> std::io::Result<()> {
    if ignored_below(path, ignored, Some(root)) {
        return Ok(());
    }
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            if let Some(digest) = &mut digests.content {
                digest.update(b"missing");
                hash_field(digest, path.as_os_str().as_encoded_bytes());
            }
            if let Some(digest) = &mut digests.metadata {
                digest.update(b"missing");
                hash_field(digest, path.as_os_str().as_encoded_bytes());
            }
            return Ok(());
        }
        Err(error) => return Err(error),
    };
    if metadata.file_type().is_symlink() {
        let target = std::fs::read_link(path)?;
        if let Some(digest) = &mut digests.content {
            digest.update(b"symlink");
            hash_field(digest, path.as_os_str().as_encoded_bytes());
            hash_field(digest, target.as_os_str().as_encoded_bytes());
        }
        if let Some(digest) = &mut digests.metadata {
            digest.update(b"symlink");
            hash_field(digest, path.as_os_str().as_encoded_bytes());
            hash_metadata(digest, &metadata);
            hash_field(digest, target.as_os_str().as_encoded_bytes());
        }
        if path.is_file() {
            if let Some(digest) = &mut digests.content {
                hash_field(digest, &std::fs::read(path)?);
            }
            if let Some(digest) = &mut digests.metadata {
                hash_metadata(digest, &std::fs::metadata(path)?);
            }
        }
    } else if metadata.is_dir() {
        if let Some(digest) = &mut digests.content {
            digest.update(b"directory");
            hash_field(digest, path.as_os_str().as_encoded_bytes());
        }
        if let Some(digest) = &mut digests.metadata {
            digest.update(b"directory");
            hash_field(digest, path.as_os_str().as_encoded_bytes());
        }
        let mut entries = std::fs::read_dir(path)?
            .map(|entry| entry.map(|entry| entry.path()))
            .collect::<std::io::Result<Vec<_>>>()?;
        entries.sort();
        for entry in entries {
            hash_tree(root, &entry, ignored, digests)?;
        }
    } else if metadata.is_file() {
        if let Some(digest) = &mut digests.content {
            digest.update(b"file");
            hash_field(digest, path.as_os_str().as_encoded_bytes());
            hash_field(digest, &std::fs::read(path)?);
        }
        if let Some(digest) = &mut digests.metadata {
            digest.update(b"file");
            hash_field(digest, path.as_os_str().as_encoded_bytes());
            hash_metadata(digest, &metadata);
        }
    }
    Ok(())
}

/// Shared generated-input policy for watching and replay.
#[must_use]
pub fn generated_path(path: &Path) -> bool {
    path.components().any(|component| {
        let name = component.as_os_str().to_string_lossy();
        GENERATED_DIRECTORY_NAMES.contains(&name.as_ref())
            || name.starts_with("pytest-cache-files-")
            || (name == ".coverage" || name.starts_with(".coverage."))
            || name.starts_with("..coverage.")
            || name.ends_with(",cover")
            || name.ends_with(".vfy-watch")
    })
}

/// Judge generated directory names below the watched root only, so a checkout
/// that lives under a directory such as `target` or `.cache` stays watched.
fn is_ignored(path: &Path, ignored: &[PathBuf], roots: &[PathBuf]) -> bool {
    let root = roots.iter().find(|root| path.starts_with(root));
    ignored_below(path, ignored, root.map(PathBuf::as_path))
}

fn ignored_below(path: &Path, ignored: &[PathBuf], root: Option<&Path>) -> bool {
    let relative = root.and_then(|root| path.strip_prefix(root).ok());
    if generated_path(relative.unwrap_or(path)) {
        return true;
    }
    ignored.iter().any(|ignored| {
        if path == ignored || path.strip_prefix(ignored).is_ok() {
            return true;
        }
        let Some(prefix) = ignored.file_name().and_then(|name| name.to_str()) else {
            return false;
        };
        if !prefix.ends_with('.') {
            return false;
        }
        path.parent() == ignored.parent()
            && path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with(prefix))
    })
}

#[cfg(test)]
mod tests;
