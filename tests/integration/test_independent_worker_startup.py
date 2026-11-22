"""Workers restore application startup after native binding."""

from __future__ import annotations

import json
import os
import sys
import venv
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("user_site", [False, True])
@pytest.mark.parametrize("workers", [1, 2])
def test_worker_restores_site_startup_after_native_binding(
    tmp_path: Path, user_site: bool, workers: int
) -> None:
    environment = tmp_path / "environment"
    venv.EnvBuilder(
        symlinks=sys.platform != "win32", with_pip=False, system_site_packages=True
    ).create(environment)
    python = environment / "bin" / "python"
    project = VfyProject(tmp_path / "project", interpreter=python)
    project.write(
        "startup_marker.py",
        """import json, os, sys, warnings
from pathlib import Path
events = []

def warning_is_error():
    try:
        warnings.warn('startup filter policy', UserWarning)
    except UserWarning:
        return True
    return False

def note(event):
    events.append(event)
    Path('startup-' + str(os.getpid()) + '.json').write_text(json.dumps({
        'events': events,
        'flags': [sys.flags.isolated, sys.flags.no_site],
        'arguments': sys.argv[:],
        'warning_is_error': warning_is_error(),
    }))
""",
    )
    project.write(
        "sitecustomize.py",
        """import os, sys, startup_marker
if sys.flags.no_site:
    sys.argv.append('-q')
startup_marker.note('site')
""",
    )
    imported = project.write("pth-imports/startup_application.py", "VALUE = 42\n").parent
    site_packages = (
        environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    (site_packages / "startup-proof.pth").write_text(
        f"{imported}\nimport startup_marker, warnings; "
        "warnings.simplefilter('ignore', UserWarning); startup_marker.note('pth')\n"
    )
    user_base = tmp_path / "user"
    location = project.run_python(
        "-c",
        "import site; print(site.getusersitepackages())",
        env={"PYTHONUSERBASE": str(user_base)},
    )
    location.expect(0)
    user_packages = Path(location.stdout.strip())
    assert user_packages.is_relative_to(user_base)
    user_packages.mkdir(parents=True)
    (user_packages / "usercustomize.py").write_text(
        "import startup_marker\nstartup_marker.note('user')\n"
    )
    configured = {
        "PYTHONPATH": os.pathsep.join([isolated_vfy_path(), str(project.root)]),
        "PYTHONUSERBASE": str(user_base),
        "PYTHONNOUSERSITE": None if user_site else "1",
        "PYTHONWARNINGS": "error::UserWarning",
    }
    oracle = project.run_python(
        "-c",
        "import json, startup_marker; print(json.dumps({"
        "'events': startup_marker.events, 'warning_is_error': startup_marker.warning_is_error()}))",
        env=configured,
    )
    oracle.expect(0)
    expected_record = json.loads(oracle.stdout)
    expected = expected_record["events"]
    assert expected_record["warning_is_error"] is False
    assert "pth" in expected
    assert "site" in expected
    assert ("user" in expected) is user_site
    for name in ["test_a.py", "test_b.py"]:
        project.write(
            name,
            """import sys, tempfile, uuid
from pathlib import Path
import startup_application

def test_startup_imports(tmp_path):
    assert startup_application.VALUE == 42
    assert sys.argv[-1] == '-q', 'startup argument changes reached the real entrypoint'
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        destination = Path(directory) / str(uuid.uuid4())
        destination.write_text('application startup restored')
        assert destination.read_text() == 'application startup restored'
""",
        )
    arguments = [
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=result.json",
    ]
    result = project.run_vfy(*arguments, env=configured)
    result.expect(0)
    record = json.loads((project.root / "result.json").read_text())
    assert record["counts"]["passed"] == 2
    pids = record["execution_topology"]["worker_pids"]
    assert len(pids) == workers
    for pid in pids:
        startup = json.loads((project.root / f"startup-{pid}.json").read_text())
        arguments = startup.pop("arguments")
        assert arguments[0] == "-m"
        assert arguments[-1] == "-q"
        assert f"--workers={workers}" in arguments
        assert startup == {
            "events": expected,
            "flags": [0, 1],
            "warning_is_error": expected_record["warning_is_error"],
        }


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("settings", ["environment", "flags"])
@pytest.mark.parametrize("site_override", [False, True])
def test_owned_interpreters_preserve_effective_startup_settings(
    tmp_path: Path, workers: int, settings: str, site_override: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "startup/appcategory.py",
        "events = ['category']\nclass StartupWarning(UserWarning): pass\n",
    )
    project.write(
        "startup/startup_probe.py",
        """import json, os, sys, warnings
from pathlib import Path
import appcategory

def snapshot():
    try:
        warnings.warn('startup warning policy', appcategory.StartupWarning)
    except appcategory.StartupWarning:
        warning_is_error = True
    else:
        warning_is_error = False
    try:
        exec(compile('assert False', '<startup-policy>', 'exec'))
    except AssertionError:
        assertion_removed = False
    else:
        assertion_removed = True
    return {
        'optimization': sys.flags.optimize,
        'debug': __debug__,
        'assertion_removed': assertion_removed,
        'warnoptions': sys.warnoptions[:],
        'warning_is_error': warning_is_error,
        'startup_order': appcategory.events[:],
        'dont_write_bytecode': sys.dont_write_bytecode,
        'pycache_prefix': sys.pycache_prefix,
        'cached_path': __spec__.cached,
        'seeded_hash': hash('vfy owned startup policy'),
        'pythonpath': os.environ.get('PYTHONPATH'),
        'cwd_on_path': any(Path(entry or '.').resolve() == Path.cwd() for entry in sys.path),
        'bootstrap_flags': [sys.flags.isolated, sys.flags.no_site, sys.flags.safe_path],
    }
""",
    )
    project.write(
        "startup/sitecustomize.py",
        "import json, os, warnings\nfrom pathlib import Path\nimport appcategory\n"
        "appcategory.events.append('site')\n"
        "if os.environ.get('VFY_TEST_SITE_WARNING_OVERRIDE') == '1':\n"
        "    warnings.simplefilter('ignore', appcategory.StartupWarning)\n"
        "import startup_probe\n"
        "startup_probe.POLICY = startup_probe.snapshot()\n"
        "Path('startup-policy-' + str(os.getpid()) + '.json').write_text(\n"
        "    json.dumps(startup_probe.POLICY))\n",
    )
    for name in ("first", "second"):
        project.write(
            f"test_{name}.py",
            "from startup_probe import POLICY\n"
            "from pathlib import Path\n"
            "import sys\n"
            "def test_startup_policy():\n"
            "    assert POLICY['optimization'] == 1\n"
            "    assert POLICY['assertion_removed']\n"
            f"    assert POLICY['warning_is_error'] is {not site_override!r}\n"
            "    assert any(Path(entry or '.').resolve() == Path.cwd() for entry in sys.path)\n",
        )
    prefix = str(tmp_path / "bytecode")
    configured = {
        "PYTHONPATH": str(tmp_path / "startup"),
        "PYTHONHASHSEED": "42",
        "PYTHONMALLOC": "malloc",
        "VFY_TEST_SITE_WARNING_OVERRIDE": "1" if site_override else "0",
        "PYTHONOPTIMIZE": "1" if settings == "environment" else None,
        "PYTHONWARNINGS": "error::appcategory.StartupWarning"
        if settings == "environment"
        else None,
        "PYTHONDONTWRITEBYTECODE": "1" if settings == "environment" else None,
        "PYTHONPYCACHEPREFIX": prefix if settings == "environment" else None,
    }
    flags = (
        ()
        if settings == "environment"
        else (
            "-O",
            "-B",
            "-W",
            "error::appcategory.StartupWarning",
            "-X",
            f"pycache_prefix={prefix}",
        )
    )
    oracle = project.run_python(
        *flags,
        "-c",
        "import json, sys; from pathlib import Path; from startup_probe import POLICY; "
        "print(json.dumps({'site': POLICY, 'cwd_after_startup': "
        "any(Path(entry or '.').resolve() == Path.cwd() for entry in sys.path)}))",
        env=configured,
    )
    oracle.expect(0)
    oracle_record = json.loads(oracle.stdout)
    assert oracle_record["cwd_after_startup"] is True
    expected = oracle_record["site"]
    original_flags = expected.pop("bootstrap_flags")
    assert expected["optimization"] == 1
    assert expected["warning_is_error"] is not site_override
    assert expected["startup_order"] == ["category", "site"]
    assert expected["dont_write_bytecode"] is True
    assert expected["pycache_prefix"] == prefix
    assert expected["cwd_on_path"] is False

    result = project.run_python(
        *flags,
        "-m",
        "vfy",
        "test",
        "--all",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=result.json",
        env=configured,
    )
    result.expect(0)
    assert "Invalid -W option" not in result.stderr
    record = json.loads((tmp_path / "result.json").read_text())
    assert record["counts"]["passed"] == 2
    pids = record["execution_topology"]["worker_pids"]
    assert len(pids) == workers
    for pid in pids:
        observed = json.loads((tmp_path / f"startup-policy-{pid}.json").read_text())
        assert observed.pop("bootstrap_flags") == [original_flags[0], 1, 1]
        assert observed == expected
