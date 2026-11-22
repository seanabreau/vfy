use super::{AsyncioLoopScope, plan_asyncio_loop_scope, validate_asyncio_config};

#[test]
fn selects_explicit_then_configured_then_fixture_scope() {
    assert_eq!(
        plan_asyncio_loop_scope(Some("module"), Some("class"), "function"),
        Ok(AsyncioLoopScope::Module)
    );
    assert_eq!(
        plan_asyncio_loop_scope(None, Some("class"), "function"),
        Ok(AsyncioLoopScope::Class)
    );
    assert_eq!(
        plan_asyncio_loop_scope(None, None, "function"),
        Ok(AsyncioLoopScope::Function)
    );
}

#[test]
fn rejects_invalid_modes_and_scopes() {
    assert!(validate_asyncio_config("invalid", None, "function").is_err());
    assert!(validate_asyncio_config("strict", Some("task"), "function").is_err());
    assert!(validate_asyncio_config("auto", None, "task").is_err());
}
