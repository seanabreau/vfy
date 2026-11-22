//! Bounded case leases shared by native and independent process workers.

use super::{NativeParallelWorker, POLL_INTERVAL, io_error};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rustix::fs::{FlockOperation, flock};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::ops::Range;
use std::path::Path;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

const WORD_BYTES: u64 = 8;
const UNCLAIMED: u64 = u64::MAX;
const QUEUE_FILE: &str = "demand-claims";

pub(crate) struct DemandQueue {
    groups: Vec<Range<usize>>,
    workers: usize,
    claimed: AtomicUsize,
    startup: Option<StartupGrace>,
    initial: Mutex<Option<Range<usize>>>,
    owned: Mutex<Vec<Range<usize>>>,
}

struct StartupGrace {
    limit_ms: u64,
    origin: Instant,
}

enum DemandClaim {
    Lease(Range<usize>),
    Waiting(Duration),
    Finished,
}

/// The lock is released when the returned descriptor is dropped, including on every error path.
fn lock_ledger(directory: &Path) -> PyResult<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(directory.join(QUEUE_FILE))
        .map_err(io_error)?;
    flock(&file, FlockOperation::LockExclusive).map_err(io_error)?;
    Ok(file)
}

impl DemandQueue {
    pub(super) fn initialize(
        directory: &Path,
        groups: Vec<(usize, usize)>,
        collected: usize,
        workers: usize,
    ) -> PyResult<Self> {
        let limit_ms = startup_grace_ms()?;
        let startup = (limit_ms > 0).then(|| StartupGrace {
            limit_ms,
            origin: Instant::now(),
        });
        let mut end = 0;
        let mut ranges = Vec::with_capacity(groups.len());
        for (start, next) in groups {
            if start != end || next <= start || next > collected {
                return Err(PyValueError::new_err("invalid native demand scope groups"));
            }
            ranges.push(start..next);
            end = next;
        }
        if end != collected {
            return Err(PyValueError::new_err(
                "native demand scope groups omit collected tests",
            ));
        }
        let header_words = if startup.is_some() { 3 } else { 1 };
        let mut bytes = vec![0u8; header_words * size_of::<u64>()];
        bytes.reserve(ranges.len() * size_of::<u64>());
        for _ in &ranges {
            bytes.extend_from_slice(&UNCLAIMED.to_le_bytes());
        }
        vfy_core::atomic_write(&directory.join(QUEUE_FILE), &bytes).map_err(io_error)?;
        Ok(Self {
            groups: ranges,
            workers,
            claimed: AtomicUsize::new(0),
            startup,
            initial: Mutex::new(None),
            owned: Mutex::new(Vec::new()),
        })
    }

    fn independent_groups(collected: usize, workers: usize) -> Vec<(usize, usize)> {
        let mut groups = Vec::new();
        let mut start = 0;
        while start < collected {
            // Shrink leases as the queue drains so the remaining cases spread
            // across the active workers. Each worker also reserves one
            // lookahead lease for nextitem semantics.
            let batch = ((collected - start) / workers.max(1).saturating_mul(2)).clamp(1, 4);
            let end = (start + batch).min(collected);
            groups.push((start, end));
            start = end;
        }
        groups
    }

    pub(super) fn initialize_independent(
        directory: &Path,
        collected: usize,
        workers: usize,
    ) -> PyResult<Self> {
        let groups = Self::independent_groups(collected, workers);
        if startup_grace_ms()? != 0 {
            return Err(PyValueError::new_err(
                "native startup grace applies only to native collected fork workers",
            ));
        }
        let mut queue = Self::initialize(directory, groups, collected, workers)?;
        // Fresh workers have no shared Instant; startup grace remains fork-only.
        queue.startup = None;
        let seeded = queue.groups.len().min(workers);
        let mut bytes = (seeded as u64).to_le_bytes().to_vec();
        for index in 0..queue.groups.len() {
            bytes.extend_from_slice(
                &(if index < seeded {
                    index as u64
                } else {
                    UNCLAIMED
                })
                .to_le_bytes(),
            );
        }
        vfy_core::atomic_write(&directory.join(QUEUE_FILE), &bytes).map_err(io_error)?;
        Ok(queue)
    }

    pub(super) fn open_independent(
        directory: &Path,
        collected: usize,
        workers: usize,
        worker: usize,
    ) -> PyResult<Self> {
        if worker >= workers {
            return Err(PyValueError::new_err(
                "native demand worker is out of range",
            ));
        }
        let groups = Self::independent_groups(collected, workers)
            .into_iter()
            .map(|(start, end)| start..end)
            .collect::<Vec<_>>();
        let initial = groups.get(worker).cloned();
        let owned = initial.iter().cloned().collect::<Vec<_>>();
        let queue = Self {
            groups,
            workers,
            claimed: AtomicUsize::new(initial.as_ref().map_or(0, ExactSizeIterator::len)),
            startup: None,
            initial: Mutex::new(initial),
            owned: Mutex::new(owned.clone()),
        };
        let assignments = queue.assignments(directory)?;
        if assignments.get(worker) != Some(&owned)
            || assignments.iter().enumerate().any(|(index, ranges)| {
                queue
                    .groups
                    .get(index)
                    .is_some_and(|seed| ranges.first() != Some(seed))
            })
        {
            return Err(PyValueError::new_err(
                "native demand seed ownership disagrees with its plan",
            ));
        }
        Ok(queue)
    }

    pub(crate) fn owned_indices(&self) -> PyResult<Vec<usize>> {
        Ok(self
            .owned
            .lock()
            .map_err(|error| PyValueError::new_err(error.to_string()))?
            .iter()
            .cloned()
            .flatten()
            .collect())
    }

    pub(crate) fn owns(&self, index: usize) -> PyResult<bool> {
        let owned = self
            .owned
            .lock()
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let candidate = owned.partition_point(|range| range.end <= index);
        Ok(owned
            .get(candidate)
            .is_some_and(|range| range.contains(&index)))
    }

    pub(super) fn claimed_count(&self) -> usize {
        self.claimed.load(Ordering::Relaxed)
    }

    fn header_words(&self) -> u64 {
        if self.startup.is_some() { 3 } else { 1 }
    }

    fn claim(&self, directory: &Path, worker: usize) -> PyResult<DemandClaim> {
        let mut file = lock_ledger(directory)?;
        let chunks = self.groups.len() as u64;
        let header_words = self.header_words();
        if worker >= self.workers
            || file.metadata().map_err(io_error)?.len() != WORD_BYTES * (chunks + header_words)
        {
            return Err(PyValueError::new_err("invalid native demand claim queue"));
        }
        let next = read_word(&mut file).map_err(io_error)?;
        if next > chunks {
            return Err(PyValueError::new_err(
                "native demand claim cursor is out of range",
            ));
        }
        if next == chunks {
            return Ok(DemandClaim::Finished);
        }
        if let Some(startup) = &self.startup {
            let completed = read_word(&mut file).map_err(io_error)?;
            let start = read_word(&mut file).map_err(io_error)?;
            validate_startup_state(next, completed, start)?;
            let now = startup.elapsed_ms();
            if start > now.saturating_add(1) {
                return Err(PyValueError::new_err(
                    "native startup grace starts in the future",
                ));
            }
            if next > 0 && completed == 0 {
                let owner = read_word(&mut file).map_err(io_error)?;
                if owner >= self.workers as u64 {
                    return Err(PyValueError::new_err(
                        "native startup owner is out of range",
                    ));
                }
                let elapsed = now.saturating_sub(start - 1);
                if owner != worker as u64 && elapsed < startup.limit_ms {
                    return Ok(DemandClaim::Waiting(Duration::from_millis(
                        startup.limit_ms - elapsed,
                    )));
                }
            }
            if next == 0 {
                file.seek(SeekFrom::Start(WORD_BYTES * 2))
                    .map_err(io_error)?;
                file.write_all(&startup.elapsed_ms().saturating_add(1).to_le_bytes())
                    .map_err(io_error)?;
            }
        }
        let owner_offset = WORD_BYTES * (next + header_words);
        file.seek(SeekFrom::Start(owner_offset)).map_err(io_error)?;
        if read_word(&mut file).map_err(io_error)? != UNCLAIMED {
            return Err(PyValueError::new_err(
                "native demand chunk is already owned",
            ));
        }
        file.seek(SeekFrom::Start(owner_offset)).map_err(io_error)?;
        file.write_all(&(worker as u64).to_le_bytes())
            .map_err(io_error)?;
        file.seek(SeekFrom::Start(0)).map_err(io_error)?;
        file.write_all(&(next + 1).to_le_bytes())
            .map_err(io_error)?;
        file.flush().map_err(io_error)?;
        let next_index = usize::try_from(next)
            .map_err(|_| PyValueError::new_err("native demand claim cursor is out of range"))?;
        let range = self.groups[next_index].clone();
        self.claimed.fetch_add(range.len(), Ordering::Relaxed);
        self.owned
            .lock()
            .map_err(|error| PyValueError::new_err(error.to_string()))?
            .push(range.clone());
        Ok(DemandClaim::Lease(range))
    }

    fn complete_first(&self, directory: &Path, worker: usize) -> PyResult<()> {
        let mut file = lock_ledger(directory)?;
        if file.metadata().map_err(io_error)?.len() != WORD_BYTES * (self.groups.len() as u64 + 3) {
            return Err(PyValueError::new_err("invalid native startup claim queue"));
        }
        let claimed = read_word(&mut file).map_err(io_error)?;
        let completed = read_word(&mut file).map_err(io_error)?;
        let start = read_word(&mut file).map_err(io_error)?;
        validate_startup_state(claimed, completed, start)?;
        if claimed > self.groups.len() as u64 {
            return Err(PyValueError::new_err(
                "native demand claim cursor is out of range",
            ));
        }
        if claimed == 0 || read_word(&mut file).map_err(io_error)? != worker as u64 {
            return Err(PyValueError::new_err(
                "native first lease completion has a different owner",
            ));
        }
        file.seek(SeekFrom::Start(WORD_BYTES)).map_err(io_error)?;
        file.write_all(&1u64.to_le_bytes()).map_err(io_error)
    }
}

impl StartupGrace {
    fn elapsed_ms(&self) -> u64 {
        u64::try_from(self.origin.elapsed().as_millis()).unwrap_or(u64::MAX - 1)
    }
}

pub(super) fn startup_grace_ms() -> PyResult<u64> {
    match std::env::var("VFY_NATIVE_STARTUP_GRACE_MS") {
        Err(std::env::VarError::NotPresent) => Ok(0),
        Err(error) => Err(PyValueError::new_err(error.to_string())),
        Ok(value) => value
            .parse::<u64>()
            .ok()
            .filter(|value| *value <= 60_000)
            .ok_or_else(|| {
                PyValueError::new_err(
                    "native startup grace must be integer milliseconds from 0 to 60000",
                )
            }),
    }
}

fn validate_startup_state(claimed: u64, completed: u64, start: u64) -> PyResult<()> {
    if completed > 1
        || (claimed == 0 && (completed != 0 || start != 0))
        || (claimed > 0 && start == 0)
    {
        return Err(PyValueError::new_err("invalid native startup grace state"));
    }
    Ok(())
}

pub(crate) struct DemandCaseCursor<'a> {
    worker: &'a NativeParallelWorker,
    queue: &'a DemandQueue,
    range: Range<usize>,
    first_end: Option<usize>,
}

impl<'a> DemandCaseCursor<'a> {
    pub(super) fn new(worker: &'a NativeParallelWorker, queue: &'a DemandQueue) -> Self {
        Self {
            worker,
            queue,
            range: queue
                .initial
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take()
                .unwrap_or(0..0),
            first_end: None,
        }
    }

    pub(crate) fn next(&mut self, py: Python<'_>) -> PyResult<Option<usize>> {
        self.peek(py)?;
        Ok(self.range.next())
    }

    pub(crate) fn peek(&mut self, py: Python<'_>) -> PyResult<Option<usize>> {
        while self.range.is_empty() {
            if !self.worker.claim_test()? {
                return Ok(None);
            }
            match self
                .queue
                .claim(&self.worker.directory, self.worker.index)?
            {
                DemandClaim::Lease(range) => {
                    if range.start == 0 && self.queue.startup.is_some() {
                        self.first_end = Some(range.end);
                    }
                    self.range = range;
                }
                DemandClaim::Finished => return Ok(None),
                DemandClaim::Waiting(remaining) => {
                    py.check_signals()?;
                    // The claim has already dropped its descriptor and flock, so no lock is held while sleeping.
                    py.detach(|| std::thread::sleep(remaining.min(POLL_INTERVAL)));
                }
            }
        }
        Ok(Some(self.range.start))
    }

    pub(crate) fn case_completed(&mut self, index: usize) -> PyResult<()> {
        if self.first_end == Some(index + 1) {
            self.queue
                .complete_first(&self.worker.directory, self.worker.index)?;
            self.first_end = None;
        }
        Ok(())
    }
}

impl DemandQueue {
    pub(super) fn assignments(&self, directory: &Path) -> PyResult<Vec<Vec<Range<usize>>>> {
        let chunks = self.groups.len() as u64;
        let mut file = lock_ledger(directory)?;
        if file.metadata().map_err(io_error)?.len() != WORD_BYTES * (chunks + self.header_words()) {
            return Err(PyValueError::new_err(
                "invalid native demand claim queue size",
            ));
        }
        let claimed = read_word(&mut file).map_err(io_error)?;
        if claimed > chunks {
            return Err(PyValueError::new_err(
                "native demand claim cursor is out of range",
            ));
        }
        if self.startup.is_some() {
            let completed = read_word(&mut file).map_err(io_error)?;
            let start = read_word(&mut file).map_err(io_error)?;
            validate_startup_state(claimed, completed, start)?;
        }
        let mut assignments = vec![Vec::new(); self.workers];
        for (index, range) in self.groups.iter().enumerate() {
            let owner = read_word(&mut file).map_err(io_error)?;
            if index as u64 >= claimed {
                if owner != UNCLAIMED {
                    return Err(PyValueError::new_err(
                        "native demand queue has an uncommitted lease",
                    ));
                }
                continue;
            }
            let owner = usize::try_from(owner)
                .map_err(|_| PyValueError::new_err("native demand owner is out of range"))?;
            assignments
                .get_mut(owner)
                .ok_or_else(|| PyValueError::new_err("native demand owner is out of range"))?
                .push(range.clone());
        }
        Ok(assignments)
    }
}

fn read_word(file: &mut File) -> std::io::Result<u64> {
    let mut bytes = [0u8; 8];
    file.read_exact(&mut bytes)?;
    Ok(u64::from_le_bytes(bytes))
}

#[cfg(test)]
mod tests;
