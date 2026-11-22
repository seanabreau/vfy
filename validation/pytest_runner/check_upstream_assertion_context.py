"""Compare pytest's assertion explanations on original 9.1.1 test cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject
from upstream_pytest import UPSTREAM_COMMIT, git_output, pytest_version

SOURCE_FILE = "testing/python/approx.py"
SELECTED = (
    "TestApprox::test_error_messages_native_dtypes",
    "TestApprox::test_mixed_sequence",
    "TestApprox::test_dict_for_div_by_zero",
    "TestApprox::test_dict_differing_lengths",
    "TestApprox::test_approx_dicts_with_mismatch_on_keys",
)
SELECTOR = " or ".join(name.rsplit("::", 1)[-1] for name in SELECTED)

RESTORATION_TESTS = """import pytest
import os
from pathlib import Path
from pytest import approx


def test_failure_does_not_skip_the_next_assertion_case():
    assert {"value": 1.0} == approx({"value": 2.0})


def test_assertion_context_is_reinstalled_after_a_failure():
    with pytest.raises(AssertionError) as error:
        assert {"value": 1.0} == approx({"value": 2.0})
    assert "comparison failed" in str(error.value)
    Path(os.environ["NATIVE_ASSERTION_MARKER"]).write_text("passed\\n", encoding="utf-8")
"""

RESTORATION_DRIVER = """import json
import sys
from pathlib import Path

import pytest
from _pytest.assertion import util


result = pytest.main([
    "-q", "--tb=no", "-c", __import__("os").devnull,
    "--rootdir", str(Path.cwd()), "-p", "no:cacheprovider", *sys.argv[1:], "test_restore.py",
])
if result != pytest.ExitCode.TESTS_FAILED:
    raise SystemExit(f"expected one intentional failure and one pass, got {result!r}")
if Path("second-case-passed").read_text(encoding="utf-8") != "passed\\n":
    raise SystemExit("pytest skipped the case after the intentional failure")
state = {
    "reprcompare_restored": util._reprcompare is None,
    "assertion_pass_restored": util._assertion_pass is None,
    "config_restored": util._config is None,
}
print("ASSERTION_STATE=" + json.dumps(state, sort_keys=True))
if not all(state.values()):
    raise SystemExit("pytest assertion globals were not restored after the failed run")
"""


def _check_source(upstream: Path) -> str:
    revision = git_output(upstream, "rev-parse", "HEAD")
    if revision != UPSTREAM_COMMIT:
        raise RuntimeError(f"pytest source must be pinned to {UPSTREAM_COMMIT}: {revision}")
    if git_output(upstream, "status", "--porcelain"):
        raise RuntimeError("pytest source checkout must be clean")
    source = upstream / SOURCE_FILE
    if not source.is_file():
        raise FileNotFoundError(f"pinned pytest source is missing: {source}")
    return hashlib.sha256(source.read_bytes()).hexdigest()


def _pytest_args(project: VfyProject, *, collect: bool, native: bool) -> list[str]:
    args = [
        "-m",
        "pytest",
        "-q",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
        "-p",
        "no:cacheprovider",
    ]
    if native:
        args.extend(("-p", "vfy._pytest.native_pytest"))
    if collect:
        args.append("--collect-only")
    args.extend(("-k", SELECTOR, "test_approx.py"))
    return args


def _nodeids(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("test_approx.py::")]


def _run_selected(
    project: VfyProject, name: str, python: Path, native_result: Path | None
) -> dict[str, object]:
    env = {"PYTEST_NATIVE_RESULT": str(native_result)} if native_result is not None else None
    collection = project.run(
        str(python), *_pytest_args(project, collect=True, native=native_result is not None), env=env
    )
    if collection.returncode != 0:
        raise RuntimeError(
            f"{name} collection failed with status {collection.returncode}:\n"
            f"{collection.stdout}\n{collection.stderr}"
        )
    identities = _nodeids(collection.stdout)
    expected = [f"test_approx.py::{name}" for name in SELECTED]
    if identities != expected:
        raise RuntimeError(f"{name} selected IDs differ: {identities}")

    run = project.run(
        str(python),
        *_pytest_args(project, collect=False, native=native_result is not None),
        env=env,
    )
    if run.returncode != 0 or "5 passed" not in run.stdout:
        raise RuntimeError(
            f"{name} selected cases did not pass: status={run.returncode}\n"
            f"{run.stdout}\n{run.stderr}"
        )
    result: dict[str, object] = {"nodeids": identities, "passed": 5}
    if native_result is not None:
        record = json.loads(native_result.read_text(encoding="utf-8"))
        cases = record.get("cases", [])
        if (
            record.get("engine") != "pytest-runner-native-v1"
            or [case.get("nodeid") for case in cases] != expected
            or any(
                [(phase.get("phase"), phase.get("outcome")) for phase in case.get("phases", [])]
                != [("setup", "passed"), ("call", "passed"), ("teardown", "passed")]
                for case in cases
            )
        ):
            raise RuntimeError(f"{name} native case phases differ: {record}")
        result["native_phases"] = [
            {"nodeid": case["nodeid"], "outcomes": [phase["outcome"] for phase in case["phases"]]}
            for case in cases
        ]
    return result


def _check_failure_restoration(
    root: Path, name: str, python: Path, native_result: Path | None
) -> dict[str, object]:
    project = VfyProject(root / f"restoration-{name}")
    project.write("test_restore.py", RESTORATION_TESTS)
    environment = {
        "NATIVE_ASSERTION_MARKER": str(project.root / "second-case-passed"),
        "PYTEST_NATIVE_RESULT": str(native_result) if native_result else None,
    }
    arguments = ("-p", "vfy._pytest.native_pytest") if native_result is not None else ()
    result = project.run(str(python), "-c", RESTORATION_DRIVER, *arguments, env=environment)
    expected_state = json.dumps(
        {
            "assertion_pass_restored": True,
            "config_restored": True,
            "reprcompare_restored": True,
        },
        sort_keys=True,
    )
    if result.returncode != 0 or f"ASSERTION_STATE={expected_state}" not in result.stdout:
        raise RuntimeError(
            f"{name} assertion context was not restored after a failed case:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    if native_result is not None:
        record = json.loads(native_result.read_text(encoding="utf-8"))
        cases = record.get("cases", [])
        outcomes = [
            phase["outcome"]
            for case in cases
            for phase in case.get("phases", [])
            if phase.get("phase") == "call"
        ]
        if record.get("engine") != "pytest-runner-native-v1" or outcomes != ["failed", "passed"]:
            raise RuntimeError(f"{name} failure/reuse outcomes differ: {record}")
    return {"call_outcomes": ["failed", "passed"], "assertion_globals_restored": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", required=True, type=Path)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    upstream = args.upstream_root.resolve()
    source_hash = _check_source(upstream)
    versions = {
        "stock": pytest_version(args.stock_python),
        "candidate": pytest_version(args.candidate_python),
    }
    if versions != {"stock": "9.1.1", "candidate": "9.1.1"}:
        raise RuntimeError(f"installed pytest versions differ from the pinned pair: {versions}")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    selected = VfyProject(output / "selected")
    selected.root.mkdir(parents=True, exist_ok=True)
    original = upstream / SOURCE_FILE
    shutil.copyfile(original, selected.root / "test_approx.py")
    if hashlib.sha256((selected.root / "test_approx.py").read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("the selected upstream source changed while it was copied")

    observations: dict[str, dict[str, object]] = {}
    native_result = output / "candidate-native-result.json"
    for name, python in (("stock", args.stock_python), ("candidate", args.candidate_python)):
        observations[name] = _run_selected(
            selected,
            name,
            python,
            native_result if name == "candidate" else None,
        )
    if observations["stock"]["nodeids"] != observations["candidate"]["nodeids"]:
        raise RuntimeError(f"stock/candidate selected IDs differ: {observations}")

    restoration: dict[str, object] = {}
    for name, python in (("stock", args.stock_python), ("candidate", args.candidate_python)):
        restoration[name] = _check_failure_restoration(
            output,
            name,
            python,
            output / f"{name}-restoration-native-result.json" if name == "candidate" else None,
        )

    report = {
        "upstream_revision": UPSTREAM_COMMIT,
        "source_file": SOURCE_FILE,
        "source_sha256": source_hash,
        "pytest_versions": versions,
        "selected_nodeids": observations["stock"]["nodeids"],
        "selected_cases": observations,
        "failure_restoration": restoration,
    }
    report_path = output / "assertion-context.json"
    descriptor, temporary_path = tempfile.mkstemp(prefix=f".{report_path.name}.", dir=output)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_path, report_path)
    finally:
        Path(temporary_path).unlink(missing_ok=True)
    sys.stdout.write(f"{report_path}\n")


if __name__ == "__main__":
    main()
