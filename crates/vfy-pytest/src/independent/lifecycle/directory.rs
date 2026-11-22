//! The invocation directory is restored once before session shutdown hooks.

use super::{Context, state::LifecycleStage};
use pyo3::prelude::*;

#[derive(Clone, Copy, Default, Eq, PartialEq)]
pub(in crate::independent) enum DirectoryRestore {
    #[default]
    NotEntered,
    Pending,
    Attempted,
}

impl Context {
    pub(super) fn enter_session_directory(&self) {
        if self.invocation.directory.get() == DirectoryRestore::NotEntered {
            self.invocation.directory.set(DirectoryRestore::Pending);
        }
    }

    pub(super) fn restore_session_directory(&self, py: Python<'_>) -> PyResult<()> {
        if self.invocation.directory.get() != DirectoryRestore::Pending {
            return Ok(());
        }
        // Mark ownership before descriptors/audit callbacks can reenter cleanup.
        // A failed chdir is not retried, and hook-selected directories survive.
        self.invocation.directory.set(DirectoryRestore::Attempted);
        self.stage.set(LifecycleStage::Finishing);
        let startpath = self.session.bind(py).getattr("startpath")?;
        py.import("os")?.call_method1("chdir", (startpath,))?;
        Ok(())
    }
}
