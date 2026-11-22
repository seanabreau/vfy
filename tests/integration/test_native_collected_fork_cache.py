"""The default forked route keeps pytest's persisted cache like a stock run."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="collected fork workers require Linux"
)

_UNSET_ROUTE_ENV: dict[str, str | None] = {
    "VFY_WORKER_START": None,
    "VFY_WORKER_COLLECTION": None,
    "VFY_NATIVE_SCHEDULING": None,
}

_MODULE = """\
import os

with open("imports.log", "a") as stream:
    stream.write(f"{os.getpid()}\\n")


def test_passes():
    pass


def test_fixed_later():
    assert FIXED


def test_stays_broken():
    assert False


FIXED = False
"""


def _write_project(project: VfyProject, *, fixed: bool) -> None:
    for name in ("first", "second"):
        project.write(
            f"tests/test_{name}.py",
            _MODULE.replace("FIXED = False", f"FIXED = {fixed}"),
        )


def _cache(project: VfyProject, name: str) -> object:
    return json.loads((project.root / ".pytest_cache/v/cache" / name).read_text())


def _executed(project: VfyProject) -> list[str]:
    root = ET.parse(project.root / "results.xml").getroot()
    return sorted(f"{case.get('classname')}.{case.get('name')}" for case in root.iter("testcase"))


@pytest.mark.parametrize("selector", ["--lf", "--ff", "--nf"])
def test_default_forked_route_selects_and_records_like_stock_pytest(
    tmp_path: Path, selector: str
) -> None:
    native = VfyProject(tmp_path / "native")
    reference = VfyProject(tmp_path / "reference")
    for project in (native, reference):
        _write_project(project, fixed=False)
    stock = reference.run_pytest("-q", "-p", "vfy.pytest_plugin", "--junitxml=results.xml")
    stock.expect(1)
    first = native.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "-q",
        "--junitxml=results.xml",
        env=_UNSET_ROUTE_ENV,
    )
    first.expect(1)
    imports = set((native.root / "imports.log").read_text().split())
    assert len(imports) == 1, "the workers fork from one import"
    assert _cache(native, "lastfailed") == _cache(reference, "lastfailed")
    assert _cache(native, "nodeids") == _cache(reference, "nodeids")

    for project in (native, reference):
        _write_project(project, fixed=True)
    stock = reference.run_pytest(
        "-q", "-p", "vfy.pytest_plugin", "--junitxml=results.xml", selector
    )
    second = native.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "-q",
        "--junitxml=results.xml",
        selector,
        env=_UNSET_ROUTE_ENV,
    )

    assert second.returncode == stock.returncode == 1, second.stdout + second.stderr
    assert _executed(native) == _executed(reference)
    assert _cache(native, "lastfailed") == _cache(reference, "lastfailed")
    assert _cache(native, "nodeids") == _cache(reference, "nodeids")
