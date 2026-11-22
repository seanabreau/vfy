"""Build a deterministic inventory for the pinned pytest test corpus."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

PINNED_COMMIT = "cf470ec0bf7eb89cd97dd56df4859eae5db46447"
PINNED_VERSION = "9.1.1"
AREA_BY_BASENAME = {
    "acceptance_test.py": "acceptance-and-public-workflows",
    "deprecated_test.py": "deprecated-api-contracts",
    "approx.py": "python-api-and-assertions",
    "collect.py": "collection-and-fixtures",
    "fixtures.py": "collection-and-fixtures",
    "integration.py": "python-api-and-integration",
    "metafunc.py": "collection-and-parametrization",
    "raises.py": "python-api-and-assertions",
    "raises_group.py": "python-api-and-assertions",
    "show_fixtures_per_test.py": "fixtures-and-introspection",
    "test_argcomplete.py": "command-line-and-entry-points",
    "test_assertion.py": "assertions-and-rewriting",
    "test_assertrewrite.py": "assertions-and-rewriting",
    "test_cacheprovider.py": "cache-and-persistent-state",
    "test_capture.py": "capture-and-streams",
    "test_collect_imported_tests.py": "collection-and-imports",
    "test_collection.py": "collection-and-imports",
    "test_compat.py": "compatibility-and-paths",
    "test_config.py": "configuration-and-plugin-hooks",
    "test_conftest.py": "configuration-and-plugin-hooks",
    "test_debugging.py": "debugging-and-failures",
    "test_doctest.py": "doctest-and-collection",
    "test_entry_points.py": "command-line-and-entry-points",
    "test_error_diffs.py": "assertions-and-rewriting",
    "test_faulthandler.py": "debugging-and-failures",
    "test_findpaths.py": "collection-and-paths",
    "test_helpconfig.py": "command-line-and-entry-points",
    "test_junitxml.py": "reporting-and-output",
    "test_legacypath.py": "compatibility-and-paths",
    "test_link_resolve.py": "compatibility-and-paths",
    "test_main.py": "command-line-and-session",
    "test_mark.py": "marks-and-selection",
    "test_mark_expression.py": "marks-and-selection",
    "test_meta.py": "configuration-and-plugin-hooks",
    "test_monkeypatch.py": "fixtures-and-state-isolation",
    "test_nodes.py": "collection-and-node-identity",
    "test_parseopt.py": "command-line-and-entry-points",
    "test_pastebin.py": "reporting-and-output",
    "test_pathlib.py": "compatibility-and-paths",
    "test_pluginmanager.py": "configuration-and-plugin-hooks",
    "test_pytester.py": "pytester-and-test-infrastructure",
    "test_python_path.py": "configuration-and-imports",
    "test_recwarn.py": "warnings-and-exceptions",
    "test_reports.py": "reporting-and-output",
    "test_runner.py": "runtest-lifecycle-and-outcomes",
    "test_runner_xunit.py": "runtest-lifecycle-and-unittest",
    "test_scope.py": "fixtures-and-lifecycle",
    "test_session.py": "collection-and-session",
    "test_setuponly.py": "fixtures-and-lifecycle",
    "test_setupplan.py": "fixtures-and-lifecycle",
    "test_skipping.py": "marks-and-selection",
    "test_stash.py": "cache-and-persistent-state",
    "test_stepwise.py": "marks-and-selection",
    "test_subtests.py": "runtest-lifecycle-and-outcomes",
    "test_terminal.py": "reporting-and-output",
    "test_threadexception.py": "warnings-and-exceptions",
    "test_tmpdir.py": "fixtures-and-filesystem",
    "test_unittest.py": "runtest-lifecycle-and-unittest",
    "test_unraisableexception.py": "warnings-and-exceptions",
    "test_warning_types.py": "warnings-and-exceptions",
    "test_warnings.py": "warnings-and-exceptions",
    "test_code.py": "code-and-exception-introspection",
    "test_excinfo.py": "code-and-exception-introspection",
    "test_source.py": "code-and-exception-introspection",
    "test_local.py": "compatibility-and-paths",
    "test_pprint.py": "terminal-and-text-output",
    "test_saferepr.py": "terminal-and-text-output",
    "test_terminalwriter.py": "terminal-and-text-output",
    "test_wcwidth.py": "terminal-and-text-output",
    "test_fixture.py": "logging-and-capture",
    "test_formatter.py": "logging-and-capture",
    "test_reporting.py": "logging-and-capture",
    "test_issue519.py": "acceptance-and-public-workflows",
    "test_trivial.py": "packaging-and-frozen-artifacts",
}

CORE_FIXTURES = {
    "capfd",
    "capfdbinary",
    "capsys",
    "capsysbinary",
    "caplog",
    "doctest_namespace",
    "monkeypatch",
    "pytestconfig",
    "pytester",
    "record_property",
    "record_testsuite_property",
    "recwarn",
    "request",
    "tmp_path",
    "tmp_path_factory",
    "tmpdir",
    "tmpdir_factory",
}

AREA_FEATURES = {
    "async": re.compile(r"\b(asyncio|async def|asynctest|pytest\.mark\.asyncio|\bawait\b)"),
    "cache": re.compile(r"\b(cache|cacheprovider|lastfailed|stepwise)\b"),
    "capture": re.compile(r"\b(capsys|capfd|capture|CaptureFixture)\b"),
    "collection": re.compile(r"\b(collection|collect|collector|nodeid|Item|Collector)\b"),
    "configuration": re.compile(r"\b(Config|ini|pyproject|pytest\.ini|addopts|rootpath)\b"),
    "fixtures": re.compile(r"\b(fixture|FixtureDef|FixtureRequest|request\.getfixturevalue)\b"),
    "hooks": re.compile(r"\bpytest_[a-z][a-z0-9_]*\b"),
    "plugins": re.compile(r"\b(plugin|pluggy|pytest_plugins|importorskip)\b"),
    "reporting": re.compile(r"\b(report|terminal|junit|TestReport|reportinfo)\b"),
    "warnings": re.compile(r"\b(warning|recwarn|pytest\.warns|pytest\.deprecated_call)\b"),
    "xdist": re.compile(r"\b(xdist|worker|\-n[ =]?(?:auto|\d+))\b"),
}


def git_value(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def selected_core_modules(root: Path, config: dict[str, Any]) -> list[Path]:
    pytest_config = config["tool"]["pytest"]
    testpaths = pytest_config["testpaths"]
    python_files = pytest_config["python_files"]
    ignored = pytest_config["norecursedirs"]
    selected: list[Path] = []
    for testpath in testpaths:
        base = root / testpath
        for path in base.rglob("*.py"):
            rel = relative(path, root)
            parts = Path(rel).parts
            if any(part.startswith(".") for part in parts):
                continue
            ancestor_paths = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
            if any(
                fnmatch.fnmatch(candidate, pattern)
                for candidate in [*parts, *ancestor_paths]
                for pattern in ignored
            ):
                continue
            name_match = any(fnmatch.fnmatch(path.name, pattern) for pattern in python_files)
            special_path_match = any(fnmatch.fnmatch(rel, pattern) for pattern in python_files)
            if name_match or special_path_match:
                selected.append(path)
    return sorted(set(selected))


def selected_doctest_files(root: Path, config: dict[str, Any]) -> list[Path]:
    """Return text doctest files selected by pytest's default ``test*.txt`` glob."""
    pytest_config = config["tool"]["pytest"]
    selected: list[Path] = []
    for testpath in pytest_config["testpaths"]:
        base = root / testpath
        for path in base.rglob("test*.txt"):
            rel = relative(path, root)
            parts = Path(rel).parts
            ancestor_paths = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
            if any(part.startswith(".") for part in parts):
                continue
            if any(
                fnmatch.fnmatch(candidate, pattern)
                for candidate in [*parts, *ancestor_paths]
                for pattern in pytest_config["norecursedirs"]
            ):
                continue
            selected.append(path)
    return sorted(selected)


def test_definition_count(tree: ast.AST) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    )


def module_area(path: Path, root: Path) -> str:
    basename = path.name
    if basename not in AREA_BY_BASENAME:
        raise ValueError(f"No category assigned for {relative(path, root)}")
    return AREA_BY_BASENAME[basename]


def module_entry(path: Path, root: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    source = raw.decode("utf-8")
    tree = ast.parse(source, filename=relative(path, root))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    non_stdlib = sorted(imports - sys.stdlib_module_names - {"__future__"})
    pytester_calls = sorted(
        {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pytester"
        }
    )
    hook_names = sorted(set(AREA_FEATURES["hooks"].findall(source)))
    fixture_args: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            fixture_args.update(arg.arg for arg in node.args.args if arg.arg in CORE_FIXTURES)
    optional_imports = sorted(
        {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pytest"
            and node.func.attr == "importorskip"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
    )
    markers = sorted(set(re.findall(r"pytest\.mark\.([A-Za-z_]\w*)", source)))
    features = sorted(name for name, pattern in AREA_FEATURES.items() if pattern.search(source))
    if "pytester" in source or "Pytester" in source:
        features.append("pytester")
    return {
        "path": relative(path, root),
        "area": module_area(path, root),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "lines": len(source.splitlines()),
        "test_definitions": test_definition_count(tree),
        "features": sorted(set(features)),
        "core_fixture_arguments": sorted(fixture_args),
        "pytester_methods": pytester_calls,
        "hook_names_referenced": hook_names,
        "pytest_markers_referenced": markers,
        "imported_packages": non_stdlib,
        "optional_imports_skipped": optional_imports,
    }


def file_entry(path: Path, root: Path, kind: str) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "path": relative(path, root),
        "kind": kind,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_collection_log(log_path: Path, expected_sources: set[str]) -> dict[str, Any]:
    text = log_path.read_text(encoding="utf-8")
    nodeids = [line.strip() for line in text.splitlines() if "::" in line]
    collected_sources = {nodeid.split("::", 1)[0] for nodeid in nodeids}
    count_match = re.search(r"(?m)^(\d+) tests? collected in ", text)
    if count_match is None:
        raise ValueError(f"no pytest collection summary found in {log_path}")
    collected_items = int(count_match.group(1))
    if len(nodeids) != collected_items:
        raise ValueError(
            f"collection log contains {len(nodeids)} node IDs, summary reports {collected_items}"
        )
    duplicate_nodeids = sorted(nodeid for nodeid, count in Counter(nodeids).items() if count > 1)
    if duplicate_nodeids:
        raise ValueError(f"collection log contains duplicate node IDs: {duplicate_nodeids}")
    missing = sorted(collected_sources - expected_sources)
    uncollected = sorted(expected_sources - collected_sources)
    if missing or uncollected:
        raise ValueError(
            f"collection sources differ from the map: unexpected={missing}, absent={uncollected}"
        )
    return {
        "local_log_name": log_path.name,
        "collected_items": collected_items,
        "source_file_count": len(collected_sources),
        "mapped_source_file_count": len(expected_sources),
        "all_collected_sources_mapped": True,
        "ordered_nodeids_sha256": hashlib.sha256(
            (json.dumps(nodeids, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
        "ordered_nodeids": nodeids,
    }


def validate_collection_identity(
    collection: dict[str, Any], upstream_commit: str, identity: dict[str, Any]
) -> None:
    if identity.get("schema_version") != 1:
        raise ValueError("unsupported pytest collection identity schema")
    if identity.get("upstream_commit") != upstream_commit:
        raise ValueError("pytest collection identity commit does not match the source checkout")
    expected_count = identity.get("node_count")
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        raise ValueError("pytest collection identity node_count must be an integer")
    if collection["collected_items"] != expected_count:
        raise ValueError(
            "pytest collection node count differs from the reviewed identity: "
            f"expected {expected_count}, found {collection['collected_items']}"
        )
    expected_digest = identity.get("ordered_nodeids_sha256")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise ValueError("pytest collection identity needs an ordered-nodeid SHA-256")
    if collection["ordered_nodeids_sha256"] != expected_digest:
        raise ValueError("pytest ordered node IDs differ from the reviewed collection identity")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pytest-root", required=True, type=Path)
    parser.add_argument("--collect-log", type=Path)
    parser.add_argument("--identity-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.collect_log and not args.identity_manifest:
        parser.error("--collect-log requires --identity-manifest")
    if args.identity_manifest and not args.collect_log:
        parser.error("--identity-manifest requires --collect-log")

    root = args.pytest_root.resolve()
    commit = git_value(root, "rev-parse", "HEAD")
    if commit != PINNED_COMMIT:
        parser.error(f"expected pytest commit {PINNED_COMMIT}, found {commit}")
    if git_value(root, "status", "--porcelain"):
        parser.error("pinned pytest source checkout must be clean")
    pyproject_path = root / "pyproject.toml"
    config = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    modules = [module_entry(path, root) for path in selected_core_modules(root, config)]
    doctest_files = [
        file_entry(path, root, "core-suite-text-doctest")
        for path in selected_doctest_files(root, config)
    ]
    collection_log_check = None
    if args.collect_log:
        expected_sources = {entry["path"] for entry in [*modules, *doctest_files]}
        collection_log_check = validate_collection_log(args.collect_log, expected_sources)
        validate_collection_identity(
            collection_log_check,
            commit,
            json.loads(args.identity_manifest.read_text(encoding="utf-8")),
        )
    fixture_files = [
        file_entry(path, root, "pytester-example-source")
        for path in sorted((root / "testing/example_scripts").rglob("*"))
        if path.is_file()
    ]
    plugin_files = [
        file_entry(path, root, "separate-plugin-integration-source")
        for path in sorted((root / "testing/plugins_integration").glob("*.py"))
    ]
    requirements = []
    requirements_path = root / "testing/plugins_integration/requirements.txt"
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            requirements.append(stripped)
    all_features: dict[str, list[str]] = {}
    for module in modules:
        for feature in module["features"]:
            all_features.setdefault(feature, []).append(module["path"])
    output = {
        "schema_version": 1,
        "upstream": {
            "project": "pytest",
            "version": PINNED_VERSION,
            "repository": "https://github.com/pytest-dev/pytest",
            "commit": commit,
            "source_checkout": "External checkout supplied with --pytest-root.",
            "source_checkout_clean": True,
        },
        "core_suite": {
            "selection_config": "pyproject.toml [tool.pytest]",
            "testpaths": config["tool"]["pytest"]["testpaths"],
            "python_files": config["tool"]["pytest"]["python_files"],
            "norecursedirs": config["tool"]["pytest"]["norecursedirs"],
            "default_plugin_args": config["tool"]["pytest"]["addopts"],
            "python_module_count": len(modules),
            "text_doctest_file_count": len(doctest_files),
            "collected_source_count": len(modules) + len(doctest_files),
            "modules": modules,
            "text_doctest_files": doctest_files,
        },
        "subsystems": dict(sorted(all_features.items())),
        "separate_lanes": [
            {
                "name": "plugin reverse-dependency integrations",
                "entrypoint": "tox -e plugins",
                "config_path": "tox.ini [testenv:plugins]",
                "source_path": "testing/plugins_integration/",
                "requirements_path": "testing/plugins_integration/requirements.txt",
                "requirements": requirements,
                "python_sources": plugin_files,
                "scope_note": (
                    "Separate tox lane; excluded from core testpaths by norecursedirs and not all "
                    "files are test modules."
                ),
            },
            {
                "name": "documentation doctests",
                "entrypoint": "tox -e doctesting (pytest --doctest-modules ... --pyargs _pytest)",
                "config_path": "tox.ini [testenv] doctesting commands",
                "source_path": "doc/en/",
                "support_dependencies": ["PyYAML", "regendoc", "Sphinx"],
                "scope_note": "Separate tox lane, not part of core testing/ collection.",
            },
            {
                "name": "frozen executable smoke test",
                "entrypoint": "tox -e py310-freeze",
                "config_path": "tox.ini [testenv:py310-freeze]",
                "source_path": "testing/freeze/",
                "scope_note": (
                    "Builds and runs a frozen pytest executable; "
                    "testing/freeze/tests/test_trivial.py is also selected by the core testpaths."
                ),
            },
            {
                "name": "optional dependency and platform variants",
                "entrypoint": (
                    "tox -e py310-{pexpect,xdist,twisted24,twisted25,asynctest,numpy,"
                    "pluggymain,pylib}, py311-exceptiongroup"
                ),
                "config_path": "tox.ini [testenv] factors",
                "source_paths": [
                    "testing/test_unittest.py",
                    "testing/test_terminal.py",
                    "testing/test_warnings.py",
                    "testing/test_capture.py",
                ],
                "scope_note": (
                    "Most variants rerun the core suite with optional packages or alternate "
                    "dependency versions; unittest extras target testing/test_unittest.py."
                ),
            },
        ],
        "pytester_examples": {
            "config_path": "pyproject.toml [tool.pytest].pytester_example_dir",
            "root": "testing/example_scripts/",
            "excluded_from_core_collection": True,
            "files": fixture_files,
        },
        "support_files": [
            file_entry(root / "testing/conftest.py", root, "core-suite-conftest"),
            file_entry(pyproject_path, root, "suite-selection-and-defaults"),
            file_entry(root / "tox.ini", root, "tox-lane-definition"),
            file_entry(requirements_path, root, "plugin-integration-requirements"),
        ],
    }
    if collection_log_check is not None:
        output["core_suite"]["collection"] = collection_log_check
    rendered = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
