"""Collected mutation execution keeps callable bindings and runtime state fresh."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(os.name != "posix", reason="collected mutation execution requires fork")
@pytest.mark.parametrize("asynchronous", [False, True])
def test_mutation_children_preserve_aliases_and_isolate_dynamic_fixture_state(
    tmp_path: Path, asynchronous: bool
) -> None:
    project = VfyProject(tmp_path)
    declaration = "async def" if asynchronous else "def"
    project.write(
        "app.py",
        f"{declaration} first():\n    return 1\n\n{declaration} second():\n    return 2\n",
    )
    project.write(
        "conftest.py",
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.fixture(scope='session')\n"
        "def dynamic_state():\n"
        "    state = []\n"
        "    with Path('lifecycle.log').open('a') as stream:\n"
        "        stream.write(f'{os.getpid()} setup\\n')\n"
        "    yield state\n"
        "    assert state == ['used'], 'fixture state escaped its mutation child'\n"
        "    with Path('lifecycle.log').open('a') as stream:\n"
        "        stream.write(f'{os.getpid()} teardown\\n')\n",
    )
    call = "await callable()" if asynchronous else "callable()"
    mark = "@pytest.mark.asyncio\n" if asynchronous else ""
    project.write(
        "test_app.py",
        "import pytest\n"
        "from app import first, second\n\n"
        f"{mark}"
        "@pytest.mark.parametrize('callable,expected', [(first, 1), (second, 2)])\n"
        f"{declaration} test_alias(request, callable, expected):\n"
        "    state = request.getfixturevalue('dynamic_state')\n"
        "    if not state:\n"
        "        state.append('used')\n"
        "    assert state == ['used']\n"
        f"    assert {call} == expected\n",
    )
    outcomes: list[list[tuple[str, str | None, str | None]]] = []
    for engine in ("auto", "portable"):
        completed = project.run_vfy(
            "mutate",
            "--engine=pytest-native",
            "--all",
            "--source",
            "app.py",
            "--operator",
            "integer_plus_one",
            "--max-mutations",
            "2",
            "--no-cache",
            "--format",
            "json",
            "-j",
            "1",
            env={
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "VFY_MUTATION_ENGINE": engine,
            },
        )
        completed.expect(0)
        report = json.loads(completed.stdout)
        outcomes.append(
            [(row["status"], row["killer"], row["phase"]) for row in report["mutations"]]
        )
        if engine == "auto":
            assert report["metrics"]["fast_results"] == 2, report
        assert [row[0] for row in outcomes[-1]] == ["protected", "protected"], report

    assert outcomes[0] == outcomes[1]
    lifecycle = Counter(
        tuple(line.split()) for line in (tmp_path / "lifecycle.log").read_text().splitlines()
    )
    assert lifecycle
    for pid, _ in lifecycle:
        assert lifecycle[(pid, "setup")] == lifecycle[(pid, "teardown")] == 1, lifecycle
