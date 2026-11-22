//! Serialize rewritten-bytecode cache population across workers.

use pyo3::prelude::*;
use std::fs::File;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

static ACTIVE_POPULATION: AtomicBool = AtomicBool::new(false);

struct PopulationReservation;

impl Drop for PopulationReservation {
    fn drop(&mut self) {
        ACTIVE_POPULATION.store(false, Ordering::Release);
    }
}

struct Population {
    // Release the OS lock before allowing another thread to reserve a lease.
    _file: File,
    _reservation: PopulationReservation,
}

#[pyclass(module = "vfy._native._pytest", unsendable)]
pub(crate) struct NativeAssertionCacheLease {
    population: Option<Population>,
}

#[pymethods]
impl NativeAssertionCacheLease {
    #[new]
    fn new(py: Python<'_>, source: &str) -> Self {
        // A compiler audit hook can import another module, including one that
        // another thread is already importing. Never wait for a second lease
        // in this process while a callback can still own the first one.
        if ACTIVE_POPULATION
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            return Self { population: None };
        }
        let reservation = PopulationReservation;
        // Lock the source inode rather than the cache directory: unrelated
        // modules can be compiled by callbacks in other processes. Aliases of
        // the same source share the OS lock, without creating lock files.
        let file = py.detach(|| {
            let file = File::open(Path::new(source)).ok()?;
            file.lock().ok()?;
            Some(file)
        });
        let population = file.map(|file| Population {
            _file: file,
            _reservation: reservation,
        });
        Self { population }
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __exit__(
        &mut self,
        _exception_type: &Bound<'_, PyAny>,
        _exception: &Bound<'_, PyAny>,
        _traceback: &Bound<'_, PyAny>,
    ) {
        drop(self.population.take());
    }
}
