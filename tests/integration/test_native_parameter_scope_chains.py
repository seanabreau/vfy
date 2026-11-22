"""Parameter scope applies to every same-name fixture in a shadow chain."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("parent_scope", ["function", "session"])
def test_indirect_function_scope_covers_every_shadow_definition(
    tmp_path: Path, parent_scope: str
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            "from pathlib import Path\n"
            "import pytest\n"
            "def record(message):\n"
            "    with Path('events.txt').open('a') as stream:\n"
            "        stream.write(message + '\\n')\n"
            f"@pytest.fixture(scope={parent_scope!r})\n"
            "def resource(request):\n"
            "    assert request.scope == 'function'\n"
            "    record('parent setup ' + request.param)\n"
            "    yield request.param\n"
            "    record('parent teardown ' + request.param)\n",
        )
        project.write(
            "test_cases.py",
            "import pytest\n"
            "from conftest import record\n"
            "@pytest.fixture\n"
            "def resource(request):\n"
            "    assert request.scope == 'function'\n"
            "    parent = request.getfixturevalue('resource')\n"
            "    assert parent is request.param\n"
            "    record('override setup ' + parent)\n"
            "    yield parent\n"
            "    record('override teardown ' + parent)\n"
            "@pytest.fixture\n"
            "def zeta(request):\n"
            "    return request.param\n"
            "@pytest.fixture\n"
            "def alpha(request):\n"
            "    return request.param\n"
            "@pytest.mark.parametrize('resource,zeta,alpha', "
            "[('amber', 22, 11), ('indigo', 44, 22)], indirect=True)\n"
            "def test_case(resource, zeta, alpha, request):\n"
            "    assert resource is request.getfixturevalue('resource')\n"
            "    assert zeta == 2 * alpha\n"
            "    assert zeta is request.getfixturevalue('zeta')\n"
            "    assert alpha is request.getfixturevalue('alpha')\n"
            "    record('call ' + resource)\n",
        )
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "2 passed" in result.stdout
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            f"{event} {parameter}"
            for parameter in ("amber", "indigo")
            for event in (
                "parent setup",
                "override setup",
                "call",
                "override teardown",
                "parent teardown",
            )
        ]
    )
