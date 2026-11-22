"""Fixture cleanup failures override intercepted control exits at session finish."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_native_parallel import _native_parallel_available
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native parallel tests require Linux or macOS and a supported pytest version",
)


@pytest.mark.parametrize("returncode", [0, 7])
@pytest.mark.parametrize("grouped", [False, True])
def test_interrupted_fixture_cleanup_retains_priority_groups_and_chains(
    tmp_path: Path, returncode: int, grouped: bool
) -> None:
    expected: object | None = None
    for route in ("stock", "serial", "file", "cases"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "def exception_tree(error):\n"
            "    grouped = isinstance(error, BaseExceptionGroup)\n"
            "    return {\n"
            "        'type': type(error).__name__,\n"
            "        'message': None if grouped else str(error),\n"
            "        'members': [exception_tree(member) for member in error.exceptions]\n"
            "                   if grouped else [],\n"
            "        'cause': exception_tree(error.__cause__) if error.__cause__ else None,\n"
            "        'context': exception_tree(error.__context__) if error.__context__ else None,\n"
            "        'suppress_context': error.__suppress_context__,\n"
            "    }\n"
            "@pytest.hookimpl(wrapper=True)\n"
            "def pytest_sessionfinish():\n"
            "    try:\n"
            "        yield\n"
            "    except BaseException as error:\n"
            "        destination = Path(f'error-{os.getpid()}.json')\n"
            "        temporary = destination.with_suffix('.tmp')\n"
            "        temporary.write_text(json.dumps(exception_tree(error)))\n"
            "        temporary.replace(destination)\n"
            "        raise\n",
        )
        project.write(
            "test_exit.py",
            "import os\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "@pytest.fixture(scope='session')\n"
            "def outer():\n"
            "    yield\n"
            "    Path(f'outer-finished-{os.getpid()}').touch()\n"
            f"    if {grouped}:\n"
            "        raise OSError('outer cleanup sentinel')\n"
            "@pytest.fixture\n"
            "def inner(outer):\n"
            "    yield\n"
            "    Path(f'inner-finished-{os.getpid()}').touch()\n"
            "    try:\n"
            "        raise KeyError('context sentinel')\n"
            "    except KeyError as cause:\n"
            "        raise ValueError('inner cleanup sentinel') from cause\n"
            "@pytest.mark.parametrize('value', range(8))\n"
            "def test_exit(value, inner):\n"
            f"    pytest.exit('requested exit sentinel', returncode={returncode})\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1" if route == "serial" else "--workers=4",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"VFY_NATIVE_SCHEDULING": route} if route in {"file", "cases"} else None,
            )
        )
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert "inner cleanup sentinel" in output
        assert "context sentinel" in output
        if grouped:
            assert "outer cleanup sentinel" in output
        assert "requested pytest exit: requested exit sentinel" not in output
        records = list(project.root.glob("error-*.json"))
        assert 1 <= len(records) <= (4 if route == "cases" else 1), output
        for path in records:
            observed = json.loads(path.read_text())
            pid = path.stem.removeprefix("error-")
            assert (project.root / f"inner-finished-{pid}").exists(), output
            assert (project.root / f"outer-finished-{pid}").exists(), output
            if route == "stock":
                expected = observed
            else:
                assert observed == expected, output
