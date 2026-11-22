"""Semantic and artifact gates for unchanged-project mutation comparisons."""

from __future__ import annotations

import importlib.metadata
import json
import sys
from argparse import Namespace
from functools import partial
from pathlib import Path

import pytest
from benchmarks.benchmark_adopter_mutation import (
    VfyEngine,
    baseline,
    baseline_coverage_policy,
    copy_project,
    declared_scope,
    environment,
    full_suite_keys,
    junit_outcomes,
    locked_versions,
    source_files,
    validate_mutation_baseline,
    validated_reference_signature,
)
from benchmarks.benchmark_mutation import (
    MUTMUT_STATUS_BY_EXIT,
    MutmutSample,
    _KillerProofs,
    _validated_mutmut_signature,
    measured_vfy,
)

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject
from vfy.mutate import Mutation
from vfy.native import analyze_mutations

_MUTATION_ENGINES = (
    pytest.param("pytest-native", marks=requires_native_pytest),
    pytest.param(
        "native",
        marks=pytest.mark.skipif(
            sys.platform != "linux", reason="independent mutations require Linux"
        ),
    ),
)


def test_adopter_rejects_real_pytest_internal_error_mapped_to_mutmut_kill(
    tmp_path: Path,
) -> None:
    reference = pytest.importorskip("mutmut.stats", reason="requires prepared mutmut reference")
    project = VfyProject(tmp_path / "project")
    project.write(
        "conftest.py",
        "def pytest_runtestloop(session):\n    raise RuntimeError('reference execution failed')\n",
    )
    project.write("test_value.py", "def test_value():\n    assert True\n")
    failed = project.run_pytest("-q", load_xdist=False)
    failed.expect(3)
    assert "reference execution failed" in failed.stdout + failed.stderr
    name = "app.x_value__mutmut_1"
    status = reference.status_by_exit_code[failed.returncode]
    assert status == MUTMUT_STATUS_BY_EXIT[failed.returncode] == "killed"
    sample: MutmutSample = {
        "wall_ms": 0.0,
        "summary": {
            "stats": {"total": 1, "killed": 1},
            "phase_timings": {},
            "outcomes": {name: status},
        },
        "process": {"raw_exit_codes": {name: failed.returncode}},
    }
    assert _validated_mutmut_signature(sample, expected_names=[name], label="diagnostic")
    with pytest.raises(RuntimeError, match="non-test mutmut exit status"):
        validated_reference_signature(sample, expected_names=[name], label="publication")


def test_adopter_accepts_real_test_pass_and_failure_exit_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("test_value.py", "def test_value():\n    assert True\n")
    passed = project.run_pytest("-q", load_xdist=False)
    project.write("test_value.py", "def test_value():\n    assert False\n")
    failed = project.run_pytest("-q", load_xdist=False)
    passed.expect(0)
    failed.expect(1)
    exits = {"app.survived": passed.returncode, "app.killed": failed.returncode, "unused": None}
    sample: MutmutSample = {
        "wall_ms": 0.0,
        "summary": {
            "stats": {"total": 2, "survived": 1, "killed": 1},
            "phase_timings": {},
            "outcomes": {"app.survived": "survived", "app.killed": "killed"},
        },
        "process": {"raw_exit_codes": exits},
    }
    assert validated_reference_signature(
        sample, expected_names=["app.survived", "app.killed"], label="publication"
    ) == (("app.survived", "survived"), ("app.killed", "killed"))
    del exits["app.survived"]
    with pytest.raises(RuntimeError, match=r"non-test mutmut exit status.*app\.survived"):
        validated_reference_signature(
            sample, expected_names=["app.survived", "app.killed"], label="publication"
        )


@pytest.mark.parametrize("code", [None, 99, True, "1"])
def test_adopter_rejects_incomplete_or_invalid_raw_reference_evidence(code: object) -> None:
    sample: MutmutSample = {
        "wall_ms": 0.0,
        "summary": {
            "stats": {"total": 1, "killed": 1},
            "phase_timings": {},
            "outcomes": {"app.mutant": "killed"},
        },
        "process": {"raw_exit_codes": {"app.mutant": code}},
    }
    with pytest.raises(RuntimeError, match="non-test mutmut exit status"):
        validated_reference_signature(sample, expected_names=["app.mutant"], label="publication")


@pytest.mark.parametrize("engine", _MUTATION_ENGINES)
def test_adopter_baseline_preserves_complete_ordered_phase_outcomes(
    tmp_path: Path, engine: VfyEngine
) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("pytest.ini", "[pytest]\naddopts = -p no:randomly\n")
    project.write(
        "test_values.py",
        "import pytest\n"
        "def test_pass():\n    assert True\n"
        "@pytest.mark.skip(reason='project condition')\n"
        "def test_skip():\n    assert False\n",
    )
    args = Namespace(expected_tests=2, pytest_arg=[])
    stock = baseline(project.root, args, tmp_path / "stock", engine=None)
    actual = baseline(project.root, args, tmp_path / engine, engine=engine)
    assert actual == stock
    assert junit_outcomes(tmp_path / engine / "junit.xml") == junit_outcomes(
        tmp_path / "stock/junit.xml"
    )
    assert [(node, phase, outcome) for node, phase, outcome, _ in stock] == [
        ("test_values.py::test_pass", "setup", "passed"),
        ("test_values.py::test_pass", "call", "passed"),
        ("test_values.py::test_pass", "teardown", "passed"),
        ("test_values.py::test_skip", "setup", "skipped"),
        ("test_values.py::test_skip", "teardown", "passed"),
    ]


@requires_native_pytest
def test_adopter_independent_baseline_preserves_original_teardown_wrapper(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write(
        "conftest.py",
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_teardown(item):\n"
        "    Path('wrapper-ran').write_text(item.nodeid)\n    yield\n",
    )
    project.write("test_app.py", "def test_value():\n    assert True\n")
    args = Namespace(expected_tests=1, pytest_arg=[])
    stock = baseline(project.root, args, tmp_path / "stock", engine=None)
    marker = project.root / "wrapper-ran"
    assert marker.read_text() == "test_app.py::test_value"
    marker.unlink()
    actual = baseline(project.root, args, tmp_path / "native", engine="native")
    assert marker.read_text() == "test_app.py::test_value"
    assert actual == stock
    assert junit_outcomes(tmp_path / "native/junit.xml") == junit_outcomes(
        tmp_path / "stock/junit.xml"
    )


@pytest.mark.parametrize("engine", _MUTATION_ENGINES)
def test_adopter_verifies_cached_import_body_mutations_use_full_suite_plan(
    tmp_path: Path, engine: VfyEngine
) -> None:
    project = VfyProject(tmp_path / "project")
    source = "def value():\n    return 1\n\ncached = value()\n"
    project.write("src/app.py", source)
    project.write(
        "test_app.py",
        "import app\ndef test_calls():\n    assert app.value() > 0\n"
        "def test_cached():\n    assert app.cached == 1\n",
    )
    report = measured_vfy(
        project.root,
        Namespace(workers=1, timeout=10, test_isolation="serial", vfy_route=engine),
        source_paths=(project.root / "src/app.py",),
        child_environment=environment(project.root),
    )
    assert report["summary"] == {"protected": 1}, report
    assert report["mutations"][0]["owner"]["phase"] == "body"
    assert report["mutations"][0]["tests"] == []
    assert report["mutations"][0]["killer"] == "test_app.py::test_cached"
    start = source.index("1")
    assert full_suite_keys(
        project.root, report, environment(project.root), tmp_path / "plan.json", engine=engine
    ) == frozenset({("src/app.py", start, start + 1, "1", "2")})


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
@pytest.mark.parametrize(("method", "covered"), [(True, True), (True, False), (False, True)])
def test_adopter_verifies_independent_fresh_scope_from_actual_baseline(
    tmp_path: Path, method: bool, covered: bool
) -> None:
    project = VfyProject(tmp_path / "project")
    source = (
        "class Value:\n    @staticmethod\n    def value():\n        return 1\n"
        if method
        else "def value():\n    return 1\n"
    )
    project.write("src/app.py", source)
    target = "app.Value.value" if method else "app.value"
    assertion = f"{target}() == 1" if covered else f"callable({target})"
    project.write("test_app.py", f"import app\ndef test_value():\n    assert {assertion}\n")
    report = measured_vfy(
        project.root,
        Namespace(workers=1, timeout=10, test_isolation="serial", vfy_route="native"),
        source_paths=(project.root / "src/app.py",),
        child_environment=environment(project.root),
    )
    assert report["summary"] == {"protected" if covered else "unreached": 1}, report
    row = report["mutations"][0]
    assert row["owner"]["phase"] == "body"
    assert row["tests"] == (["test_app.py::test_value"] if covered and not method else [])
    # An empty report selection alone cannot prove full-suite execution.
    row["status"] = "protected"
    row["tests"] = []
    plan_path = tmp_path / "plan.json"

    def verify() -> frozenset[tuple[str, int, int, str, str]]:
        return full_suite_keys(
            project.root, report, environment(project.root), plan_path, engine="native"
        )

    if covered and method:
        start = source.index("1")
        assert verify() == frozenset({("src/app.py", start, start + 1, "1", "2")})
    else:
        with pytest.raises(RuntimeError, match="does not have a full-suite plan"):
            verify()
    plans = json.loads(json.loads(plan_path.read_text())["stdout"])
    assert plans["initial_plans"][row["id"]] == (
        ["selected", ["test_app.py::test_value"]] if covered else ["unreached", []]
    )
    assert plans["plans"][row["id"]] == (
        ["full_suite", []] if covered and method else plans["initial_plans"][row["id"]]
    )


@requires_native_pytest
def test_adopter_verifies_definition_mutations_use_native_full_suite_plan(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("src/app.py", "def value(answer=1):\n    return answer\n")
    project.write(
        "test_value.py",
        "from app import value\ndef test_default():\n"
        "    assert value() == 1\n    assert value.__defaults__ == (1,)\n",
    )
    report = measured_vfy(
        project.root,
        Namespace(workers=1, timeout=10, test_isolation="serial"),
        source_paths=(project.root / "src/app.py",),
        child_environment=environment(project.root),
    )
    assert report["summary"] == {"protected": 2}
    assert full_suite_keys(
        project.root, report, environment(project.root), tmp_path / "plan.json"
    ) == frozenset({("src/app.py", 17, 18, "1", "2")})


def test_adopter_junit_retains_duplicate_case_multiplicity(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuite><testcase classname="suite" name="same"/>'
        '<testcase classname="suite" name="same"/>'
        '<testcase classname="suite" name="same">'
        '<skipped type="pytest.skip" message="condition">reason</skipped></testcase>'
        "</testsuite>",
        encoding="utf-8",
    )
    assert junit_outcomes(path) == {
        ("suite", "same", "passed", "", "", ""): 2,
        ("suite", "same", "skipped", "pytest.skip", "condition", "reason"): 1,
    }


def test_adopter_dependency_lock_rejects_unhashed_and_wrong_installed_pins(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    version = importlib.metadata.version("pytest")
    path.write_text(f"pytest=={version}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no SHA-256 hash"):
        locked_versions(path)
    hash_option = "--hash=sha256:" + "a" * 64
    path.write_text(f"pytest=={version} \\\n    {hash_option}\n", encoding="utf-8")
    assert locked_versions(path) == {"pytest": version}
    path.write_text(f"pytest==0.0.0 {hash_option}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="differs from lock"):
        locked_versions(path)


def test_adopter_source_scope_rejects_files_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside.py").write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inside the project"):
        source_files(project, ["../outside.py"])


def test_adopter_defaults_to_original_mutmut_scope_and_rejects_changed_overrides(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mutmut.configuration", reason="requires the prepared mutmut reference")
    project = VfyProject(tmp_path / "project")
    project.write(
        "pyproject.toml",
        '[tool.mutmut]\nsource_paths = ["src"]\nonly_mutate = ["src/app.py"]\n'
        'pytest_add_cli_args = ["-x"]\npytest_add_cli_args_test_selection = ["tests"]\n',
    )
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write("src/other.py", "def other():\n    return 2\n")
    project.write("tests/test_app.py", "def test_value():\n    assert True\n")
    options = Namespace(source=None, pytest_arg=None)
    declaration = declared_scope(project.root, options, tmp_path / "derived.json")
    assert declaration == {
        "files": ["src/app.py"],
        "pytest_args": ["tests", "-x"],
        "execution_args": ["-x"],
        "selection_args": ["tests"],
        "mutate_only_covered_lines": False,
    }
    assert (options.source, options.pytest_arg) == (["src/app.py"], ["tests", "-x"])
    with pytest.raises(RuntimeError, match="source scope differs"):
        declared_scope(
            project.root,
            Namespace(source=["src/other.py"], pytest_arg=None),
            tmp_path / "changed-source.json",
        )
    with pytest.raises(RuntimeError, match="pytest arguments differ"):
        declared_scope(
            project.root,
            Namespace(source=None, pytest_arg=[]),
            tmp_path / "changed-tests.json",
        )


def test_adopter_preserves_mutmut_execution_argument_precedence(tmp_path: Path) -> None:
    pytest.importorskip("mutmut.configuration", reason="requires the prepared mutmut reference")
    project = VfyProject(tmp_path / "project")
    project.write(
        "pyproject.toml",
        '[tool.mutmut]\nsource_paths = ["src"]\n'
        'pytest_add_cli_args = ["-k", "execution"]\n'
        'pytest_add_cli_args_test_selection = ["tests", "-k", "selection"]\n',
    )
    project.write("src/app.py", "def value():\n    return 1\n")
    tests = "def test_selection():\n    assert True\ndef test_execution():\n    assert True\n"
    project.write("tests/test_scope.py", tests)
    args = Namespace(source=None, pytest_arg=None, expected_tests=1)
    declared_scope(project.root, args, tmp_path / "scope.json")
    observed = baseline(project.root, args, tmp_path / "baseline", engine=None)
    project.write("mutants/tests/test_scope.py", tests)
    reference_path = tmp_path / "reference.json"
    oracle = project.run_python(
        "-c",
        "import json,sys\nfrom pathlib import Path\n"
        "from mutmut.runners.harness import PytestRunner\n"
        "Path(sys.argv[1]).write_text(json.dumps(sorted(PytestRunner().list_all_tests().ids)))\n",
        str(reference_path),
    )
    oracle.expect(0)
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    assert reference == ["tests/test_scope.py::test_execution"]
    assert sorted({node for node, _, _, _ in observed}) == reference


def test_adopter_rejects_unmapped_covered_line_catalogs(tmp_path: Path) -> None:
    pytest.importorskip("mutmut.configuration", reason="requires the prepared mutmut reference")
    project = VfyProject(tmp_path / "project")
    project.write(
        "pyproject.toml",
        '[tool.mutmut]\nsource_paths = ["src"]\nmutate_only_covered_lines = true\n',
    )
    project.write("src/app.py", "def value():\n    return 1\n")
    receipt = tmp_path / "scope.json"
    with pytest.raises(RuntimeError, match="covered-line mutation catalogs"):
        declared_scope(project.root, Namespace(source=None, pytest_arg=None), receipt)
    recorded = json.loads(receipt.read_text(encoding="utf-8"))
    assert recorded["returncode"] == 0
    assert json.loads(recorded["stdout"])["mutate_only_covered_lines"] is True


@requires_native_pytest
def test_adopter_preserves_explicit_disabled_coverage(tmp_path: Path) -> None:
    pytest.importorskip("mutmut.configuration", reason="requires the prepared mutmut reference")
    project = VfyProject(tmp_path / "project")
    project.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\naddopts = "--cov=src --cov-report=xml:public.xml"\n'
        '[tool.mutmut]\nsource_paths = ["src"]\n'
        'pytest_add_cli_args = ["--no-cov"]\n'
        'pytest_add_cli_args_test_selection = ["tests"]\n',
    )
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_value.py",
        "def test_options(cov, pytestconfig):\n"
        "    assert cov is None\n"
        "    assert pytestconfig.option.no_cov is True\n"
        "    assert pytestconfig.option.cov_source == ['src']\n"
        "    assert pytestconfig.option.cov_report == {'xml': 'public.xml'}\n",
    )
    args = Namespace(source=None, pytest_arg=None, expected_tests=1)
    declared_scope(project.root, args, tmp_path / "scope.json")
    policy = baseline_coverage_policy(project.root, args, tmp_path / "policy.json")
    assert policy == "disabled"
    stock = baseline(project.root, args, tmp_path / "stock", engine=None)
    native = baseline(
        project.root,
        args,
        tmp_path / "pytest-native",
        engine="pytest-native",
        coverage_policy=policy,
    )
    assert native == stock
    assert not (project.root / "public.xml").exists()


def test_adopter_matches_real_random_order_inputs_in_baselines_and_proofs(tmp_path: Path) -> None:
    pytest.importorskip("random_order", reason="requires the prepared random-order reference")
    project = VfyProject(tmp_path / "project")
    project.write("pytest.ini", "[pytest]\naddopts = --random-order -p no:randomly\n")
    source = project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_value.py",
        "import os\nimport pytest\nfrom app import value\n"
        "@pytest.mark.parametrize('index', range(8))\n"
        "def test_value(index, pytestconfig):\n"
        "    assert pytestconfig.pluginmanager.hasplugin('random_order')\n"
        "    assert pytestconfig.option.random_order_seed == "
        "os.environ['PYTEST_ADDOPTS'].removeprefix('--random-order-seed=')\n"
        "    assert value() == 1\n",
    )
    rows = []
    for index, seed in enumerate((0, 0, 1)):
        root = tmp_path / f"copy-{index}"
        copy_project(project.root, root)
        rows.append(
            baseline(
                root,
                Namespace(expected_tests=8, pytest_arg=[], random_order_seed=seed),
                tmp_path / f"baseline-{index}",
                engine=None,
            )
        )
    assert rows[0] == rows[1]
    assert {row[0] for row in rows[0]} == {row[0] for row in rows[2]}
    assert rows[0] != rows[2]
    mutation = dict(Mutation.from_rust(analyze_mutations(str(source))[0]).as_dict())
    killer = rows[0][0][0]
    mutation["tests"] = [killer]
    proofs = _KillerProofs(
        tmp_path / "proofs",
        python=sys.executable,
        timeout=30,
        test_environment=partial(environment, random_order_seed=0),
    )
    proofs.prove(project.root, mutation, [killer], label="matching random-order seed")
    assert [record["valid"] for record in proofs.records()] == [True]


@pytest.mark.parametrize("engine", _MUTATION_ENGINES)
def test_adopter_rejects_coverage_only_skip_in_actual_mutation_baseline(
    tmp_path: Path, engine: VfyEngine
) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("pytest.ini", "[pytest]\naddopts = -p no:randomly\n")
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py",
        "import pytest\nimport vfy\nfrom app import value\n"
        "def test_value():\n    assert value() == 1\n"
        "@pytest.mark.skipif(vfy.Coverage.current() is not None, reason='coverage-only skip')\n"
        "def test_coverage_sensitive():\n    assert True\n",
    )
    args = Namespace(
        expected_tests=2,
        pytest_arg=[],
        workers=1,
        timeout=10,
        test_isolation="serial",
        vfy_route=engine,
    )
    stock = baseline(project.root, args, tmp_path / "stock", engine=None)
    audit = baseline(
        project.root, args, tmp_path / "audit", engine=engine, coverage_policy="implicit"
    )
    assert audit == stock
    mutation_environment = environment(project.root)
    result_path = tmp_path / "mutation-baseline.json"
    mutation_environment["PYTEST_NATIVE_RESULT"] = str(result_path)
    report = measured_vfy(
        project.root,
        args,
        source_paths=(project.root / "src/app.py",),
        child_environment=mutation_environment,
    )
    assert report["summary"] == {"protected": 1}
    assert report["mutations"][0]["tests"] == ["tests/test_app.py::test_value"]
    with pytest.raises(RuntimeError, match="instrumented mutation baseline outcomes differ"):
        validate_mutation_baseline(result_path, 2, stock, engine=engine)


def test_adopter_killer_proofs_preserve_original_serial_worker_override(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("pytest.ini", "[pytest]\naddopts = -n auto\n")
    source = project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py",
        "from app import value\n"
        "def test_value(worker_id):\n"
        "    assert worker_id == 'master'\n"
        "    assert value() == 1\n",
    )
    killer = "tests/test_app.py::test_value"
    mutation: dict[str, object] = dict(
        Mutation.from_rust(analyze_mutations(str(source))[0]).as_dict()
    )
    mutation["tests"] = [killer]
    proofs = _KillerProofs(
        tmp_path / "proofs",
        python=sys.executable,
        timeout=30,
        test_environment=environment,
        pytest_args=("--numprocesses=0", "-p", "no:cacheprovider"),
    )
    proofs.prove(project.root, mutation, [killer], label="serial mutation configuration")
    assert [record["valid"] for record in proofs.records()] == [True]
