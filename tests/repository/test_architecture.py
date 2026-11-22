"""Mechanical checks for the Rust/Python ownership boundary."""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import textwrap
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _workflow_test_reference_matches(path: str) -> bool:
    return any(match.is_file() for match in ROOT.glob(path))


def _imported_roots(tree: ast.AST) -> set[str]:
    """Return the top-level absolute module names imported anywhere in the tree."""
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
    )
    return imported


def test_collection_adapter_does_not_reimplement_rust_engine_state() -> None:
    collector = ROOT / "python" / "vfy" / "collector.py"
    tree = ast.parse(collector.read_text(encoding="utf-8"))

    imports = _imported_roots(tree)
    assert imports.isdisjoint(
        {"ast", "collections", "fnmatch", "os", "pathlib", "site", "sqlite3", "sysconfig"}
    )

    attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    assert attributes.isdisjoint(
        {"_filenames", "_offset_lines", "_excluded_roots", "_package_root"}
    )

    methods = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MonitoringCollector"
        for node in node.body
        if isinstance(node, ast.FunctionDef)
    }
    callbacks = {
        "_start_frame",
        "_resume_frame",
        "_throw_frame",
        "_suspend_frame",
        "_line",
        "_branch",
        "_end_frame",
    }
    assert methods.isdisjoint(callbacks), "monitoring events must not enter Python callbacks"

    extension_callbacks = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
        and node.value.attr == "_core"
        and node.attr.startswith("callback_")
    }
    assert extension_callbacks, "monitoring callbacks must enter the Rust collector directly"


def test_source_analysis_adapter_contains_no_python_analysis_engine() -> None:
    adapter = ROOT / "python" / "vfy" / "analysis.py"
    tree = ast.parse(adapter.read_text(encoding="utf-8"))
    forbidden_imports = {"ast", "pathlib", "re", "tokenize"}
    imported = _imported_roots(tree)
    assert imported.isdisjoint(forbidden_imports)

    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "analyze_file" in calls


@pytest.mark.parametrize("module", ["reports.py", "semantic_reports.py"])
def test_report_adapter_only_translates_inputs_and_writes_outputs(module: str) -> None:
    adapter = ROOT / "python" / "vfy" / module
    tree = ast.parse(adapter.read_text(encoding="utf-8"))
    imported = _imported_roots(tree)
    assert imported.isdisjoint({"datetime", "hashlib", "html", "json", "re", "xml"}), (
        "report semantics and rendering must remain in Rust"
    )


def test_configuration_adapter_contains_no_python_parser() -> None:
    adapter = ROOT / "python" / "vfy" / "config.py"
    tree = ast.parse(adapter.read_text(encoding="utf-8"))
    imported = _imported_roots(tree)
    assert imported.isdisjoint({"configparser", "os", "tomllib"})


def test_core_crate_is_free_of_python_boundary_types() -> None:
    manifest = ROOT / "crates" / "vfy-core" / "Cargo.toml"
    assert manifest.is_file(), "the pure Rust core crate must remain explicit"
    assert "pyo3" not in manifest.read_text(encoding="utf-8").lower()

    forbidden = re.compile(
        r"pyo3|PyResult|PyAny|PyRef|PyErr|PyObject|PyModule|PyDict|PyList|"
        r"Python\s*<|Py\s*<|Bound\s*<|#\[(?:pyclass|pymethods|pyfunction)",
        re.IGNORECASE,
    )
    leaks = []
    for source in sorted((ROOT / "crates" / "vfy-core" / "src").rglob("*.rs")):
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                leaks.append(f"{source.relative_to(ROOT)}:{line_number}")
    assert not leaks, f"Python boundary types leaked into vfy-core: {leaks}"


def test_core_crate_dependency_graph_is_free_of_pyo3() -> None:
    metadata = json.loads(
        subprocess.run(
            ["cargo", "metadata", "--locked", "--format-version", "1"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    packages = {package["id"]: package for package in metadata["packages"]}
    core = next(package for package in packages.values() if package["name"] == "vfy-core")
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    pending = [core["id"]]
    dependencies: set[str] = set()
    while pending:
        package_id = pending.pop()
        if package_id in dependencies:
            continue
        dependencies.add(package_id)
        pending.extend(nodes[package_id]["dependencies"])

    python_dependencies = sorted(
        packages[package_id]["name"]
        for package_id in dependencies
        if packages[package_id]["name"].lower().startswith("pyo3")
    )
    assert not python_dependencies, (
        f"vfy-core dependencies must not introduce PyO3: {python_dependencies}"
    )


def test_python_extension_export_inventory_is_explicit_and_symmetric() -> None:
    inventory = ROOT / "python" / "vfy" / "native.py"
    tree = ast.parse(inventory.read_text(encoding="utf-8"))
    imports = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "_native" and node.level == 1
        for alias in node.names
        if not (alias.asname or alias.name).startswith("_")
    }
    all_assignments = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]
    assert len(all_assignments) == 1
    assert isinstance(all_assignments[0], ast.List)
    exported = {
        value.value
        for value in all_assignments[0].elts
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    assert all(alias.name != "*" for alias in ast.walk(tree) if isinstance(alias, ast.alias))
    assert imports == exported


def test_python_extension_imports_are_routed_through_extension_inventory() -> None:
    direct_imports: list[str] = []
    for source in sorted((ROOT / "python").rglob("*.py")):
        if source.name == "native.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                direct_imports.extend(
                    f"{source.relative_to(ROOT)}:{node.lineno}"
                    for alias in node.names
                    if alias.name == "vfy._native"
                )
            elif isinstance(node, ast.ImportFrom):
                direct_import = (node.module == "_native" and node.level == 1) or (
                    node.level == 0
                    and node.module in {"vfy", "vfy._native"}
                    and (
                        node.module == "vfy._native"
                        or any(alias.name == "_native" for alias in node.names)
                    )
                )
                if direct_import:
                    direct_imports.append(f"{source.relative_to(ROOT)}:{node.lineno}")
    assert not direct_imports, (
        f"Python modules must use the explicit vfy.native export inventory: {direct_imports}"
    )


def _has_local_suppression_reason(lines: list[str], attribute_index: int) -> bool:
    """Accept a reason on the attribute or its immediately following comment."""

    for index in range(attribute_index, min(attribute_index + 12, len(lines))):
        line = lines[index]
        if "reason =" in line:
            return True
        if index == attribute_index and "//" in line:
            return True
        closing = line.find(")]")
        if closing < 0:
            continue
        if "//" in line[closing + 2 :]:
            return True
        for following in lines[index + 1 :]:
            stripped = following.strip()
            if not stripped:
                continue
            return stripped.startswith("//")
        return False
    return False


def test_production_lint_suppressions_have_local_reasons() -> None:
    missing: list[str] = []
    for source_root in sorted((ROOT / "crates").glob("*/src")):
        for source in sorted(source_root.rglob("*.rs")):
            if source.name == "tests.rs" or "tests" in source.parts:
                continue
            lines = source.read_text(encoding="utf-8").splitlines()
            for line_number, line in enumerate(lines, 1):
                if "#[allow(" not in line and "#[expect(" not in line:
                    continue
                if _has_local_suppression_reason(lines, line_number - 1):
                    continue
                missing.append(f"{source.relative_to(ROOT)}:{line_number}")
    assert not missing, f"production lint suppressions need local reasons: {missing}"


def test_python_suppressions_name_rules_and_explain_the_boundary() -> None:
    missing: list[str] = []
    for root in (ROOT / "python", ROOT / "scripts", ROOT / "benchmarks", ROOT / "tests"):
        for source in sorted(root.rglob("*.py*")):
            if source.suffix not in {".py", ".pyi"} or "fixtures" in source.parts:
                continue
            content = source.read_text(encoding="utf-8")
            lines = content.splitlines()
            for token in tokenize.generate_tokens(io.StringIO(content).readline):
                if token.type != tokenize.COMMENT:
                    continue
                suppression = re.match(
                    r"#\s*(?:noqa\b|ty:\s*ignore\b|type:\s*ignore\b)", token.string
                )
                if suppression is None:
                    continue
                named = re.match(
                    r"#\s*(?:noqa:\s*[A-Z]+\d+|ty:\s*ignore\[[a-z-]+(?:,\s*[a-z-]+)*\])",
                    token.string,
                )
                line_index = token.start[0] - 1
                neighboring = (
                    lines[max(0, line_index - 1) : line_index]
                    + lines[line_index + 1 : line_index + 2]
                )
                explained = " - " in token.string or any(
                    line.strip().startswith("# ") for line in neighboring
                )
                if not named or not explained:
                    missing.append(f"{source.relative_to(ROOT)}:{token.start[0]}")
    assert not missing, f"suppressions require a named rule and local reason: {missing}"


def test_mutation_adapter_delegates_engine_policy_and_commits() -> None:
    source = ROOT / "python" / "vfy" / "mutate.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    # Python may own temporary child-result directories, but Rust owns hashes
    # and the atomic persistence of mutation caches and reports.
    assert "hashlib" not in imports
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert attributes.isdisjoint({"rglob", "walk", "replace", "write_text", "write_bytes"}), (
        "mutation source discovery, cache interpretation, rendering, and commits belong in Rust"
    )
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "select_mutation_catalog",
        "mutation_contexts",
        "mutation_fingerprints",
        "mutation_baseline_valid",
        "write_mutation_baseline",
        "read_mutation_cache",
        "write_mutation_cache",
        "build_mutation_report",
        "render_mutation_report",
    } <= calls


def test_materialized_coverage_records_use_the_native_merge_transaction() -> None:
    tree = ast.parse((ROOT / "python" / "vfy" / "data.py").read_text(encoding="utf-8"))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "merge" not in {
        node.name for node in classes["FileCoverage"].body if isinstance(node, ast.FunctionDef)
    }
    merge = next(
        node
        for node in classes["CoverageStore"].body
        if isinstance(node, ast.FunctionDef) and node.name == "merge"
    )
    attributes = {node.attr for node in ast.walk(merge) if isinstance(node, ast.Attribute)}
    assert "merge_replace" in attributes
    assert attributes.isdisjoint({"update", "union", "add"})


def test_ci_workflows_reference_only_existing_python_tests() -> None:
    test_paths = {
        match
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml")
        for match in re.findall(
            r"(?<![\w/-])(tests/[^\s]+\.py)(?![\w])",
            workflow.read_text(encoding="utf-8"),
        )
    }
    missing = sorted(path for path in test_paths if not _workflow_test_reference_matches(path))
    assert not missing, f"CI references missing test files: {missing}"


@pytest.mark.skipif(os.name != "posix", reason="Native wheel CI runs on Linux and macOS")
def test_native_wheel_workflow_discovers_new_regression_suites(tmp_path: Path) -> None:
    workflow = (ROOT / ".github/workflows/native-pytest.yml").read_text(encoding="utf-8")
    command = re.search(
        r"      - name: Test native behavior through the installed vfy wheel\n"
        r"        run: \|\n((?:          [^\n]*\n)+)",
        workflow,
    )
    assert command is not None, "Native installed-wheel test command is missing"
    integration = tmp_path / "tests/integration"
    integration.mkdir(parents=True)
    for name in (
        "test_native_existing.py",
        "test_native_new_regression.py",
        "test_pytest_config_projection.py",
        "test_unrelated.py",
    ):
        (integration / name).touch()
    executables = tmp_path / "bin"
    executables.mkdir()
    uv = executables / "uv"
    # Record the arguments dispatched by the real workflow shell command.
    uv.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    uv.chmod(0o755)
    completed = subprocess.run(
        ["/bin/sh", "-c", textwrap.dedent(command.group(1))],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{executables}{os.pathsep}{os.defpath}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    selected = [line for line in completed.stdout.splitlines() if line.startswith("tests/")]
    assert sorted(selected) == [
        "tests/integration/test_native_existing.py",
        "tests/integration/test_native_new_regression.py",
        "tests/integration/test_pytest_config_projection.py",
    ], f"Native wheel CI omitted or duplicated a regression suite: {selected}"
