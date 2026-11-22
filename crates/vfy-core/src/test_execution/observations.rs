//! Measured frontend inputs and activation records carried with test results.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Configuration values actually consumed by a test frontend.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ConfigurationObservation {
    pub ini_path: Option<String>,
    pub ini_sha256: Option<String>,
    pub values: BTreeMap<String, Vec<String>>,
    pub strict_config: bool,
    pub strict_markers: bool,
}

/// Marker names read from a selected live test item.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct CaseMarkerObservation {
    pub nodeid: String,
    pub names: Vec<String>,
}

/// Registered plugin identity and hook implementation invocations.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct PluginObservation {
    #[serde(default, skip_serializing_if = "zero_generation")]
    pub generation: u64,
    pub name: String,
    pub module: String,
    pub origin: Option<String>,
    pub sha256: Option<String>,
    pub hooks: Vec<String>,
    pub hook_calls: BTreeMap<String, u64>,
}

#[expect(
    clippy::trivially_copy_pass_by_ref,
    reason = "Serde skip callbacks receive borrowed fields"
)]
fn zero_generation(generation: &u64) -> bool {
    *generation == 0
}

/// Requested concurrency and the processes that actually executed the suite.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ExecutionTopology {
    pub requested_workers: usize,
    pub active_workers: usize,
    pub worker_pids: Vec<u32>,
}

/// Optional result observations, omitted by frontends that do not record them.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct RunObservations {
    pub configuration: ConfigurationObservation,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub coordinator_configuration: Option<ConfigurationObservation>,
    pub case_markers: Vec<CaseMarkerObservation>,
    pub plugins: Vec<PluginObservation>,
    pub execution_topology: ExecutionTopology,
}

impl RunObservations {
    /// Combine observations from workers that consumed the same configuration.
    ///
    /// # Errors
    /// Returns an error for differing configuration, conflicting plugin identities,
    /// or overflowing invocation counts.
    pub fn merge(&mut self, other: Self) -> Result<(), super::RunRecordMergeError> {
        if self.configuration != other.configuration
            || self.coordinator_configuration != other.coordinator_configuration
        {
            return Err(super::RunRecordMergeError::Observations);
        }
        self.case_markers.extend(other.case_markers);
        self.merge_plugins(other.plugins)?;
        let topology = &mut self.execution_topology;
        topology.requested_workers = topology
            .requested_workers
            .max(other.execution_topology.requested_workers);
        topology
            .worker_pids
            .extend(other.execution_topology.worker_pids);
        topology.worker_pids.sort_unstable();
        topology.worker_pids.dedup();
        topology.active_workers = topology.worker_pids.len();
        Ok(())
    }

    /// Add measured lifecycle plugin activity without replacing collection inputs.
    ///
    /// A coordinator does not collect tests, so collection hooks can legitimately
    /// change worker configuration without changing its configuration.
    ///
    /// # Errors
    /// Returns an error if an invocation count overflows.
    pub fn merge_plugins(
        &mut self,
        plugins: Vec<PluginObservation>,
    ) -> Result<(), super::RunRecordMergeError> {
        for plugin in plugins {
            if let Some(existing) = self.plugins.iter_mut().find(|item| {
                item.name == plugin.name
                    && item.generation == plugin.generation
                    && item.module == plugin.module
                    && item.origin == plugin.origin
                    && item.sha256 == plugin.sha256
                    && item.hooks == plugin.hooks
            }) {
                for (name, count) in plugin.hook_calls {
                    let value = existing.hook_calls.entry(name).or_default();
                    *value = value
                        .checked_add(count)
                        .ok_or(super::RunRecordMergeError::Observations)?;
                }
            } else {
                self.plugins.push(plugin);
            }
        }
        self.plugins.sort_by(|left, right| {
            (&left.name, left.generation, &left.module, &left.origin).cmp(&(
                &right.name,
                right.generation,
                &right.module,
                &right.origin,
            ))
        });
        Ok(())
    }
}

#[cfg(test)]
mod tests;
