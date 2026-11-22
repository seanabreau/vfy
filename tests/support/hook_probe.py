"""Run a plug-in manager probe under stock pytest and vfy and return what each recorded."""

from __future__ import annotations

from pathlib import Path

from .vfy_project import VfyProject

_CONFTEST = """\
import contextlib
import io
import os
from pathlib import Path


def pytest_configure(config):
    source = Path("probe_source.py").read_text()
    namespace = {
        "__file__": "probe_source.py",
        "registry": config.pluginmanager,
        "case": os.environ["PROBE_CASE"],
    }
    output = io.StringIO()
    existing = {name for name, _ in config.pluginmanager.list_name_plugin()}
    with contextlib.redirect_stdout(output):
        try:
            exec(compile(source, "probe_source.py", "exec"), namespace)
        finally:
            # A probe may leave partial registrations that later pytest hooks would reject.
            for name, _ in config.pluginmanager.list_name_plugin():
                if name not in existing:
                    config.pluginmanager.unregister(name=name)
    Path("probe.out").write_text(output.getvalue())
"""


def run_manager_probe(tmp_path: Path, source: str, case: str) -> tuple[str, str]:
    """Run ``source`` against ``config.pluginmanager`` in stock pytest and in vfy.

    The source sees ``registry`` (the plug-in manager) and ``case`` and prints its
    observations to stdout. Returns the recorded output of stock pytest and of vfy.
    """

    outputs = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _CONFTEST)
        project.write("probe_source.py", source)
        project.write("test_probe.py", "def test_probe():\n    pass\n")
        result = project.run_engine(engine, "-q", env={"PROBE_CASE": case})
        assert result.returncode == 0, f"{engine}: {result.stdout}{result.stderr}"
        outputs.append((project.root / "probe.out").read_text())
    return outputs[0], outputs[1]
