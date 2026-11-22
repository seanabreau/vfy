"""Owned asyncio support acquires policies only when a test needs a runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "owned-plugin"])
def test_owned_sync_tests_do_not_acquire_an_unused_policy(tmp_path: Path, explicit: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "def unused_policy():\n"
        "    raise AssertionError('unused policy acquired')\n"
        "def pytest_fixture_setup(fixturedef):\n"
        "    if fixturedef.argname == 'event_loop_policy':\n"
        "        fixturedef.func = unused_policy\n",
    )
    project.write(
        "test_sync.py",
        "import json\nfrom pathlib import Path\n"
        "def test_sync(request):\n"
        "    Path('names.json').write_text(json.dumps(request.fixturenames))\n",
    )

    result = project.run_vfy(
        "test", "--no-cov", "-q", *(["-p", "vfy.independent.asyncio_support"] if explicit else [])
    )

    result.expect(0)
    assert json.loads((tmp_path / "names.json").read_text()) == ["request"]


def test_owned_async_test_acquires_and_closes_the_overridden_policy(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import asyncio\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.fixture(scope='session')\ndef event_loop_policy():\n"
        "    class Policy(asyncio.DefaultEventLoopPolicy):\n"
        "        def new_event_loop(self):\n"
        "            loop = super().new_event_loop()\n"
        "            close = loop.close\n"
        "            def closed():\n"
        "                close()\n                Path('loop-closed').touch()\n"
        "            loop.close = closed\n"
        "            return loop\n"
        "    Path('policy-acquired').touch()\n"
        "    yield Policy()\n"
        "    assert Path('loop-closed').is_file()\n"
        "    Path('policy-finalized').touch()\n",
    )
    project.write(
        "test_async.py",
        "import asyncio\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.mark.asyncio\nasync def test_async():\n"
        "    assert asyncio.get_running_loop().is_running()\n"
        "    Path('awaited').touch()\n",
    )

    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")

    result.expect(0)
    assert {path.name for path in tmp_path.iterdir() if path.is_file()} >= {
        "policy-acquired",
        "awaited",
        "loop-closed",
        "policy-finalized",
    }
