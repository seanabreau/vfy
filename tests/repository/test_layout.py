"""Guard the repository's test ownership and file-layout conventions."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
INLINE_TEST_MODULE = re.compile(r"(?m)^mod tests \{")
EXTERNAL_TEST_MODULE = re.compile(r"(?m)^mod tests;")


def test_rust_unit_suites_live_in_sibling_tests_modules() -> None:
    inline: list[str] = []
    missing: list[str] = []
    declared: set[Path] = set()

    source_roots = sorted((ROOT / "crates").glob("*/src"))
    for source_root in source_roots:
        for source in sorted(source_root.rglob("*.rs")):
            contents = source.read_text(encoding="utf-8")
            relative = source.relative_to(ROOT).as_posix()
            if INLINE_TEST_MODULE.search(contents):
                inline.append(relative)
            if EXTERNAL_TEST_MODULE.search(contents):
                tests = (
                    source.parent / "tests.rs"
                    if source.name == "lib.rs"
                    else source.with_suffix("") / "tests.rs"
                )
                declared.add(tests)
                if not tests.is_file():
                    missing.append(f"{relative} -> {tests.relative_to(ROOT).as_posix()}")

    orphaned = sorted(
        tests.relative_to(ROOT).as_posix()
        for source_root in source_roots
        for tests in source_root.rglob("tests.rs")
        if tests not in declared
    )
    assert not inline, f"Rust tests must not remain inline: {inline}"
    assert not missing, f"Rust test module declarations have no sibling file: {missing}"
    assert not orphaned, f"Rust sibling test files have no module declaration: {orphaned}"


def test_python_package_tests_use_pytest_names() -> None:
    package_tests = ROOT / "tests" / "integration"
    unexpected = sorted(
        path.relative_to(ROOT).as_posix()
        for path in package_tests.rglob("*.py")
        if path.name not in {"__init__.py", "conftest.py"} and not path.name.startswith("test_")
    )
    assert not unexpected, f"Python package tests must use test_*.py names: {unexpected}"
