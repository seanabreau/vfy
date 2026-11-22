//! Thread-affine state behind Python objects that any thread may release.
//!
//! A Python garbage collection can release an object on whichever thread runs
//! it, such as an event loop or server thread that a test started. pytest's
//! objects tolerate that. A value here stays in its creating thread's
//! registry: that thread reads and releases it, and a release on another
//! thread leaves the value to the creating thread's registry.

use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::*;
use std::any::Any;
use std::cell::RefCell;
use std::collections::HashMap;
use std::marker::PhantomData;
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread::ThreadId;

thread_local! {
    static VALUES: RefCell<HashMap<u64, Rc<dyn Any>>> = RefCell::new(HashMap::new());
}

static NEXT: AtomicU64 = AtomicU64::new(0);

pub(crate) struct ThreadBound<T: 'static> {
    thread: ThreadId,
    key: u64,
    value: PhantomData<fn() -> T>,
}

impl<T: 'static> ThreadBound<T> {
    pub(crate) fn new(value: T) -> Self {
        let key = NEXT.fetch_add(1, Ordering::Relaxed);
        VALUES.with(|values| {
            values
                .borrow_mut()
                .insert(key, Rc::new(value) as Rc<dyn Any>);
        });
        Self {
            thread: std::thread::current().id(),
            key,
            value: PhantomData,
        }
    }

    /// The value, on the thread that created it.
    pub(crate) fn get(&self) -> PyResult<Rc<T>> {
        if std::thread::current().id() != self.thread {
            return Err(PyRuntimeError::new_err(
                "this test object can be used only on the thread that runs its test",
            ));
        }
        VALUES
            .with(|values| values.borrow().get(&self.key).cloned())
            .and_then(|value| value.downcast::<T>().ok())
            .ok_or_else(|| PyAssertionError::new_err("this test object has been released"))
    }
}

impl<T: 'static> Drop for ThreadBound<T> {
    fn drop(&mut self) {
        if std::thread::current().id() == self.thread {
            let removed = VALUES
                .try_with(|values| values.borrow_mut().remove(&self.key))
                .ok()
                .flatten();
            drop(removed);
        }
    }
}
