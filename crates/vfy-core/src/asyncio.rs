const VALID_SCOPES: [&str; 5] = ["function", "class", "module", "package", "session"];
const FIXTURE_CACHING_SCOPE: &str = "fixture-scope";

/// Discovery policy for coroutine tests and fixtures.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AsyncioMode {
    Strict,
    Auto,
}

impl AsyncioMode {
    /// Parse the maintained pytest-asyncio discovery policy.
    ///
    /// # Errors
    ///
    /// Returns an error for a mode other than `auto` or `strict`.
    pub fn parse(value: &str) -> Result<Self, String> {
        match value {
            "strict" => Ok(Self::Strict),
            "auto" => Ok(Self::Auto),
            _ => Err(format!(
                "'{value}' is not a valid asyncio_mode. Valid modes: auto, strict."
            )),
        }
    }
}

/// The scope of an asyncio event loop shared by fixtures or tests.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AsyncioLoopScope {
    Function,
    Class,
    Module,
    Package,
    Session,
}

impl AsyncioLoopScope {
    /// Parse a pytest-compatible loop scope.
    ///
    /// # Errors
    ///
    /// Returns an error when `value` is not one of the five pytest loop scopes.
    pub fn parse(value: &str) -> Result<Self, String> {
        match value {
            "function" => Ok(Self::Function),
            "class" => Ok(Self::Class),
            "module" => Ok(Self::Module),
            "package" => Ok(Self::Package),
            "session" => Ok(Self::Session),
            _ => Err(format!(
                "{value:?} is not a valid asyncio loop scope. Valid scopes: {}.",
                VALID_SCOPES.join(", ")
            )),
        }
    }

    /// Return the pytest spelling of the scope.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Function => "function",
            Self::Class => "class",
            Self::Module => "module",
            Self::Package => "package",
            Self::Session => "session",
        }
    }
}

/// Select the effective loop scope using explicit, configured, then fixture scope precedence.
/// A configured `fixture-scope` uses the fixture's caching scope.
///
/// # Errors
///
/// Returns an error when the selected value is not a pytest-compatible loop scope.
pub fn plan_asyncio_loop_scope(
    explicit: Option<&str>,
    configured: Option<&str>,
    fixture_scope: &str,
) -> Result<AsyncioLoopScope, String> {
    let configured = configured.filter(|&scope| scope != FIXTURE_CACHING_SCOPE);
    AsyncioLoopScope::parse(explicit.or(configured).unwrap_or(fixture_scope))
}

/// Validate the built-in asyncio configuration before pytest collection begins.
///
/// # Errors
///
/// Returns an error when the mode or either configured loop scope is invalid.
pub fn validate_asyncio_config(
    mode: &str,
    default_fixture_loop_scope: Option<&str>,
    default_test_loop_scope: &str,
) -> Result<(), String> {
    AsyncioMode::parse(mode)?;
    if let Some(scope) = default_fixture_loop_scope.filter(|&scope| scope != FIXTURE_CACHING_SCOPE)
    {
        AsyncioLoopScope::parse(scope).map_err(|_| {
            format!(
                "'{scope}' is not a valid asyncio_default_fixture_loop_scope. Valid scopes are: {}.",
                VALID_SCOPES.join(", ")
            )
        })?;
    }
    AsyncioLoopScope::parse(default_test_loop_scope).map_err(|_| {
        format!(
            "'{default_test_loop_scope}' is not a valid asyncio_default_test_loop_scope. Valid scopes are: {}.",
            VALID_SCOPES.join(", ")
        )
    })?;
    Ok(())
}

#[cfg(test)]
mod tests;
