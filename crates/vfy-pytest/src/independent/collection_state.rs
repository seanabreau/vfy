//! Move a prepared collector across Python calls without retaining an interpreter token.

use super::{Collector, FixtureBinding, TestBinding, collection, lifecycle};
use crate::independent::imports::ImportMode;
use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::collections::BTreeMap;
use std::path::PathBuf;
use vfy_core::FixtureId;
use vfy_core::independent_collection::CollectionSpecification;
use vfy_core::test_execution::CollectionReport;
use vfy_core::test_plan::{FixturePlan, TestPlan};

pub(super) struct OwnedCollector {
    inspect: Py<PyModule>,
    fixtures: Vec<FixturePlan>,
    fixture_bindings: Vec<FixtureBinding>,
    direct_fixtures: BTreeMap<(vfy_core::CollectionNodeId, String), FixtureId>,
    tests: Vec<TestPlan>,
    test_bindings: Vec<TestBinding>,
    modules: BTreeMap<PathBuf, Py<PyAny>>,
    module_marks: Vec<Py<PyAny>>,
    context: Option<lifecycle::Context>,
    collection_reports: Vec<CollectionReport>,
    deselected_nodeids: Vec<String>,
    plugins: BTreeMap<String, Py<PyAny>>,
    current_node: Option<collection::arena::CollectionNodeId>,
    collected_order: Vec<collection::arena::CollectionNodeId>,
    specification: Option<CollectionSpecification>,
    root: PathBuf,
    import_mode: ImportMode,
    collection_scope: super::selection::CollectionScopeState,
}

impl OwnedCollector {
    pub(super) fn new(collector: Collector<'_>) -> Self {
        Self {
            inspect: collector.inspect.unbind(),
            fixtures: collector.fixtures,
            fixture_bindings: collector.fixture_bindings,
            direct_fixtures: collector.direct_fixtures,
            tests: collector.tests,
            test_bindings: collector.test_bindings,
            modules: collector.modules,
            module_marks: collector.module_marks,
            context: collector.context,
            collection_reports: collector.collection_reports,
            deselected_nodeids: collector.deselected_nodeids,
            plugins: collector.plugins,
            current_node: collector.current_node,
            collected_order: collector.collected_order,
            specification: collector.specification,
            root: collector.root,
            import_mode: collector.import_mode,
            collection_scope: collector.collection_scope,
        }
    }

    pub(super) fn bind(self, py: Python<'_>) -> Collector<'_> {
        Collector {
            py,
            inspect: self.inspect.into_bound(py),
            fixtures: self.fixtures,
            fixture_bindings: self.fixture_bindings,
            direct_fixtures: self.direct_fixtures,
            tests: self.tests,
            test_bindings: self.test_bindings,
            modules: self.modules,
            module_marks: self.module_marks,
            context: self.context,
            collection_reports: self.collection_reports,
            deselected_nodeids: self.deselected_nodeids,
            plugins: self.plugins,
            current_node: self.current_node,
            collected_order: self.collected_order,
            specification: self.specification,
            root: self.root,
            import_mode: self.import_mode,
            collection_scope: self.collection_scope,
        }
    }
}
