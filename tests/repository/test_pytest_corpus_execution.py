"""Protect the full pytest corpus selection and checkout boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.support import VfyProject

ROOT = Path(__file__).resolve().parents[2]


def load_corpus_module() -> ModuleType:
    path = ROOT / "scripts" / "pytest_corpus_execution.py"
    spec = importlib.util.spec_from_file_location("pytest_corpus_execution", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def stub_corpus_preflight(
    corpus: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    classification: dict[str, Any],
    rejection: dict[str, Any],
    unsupported: list[dict[str, str]],
) -> tuple[Path, Path, Path, Path]:
    classification_path = tmp_path / "classification.json"
    rejection_path = tmp_path / "rejections.json"
    stock_python = tmp_path / "stock-python"
    candidate_python = tmp_path / "candidate-python"
    classification_path.write_text(json.dumps(classification), encoding="utf-8")
    rejection_path.write_text(json.dumps(rejection), encoding="utf-8")
    monkeypatch.setattr(
        corpus.pytest_execution_accounting,
        "validate_three_run_classification",
        lambda _: (unsupported, [], unsupported),
    )
    monkeypatch.setattr(corpus.pytest_execution_record, "validate_upstream", lambda *_: "commit")
    monkeypatch.setattr(corpus.pytest_execution_record, "resolve_executable", lambda path: path)
    monkeypatch.setattr(
        corpus.pytest_execution_record,
        "artifact_records",
        lambda paths, _: [
            {"filename": path.name, "sha256": "e" * 64}
            for path in sorted(paths, key=lambda item: item.name)
        ],
    )
    monkeypatch.setattr(
        corpus.pytest_execution_record,
        "verify_installed_artifacts",
        lambda _, artifacts, runner: [
            {"distribution": "pytest", "version": "9.1.1"}
            if runner == "pytest"
            else {"distribution": "vfy", "version": "0.1.0"}
        ],
    )
    monkeypatch.setattr(
        corpus.pytest_execution_record, "validate_runner_artifacts", lambda *_: None
    )
    environment = {
        "schema_version": 2,
        "python": "3.14.7",
        "platform": "linux-x86_64",
        "packages": [
            {"name": "vfy", "version": "0.1.0"},
            {"name": "pytest", "version": "9.1.1"},
        ],
        "pytest_plugins": [],
    }
    monkeypatch.setattr(
        corpus.pytest_execution_record, "resolved_environment", lambda _: environment
    )
    monkeypatch.setattr(
        corpus.pytest_execution_record,
        "interpreter_metadata",
        lambda *_: {"python": "3.14.7", "platform": "linux-x86_64"},
    )
    return classification_path, rejection_path, stock_python, candidate_python


def test_candidate_only_deselects_explicitly_unsupported_nodeids() -> None:
    corpus = load_corpus_module()
    classifications = [
        {"nodeid": "testing/test_one.py::test_supported", "disposition": "parity-required"},
        {
            "nodeid": "testing/test_two.py::test_rejected",
            "disposition": "deliberately-unsupported",
        },
        {
            "nodeid": "testing/test_three.py::test_platform_skip",
            "disposition": "reference/platform-skipped",
        },
    ]

    assert corpus.candidate_arguments(classifications) == [
        "-q",
        "--runpytest=inprocess",
        "--confcutdir=<CORPUS_PARENT>",
        "--deselect=testing/test_two.py::test_rejected",
    ]


def test_existing_corpus_output_is_never_replaced(tmp_path: Path) -> None:
    corpus = load_corpus_module()
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "candidate-execution-record.json"
    sentinel.write_text("preserve prior evidence\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        corpus.create_fresh_output_directory(output)

    assert sentinel.read_text(encoding="utf-8") == "preserve prior evidence\n"


def test_failed_stock_baseline_stops_before_ancestor_config_or_controlled_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = load_corpus_module()
    classification = {
        "inventory": {"upstream_commit": "a" * 40, "sha256": "b" * 64},
        "policy": {"sha256": "c" * 64},
    }
    classification_path, rejection_path, stock_python, candidate_python = stub_corpus_preflight(
        corpus, monkeypatch, tmp_path, classification, {}, []
    )
    monkeypatch.setattr(
        corpus.pytest_execution_accounting,
        "validate_rejection_envelope",
        lambda *_args, **_kwargs: {
            "rejections": [],
            "plugin_proofs": [],
            "hypothesis_lifecycle_proof": [],
        },
    )
    monkeypatch.setattr(
        corpus.pytest_execution_accounting, "validate_rejection_rows", lambda *_: None
    )
    monkeypatch.setattr(
        corpus.pytest_execution_accounting, "validate_plugin_proofs", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(corpus, "clone_pristine_upstream", lambda *args: None)
    calls: list[str] = []

    def failed_baseline(**kwargs: object) -> dict[str, int]:
        calls.append(str(kwargs["plugin_autoload"]))
        return {"exit_status": 3}

    monkeypatch.setattr(corpus.pytest_execution_record, "record_run", failed_baseline)
    output = tmp_path / "evidence"
    with pytest.raises(ValueError, match="full stock baseline must exit successfully"):
        corpus.execute_corpus(
            upstream_root=tmp_path / "upstream",
            classification_path=classification_path,
            stock_python=stock_python,
            candidate_python=candidate_python,
            stock_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            candidate_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            output=output,
            rejection_path=rejection_path,
        )

    assert calls == ["enabled"]
    assert json.loads((output / "stock-execution-record.json").read_text()) == {"exit_status": 3}
    assert list(output.rglob("conftest.py")) == []


def test_invalid_upstream_is_rejected_before_creating_corpus_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = load_corpus_module()
    classification = {
        "inventory": {"upstream_commit": "a" * 40, "sha256": "b" * 64},
        "policy": {"sha256": "c" * 64},
    }
    classification_path = tmp_path / "classification.json"
    output = tmp_path / "evidence"
    classification_path.write_text(json.dumps(classification), encoding="utf-8")
    monkeypatch.setattr(
        corpus.pytest_execution_accounting,
        "validate_three_run_classification",
        lambda _: ([], [], []),
    )

    def reject_upstream(*_: object) -> str:
        raise ValueError("upstream checkout must be clean")

    monkeypatch.setattr(corpus.pytest_execution_record, "validate_upstream", reject_upstream)
    with pytest.raises(ValueError, match="upstream checkout must be clean"):
        corpus.execute_corpus(
            upstream_root=tmp_path / "upstream",
            classification_path=classification_path,
            stock_python=tmp_path / "stock-python",
            candidate_python=tmp_path / "candidate-python",
            stock_artifacts=[],
            candidate_artifacts=[],
            output=output,
            rejection_path=tmp_path / "rejections.json",
        )

    assert not output.exists()


def test_malformed_rejection_record_is_rejected_before_creating_corpus_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = load_corpus_module()
    unsupported = [
        {
            "nodeid": "testing/test_plugin.py::test_unsupported",
            "disposition": "deliberately-unsupported",
            "rejection_proof": (
                "validation/pytest_runner/check_native_rejections.py#hypothesis-plugin"
            ),
        }
    ]
    classification = {
        "inventory": {"upstream_commit": "a" * 40, "sha256": "b" * 64},
        "policy": {"sha256": "c" * 64},
    }
    classification_path, rejection_path, stock_python, candidate_python = stub_corpus_preflight(
        corpus, monkeypatch, tmp_path, classification, {"schema_version": 2}, unsupported
    )
    output = tmp_path / "evidence"

    with pytest.raises(ValueError, match="rejection record has an invalid schema"):
        corpus.execute_corpus(
            upstream_root=tmp_path / "upstream",
            classification_path=classification_path,
            stock_python=stock_python,
            candidate_python=candidate_python,
            stock_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            candidate_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            output=output,
            rejection_path=rejection_path,
        )

    assert not output.exists()


def test_rejection_provenance_mismatch_is_rejected_before_creating_corpus_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = load_corpus_module()
    unsupported = [
        {
            "nodeid": "testing/test_plugin.py::test_unsupported",
            "disposition": "deliberately-unsupported",
            "rejection_proof": (
                "validation/pytest_runner/check_native_rejections.py#hypothesis-plugin"
            ),
        }
    ]
    classification = {
        "inventory": {"upstream_commit": "a" * 40, "sha256": "b" * 64},
        "policy": {"sha256": "c" * 64},
    }
    rejection = {
        "schema_version": 3,
        "provenance": {
            "upstream_commit": "d" * 40,
            "inventory_sha256": "b" * 64,
            "policy_sha256": "c" * 64,
            "python": "3.14.7",
            "platform": "linux-x86_64",
            "stock_runner_artifact_set_sha256": "e" * 64,
            "stock_resolved_environment_sha256": "f" * 64,
            "runner_artifact_set_sha256": "e" * 64,
            "resolved_environment_sha256": "f" * 64,
            "project_identity_sha256": "1" * 64,
            "checker_artifacts": [],
        },
        "rejections": [],
        "plugin_proofs": [],
        "hypothesis_lifecycle_proof": None,
    }
    classification_path, rejection_path, stock_python, candidate_python = stub_corpus_preflight(
        corpus, monkeypatch, tmp_path, classification, rejection, unsupported
    )
    output = tmp_path / "evidence"

    with pytest.raises(
        ValueError, match="rejection evidence provenance differs for upstream_commit"
    ):
        corpus.execute_corpus(
            upstream_root=tmp_path / "upstream",
            classification_path=classification_path,
            stock_python=stock_python,
            candidate_python=candidate_python,
            stock_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            candidate_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            output=output,
            rejection_path=rejection_path,
        )

    assert not output.exists()


def test_environment_mismatch_is_rejected_before_creating_corpus_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = load_corpus_module()
    classification = {
        "inventory": {"upstream_commit": "a" * 40, "sha256": "b" * 64},
        "policy": {"sha256": "c" * 64},
    }
    classification_path, rejection_path, stock_python, candidate_python = stub_corpus_preflight(
        corpus, monkeypatch, tmp_path, classification, {}, []
    )
    resolved_environment = corpus.pytest_execution_record.resolved_environment

    def different_candidate_environment(python: Path) -> dict[str, Any]:
        environment = resolved_environment(python)
        if python == candidate_python:
            return environment | {"python": "3.14.8"}
        return environment

    monkeypatch.setattr(
        corpus.pytest_execution_record,
        "resolved_environment",
        different_candidate_environment,
    )
    output = tmp_path / "evidence"

    with pytest.raises(ValueError, match="three-run resolved dependency environments differ"):
        corpus.execute_corpus(
            upstream_root=tmp_path / "upstream",
            classification_path=classification_path,
            stock_python=stock_python,
            candidate_python=candidate_python,
            stock_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            candidate_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            output=output,
            rejection_path=rejection_path,
        )

    assert not output.exists()


def test_runner_artifact_mismatch_is_rejected_before_creating_corpus_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = load_corpus_module()
    classification = {
        "inventory": {"upstream_commit": "a" * 40, "sha256": "b" * 64},
        "policy": {"sha256": "c" * 64},
    }
    classification_path, rejection_path, stock_python, candidate_python = stub_corpus_preflight(
        corpus, monkeypatch, tmp_path, classification, {}, []
    )
    output = tmp_path / "evidence"

    with pytest.raises(ValueError, match="three-run runner artifact sets differ"):
        corpus.execute_corpus(
            upstream_root=tmp_path / "upstream",
            classification_path=classification_path,
            stock_python=stock_python,
            candidate_python=candidate_python,
            stock_artifacts=[tmp_path / "pytest.whl"],
            candidate_artifacts=[tmp_path / "vfy.whl", tmp_path / "pytest.whl"],
            output=output,
            rejection_path=rejection_path,
        )

    assert not output.exists()


@pytest.mark.skipif(os.name != "posix", reason="native corpus execution requires Unix")
def test_paired_corpus_supports_pytester_inprocess_arguments(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "upstream"
    project = VfyProject(project_root)
    (tmp_path / "conftest.py").write_bytes(
        (ROOT / "scripts" / "pytest_corpus_conftest.py").read_bytes()
    )
    project.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\naddopts = ["-p", "pytester"]\n',
    )
    project.write(
        "testing/test_warnings.py",
        """
def test_warning_recorded_hook(pytester):
    result = pytester.runpytest("--version", no_reraise_ctrlc=True)
    assert result.ret == 0
""",
    )

    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        "--runpytest=inprocess",
        f"--confcutdir={tmp_path}",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("engine", ["pytest", "pytest-native"])
@pytest.mark.skipif(os.name != "posix", reason="the corpus process-state proof uses Unix symlinks")
def test_ancestor_config_preserves_parent_state_and_restores_inprocess(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path / "upstream")
    (tmp_path / "conftest.py").write_bytes(
        (ROOT / "scripts" / "pytest_corpus_conftest.py").read_bytes()
    )
    project.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\naddopts = ["-p", "pytester"]\n'
        'python_files = ["*_test.py", "*.py"]\n',
    )
    project.write(
        "testing/acceptance_test.py",
        r"""
import os

class TestGeneralUsage:
    def test_issue93_initialnode_importing_capturing(self, pytester):
        pytester.makeconftest('print("initial conftest output")')
        result = pytester.runpytest()
        assert result.ret == 5
        result.stdout.no_fnmatch_line("*initial conftest output*")

class TestInvocationVariants:
    def test_cmdline_python_package_symlink(self, pytester, monkeypatch):
        library = pytester.mkdir("lib")
        package = library / "corpus_package"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "test_package.py").write_text("def test_package(): pass\n")
        symlink_root = pytester.mkdir("symlink_root")
        link = symlink_root / "lib"
        link.symlink_to(library, target_is_directory=True)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(library), str(link))))
        monkeypatch.syspath_prepend(str(library))
        monkeypatch.syspath_prepend(str(link))
        result = pytester.runpytest("--pyargs", "-v", "corpus_package")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines([
            "symlink_root/lib/corpus_package/test_package.py::test_package PASSED*"
        ])
""",
    )
    project.write(
        "testing/python/fixtures.py",
        r"""
class TestParameterizedSubRequest:
    def test_non_relative_path(self, pytester, monkeypatch):
        fixtures = pytester.mkdir("fixtures")
        (fixtures / "corpus_fixture.py").write_text(
            "import pytest\n@pytest.fixture\ndef inherited_fixture(): return 42\n"
        )
        tests = pytester.mkdir("tests")
        (tests / "test_inherited.py").write_text(
            "from corpus_fixture import inherited_fixture\n"
            "def test_inherited(inherited_fixture): assert inherited_fixture == 42\n"
        )
        monkeypatch.chdir(tests)
        pytester.syspathinsert(fixtures)
        pytester.runpytest().assert_outcomes(passed=1)
""",
    )
    project.write(
        "testing/test_junitxml.py",
        r"""
import os

def _assert_child(pytester, monkeypatch):
    monkeypatch.setenv("CORPUS_PARENT_PID", str(os.getpid()))
    pytester.makepyfile(
        "import os\ndef test_child():\n"
        "    assert os.getpid() != int(os.environ['CORPUS_PARENT_PID'])\n"
    )
    pytester.runpytest().assert_outcomes(passed=1)

def test_random_report_log_xdist(pytester, monkeypatch):
    _assert_child(pytester, monkeypatch)

def test_runs_twice_xdist(pytester, monkeypatch):
    _assert_child(pytester, monkeypatch)
""",
    )
    project.write(
        "testing/test_runner.py",
        r"""
import inspect

def test_makereport_getsource_dynamic_code(pytester, monkeypatch):
    original_findsource = inspect.findsource
    calls = []

    def findsource(obj):
        if getattr(obj, "__name__", None) == "foo":
            calls.append(obj)
            raise IndexError()
        return original_findsource(obj)

    monkeypatch.setattr(inspect, "findsource", findsource)
    pytester.makepyfile(
        "import pytest\n@pytest.fixture\ndef foo(missing): pass\n"
        "def test_fix(foo): pass\n"
    )
    result = pytester.runpytest("-vv")
    result.assert_outcomes(errors=1)
    result.stdout.no_fnmatch_line("*INTERNALERROR*")
    assert calls, "nested pytest must exercise the parent's patched source lookup"
""",
    )
    project.write(
        "testing/test_terminal.py",
        """
from test_junitxml import _assert_child

class TestTerminalFunctional:
    def test_header_trailer_info(self, pytester, monkeypatch):
        _assert_child(pytester, monkeypatch)
    def test_no_header_trailer_info(self, pytester, monkeypatch):
        _assert_child(pytester, monkeypatch)

class TestProgressOutputStyle:
    def test_xdist_normal(self, pytester, monkeypatch):
        _assert_child(pytester, monkeypatch)
    def test_xdist_normal_count(self, pytester, monkeypatch):
        _assert_child(pytester, monkeypatch)
    def test_xdist_verbose(self, pytester, monkeypatch):
        _assert_child(pytester, monkeypatch)
    def test_xdist_times(self, pytester, monkeypatch):
        _assert_child(pytester, monkeypatch)

class TestProgressWithTeardown:
    def test_xdist_normal(self, pytester, monkeypatch):
        _assert_child(pytester, monkeypatch)
""",
    )
    project.write(
        "testing/test_z_after.py",
        r"""
import os

def test_unlisted_case_stays_in_the_parent_process(pytester, monkeypatch):
    monkeypatch.setenv("CORPUS_PARENT_PID", str(os.getpid()))
    pytester.makepyfile(
        "import os\ndef test_parent():\n"
        "    assert os.getpid() == int(os.environ['CORPUS_PARENT_PID'])\n"
    )
    pytester.runpytest().assert_outcomes(passed=1)
""",
    )
    arguments = ("--runpytest=inprocess", f"--confcutdir={tmp_path}", "-q")
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = (
        project.run_pytest(*arguments, env=environment)
        if engine == "pytest"
        else project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=1",
            "--no-cov",
            *arguments,
            env=environment,
        )
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "14 passed" in result.stdout


def test_full_corpus_requires_a_clean_pinned_source_checkout(tmp_path: Path) -> None:
    corpus = load_corpus_module()
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "vfy test")
    git(source, "config", "user.email", "vfy@example.invalid")
    (source / "test_sample.py").write_text("def test_sample(): pass\n", encoding="utf-8")
    git(source, "add", "test_sample.py")
    git(source, "commit", "-qm", "pinned test source")
    revision = git(source, "rev-parse", "HEAD")

    destination = tmp_path / "copy"
    corpus.clone_pristine_upstream(source, destination, revision)

    assert git(destination, "rev-parse", "HEAD") == revision
    assert git(destination, "status", "--porcelain") == ""
    assert destination != source

    (source / "local-change.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be clean and pristine"):
        corpus.clone_pristine_upstream(source, tmp_path / "dirty-copy", revision)
