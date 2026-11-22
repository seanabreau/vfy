"""Autoload follows live entry points in distribution order."""

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_autoload_observes_later_distribution_changes_and_skips_duplicate_names(
    tmp_path: Path,
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        for name in ("first", "second"):
            project.write(
                f"{name}_vfyprobe-1.0.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}-vfyprobe\nVersion: 1.0\n",
            )
        order = project.run_python(
            "-c",
            "import importlib.metadata as m; "
            "print(','.join(d.name for d in m.distributions() "
            "if d.name in ('first-vfyprobe', 'second-vfyprobe')))",
        )
        order.expect(0)
        first, second = (name.replace("-", "_") for name in order.stdout.strip().split(","))
        project.write(
            f"{first}-1.0.dist-info/entry_points.txt",
            "[pytest11]\nvfy_probe_first = vfy_probe_first\n",
        )
        project.write(
            f"{second}-1.0.dist-info/entry_points.txt",
            "[pytest11]\nvfy_probe_stale = vfy_probe_old\n",
        )
        project.write(
            "vfy_probe_first.py",
            "from pathlib import Path\n"
            "Path('first-loaded').touch()\n"
            f"Path('{second}-1.0.dist-info/entry_points.txt').write_text(\n"
            "    '[pytest11]\\nvfy_probe_first = vfy_probe_duplicate\\n'"
            "    'vfy_probe_later = vfy_probe_new\\n')\n"
            "added = Path('added_vfyprobe-1.0.dist-info')\n"
            "added.mkdir()\n"
            "(added / 'METADATA').write_text('Name: added-vfyprobe\\nVersion: 1.0\\n')\n"
            "(added / 'entry_points.txt').write_text(\n"
            "    '[pytest11]\\nvfy_probe_added = vfy_probe_added\\n')\n",
        )
        for name in ("old", "new", "duplicate", "added"):
            project.write(
                f"vfy_probe_{name}.py", f"from pathlib import Path\nPath('{name}-loaded').touch()\n"
            )
        project.write("test_ok.py", "def test_ok(): pass\n")
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
        blocked = tuple(
            argument
            for entry in entry_points(group="pytest11")
            if entry.name != "pytest_cov"
            for argument in ("-p", f"no:{entry.name}")
        )
        result = (
            project.run_python("-m", "pytest", "-q", *blocked, env=env)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", *blocked, env=env)
        )
        result.expect(0)
        observations.append(
            {
                name
                for name in ("first", "old", "new", "duplicate", "added")
                if (project.root / f"{name}-loaded").exists()
            }
        )
    assert observations == [{"first", "new"}, {"first", "new"}]


@requires_native_pytest
def test_explicit_plugin_lookup_observes_metadata_changed_by_earlier_plugin(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "vfy_explicit_probe-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: vfy-explicit-probe\nVersion: 1.0\n",
        )
        project.write(
            "vfy_explicit_probe-1.0.dist-info/entry_points.txt",
            "[pytest11]\nvfy_explicit_later = vfy_explicit_old\n",
        )
        project.write(
            "vfy_explicit_first.py",
            "from pathlib import Path\n"
            "Path('vfy_explicit_probe-1.0.dist-info/entry_points.txt').write_text(\n"
            "    '[pytest11]\\nvfy_explicit_later = vfy_explicit_new\\n')\n",
        )
        for name in ("old", "new"):
            project.write(
                f"vfy_explicit_{name}.py",
                f"from pathlib import Path\nPath('{name}-loaded').touch()\n",
            )
        project.write("test_ok.py", "def test_ok(): pass\n")
        arguments = ("-q", "-p", "vfy_explicit_first", "-p", "vfy_explicit_later")
        result = (
            project.run_python("-m", "pytest", *arguments)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
        )
        result.expect(0)
        observations.append(
            {name for name in ("old", "new") if (project.root / f"{name}-loaded").exists()}
        )
    assert observations == [{"new"}, {"new"}]


@requires_native_pytest
@pytest.mark.parametrize(
    "owned_name",
    [
        "vfy.independent.mock_support",
        "vfy.independent.mock_fixtures",
        "vfy.independent.benchmark",
        "vfy.independent.timeout",
    ],
)
def test_owned_replacement_cannot_be_replaced_by_entry_point(
    tmp_path: Path, owned_name: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "collision-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: collision\nVersion: 1.0\n",
    )
    project.write(
        "collision-1.0.dist-info/entry_points.txt", f"[pytest11]\n{owned_name} = collision\n"
    )
    project.write("collision.py", "raise AssertionError('owned replacement was replaced')\n")
    project.write(
        "test_ok.py",
        "def test_ok(request):\n"
        f"    plugin = request.config.pluginmanager.getplugin({owned_name!r})\n"
        f"    assert plugin.__name__ == {owned_name!r}\n",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-p", owned_name)
    result.expect(0)


@requires_native_pytest
def test_absent_asyncio_distribution_uses_owned_non_autouse_policy(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "metadata_override/sitecustomize.py",
        "import importlib.metadata as metadata\n"
        "original_distribution = metadata.distribution\n"
        "def distribution(name):\n"
        "    if name == 'pytest-asyncio':\n"
        "        raise metadata.PackageNotFoundError(name)\n"
        "    return original_distribution(name)\n"
        "metadata.distribution = distribution\n"
        "metadata.distributions = lambda **kwargs: iter(())\n",
    )
    project.write(
        "test_policy.py",
        "import json\nfrom pathlib import Path\n"
        "def test_policy(request):\n"
        "    Path('fixtures.json').write_text(json.dumps(request.fixturenames))\n",
    )
    stock = project.run_python("-m", "pytest", "-q", "-p", "no:asyncio")
    stock.expect(0)
    stock_names = json.loads((project.root / "fixtures.json").read_text())
    assert "event_loop_policy" not in stock_names

    environment = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
        "PYTHONPATH": str(project.root / "metadata_override"),
    }
    for plugin, expected in (((), False), (("-p", "asyncio"), True)):
        result = project.run_python("-m", "vfy.independent.entry", "-q", *plugin, env=environment)
        result.expect(0)
        names = json.loads((project.root / "fixtures.json").read_text())
        assert ("event_loop_policy" in names) is expected
