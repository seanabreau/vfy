"""Disabled coverage leaves the native execution boundary independent of its API."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="native execution requires Linux or macOS",
)
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("coverage_enabled", [False, True])
def test_native_execution_loads_coverage_modules_only_when_enabled(
    tmp_path: Path, workers: int, coverage_enabled: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("product.py", "def answer():\n    return 42\n")
    for name in ("one", "two"):
        project.write(
            f"test_{name}.py",
            "from pathlib import Path\n"
            "from product import answer\n\n"
            f"def test_{name}(cov):\n"
            "    assert cov is None\n"
            "    assert answer() == 42\n"
            f"    Path('executed-{name}').touch()\n",
        )
    probe = project.write(
        "probe/sitecustomize.py",
        "import importlib.abc\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "class CoverageImportGuard(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname in {'vfy.api', 'vfy.data'}:\n"
        "            Path('coverage-import-attempt').touch()\n"
        "            raise ImportError('coverage API import blocked by startup guard')\n"
        "sys.meta_path.insert(0, CoverageImportGuard())\n"
        "Path('startup-guard-installed').touch()\n",
    )

    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        f"--workers={workers}",
        "--cov=product",
        "--cov-report=",
        *([] if coverage_enabled else ["--no-cov"]),
        "-q",
        env={"PYTHONPATH": str(probe.parent)},
    )

    assert (tmp_path / "startup-guard-installed").exists()
    if coverage_enabled:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "coverage API import blocked by startup guard" in result.stdout + result.stderr
        assert (tmp_path / "coverage-import-attempt").exists()
        assert not list(tmp_path.glob("executed-*"))
    else:
        result.expect(0)
        assert "2 passed" in result.stdout
        assert (tmp_path / "executed-one").exists()
        assert (tmp_path / "executed-two").exists()
        assert not (tmp_path / "coverage-import-attempt").exists()
    assert not (tmp_path / ".coverage").exists()
