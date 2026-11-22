/// Python version fields used to choose the monitoring strategy.
pub struct PythonVersion<'a> {
    pub major: u8,
    pub minor: u8,
    pub micro: u8,
    pub release_level: &'a str,
    pub serial: u8,
}

/// The coverage core selection requested at the process boundary.
pub enum RequestedCore<'a> {
    Default,
    Named(&'a str),
}

/// Inputs that determine whether branch arcs use `sys.monitoring`.
pub struct RuntimePolicy<'a> {
    pub implementation: &'a str,
    pub version: PythonVersion<'a>,
    pub requested_core: RequestedCore<'a>,
}

impl RuntimePolicy<'_> {
    /// Whether the active `CPython` release exposes the branch-arc monitoring core.
    #[must_use]
    pub fn uses_sysmon_branch_arcs(&self) -> bool {
        if matches!(
            self.requested_core,
            RequestedCore::Named("ctrace" | "pytrace")
        ) || self.implementation != "cpython"
        {
            return false;
        }

        let version = &self.version;
        if (version.major, version.minor) != (3, 14) {
            return (version.major, version.minor) > (3, 14);
        }

        if version.micro > 0 {
            return true;
        }
        let release_rank = match version.release_level {
            "alpha" => 0,
            "beta" => 1,
            "candidate" => 2,
            "final" => 3,
            _ => return false,
        };
        release_rank > 0 || version.serial > 5
    }
}

#[cfg(test)]
mod tests;
