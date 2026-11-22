"""Admit pytest's warning cleanup without admitting user callbacks inside it."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = [
    requires_native_pytest,
    pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux"),
]

_ENV = {
    "VFY_WORKER_START": "fork",
    "VFY_WORKER_COLLECTION": "0",
    "VFY_NATIVE_SCHEDULING": "cases",
}


def test_collected_fork_configured_warnings_match_stock_pytest(tmp_path: Path) -> None:
    outcomes = {}
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini", "[pytest]\nfilterwarnings = error:configured warning:UserWarning\n"
        )
        for label in ("one", "two"):
            project.write(
                f"test_{label}.py",
                "import json, os, warnings\nfrom pathlib import Path\nimport pytest\n"
                "with Path('imports.txt').open('a') as stream:\n"
                "    stream.write(str(os.getpid()) + '\\n')\n"
                "@pytest.mark.filterwarnings('ignore:ignored warning:UserWarning')\n"
                "def test_value():\n"
                "    with pytest.raises(UserWarning, match='configured warning'):\n"
                "        warnings.warn('configured warning', UserWarning)\n"
                "    warnings.warn('ignored warning', UserWarning)\n"
                f"    Path('body.{label}.json').write_text(json.dumps(os.getpid()))\n",
            )
        arguments = ["-q", "--capture=sys", "--junitxml=results.xml"]
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                *arguments,
                env=_ENV,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout
        imports = set((project.root / "imports.txt").read_text().splitlines())
        assert len(imports) == 1, "collected fork must import only in the controller"
        bodies = {str(json.loads(path.read_text())) for path in project.root.glob("body.*.json")}
        assert len(bodies) == (1 if engine == "stock" else 2)
        if engine == "pytest-native":
            assert imports.isdisjoint(bodies)
        cases = ET.parse(project.root / "results.xml").getroot().iter("testcase")
        outcomes[engine] = [(case.get("classname"), case.get("name"), len(case)) for case in cases]
    assert outcomes["pytest-native"] == outcomes["stock"]


@pytest.mark.skipif(
    tuple(int(part) for part in pytest.version_tuple[:2]) < (9, 2),
    reason="pytest 9.2 owns the configured-warning context",
)
@pytest.mark.parametrize("nested", [False, True], ids=["callback", "nested-stack"])
def test_collected_fork_rejects_user_cleanup_inside_a_warning_stack(
    tmp_path: Path, nested: bool
) -> None:
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            "import os\nfrom contextlib import ExitStack\nfrom pathlib import Path\n"
            "def pytest_configure(config):\n"
            "    stack = ExitStack()\n"
            "    stack.enter_context(config._catch_configured_warnings(record=False))\n"
            "    def foreign_cleanup():\n"
            "        with Path('cleanup.txt').open('a') as stream:\n"
            "            stream.write(str(os.getpid()) + '\\n')\n"
            "    stack.callback(foreign_cleanup)\n"
            + (
                "    outer = ExitStack()\n    outer.enter_context(stack)\n    stack = outer\n"
                if nested
                else ""
            )
            + "    config.add_cleanup(stack.close)\n"
            "    Path('registered.txt').write_text(str(os.getpid()))\n",
        )
        project.write(
            "test_body.py",
            "from pathlib import Path\ndef test_body():\n    Path('body.txt').touch()\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "-q",
                env=_ENV,
            )
        )
        assert (project.root / "registered.txt").exists(), result.stdout + result.stderr
        assert result.returncode == (0 if engine == "stock" else 4), result.stdout + result.stderr
        assert (project.root / "body.txt").exists() == (engine == "stock")
        assert (project.root / "cleanup.txt").read_text().splitlines() == [
            (project.root / "registered.txt").read_text()
        ]
        if engine == "pytest-native":
            assert "unreviewed config cleanup callback" in result.stdout + result.stderr
