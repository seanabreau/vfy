"""Upstream xdist workers publish native coverage through one Rust transaction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="native execution requires Unix")


@pytest.mark.parametrize("distribution", ["load", "each", "no", "controller-child"])
def test_native_xdist_branch_context_and_child_coverage_matches_pytest(
    tmp_path: Path, distribution: str
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("reference", "native")}
    for project in projects.values():
        project.write(
            "app.py",
            "def choose(value):\n"
            "    if value:\n"
            "        return 'yes'\n"
            "    return 'no'\n"
            "def excluded():\n"
            "    return 'excluded'\n"
            "def child():\n"
            "    return 'child'\n"
            "def controller_child():\n"
            "    return 'controller child'\n",
        )
        if distribution == "controller-child":
            project.write(
                "conftest.py",
                "import subprocess\nimport sys\n"
                "def pytest_configure_node(node):\n"
                "    subprocess.run([sys.executable, '-c', "
                "'from app import controller_child; controller_child()'], check=True)\n",
            )
        project.write(
            "test_app.py",
            "import subprocess\nimport sys\nimport pytest\n"
            "from app import choose, excluded\n"
            "@pytest.mark.parametrize('value', [False, True])\n"
            "def test_choose(value):\n"
            "    assert choose(value) == ('yes' if value else 'no')\n"
            "@pytest.mark.no_cover\n"
            "def test_excluded():\n"
            "    assert excluded() == 'excluded'\n"
            "def test_child():\n"
            "    subprocess.run([sys.executable, '-c', 'from app import child; child()'], "
            "check=True)\n",
        )
    arguments = (
        "--numprocesses=0" if distribution == "no" else "--numprocesses=2",
        f"--dist={'load' if distribution == 'controller-child' else distribution}",
        "--cov=app",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=json:coverage.json",
        "-q",
    )
    reference = projects["reference"].run_pytest(*arguments)
    native = projects["native"].run_vfy("test", "--engine=pytest-native", "--no-replay", *arguments)
    reference.expect(0)
    assert native.returncode == reference.returncode, native.stdout + native.stderr
    expected_count = 8 if distribution == "each" else 4
    assert f"{expected_count} passed" in native.stdout
    native_data = projects["native"].data()
    reference_data = projects["reference"].data()
    native_path = str(projects["native"].root / "app.py")
    reference_path = str(projects["reference"].root / "app.py")
    assert native_data.lines(native_path) == reference_data.lines(reference_path)
    assert sorted(native_data.arcs(native_path) or []) == sorted(
        reference_data.arcs(reference_path) or []
    )
    assert {
        line: sorted(contexts)
        for line, contexts in native_data.contexts_by_lineno(native_path).items()
    } == {
        line: sorted(contexts)
        for line, contexts in reference_data.contexts_by_lineno(reference_path).items()
    }
    assert native_data.measured_contexts() == reference_data.measured_contexts()
    assert 6 not in (native_data.lines(native_path) or [])
    assert 8 in (native_data.lines(native_path) or [])
    assert (10 in (native_data.lines(native_path) or [])) == (distribution == "controller-child")
    assert not list(projects["native"].root.glob(".coverage.*"))
    assert not list(projects["native"].root.glob(".vfy-native-child-*"))


def test_corrupt_xdist_worker_child_coverage_preserves_previous_artifacts(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\ndef baseline_only():\n    return 2\n")
    project.write(
        "test_app.py",
        "import json\nimport os\nfrom pathlib import Path\n"
        "from app import value, baseline_only\n"
        "def test_value():\n"
        "    assert value() == 1\n"
        "    if os.getenv('BAD_SHARD'):\n"
        "        base = Path(json.loads(os.environ['VFY_PROCESS_OPTIONS'])['data_file'])\n"
        "        base.with_name(base.name + '.bad').write_bytes(b'invalid coverage shard')\n"
        "        Path('created-bad-shard').touch()\n"
        "    else:\n"
        "        assert baseline_only() == 2\n"
        "def test_other():\n"
        "    assert value() == 1\n",
    )
    arguments = (
        "test",
        "--engine=pytest-native",
        "--no-replay",
        "--numprocesses=2",
        "--cov=app",
        "--cov-branch",
        "--cov-report=json:coverage.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments)
    baseline.expect(0)
    artifacts = (".coverage", "coverage.json", ".vfy/test-history.sqlite3")
    preserved = {name: (tmp_path / name).read_bytes() for name in artifacts}
    rejected = project.run_vfy(*arguments, env={"BAD_SHARD": "1"})
    assert (tmp_path / "created-bad-shard").exists(), rejected.stdout + rejected.stderr
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert "native pytest-xdist workers did not publish coverage" in (
        rejected.stdout + rejected.stderr
    )
    assert {name: (tmp_path / name).read_bytes() for name in artifacts} == preserved
    project.write(
        "test_app.py", "from app import value\ndef test_value():\n    assert value() == 1\n"
    )
    recovered = project.run_vfy(*arguments)
    recovered.expect(0)
    assert project.lines("app.py") == [1, 2, 3]
    assert not list(tmp_path.glob(".coverage.*"))


def test_native_xdist_rejects_unrelated_enclosing_collector_before_publication(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "import os\nfrom pathlib import Path\nfrom app import value\n"
        "def test_first():\n"
        "    if os.getenv('ENCLOSING_RUN'):\n"
        "        Path('executed').touch()\n"
        "    assert value() == 1\n"
        "def test_second():\n"
        "    assert value() == 1\n",
    )
    arguments = (
        "test",
        "--engine=pytest-native",
        "--no-replay",
        "--numprocesses=2",
        "--cov=app",
        "--cov-branch",
        "--cov-report=json:coverage.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments)
    baseline.expect(0)
    artifacts = (".coverage", "coverage.json", ".vfy/test-history.sqlite3")
    preserved = {name: (tmp_path / name).read_bytes() for name in artifacts}
    rejected = project.run_vfy(
        "coverage",
        "run",
        "--data-file=enclosing.coverage",
        "--source=app",
        "--branch",
        "-m",
        "vfy",
        *arguments,
        env={"ENCLOSING_RUN": "1"},
    )
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert "enclosing active Coverage session" in rejected.stdout + rejected.stderr
    assert not (tmp_path / "executed").exists()
    assert {name: (tmp_path / name).read_bytes() for name in artifacts} == preserved


def test_crashed_xdist_worker_child_shards_are_reclaimed_without_replacing_artifacts(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py",
        "import json\nimport os\nimport subprocess\nimport sys\nfrom pathlib import Path\n"
        "from app import value\n"
        "def test_crash():\n"
        "    assert value() == 1\n"
        "    if os.getenv('CRASH_RUN') and not Path('crashed').exists():\n"
        "        subprocess.run([sys.executable, '-c', 'from app import value; value()'], "
        "check=True)\n"
        "        base = Path(json.loads(os.environ['VFY_PROCESS_OPTIONS'])['data_file'])\n"
        "        assert list(base.parent.glob(base.name + '.*'))\n"
        "        Path('crashed').touch()\n"
        "        os._exit(23)\n"
        "def test_pending():\n"
        "    assert value() == 1\n",
    )
    arguments = (
        "test",
        "--engine=pytest-native",
        "--no-replay",
        "--numprocesses=2",
        "--cov=app",
        "--cov-report=json:coverage.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments)
    baseline.expect(0)
    artifacts = (".coverage", "coverage.json", ".vfy/test-history.sqlite3")
    preserved = {name: (tmp_path / name).read_bytes() for name in artifacts}
    unrelated = tmp_path / ".coverage.unrelated"
    unrelated.write_bytes(preserved[".coverage"])

    crashed = project.run_vfy(*arguments, env={"CRASH_RUN": "1"})
    assert (tmp_path / "crashed").exists(), crashed.stdout + crashed.stderr
    assert crashed.returncode != 0, crashed.stdout + crashed.stderr
    assert "native pytest-xdist workers did not publish coverage" in (
        crashed.stdout + crashed.stderr
    )
    assert {name: (tmp_path / name).read_bytes() for name in artifacts} == preserved
    assert not list(tmp_path.glob(".vfy-native-child-*"))
    assert sorted(path.name for path in tmp_path.glob(".coverage.*")) == [unrelated.name]
    assert unrelated.read_bytes() == preserved[".coverage"]

    recovered = project.run_vfy(*arguments)
    recovered.expect(0)
    assert project.lines("app.py") == [1, 2]
    assert not list(tmp_path.glob(".vfy-native-child-*"))
    assert sorted(path.name for path in tmp_path.glob(".coverage.*")) == [unrelated.name]
    assert unrelated.read_bytes() == preserved[".coverage"]
