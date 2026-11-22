use super::{PluginObservation, RunObservations};
fn observed(generation: u64, module: &str, calls: u64) -> RunObservations {
    serde_json::from_value(serde_json::json!({
        "configuration": {"ini_path": null, "ini_sha256": null, "values": {},
            "strict_config": false, "strict_markers": false},
        "case_markers": [], "plugins": [{"generation": generation, "name": "replaceable",
            "module": module, "origin": null, "sha256": null, "hooks": ["pytest_configure"],
            "hook_calls": {"pytest_configure": calls}}],
        "execution_topology": {"requested_workers": 2, "active_workers": 1, "worker_pids": [1]}
    }))
    .unwrap()
}
#[test]
fn plugin_generations_keep_replaced_identity_and_per_generation_counts() {
    let mut first = observed(1, "first", 1);
    first.merge(observed(2, "second", 3)).unwrap();
    first.merge(observed(1, "first", 4)).unwrap();
    assert_eq!(first.plugins.len(), 2);
    assert_eq!(first.plugins[0].module, "first");
    assert_eq!(first.plugins[0].hook_calls["pytest_configure"], 5);
    assert_eq!(first.plugins[1].module, "second");
    assert_eq!(first.plugins[1].hook_calls["pytest_configure"], 3);
}
#[test]
fn legacy_plugin_identity_omits_default_generation() {
    let value = serde_json::json!({"name": "old", "module": "old", "origin": null,
        "sha256": null, "hooks": [], "hook_calls": {}});
    let parsed: PluginObservation = serde_json::from_value(value.clone()).unwrap();
    assert_eq!(parsed.generation, 0);
    assert_eq!(serde_json::to_value(parsed).unwrap(), value);
}

#[test]
fn lifecycle_plugin_merge_preserves_worker_configuration_and_execution() {
    let mut workers = observed(1, "worker", 2);
    workers
        .configuration
        .values
        .insert("markers".to_owned(), vec!["late: collected".to_owned()]);
    let expected = workers.clone();
    let coordinator = observed(1, "worker", 1);
    workers.merge_plugins(coordinator.plugins).unwrap();
    assert_eq!(workers.configuration, expected.configuration);
    assert_eq!(workers.case_markers, expected.case_markers);
    assert_eq!(workers.execution_topology, expected.execution_topology);
    assert_eq!(workers.plugins[0].hook_calls["pytest_configure"], 3);
    assert!(workers.merge(observed(1, "worker", 1)).is_err());
    workers.coordinator_configuration = Some(observed(1, "worker", 1).configuration);
    let encoded = serde_json::to_string(&workers).unwrap();
    let restored: RunObservations = serde_json::from_str(&encoded).unwrap();
    assert_eq!(restored, workers);
    assert_ne!(
        restored.coordinator_configuration.as_ref().unwrap(),
        &restored.configuration
    );
}
