"""Stopped collectors release monitoring masks and retain rejected transfers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize("branch", [False, True])
def test_stopped_collector_releases_local_masks_before_slot_reuse(
    tmp_path: Path, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def value():\n    return 42\n")
    project.write(
        "ownership.py",
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from vfy import Coverage\n"
        "from subject import value\n"
        "monitoring = sys.monitoring\n"
        "slot = monitoring.COVERAGE_ID\n"
        f"cov = Coverage(data_file=None, config_file=False, source=['.'], branch={branch!r})\n"
        "cov.start()\n"
        "assert value() == 42\n"
        "assert monitoring.get_local_events(slot, value.__code__) != 0\n"
        "cov.stop()\n"
        "mask = monitoring.get_local_events(slot, value.__code__)\n"
        "assert monitoring.get_tool(slot) is None\n"
        "seen = []\n"
        "def observe(code, line):\n"
        "    if code is value.__code__:\n"
        "        seen.append(line)\n"
        "monitoring.use_tool_id(slot, 'next owner')\n"
        "try:\n"
        "    monitoring.register_callback(slot, monitoring.events.LINE, observe)\n"
        "    monitoring.set_events(slot, monitoring.events.NO_EVENTS)\n"
        "    monitoring.restart_events()\n"
        "    assert value() == 42\n"
        "finally:\n"
        "    monitoring.set_local_events(slot, value.__code__, 0)\n"
        "    monitoring.register_callback(slot, monitoring.events.LINE, None)\n"
        "    monitoring.free_tool_id(slot)\n"
        "cov.start()\n"
        "assert value() == 42\n"
        "cov.stop()\n"
        "path = str(Path('subject.py').resolve())\n"
        "print(json.dumps([mask, seen, cov.get_data().lines(path)]))\n",
    )

    result = project.run_python("ownership.py")

    result.expect(0)
    assert json.loads(result.stdout) == [0, [], [2]]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork requires POSIX")
@pytest.mark.parametrize("branch", [False, True])
def test_fork_restored_active_frames_release_local_masks(tmp_path: Path, branch: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "def fork_and_stop(cov):\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        cov.stop()\n"
        "        monitoring = sys.monitoring\n"
        "        codes = [fork_and_stop.__code__, sys._getframe(1).f_code]\n"
        "        masks = [monitoring.get_local_events(monitoring.COVERAGE_ID, code)\n"
        "                 for code in codes]\n"
        "        Path('child.json').write_text(json.dumps(masks))\n"
        "        os._exit(0)\n"
        "    _, status = os.waitpid(pid, 0)\n"
        "    assert os.waitstatus_to_exitcode(status) == 0\n",
    )
    project.write(
        "collect.py",
        "import json\n"
        "from pathlib import Path\n"
        "from vfy import Coverage\n"
        "from subject import fork_and_stop\n"
        f"cov = Coverage(data_file=None, config_file=False, source=['.'], branch={branch!r})\n"
        "cov.start()\n"
        "fork_and_stop(cov)\n"
        "cov.stop()\n"
        "print(Path('child.json').read_text())\n",
    )

    result = project.run_python("collect.py")

    result.expect(0)
    assert json.loads(result.stdout) == [0, 0]


@pytest.mark.parametrize("branch", [False, True])
def test_failed_plugin_discovery_preserves_measurements_for_retry(
    tmp_path: Path, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def value():\n    return 42\n")
    project.write(
        "collect.py",
        "import importlib\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from subject import value\n"
        "tool = sys.argv[1]\n"
        "Coverage = importlib.import_module(tool).Coverage\n"
        "CoveragePlugin = importlib.import_module(tool + '.plugin').CoveragePlugin\n"
        "class Plugin(CoveragePlugin):\n"
        "    fail = True\n"
        "    def find_executable_files(self, source):\n"
        "        if self.fail:\n"
        "            self.fail = False\n"
        "            raise RuntimeError('discovery failed once')\n"
        "        return []\n"
        "plugin = Plugin()\n"
        "def initialize(registration):\n"
        "    registration.add_file_tracer(plugin)\n"
        "cov = Coverage(data_file=None, config_file=False, source=['.'],\n"
        f"               branch={branch!r}, plugins=[initialize])\n"
        "cov.start()\n"
        "assert value() == 42\n"
        "try:\n"
        "    cov.stop()\n"
        "    # Coverage.py discovers sources at get_data; vfy does so at stop.\n"
        "    cov.get_data()\n"
        "except RuntimeError as error:\n"
        "    assert str(error) == 'discovery failed once'\n"
        "else:\n"
        "    raise AssertionError('plugin discovery did not reject the transfer')\n"
        "cov.stop()\n"
        "path = str(Path('subject.py').resolve())\n"
        "before = cov.get_data().lines(path)\n"
        "cov.start()\n"
        "assert value() == 42\n"
        "cov.stop()\n"
        "data = cov.get_data()\n"
        "print(json.dumps([before, data.lines(path), sorted(data.arcs(path) or [])]))\n",
    )

    reports = {}
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
        reports[tool] = json.loads(result.stdout)
    assert (
        reports["vfy"]
        == reports["coverage"]
        == [
            [2],
            [2],
            [[-1, 2], [2, -1]] if branch else [],
        ]
    )


def test_conflicting_discovered_tracers_preserve_measurements_for_retry(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def value():\n    return 42\n")
    project.write(
        "collect.py",
        "import importlib\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from subject import value\n"
        "tool = sys.argv[1]\n"
        "Coverage = importlib.import_module(tool).Coverage\n"
        "CoveragePlugin = importlib.import_module(tool + '.plugin').CoveragePlugin\n"
        "path = str(Path('subject.py').resolve())\n"
        "rejection = ValueError if tool == 'vfy' else "
        "importlib.import_module('coverage.exceptions').DataError\n"
        "class First(CoveragePlugin):\n"
        "    def find_executable_files(self, source):\n"
        "        return [path]\n"
        "class Second(CoveragePlugin):\n"
        "    conflict = True\n"
        "    def find_executable_files(self, source):\n"
        "        if self.conflict:\n"
        "            self.conflict = False\n"
        "            return [path]\n"
        "        return []\n"
        "def initialize(registration):\n"
        "    registration.add_file_tracer(First())\n"
        "    registration.add_file_tracer(Second())\n"
        "cov = Coverage(data_file=None, config_file=False, source=['.'], plugins=[initialize])\n"
        "cov.start()\n"
        "assert value() == 42\n"
        "try:\n"
        "    cov.stop()\n"
        "    cov.get_data()\n"
        "except rejection as error:\n"
        "    assert 'conflicting file tracer' in str(error).lower(), str(error)\n"
        "else:\n"
        "    raise AssertionError('conflicting discovered tracers were accepted')\n"
        "cov.stop()\n"
        "data = cov.get_data()\n"
        "print(json.dumps(data.lines(path)))\n",
    )

    reports = {}
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
        reports[tool] = json.loads(result.stdout)
    assert reports["vfy"] == reports["coverage"] == [2]


@pytest.mark.parametrize("branch", [False, True])
def test_failed_source_package_discovery_preserves_measurements_for_retry(
    tmp_path: Path, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def value():\n    return 42\n")
    project.write(
        "collect.py",
        "import importlib\n"
        "import json\n"
        "import sys\n"
        "import types\n"
        "from pathlib import Path\n"
        "import subject\n"
        "class Module(types.ModuleType):\n"
        "    fail = False\n"
        "    def __getattribute__(self, name):\n"
        "        if name == '__file__' and self.fail:\n"
        "            self.fail = False\n"
        "            raise RuntimeError('source discovery failed once')\n"
        "        return super().__getattribute__(name)\n"
        "subject.__class__ = Module\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "cov = Coverage(data_file=None, config_file=False, source_pkgs=['subject'],\n"
        f"               branch={branch!r})\n"
        "cov.start()\n"
        "assert subject.value() == 42\n"
        "subject.fail = True\n"
        "try:\n"
        "    cov.stop()\n"
        "    cov.get_data()\n"
        "except RuntimeError as error:\n"
        "    assert str(error) == 'source discovery failed once'\n"
        "else:\n"
        "    raise AssertionError('source discovery did not reject the transfer')\n"
        "monitoring = sys.monitoring\n"
        "assert monitoring.get_tool(monitoring.COVERAGE_ID) is None\n"
        "assert monitoring.get_local_events(monitoring.COVERAGE_ID, subject.value.__code__) == 0\n"
        "cov.stop()\n"
        "data = cov.get_data()\n"
        "path = str(Path('subject.py').resolve())\n"
        "print(json.dumps([data.lines(path), sorted(data.arcs(path) or [])]))\n",
    )

    reports = {}
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
        reports[tool] = json.loads(result.stdout)
    assert reports["vfy"] == reports["coverage"] == [[2], [[-1, 2], [2, -1]] if branch else []]
