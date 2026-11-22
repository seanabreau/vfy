"""Authenticated workers restore original codecs before application startup."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import vfy
from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PACKAGE_ROOT = str(Path(vfy.__file__).resolve().parents[1])
_STARTUP = """import os
import sys

def settings():
    return {
        name: {
            'encoding': getattr(sys, '__' + name + '__').encoding,
            'errors': getattr(sys, '__' + name + '__').errors,
        }
        for name in ('stdout', 'stderr')
    }

startup_pid = os.getpid()
startup_settings = settings()
startup_flags = {
    'isolated': sys.flags.isolated,
    'ignore_environment': sys.flags.ignore_environment,
    'no_site': sys.flags.no_site,
}
"""

_TEST = """import json
import os
from pathlib import Path
import sys

def test_BODY():
    assert 'sitecustomize' in sys.modules
    startup = sys.modules['sitecustomize']
    assert startup.startup_pid == os.getpid()
    observed = {
        'pid': os.getpid(),
        'startup': startup.startup_settings,
        'body': startup.settings(),
        'flags': startup.startup_flags,
        'startup_origin': startup.__file__,
    }
    assert observed['startup'] == observed['body']
    Path('body-BODY.json').write_text(json.dumps(observed))
"""


@pytest.mark.parametrize("ioencoding", ["latin-1:ignore", "utf-8:strict"])
def test_worker_original_stream_codecs_match_stock_before_sitecustomize_and_in_bodies(
    tmp_path: Path, ioencoding: str
) -> None:
    records = {}
    for route in ("stock", "candidate"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("sitecustomize.py", _STARTUP)
        for name in ("first", "second"):
            project.write(f"test_{name}.py", _TEST.replace("BODY", name))
        environment = {
            "PYTHONPATH": os.pathsep.join((str(project.root), _PACKAGE_ROOT)),
            "PYTHONIOENCODING": ioencoding,
        }
        if route == "stock":
            result = project.run_python("-m", "pytest", "-q", env=environment)
        else:
            result = project.run_vfy(
                "test",
                "--engine=native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "--native-result-json=execution.json",
                "-q",
                env=environment,
            )
        result.expect(0)
        bodies = [
            json.loads((project.root / f"body-{name}.json").read_text())
            for name in ("first", "second")
        ]
        assert all(
            body["startup_origin"] == str(project.root / "sitecustomize.py") for body in bodies
        )
        records[route] = bodies
        if route == "candidate":
            execution = json.loads((project.root / "execution.json").read_text())
            assert execution["engine"] == "vfy-independent-v1"
            assert execution["counts"]["passed"] == 2
            pids = {body["pid"] for body in bodies}
            assert len(pids) == 2
            assert set(execution["execution_topology"]["worker_pids"]) == pids
            assert all(
                body["flags"] == {"isolated": 0, "ignore_environment": 0, "no_site": 1}
                for body in bodies
            )
        else:
            assert len({body["pid"] for body in bodies}) == 1
    expected = records["stock"][0]["startup"]
    assert expected["stderr"]["errors"] == "backslashreplace"
    for bodies in records.values():
        assert all(body["startup"] == body["body"] == expected for body in bodies)
