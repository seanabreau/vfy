"""Mutation forks share asyncio imports while policies remain child-owned."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="mutation import sharing requires fork")


@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "unregistered"])
def test_asyncio_import_sharing_preserves_policy_and_loop_ownership(
    tmp_path: Path, enabled: bool
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def first():\n    return 1\n\ndef second():\n    return 1\n")
    project.write(
        "conftest.py",
        "import json, os, sys\nfrom pathlib import Path\n\n"
        "owner = int(os.environ['VFY_TEST_CONTROLLER_PID'])\n"
        "def guard_parent_calls(frame, event, argument):\n"
        "    if os.getpid() != owner or event != 'call':\n"
        "        return\n"
        "    module = frame.f_globals.get('__name__', '')\n"
        "    name = frame.f_code.co_name\n"
        "    if (module == 'asyncio.events' and name in {\n"
        "        'get_event_loop_policy', 'set_event_loop_policy', 'new_event_loop',\n"
        "        '_init_event_loop_policy'\n"
        "    }) or (\n"
        "        module in {'asyncio.runners', 'asyncio.base_events'} and name == '__init__'\n"
        "    ) or (\n"
        "        module.startswith('asyncio.') and name == '__init__' and\n"
        "        type(frame.f_locals.get('self')).__name__.endswith('Policy')\n"
        "    ) or (\n"
        "        module == 'vfy.asyncio_plugin' and name in {'event_loop_policy', '_policy'}\n"
        "    ):\n"
        "        raise AssertionError(\n"
        "            'parent acquired asyncio runtime state: ' + module + '.' + name\n"
        "        )\n"
        "def record_inherited_state():\n"
        "    events = sys.modules.get('asyncio.events')\n"
        "    record = {\n"
        "        'imported': 'asyncio' in sys.modules,\n"
        "        'policy_is_none': events is None or events._event_loop_policy is None\n"
        "    }\n"
        "    Path(f'fork-{os.getpid()}.json').write_text(json.dumps(record))\n"
        "if os.getpid() == owner:\n"
        "    sys.setprofile(guard_parent_calls)\n"
        "    os.register_at_fork(after_in_child=record_inherited_state)\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        f"    if {not enabled!r} and os.getpid() == owner:\n"
        "        plugin = config.pluginmanager.get_plugin('vfy.asyncio_plugin')\n"
        "        assert plugin is not None\n"
        "        config.pluginmanager.unregister(plugin)\n",
    )
    if enabled:
        body = (
            "    import asyncio\n"
            "    assert not hasattr(event_loop_policy, 'mutation_owned_state')\n"
            "    event_loop_policy.mutation_owned_state = os.getpid()\n"
            "    loop = asyncio.new_event_loop()\n"
            "    try:\n"
            "        assert not loop.is_closed()\n"
            "        event_loop_policy.set_event_loop(loop)\n"
            "        assert event_loop_policy.get_event_loop() is loop\n"
            "    finally:\n"
            "        event_loop_policy.set_event_loop(None)\n"
            "        loop.close()\n"
        )
        parameter = "event_loop_policy"
    else:
        body = ""
        parameter = ""
    project.write(
        "tests/test_app.py",
        "import os, sys\nimport app\n\n"
        f"def test_values({parameter}):\n"
        + body
        + "    assert (app.first(), app.second()) == (1, 1)\n",
    )
    arguments = [
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--workers",
        "2",
        "--format",
        "json",
    ]
    project.write(
        "driver.py",
        "import json, os, sys\nfrom pathlib import Path\nfrom vfy.cli import main\n"
        "os.environ['VFY_TEST_CONTROLLER_PID'] = str(os.getpid())\n"
        f"result = main({arguments!r})\n"
        "events = sys.modules.get('asyncio.events')\n"
        "Path('parent-state.json').write_text(json.dumps({\n"
        "    'imported': 'asyncio' in sys.modules,\n"
        "    'policy_is_none': events is None or events._event_loop_policy is None\n"
        "}))\n"
        "raise SystemExit(result)\n",
    )

    completed = project.run(sys.executable, "driver.py", env={"VFY_MUTATION_ENGINE": "auto"})
    assert completed.returncode == 0, completed
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 2}, report
    assert report["metrics"]["fast_results"] == 2, report
    parent = json.loads((tmp_path / "parent-state.json").read_text())
    assert parent["policy_is_none"] is True
    if enabled:
        assert parent["imported"] is True
    inherited = [json.loads(path.read_text()) for path in tmp_path.glob("fork-*.json")]
    assert inherited == [parent, parent]
