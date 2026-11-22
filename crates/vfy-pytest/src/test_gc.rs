//! Synchronize explicit collection and weakref assertions in the shared interpreter.

use std::sync::{Mutex, MutexGuard};

static GC_ASSERTION_LOCK: Mutex<()> = Mutex::new(());

/// Acquire before initializing or attaching to Python so a waiter cannot hold the GIL.
pub(crate) fn lock() -> MutexGuard<'static, ()> {
    // The guard stores no state. Recovery keeps later assertions active after a failure.
    GC_ASSERTION_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
