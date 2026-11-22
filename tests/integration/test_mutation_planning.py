"""Public mutation-planning boundaries for full-scope and fork-hook cases."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "default",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
        "auto",
        "portable",
    ],
)
def test_definition_phase_uses_full_scope_despite_incidental_line_contexts(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "app.py",
        "def value(argument=1):\n    return argument\n\ndef unrelated():\n    return 3\n",
    )
    project.write(
        "tests/test_app.py",
        "import importlib\n"
        "import app\n\n"
        "def test_definition_context():\n"
        "    importlib.reload(app)\n"
        "    assert app.unrelated() == 3\n\n"
        "def test_default_metadata():\n"
        "    assert app.value.__defaults__ == (1,)\n",
    )
    engine_options = () if engine == "default" else ("--engine=pytest-native",)
    environment = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "VFY_MUTATION_ENGINE": "auto" if engine == "default" else engine,
    }

    baseline = project.run_vfy(
        "test", *engine_options, "--all", "--source", "app.py", "-q", env=environment
    )
    baseline.expect(0)
    line_contexts = project.data().contexts_by_lineno(str(source)).get(1, [])
    assert any("test_definition_context" in context for context in line_contexts)
    assert not any("test_default_metadata" in context for context in line_contexts)

    completed = project.run_vfy(
        "mutate",
        *engine_options,
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--max-mutations",
        "1",
        "--no-cache",
        "--format",
        "json",
        env=environment,
    )
    completed.expect(0)
    result = json.loads(completed.stdout)["mutations"][0]
    assert result["owner"]["phase"] == "definition"
    assert result["tests"] == []
    assert result["status"] == "protected"
    assert result["killer"] == "tests/test_app.py::test_default_metadata"


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
def test_nested_class_definition_uses_full_scope_for_cached_metadata_consumer(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "app.py",
        "_cached = None\n\n"
        "def cached_class():\n"
        "    global _cached\n"
        "    if _cached is None:\n"
        "        class Cached:\n"
        "            answer = 41\n"
        "        _cached = Cached\n"
        "    return _cached\n",
    )
    project.write(
        "tests/test_app.py",
        "import app\n\n"
        "def test_01_creates_cached_class():\n"
        "    assert app.cached_class() is not None\n\n"
        "def test_02_reads_cached_class_metadata():\n"
        "    assert app._cached.answer == 41\n",
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

    baseline = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "-j",
        "1",
        "--source",
        "app.py",
        "-q",
        env=environment,
    )
    baseline.expect(0)
    class_body_line = source.read_text().splitlines().index("            answer = 41") + 1
    line_contexts = project.data().contexts_by_lineno(str(source)).get(class_body_line, [])
    assert any("test_01_creates_cached_class" in context for context in line_contexts)
    assert not any("test_02_reads_cached_class_metadata" in context for context in line_contexts)

    completed = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--max-mutations",
        "1",
        "--no-cache",
        "--format",
        "json",
        env=environment,
    )
    completed.expect(0)
    result = json.loads(completed.stdout)["mutations"][0]
    assert result["owner"]["phase"] == "definition"
    assert result["tests"] == []
    assert result["status"] == "protected"
    assert result["killer"] == "tests/test_app.py::test_02_reads_cached_class_metadata"


@pytest.mark.skipif(os.name != "posix", reason="the collected mutation backend requires fork")
@pytest.mark.parametrize("change", ["add", "replace", "filename"])
def test_at_fork_module_alias_change_uses_safe_fallback(tmp_path: Path, change: str) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import os\n"
        "import sys\n"
        "from types import ModuleType\n\n"
        "alias = ModuleType('app_at_fork_duplicate')\n"
        "alias.__file__ = __file__\n"
        "sys.modules[alias.__name__] = alias\n"
        "def add_duplicate_module_after_fork():\n"
        "    original = sys.modules.get('app')\n"
        "    if original is not None:\n"
        "        duplicate = ModuleType('app_at_fork_duplicate')\n"
        "        duplicate.__file__ = original.__file__\n"
        f"        change = {change!r}\n"
        "        if change == 'add':\n"
        "            sys.modules['new_app_at_fork_duplicate'] = duplicate\n"
        "        elif change == 'replace':\n"
        "            sys.modules['app'] = duplicate\n"
        "        else:\n"
        "            alias.__file__ = original.__file__\n\n"
        "os.register_at_fork(after_in_child=add_duplicate_module_after_fork)\n",
    )
    project.write(
        "tests/test_app.py",
        "import os\nimport pytest\nfrom app import value\n\n"
        "@pytest.mark.parametrize('case', [1], ids=[str(os.getpid())])\n"
        "def test_value(case):\n    assert value() == case\n",
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "--source", "app.py", "-q", env=environment
    )
    baseline.expect(0)
    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--format",
        "json",
        env={**environment, "VFY_MUTATION_ENGINE": "auto"},
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["fast_results"] == 0
    assert report["metrics"]["portable_fallbacks"] == 1
    assert report["mutations"][0]["killer"].startswith("tests/test_app.py::test_value[")
    assert report["mutations"][0]["tests"] == []
    assert "pytest did not collect 1 test IDs" in completed.stderr


@pytest.mark.skipif(os.name != "posix", reason="fork hooks and symlinks require POSIX")
def test_at_fork_symlink_retarget_detects_a_new_source_alias(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write("other.py", "placeholder = None\n")
    (tmp_path / "alias.py").symlink_to("other.py")
    project.write(
        "conftest.py",
        "import os, sys\nfrom pathlib import Path\nfrom types import ModuleType\n"
        "alias_path = Path(__file__).parent / 'alias.py'\n"
        "alias = ModuleType('fork_alias')\n"
        "alias.__file__ = str(alias_path)\n"
        "sys.modules['fork_alias'] = alias\n"
        "def retarget():\n"
        "    alias_path.unlink()\n"
        "    alias_path.symlink_to('app.py')\n"
        "os.register_at_fork(after_in_child=retarget)\n",
    )
    project.write(
        "tests/test_app.py",
        "import app\ndef test_value():\n    assert app.value() == 1\n",
    )
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "--source", "app.py", "-j", "1", "-q"
    )
    baseline.expect(0)
    # The symlink must change after the mutation parent's snapshot, not during
    # a file-isolated baseline's earlier fork.
    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--test-isolation",
        "serial",
        "--format",
        "json",
        "-j",
        "1",
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["fast_results"] == 0
    assert report["metrics"]["portable_fallbacks"] == 1
