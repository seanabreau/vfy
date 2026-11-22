//! Registered plugin and blocked-name storage in registration order.

use pyo3::prelude::*;
use std::path::PathBuf;

pub(super) struct Plugin {
    pub(super) sequence: u64,
    pub(super) name: String,
    pub(super) object: Option<Py<PyAny>>,
    /// Directory of a registered `conftest.py`, read once at registration.
    pub(super) conftest_directory: Option<PathBuf>,
}

#[derive(Default)]
pub(super) struct PluginStore {
    ordinary: Vec<Plugin>,
    pub(super) conftests: Vec<Plugin>,
    next_sequence: u64,
}

impl PluginStore {
    pub(super) fn insert(
        &mut self,
        name: String,
        object: Option<Py<PyAny>>,
        directory: Option<PathBuf>,
    ) -> PyResult<()> {
        let sequence = self.next_sequence;
        self.next_sequence = sequence.checked_add(1).ok_or_else(|| {
            pyo3::exceptions::PyOverflowError::new_err("plugin registration order exceeds u64")
        })?;
        let plugin = Plugin {
            sequence,
            name,
            object,
            conftest_directory: directory,
        };
        if plugin.conftest_directory.is_some() {
            self.conftests.push(plugin);
        } else {
            self.ordinary.push(plugin);
        }
        Ok(())
    }

    pub(super) fn iter(&self) -> impl Iterator<Item = &Plugin> {
        let mut ordinary = self.ordinary.iter().peekable();
        let mut conftests = self.conftests.iter().peekable();
        std::iter::from_fn(move || match (ordinary.peek(), conftests.peek()) {
            (Some(left), Some(right)) if left.sequence < right.sequence => ordinary.next(),
            (_, Some(_)) => conftests.next(),
            (Some(_), None) => ordinary.next(),
            (None, None) => None,
        })
    }

    pub(super) fn remove(&mut self, name: &str, blocked: bool) -> Option<Plugin> {
        for plugins in [&mut self.ordinary, &mut self.conftests] {
            if let Some(index) = plugins
                .iter()
                .position(|entry| entry.name == name && entry.object.is_none() == blocked)
            {
                return Some(plugins.remove(index));
            }
        }
        None
    }
}

impl Drop for PluginStore {
    fn drop(&mut self) {
        let mut ordinary = std::mem::take(&mut self.ordinary).into_iter().peekable();
        let mut conftests = std::mem::take(&mut self.conftests).into_iter().peekable();
        loop {
            let next = match (ordinary.peek(), conftests.peek()) {
                (Some(left), Some(right)) if left.sequence < right.sequence => ordinary.next(),
                (_, Some(_)) => conftests.next(),
                (Some(_), None) => ordinary.next(),
                (None, None) => None,
            };
            let Some(plugin) = next else { break };
            drop(plugin);
        }
    }
}
