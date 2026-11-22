"""Verify controlled corpus inputs before collection and clock cleanup."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from validation.pytest_runner.real_packages import corpus

from tests.support import VfyProject


def test_collection_clock_recipe_binds_helper_and_environment() -> None:
    entry = {"collection_time": "2020-01-02T03:04:05"}
    environment = {"PYTHONPATH": "/existing", "PYTEST_PLUGINS": "other"}
    corpus._configure_collection_clock(entry, environment)
    assert environment["VFY_CORPUS_COLLECTION_CLOCK"] == entry["collection_time"]
    assert environment["PYTEST_PLUGINS"] == "other,corpus_clock"
    assert environment["PYTHONPATH"] == os.pathsep.join((str(corpus.HERE), "/existing"))
    assert corpus._recipe(entry) != corpus._recipe({})
    with pytest.raises(ValueError, match="naive ISO datetime"):
        corpus._recipe({"collection_time": "2020-01-02T03:04:05+00:00"})


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires Unix")
@pytest.mark.parametrize("broken", [False, True])
def test_collection_clock_is_shared_and_restored(tmp_path: Path, broken: bool) -> None:
    freezegun = pytest.importorskip("freezegun")
    dateutil = pytest.importorskip("dateutil")
    project = VfyProject(tmp_path / "project")
    project.write(
        "verifier.py",
        """
import datetime
from pathlib import Path

def pytest_configure(config):
    def verify():
        assert datetime.datetime.now().year != 2020
        Path("clock-restored").write_text("yes")
    config.add_cleanup(verify)
""",
    )
    project.write(
        "test_clock.py",
        """
import datetime
import pytest

@pytest.mark.parametrize("value", [datetime.datetime.now().strftime("%H:%M:%S %Y-%m-%d")])
def test_clock(value):
    assert value == "03:04:05 2020-01-02"
    assert datetime.datetime.now().year != 2020
"""
        + ('\nraise RuntimeError("collection error")\n' if broken else ""),
    )
    entry = {"collection_time": "2020-01-02T03:04:05"}
    env = {
        "PYTHONPATH": os.pathsep.join(
            [
                str(Path(freezegun.__file__).parent.parent),
                str(Path(dateutil.__file__).parent.parent),
            ]
        ),
        "PYTEST_PLUGINS": "verifier",
    }
    corpus._configure_collection_clock(entry, env)
    # The verifier registers first; its cleanup runs after the clock cleanup.
    env["PYTEST_PLUGINS"] = "corpus_clock,verifier"
    stock = project.run_python("-m", "pytest", "-q", "--junitxml=stock.xml", env=env)
    assert stock.returncode == (2 if broken else 0), stock.stdout + stock.stderr
    marker = project.root / "clock-restored"
    assert marker.read_text() == "yes"
    marker.unlink()
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "--junitxml=vfy.xml", env=env
    )
    assert candidate.returncode == (2 if broken else 0), candidate.stdout + candidate.stderr
    assert marker.read_text() == "yes"
    if not broken:
        names = []
        for file in ("stock.xml", "vfy.xml"):
            root = ET.parse(project.root / file).getroot()
            names.append([case.attrib["name"] for case in root.iter("testcase")])
        assert names == [["test_clock[03:04:05 2020-01-02]"]] * 2
