//! Observe the live frontend state without reconstructing configuration or hooks.

use super::lifecycle::Context;
use pyo3::prelude::*;
use std::collections::BTreeMap;
use vfy_core::test_execution::{
    CaseMarkerObservation, ConfigurationObservation, ExecutionTopology, RunObservations,
};

pub(super) fn snapshot(py: Python<'_>, context: &Context) -> PyResult<RunObservations> {
    let mut case_markers = Vec::new();
    for item in &context.items {
        let item = item.bind(py);
        let names = item
            .call_method0("iter_markers")?
            .try_iter()?
            .map(|mark| mark?.getattr("name")?.extract::<String>())
            .collect::<PyResult<Vec<_>>>()?;
        case_markers.push(CaseMarkerObservation {
            nodeid: item.getattr("nodeid")?.extract()?,
            names,
        });
    }
    Ok(RunObservations {
        configuration: configuration(py, context)?,
        coordinator_configuration: None,
        case_markers,
        plugins: context.registry.borrow(py).plugin_observations()?,
        execution_topology: ExecutionTopology {
            requested_workers: {
                let options = context.config.bind(py).getattr("option")?;
                if options.hasattr("_requested_workers")? {
                    options.getattr("_requested_workers")?.extract()?
                } else {
                    context
                        .config
                        .bind(py)
                        .call_method1("getoption", ("workers", 1))?
                        .extract()?
                }
            },
            active_workers: 1,
            worker_pids: vec![std::process::id()],
        },
    })
}

pub(super) fn configuration(
    py: Python<'_>,
    context: &Context,
) -> PyResult<ConfigurationObservation> {
    let config = context.config.bind(py);
    let inipath = config.getattr("inipath")?;
    let ini_path = (!inipath.is_none())
        .then(|| {
            inipath
                .str()
                .map(|value| value.to_string_lossy().into_owned())
        })
        .transpose()?;
    let ini_sha256 = config
        .getattr("_configuration")?
        .call_method0("ini_sha256")?
        .extract()?;
    let mut values = BTreeMap::new();
    for name in [
        "addopts",
        "markers",
        "filterwarnings",
        "testpaths",
        "norecursedirs",
        "python_files",
        "python_classes",
        "python_functions",
    ] {
        values.insert(
            name.to_owned(),
            config.call_method1("getini", (name,))?.extract()?,
        );
    }
    Ok(ConfigurationObservation {
        ini_path,
        ini_sha256,
        values,
        strict_config: config
            .call_method1("getoption", ("strict_config",))?
            .is_truthy()?,
        strict_markers: config
            .call_method1("getoption", ("strict_markers",))?
            .is_truthy()?,
    })
}
