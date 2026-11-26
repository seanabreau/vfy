//! Read fixture origins from the registry's historic registration events.

use super::ArgumentBinding;
use super::Collector;
use super::collection::arena::{self, CollectionNodeId, CollectionNodeKind};
use super::collection::objects::FixtureRegistration;
use super::fixtures;
use pyo3::exceptions::{PyAttributeError, PyException, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule, PyType};
use vfy_core::test_plan::FixtureChains;

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum FixtureOrigin {
    Conftest(ConftestDirectory),
    Module(CollectionNodeId),
    Class(CollectionNodeId),
    PluginRegistration(usize),
    DirectParameter,
}

/// Where a conftest sits relative to the rootdir.
#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum ConftestDirectory {
    Rootdir,
    Other,
}

impl FixtureOrigin {
    pub(super) fn is_class(self) -> bool {
        matches!(self, Self::Class(_))
    }

    pub(super) fn reusable(self) -> bool {
        matches!(self, Self::Conftest(_) | Self::PluginRegistration(_))
    }

    /// The `FixtureDef.baseid` pytest reports for a definition with this scope key.
    ///
    /// pytest names the rootdir's `Directory` node `.`, while a package scope
    /// key for the rootdir is empty.
    pub(super) fn public_baseid(self, package_baseid: &str) -> &str {
        match self {
            Self::Conftest(ConftestDirectory::Rootdir) => ".",
            Self::Conftest(ConftestDirectory::Other)
            | Self::Module(_)
            | Self::Class(_)
            | Self::PluginRegistration(_)
            | Self::DirectParameter => package_baseid,
        }
    }

    pub(super) fn validate(self, collector: &Collector<'_>) -> PyResult<()> {
        let (owner, expected) = match self {
            Self::Module(owner) => (owner, CollectionNodeKind::Module),
            Self::Class(owner) => (owner, CollectionNodeKind::Class),
            Self::Conftest(_) | Self::PluginRegistration(_) | Self::DirectParameter => {
                return Ok(());
            }
        };
        if collector.current_node != Some(owner) {
            return Err(PyRuntimeError::new_err(
                "fixture declaration has a different collection owner",
            ));
        }
        let context = collector.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("local fixture declaration requires a live collection")
        })?;
        let arena = arena::for_context(collector.py, context)?;
        if arena.borrow().kind(owner)? != expected {
            return Err(PyRuntimeError::new_err(
                "fixture declaration has an invalid collection owner",
            ));
        }
        arena::NativeCollectionArena::validate_parent(&arena, owner)
    }
}

impl<'py> Collector<'py> {
    pub(super) fn add_plugin_fixtures(
        &mut self,
        plugin: &Bound<'py, PyAny>,
        chains: &mut FixtureChains,
        registration: usize,
    ) -> PyResult<()> {
        let origin = FixtureOrigin::PluginRegistration(registration);
        if plugin.is_instance_of::<PyModule>() || plugin.is_instance_of::<PyType>() {
            return self.add_fixtures(plugin, chains, "", origin);
        }
        let definition_type = self
            .py
            .import("vfy.independent.fixtures")?
            .getattr("FixtureFunctionDefinition")?;
        let names = self
            .py
            .import("builtins")?
            .getattr("dir")?
            .call1((plugin,))?;
        for name in names.try_iter()? {
            let name = name?;
            let original = match plugin
                .get_type()
                .getattr(name.cast::<pyo3::types::PyString>()?)
            {
                Ok(value) => value,
                Err(error) if error.is_instance_of::<PyException>(self.py) => continue,
                Err(error) => return Err(error),
            };
            if !original.get_type().is(&definition_type) {
                continue;
            }
            let metadata = original.getattr("__vfy_fixture__")?.cast_into::<PyDict>()?;
            let actual = match plugin.getattr(name.cast::<pyo3::types::PyString>()?) {
                Ok(value) => value,
                Err(error) if error.is_instance_of::<PyAttributeError>(self.py) => original.clone(),
                Err(error) => return Err(error),
            };
            let function = actual.call_method0("_get_wrapped_function")?;
            let declaration = fixtures::Declaration {
                metadata,
                callable: function.clone(),
                function,
            };
            // FixtureFunctionDefinition.__get__ returns a fresh wrapper for a
            // class lookup. Retain the defining descriptor as the registration
            // identity so a later module reuses the same fixture definition.
            let identity = match self
                .inspect
                .call_method1("getattr_static", (plugin.get_type(), &name))
            {
                Ok(value) => value,
                Err(error) if error.is_instance_of::<PyAttributeError>(self.py) => original.clone(),
                Err(error) => return Err(error),
            };
            self.add_fixture_declaration(
                FixtureRegistration {
                    attribute_name: name,
                    original: &identity,
                    declaration,
                    arguments: ArgumentBinding::Function,
                    origin,
                },
                chains,
                "",
            )?;
        }
        Ok(())
    }

    pub(super) fn registered_fixture_plugins(&self) -> PyResult<Vec<(usize, Py<PyAny>)>> {
        let Some(context) = &self.context else {
            return Ok(Vec::new());
        };
        let arguments = context
            .registry
            .bind(self.py)
            .borrow()
            .historic_arguments(self.py, "pytest_plugin_registered");
        let mut plugins = Vec::new();
        // The registry borrow has ended before examining Python dictionaries.
        // Removed registrations remain in this history.
        for (registration, arguments) in arguments.into_iter().enumerate() {
            let arguments = arguments.bind(self.py);
            let Some(name) = arguments.get_item("plugin_name")? else {
                continue;
            };
            let name = name.extract::<String>()?;
            if name.ends_with("conftest.py") {
                continue;
            }
            if let Some(plugin) = arguments.get_item("plugin")? {
                plugins.push((registration, plugin.unbind()));
            }
        }
        Ok(plugins)
    }
}
