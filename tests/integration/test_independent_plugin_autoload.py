"""Autoload follows live entry points in distribution order."""

import json
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
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
def test_owned_replacements_fill_missing_providers_with_installed_metadata(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    environment = project.root / ".independent-python"
    project.run_python("-m", "venv", "--without-pip", str(environment)).expect(0)
    python = str(environment / "bin" / "python")
    location = project.run(python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))")
    location.expect(0)
    site_packages = Path(location.stdout.strip()).relative_to(project.root)
    for name in ("mock", "benchmark", "codspeed", "timeout", "xdist", "asyncio"):
        metadata = site_packages / f"pytest_{name}-99.1.dist-info" / "METADATA"
        project.write(
            metadata.as_posix(), f"Metadata-Version: 2.1\nName: pytest-{name}\nVersion: 99.1\n"
        )
    project.write(
        "test_owned.py",
        """def test_owned(mocker, benchmark, worker_id, event_loop_policy, request):
    from pytest_mock import MockerFixture

    assert isinstance(mocker, MockerFixture)
    assert benchmark(lambda: 'called') == 'called'
    assert worker_id == 'master'
    assert event_loop_policy is not None
    assert request.config.getoption('timeout') == 1.0
""",
    )
    project.write(
        "test_unavailable.py",
        """import pytest
from importlib.util import find_spec

def test_unavailable(request):
    for name in ('mocker', 'benchmark', 'worker_id', 'event_loop_policy'):
        with pytest.raises(pytest.FixtureLookupError):
            request.getfixturevalue(name)
    assert request.config.getoption('timeout', None) is None
    assert find_spec('pytest_mock') is None
""",
    )
    arguments = ("-m", "vfy", "test", "--engine=native", "--no-cov", "-q")
    env = {"PYTHONPATH": isolated_vfy_path(), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
    project.run(
        python, *arguments, "--timeout=1", "--benchmark-disable", "test_owned.py", env=env
    ).expect(0)
    project.run(
        python,
        "-m",
        "vfy.independent.entry",
        "-q",
        "--timeout=1",
        "--benchmark-disable",
        "test_owned.py",
        env=env,
    ).expect(0)

    blocked = ("vfy.independent.mock_fixtures", "benchmark", "timeout", "xdist", "asyncio")
    exclusions = [part for name in blocked for part in ("-p", f"no:{name}")]
    project.run(python, *arguments, *exclusions, "test_unavailable.py", env=env).expect(0)
    project.run(
        python,
        "-m",
        "vfy.independent.entry",
        "-q",
        "test_unavailable.py",
        env={**env, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    ).expect(0)

    installed_timeout = find_spec("pytest_timeout")
    assert installed_timeout is not None
    assert installed_timeout.origin is not None
    project.write(
        (site_packages / "pytest_timeout.py").as_posix(),
        Path(installed_timeout.origin).read_text(encoding="utf-8"),
    )
    project.run(
        python,
        *arguments,
        "-p",
        "pytest_timeout",
        "--timeout=1",
        "--benchmark-disable",
        "test_owned.py",
        env=env,
    ).expect(0)

    project.write((site_packages / "pytest_mock.py").as_posix(), "provider = 'installed module'\n")
    project.write(
        "test_mock_import.py",
        """import pytest_mock

def test_mock_import(mocker):
    assert pytest_mock.provider == 'installed module'
    assert mocker.stub().call_count == 0
""",
    )
    project.run(python, *arguments, "test_mock_import.py", env=env).expect(0)


@requires_native_pytest
def test_owned_replacements_defer_to_loaded_installed_providers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    names = {"pytest_mock", "benchmark", "timeout", "xdist"}
    entries = entry_points(group="pytest11")
    providers = {entry.name: entry.module for entry in entries if entry.name in names}
    assert providers.keys() == names
    project.write(
        "test_providers.py",
        f"""def test_providers(mocker, benchmark, worker_id, request):
    manager = request.config.pluginmanager
    for name, module in {providers!r}.items():
        assert manager.getplugin(name).__name__ == module
    assert manager.has_plugin('asyncio')
    assert mocker.stub().call_count == 0
    assert benchmark(lambda: 'called') == 'called'
    assert worker_id == 'master'
    assert request.config.getoption('timeout') == 1.0
""",
    )
    exclusions = [
        part
        for entry in entries
        if entry.name not in {*providers, "asyncio", "pytest_cov"}
        for part in ("-p", f"no:{entry.name}")
    ]
    project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        *exclusions,
        "--timeout=1",
        "--benchmark-disable",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
    ).expect(0)


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
