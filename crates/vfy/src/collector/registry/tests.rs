use std::fmt::Write;
use std::path::Path;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule, PyWeakrefMethods, PyWeakrefReference};

use super::{
    CodeRegistry, ExcludedCodeRetention, PathPolicy, SemanticBranch, next_ordinal, source_range,
};

#[test]
fn library_coverage_without_source_roots_accepts_project_and_library_files() {
    let (directory, source, package) = source_tree();
    let library = directory.path().join("stdlib");
    let registry = CodeRegistry::new(
        &[],
        &[library.to_string_lossy().into_owned()],
        Path::new(&package).parent().unwrap().to_str().unwrap(),
        &[],
        &[],
        Vec::new(),
        PathPolicy::new(true, false),
    )
    .unwrap();
    assert!(
        registry
            .resolve_filename(&format!("{source}/module.py"))
            .is_some()
    );
    assert!(
        registry
            .resolve_filename(&library.join("module.py").to_string_lossy())
            .is_some()
    );
    assert!(registry.resolve_filename(&package).is_none());
}

fn source_tree() -> (tempfile::TempDir, String, String) {
    let directory = tempfile::tempdir().expect("temporary registry tree");
    let source_root = directory.path().join("src");
    let package_root = directory.path().join("package");
    std::fs::create_dir_all(&source_root).expect("source root");
    std::fs::create_dir_all(&package_root).expect("package root");
    let package_file = package_root.join("__init__.py");
    std::fs::write(&package_file, "").expect("package file");
    (
        directory,
        source_root.to_string_lossy().into_owned(),
        package_file.to_string_lossy().into_owned(),
    )
}

#[test]
fn explicit_source_file_discovery_preserves_omit_and_relative_paths_without_siblings() {
    let (_directory, source_root, package_file) = source_tree();
    let root = vfy_core::absolute_path(Path::new(&source_root));
    let source = root.join("app.py");
    std::fs::write(&source, "def value():\n    return 1\n").expect("unexecuted explicit source");
    std::fs::write(root.join("sibling.py"), "value = 2\n").expect("unselected sibling source");
    let source = source.to_string_lossy().into_owned();
    for relative_files in [false, true] {
        for omitted in [false, true] {
            let omit = if omitted {
                vec!["app.py".to_owned()]
            } else {
                Vec::new()
            };
            let registry = CodeRegistry::new(
                std::slice::from_ref(&source),
                &[],
                &package_file,
                &[],
                &omit,
                Vec::new(),
                PathPolicy {
                    cover_pylib: false,
                    relative_root: relative_files.then(|| root.clone()),
                    working_root: Some(root.clone()),
                },
            )
            .expect("explicit-file registry");
            let expected = if omitted {
                Vec::new()
            } else if relative_files {
                vec!["app.py".to_owned()]
            } else {
                vec![source.clone()]
            };
            assert_eq!(
                registry.discover_source_files(),
                expected,
                "relative_files={relative_files}, omitted={omitted}",
            );
        }
    }
}

#[test]
fn loaded_module_file_discovery_requires_an_explicit_source_root() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let source_path = vfy_core::absolute_path(&Path::new(&source_root).join("target.py"));
        std::fs::write(&source_path, "value = 1\n").expect("unexecuted loaded module source");
        let source = source_path.to_string_lossy().into_owned();
        for explicit in [false, true] {
            let roots = if explicit {
                vec![source.clone()]
            } else {
                Vec::new()
            };
            let mut registry = CodeRegistry::new(
                &roots,
                &[],
                &package_file,
                &[],
                &[],
                Vec::new(),
                PathPolicy::new(false, false),
            )
            .expect("module-source registry");
            registry
                .set_source_packages(py, vec!["target".to_owned()])
                .expect("configured module source");
            let module = PyModule::new(py, "target").expect("loaded module metadata");
            module
                .setattr("__file__", &source)
                .expect("module source file");
            let modules = PyDict::new(py);
            modules
                .set_item("target", module)
                .expect("module inventory");
            registry
                .source_packages
                .as_mut()
                .expect("module sources")
                .modules = modules.into_any().unbind();
            registry
                .resolve_loaded_source_packages(py)
                .expect("loaded module source resolves without execution");
            assert_eq!(
                registry
                    .source_packages
                    .as_ref()
                    .expect("module sources")
                    .discovery_roots,
                vec![source_path.clone()],
            );
            assert_eq!(
                registry.discover_source_files(),
                if explicit {
                    vec![source.clone()]
                } else {
                    Vec::new()
                },
                "explicit={explicit}",
            );
        }
    });
}

fn compile<'py>(py: Python<'py>, source: &str, filename: &str) -> Bound<'py, PyAny> {
    py.import("builtins")
        .expect("builtins")
        .call_method1("compile", (source, filename, "exec"))
        .expect("compile source")
}

fn nested_code<'py>(code: &Bound<'py, PyAny>) -> Bound<'py, PyAny> {
    code.getattr("co_consts")
        .expect("constants")
        .try_iter()
        .expect("constant iterator")
        .filter_map(Result::ok)
        .find(|constant| constant.get_type().name().is_ok_and(|name| name == "code"))
        .expect("nested code object")
}

#[test]
fn ordinal_and_instruction_position_helpers_are_total_and_monotonic() {
    let mut ordinals = std::collections::BTreeMap::new();
    assert_eq!(next_ordinal(&mut ordinals, 10), 0);
    assert_eq!(next_ordinal(&mut ordinals, 10), 1);
    assert_eq!(next_ordinal(&mut ordinals, 5), 0);
    assert_eq!(
        source_range(Some((Some(1), Some(2), Some(3), Some(4)))),
        Some((1, 3, 2, 4))
    );
    assert_eq!(source_range(Some((Some(1), None, Some(3), Some(4)))), None);
    assert_eq!(source_range(None), None);
}

#[test]
fn registry_resolves_source_policy_and_caches_code_identity() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let source_path = Path::new(&source_root).join("module.py");
        std::fs::write(&source_path, "value = 1\n").expect("source file");
        let mut registry = CodeRegistry::new(
            std::slice::from_ref(&source_root),
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .expect("registry");
        let code = compile(py, "value = 1\n", &source_path.to_string_lossy());
        let first = registry
            .filename(&code)
            .expect("filename")
            .expect("source filename")
            .to_owned();
        let second = registry
            .filename(&code)
            .expect("cached filename")
            .expect("source filename")
            .to_owned();
        assert_eq!(first, second);
        assert_eq!(registry.first_line(&code).expect("first line"), 1);
        let offset_lines = registry.offset_lines_for(&code).expect("offset map");
        let (offset, expected_line) = offset_lines
            .iter()
            .find(|(_, line)| **line > 0)
            .map(|(offset, line)| (*offset, *line))
            .expect("mapped source line");
        assert_eq!(
            registry
                .line_for_offset(&code, offset)
                .expect("line for offset"),
            expected_line
        );
        assert_eq!(registry.resolve_filename("<string>"), None);
    });
}

#[test]
fn offset_line_table_preserves_holes_and_rejects_non_word_offsets() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let source_path = Path::new(&source_root).join("offsets.py");
        let source = "def choose(value):\n    try:\n        if value:\n            raise ValueError\n    finally:\n        value = 0\n";
        std::fs::write(&source_path, source).expect("source file");
        let mut registry = CodeRegistry::new(
            std::slice::from_ref(&source_root),
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .expect("registry");
        let module = compile(py, source, &source_path.to_string_lossy());
        let code = nested_code(&module);
        let bytecode_length = code
            .getattr("co_code")
            .expect("bytecode")
            .len()
            .expect("length");
        let max_offset = i32::try_from(bytecode_length).expect("small test bytecode");
        let mut expected = std::collections::BTreeMap::new();
        let mut saw_unmapped_slot = false;
        for item in code
            .call_method0("co_lines")
            .expect("line spans")
            .try_iter()
            .expect("line span iterator")
        {
            let (start, end, line): (i32, i32, Option<i32>) = item
                .expect("line span")
                .extract()
                .expect("line span fields");
            let start = start.max(0);
            let end = end.min(max_offset);
            for offset in (start..end).step_by(2) {
                if offset % 2 == 0 && offset < max_offset {
                    if let Some(line) = line {
                        expected.insert(offset, line);
                    } else {
                        saw_unmapped_slot = true;
                    }
                }
            }
        }
        assert!(saw_unmapped_slot, "fixture needs an unlined bytecode word");

        assert_eq!(
            registry
                .line_for_offset_exact(&code, 0)
                .expect("initial exact line"),
            expected.get(&0).copied()
        );
        assert_eq!(
            registry.offset_lines_for(&code).expect("cold offset map"),
            expected
        );
        for offset in (0..max_offset).step_by(2) {
            assert_eq!(
                registry
                    .line_for_offset_exact(&code, offset)
                    .expect("exact line"),
                expected.get(&offset).copied(),
                "offset {offset}"
            );
        }
        for offset in [-2, -1, 1, max_offset, max_offset + 1, max_offset + 2] {
            assert_eq!(
                registry
                    .line_for_offset_exact(&code, offset)
                    .expect("invalid exact line"),
                None,
                "invalid offset {offset}"
            );
        }
    });
}

#[test]
fn registry_include_and_omit_patterns_are_applied_to_absolute_and_relative_paths() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let source_path = Path::new(&source_root).join("module.py");
        std::fs::write(&source_path, "pass\n").expect("source file");
        let include = vec!["*/src/*.py".to_owned()];
        let omit = vec!["*/generated/*.py".to_owned()];
        let registry = CodeRegistry::new(
            std::slice::from_ref(&source_root),
            &[],
            &package_file,
            &include,
            &omit,
            Vec::new(),
            PathPolicy::new(false, true),
        )
        .expect("registry");
        let rendered = source_path.to_string_lossy().replace('\\', "/");
        assert!(registry.resolve_filename(&rendered).is_some());
        assert!(registry.resolve_filename("/outside/project.py").is_none());
        let _ = py;
    });
}

#[test]
fn semantic_branch_mapping_discovers_real_jump_positions() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let source_path = Path::new(&source_root).join("branch.py");
        std::fs::write(&source_path, "if value:\n    pass\nelse:\n    pass\n")
            .expect("source file");
        let mut registry = CodeRegistry::new(
            std::slice::from_ref(&source_root),
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .expect("registry");
        let code = compile(
            py,
            "if value:\n    pass\nelse:\n    pass\n",
            &source_path.to_string_lossy(),
        );
        let bytecode: Vec<u8> = code
            .getattr("co_code")
            .expect("bytecode")
            .extract()
            .expect("bytecode bytes");
        let mut found: Option<(i32, SemanticBranch)> = None;
        for offset in (0..bytecode.len()).step_by(2) {
            let offset = i32::try_from(offset).expect("offset");
            if let Some(branch) = registry
                .semantic_branch(&code, offset)
                .expect("semantic branch")
            {
                found = Some((offset, branch));
                break;
            }
        }
        let (origin, branch) = found.expect("conditional jump");
        assert!(branch.line >= 1);
        assert!(
            registry
                .semantic_branch_outcome(&code, origin, origin + 2)
                .expect("branch outcome")
                .is_some()
        );
    });
}

#[test]
fn direct_branch_to_implicit_return_resolves_to_function_exit() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let source_path = Path::new(&source_root).join("return_const.py");
        let source = "def choose(flag):\n    if flag:\n        return 1\n";
        std::fs::write(&source_path, source).expect("source file");
        let mut registry = CodeRegistry::new(
            &[source_root],
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .expect("registry");
        let code = nested_code(&compile(py, source, &source_path.to_string_lossy()));
        let dis = py.import("dis").expect("dis");
        let (origin, destination) = dis
            .call_method1("get_instructions", (&code,))
            .expect("instructions")
            .try_iter()
            .expect("instruction iterator")
            .filter_map(Result::ok)
            .find_map(|instruction| {
                let opname: String = instruction.getattr("opname").ok()?.extract().ok()?;
                (opname == "POP_JUMP_IF_FALSE").then(|| {
                    (
                        instruction
                            .getattr("offset")
                            .expect("conditional offset")
                            .extract()
                            .expect("conditional offset number"),
                        instruction
                            .getattr("argval")
                            .expect("conditional destination")
                            .extract()
                            .expect("conditional destination offset"),
                    )
                })
            })
            .expect("conditional instruction");

        assert_eq!(
            registry
                .resolve_branch(&code, origin, destination)
                .expect("resolved branch"),
            Some((2, -1))
        );
    });
}

#[test]
fn registry_keeps_nested_code_identity_and_distinct_source_files() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let first_path = Path::new(&source_root).join("first.py");
        let second_path = Path::new(&source_root).join("second.py");
        std::fs::write(&first_path, "def outer():\n    return 1\n").expect("first source");
        std::fs::write(&second_path, "def outer():\n    return 1\n").expect("second source");
        let mut registry = CodeRegistry::new(
            std::slice::from_ref(&source_root),
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .expect("registry");
        let first = compile(
            py,
            "def outer():\n    return 1\n",
            &first_path.to_string_lossy(),
        );
        let second = compile(
            py,
            "def outer():\n    return 1\n",
            &second_path.to_string_lossy(),
        );
        let first_filename = registry
            .filename(&first)
            .expect("first filename")
            .expect("first source")
            .to_owned();
        let second_filename = registry
            .filename(&second)
            .expect("second filename")
            .expect("second source")
            .to_owned();
        assert_ne!(first_filename, second_filename);

        let nested = nested_code(&first);
        assert_eq!(
            registry
                .filename(&nested)
                .expect("nested filename")
                .expect("nested source"),
            first_filename
        );
        assert_eq!(registry.first_line(&nested).expect("nested first line"), 1);
        assert_ne!(
            CodeRegistry::code_id(&first),
            CodeRegistry::code_id(&nested)
        );
    });
}

#[test]
fn branch_lines_are_normalized_once_without_replacing_physical_offset_lines() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let source_path = Path::new(&source_root).join("multiline.py");
        let source = "def gen(flag):\n    if flag:\n        yield (\n            1\n        )\n    else:\n        yield 2\n";
        std::fs::write(&source_path, source).unwrap();
        let mut registry = CodeRegistry::new(
            &[source_root],
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .unwrap();
        let code = nested_code(&compile(py, source, &source_path.to_string_lossy()));
        registry.prepare_branch_lines(&code).unwrap();
        let lines = registry.offset_lines_for(&code).unwrap();
        let offset = *lines.iter().find(|(_, line)| **line == 4).unwrap().0;
        assert_eq!(
            registry.line_for_offset_exact(&code, offset).unwrap(),
            Some(4)
        );
        assert_eq!(
            registry.branch_line_for_offset(&code, offset).unwrap(),
            Some(3)
        );

        std::fs::write(&source_path, "invalid source (").unwrap();
        registry.prepare_branch_lines(&code).unwrap();
        assert_eq!(
            registry.branch_line_for_offset(&code, offset).unwrap(),
            Some(3)
        );
    });
}

#[test]
fn instruction_line_sequence_matches_cpython_for_large_closure_literals() {
    Python::initialize();
    Python::attach(|py| {
        if py.version_info() >= (3, 13) {
            return;
        }
        let (_directory, source_root, package_file) = source_tree();
        let source_path = Path::new(&source_root).join("closure.py");
        let mut source = "def make(seed):\n    def fill():\n        values = {\n".to_owned();
        for index in 0..2000 {
            writeln!(source, "            {index}: {index},").expect("literal entry");
        }
        source.push_str(
            "        }\n        return seed + len(values)\n    return fill\nfill = make(1)\n",
        );
        let namespace = pyo3::types::PyDict::new(py);
        py.import("builtins")
            .expect("builtins")
            .call_method1(
                "exec",
                (
                    compile(py, &source, &source_path.to_string_lossy()),
                    &namespace,
                ),
            )
            .expect("closure definition");
        let fill = namespace
            .get_item("fill")
            .expect("fill lookup")
            .expect("fill function");
        let code = fill.getattr("__code__").expect("closure code");
        namespace.set_item("target", &code).expect("target code");
        py.run(
            c"import sys\nobserved = []\ndef observe(code, line):\n    if code is target:\n        observed.append(line)\nmonitoring = sys.monitoring\nslot = monitoring.DEBUGGER_ID\nmonitoring.use_tool_id(slot, 'line sequence oracle')\ntry:\n    monitoring.register_callback(slot, monitoring.events.LINE, observe)\n    monitoring.set_local_events(slot, target, monitoring.events.LINE)\n    assert fill() == 2001\nfinally:\n    monitoring.set_local_events(slot, target, 0)\n    monitoring.register_callback(slot, monitoring.events.LINE, None)\n    monitoring.free_tool_id(slot)\n",
            Some(&namespace),
            None,
        ).expect("independent CPython LINE oracle");
        let expected: Vec<i32> = namespace
            .get_item("observed")
            .expect("observed lookup")
            .expect("observed sequence")
            .extract()
            .expect("line sequence");
        let mut registry = CodeRegistry::new(
            &[source_root],
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .expect("registry");
        registry.filename(&code).expect("retained code identity");
        registry
            .prepare_instruction_monitoring(py)
            .expect("opcode metadata");
        assert!(
            registry
                .prepare_instruction_lines(&code)
                .expect("instruction strategy")
        );
        let bytecode_length = code
            .getattr("co_code")
            .expect("bytecode")
            .len()
            .expect("length");
        let actual: Vec<_> = (0..i32::try_from(bytecode_length).expect("fixture bytecode bound"))
            .step_by(2)
            .filter_map(|offset| registry.instruction_line(CodeRegistry::code_id(&code), offset))
            .collect();
        assert_eq!(actual, expected);
    });
}

#[test]
fn weakly_held_excluded_code_does_not_lend_its_address_to_new_code() {
    Python::initialize();
    Python::attach(|py| {
        let (_directory, source_root, package_file) = source_tree();
        let measured_path = format!("{source_root}/module.py");
        let mut registry = CodeRegistry::new(
            &[source_root],
            &[],
            &package_file,
            &[],
            &[],
            Vec::new(),
            PathPolicy::new(false, false),
        )
        .expect("registry");
        registry.excluded_code = ExcludedCodeRetention::Weak;

        let excluded = compile(py, "value = 1\n", "<generated>");
        assert!(registry.filename(&excluded).expect("excluded").is_none());
        let reference = PyWeakrefReference::new(&excluded)
            .expect("excluded reference")
            .unbind();
        drop(excluded);
        assert!(
            reference.bind(py).upgrade().is_none(),
            "the registry must not keep excluded code alive"
        );

        // Model CPython reusing the freed address for measured code.
        let measured = compile(py, "value = 1\n", &measured_path);
        let measured_id = CodeRegistry::code_id(&measured);
        registry
            .code_objects
            .insert(measured_id, super::CodeHandle::Weak(reference));
        registry.filenames.insert(measured_id, None);
        registry
            .set_source_packages(py, vec!["module".to_owned()])
            .expect("source package lookup");
        assert!(
            registry
                .frame_lookup(&measured)
                .expect("frame lookup")
                .is_some(),
            "a reused address must request the new frame's module metadata"
        );
        assert!(registry.filename(&measured).expect("measured").is_some());
        assert!(matches!(
            registry.code_objects.get(&measured_id),
            Some(super::CodeHandle::Strong(_))
        ));
    });
}
