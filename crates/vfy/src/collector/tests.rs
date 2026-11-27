use super::{CodeRegistry, PathPolicy};
use vfy_core::{absolute_path, glob_set};

#[test]
fn coverage_globs_span_path_separators() {
    let patterns = glob_set(&["*/package/*.py".to_owned()]).unwrap().unwrap();
    assert!(patterns.is_match("/work/package/module.py"));
    assert!(!patterns.is_match("/work/package/module.rs"));
}

#[test]
fn source_overrides_include_and_preserves_omit() {
    let temporary = tempfile::tempdir().unwrap();
    let source = temporary.path().join("source");
    std::fs::create_dir_all(source.join("generated")).unwrap();
    let registry = CodeRegistry::new(
        &[source.to_string_lossy().into_owned()],
        &[],
        &temporary.path().join("engine").to_string_lossy(),
        &["*.py".to_owned()],
        &["source/generated/*".to_owned()],
        Vec::new(),
        PathPolicy {
            cover_pylib: false,
            relative_root: None,
            working_root: Some(absolute_path(temporary.path())),
        },
    )
    .unwrap();

    assert!(
        registry
            .resolve_filename(&source.join("module.py").to_string_lossy())
            .is_some()
    );
    assert!(
        registry
            .resolve_filename(&source.join("module.rs").to_string_lossy())
            .is_some()
    );
    assert!(
        registry
            .resolve_filename(&source.join("generated/code.py").to_string_lossy())
            .is_none()
    );
    assert!(registry.resolve_filename("<string>").is_none());
}
