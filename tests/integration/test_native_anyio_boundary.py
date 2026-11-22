"""AnyIO remains a stock-pytest workflow, including with vfy coverage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Unix")
@pytest.mark.parametrize("mode", ["strict", "auto"])
@pytest.mark.parametrize("activation", ["marker", "backend", "dependency"])
def test_native_anyio_advises_stock_pytest_with_coverage(
    tmp_path: Path, mode: str, activation: str
) -> None:
    project = VfyProject(tmp_path)
    marker = "@pytest.mark.anyio\n" if activation == "marker" else ""
    argument = {"marker": "", "backend": "anyio_backend", "dependency": "backend_label"}[activation]
    project.write(
        "pytest.ini",
        "[pytest]\nmarkers = anyio: run using AnyIO\n"
        f"asyncio_mode = {mode}\nasyncio_default_fixture_loop_scope = function\n",
    )
    project.write("app.py", "def answer():\n    return 42\n")
    project.write(
        "test_app.py",
        "from pathlib import Path\n"
        "import anyio\n"
        "import pytest\n"
        "import sniffio\n"
        "from app import answer\n"
        "@pytest.fixture\n"
        "def anyio_backend():\n    return 'trio'\n"
        "@pytest.fixture\n"
        "def backend_label(anyio_backend):\n    return anyio_backend\n"
        f"{marker}"
        f"async def test_answer({argument}):\n"
        "    assert sniffio.current_async_library() == 'trio'\n"
        "    await anyio.sleep(0)\n"
        "    assert answer() == 42\n"
        "    Path('executed').touch()\n",
    )
    for coverage in (False, True):
        args = ["-q", "-p", "no:asyncio"]
        if coverage:
            args.extend(["-p", "no:pytest_cov", "-p", "vfy.pytest_plugin", "--cov=app"])
        result = project.run_pytest(
            *args, load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
        )
        result.expect(0)
        assert (tmp_path / "executed").exists()
        (tmp_path / "executed").unlink()
    assert project.lines("app.py") == [1, 2]
    unhandled = project.run_pytest("-q", load_xdist=False)
    assert not (tmp_path / "executed").exists()
    native = project.run_vfy("test", "--engine=pytest-native", "--all", "--no-cov", "-q", "-rs")
    transcript = native.stdout + native.stderr
    assert native.returncode == unhandled.returncode, transcript
    assert "native execution does not support AnyIO tests" in transcript
    assert "pytest -p no:pytest_cov -p vfy.pytest_plugin --cov" in transcript
    assert "install a suitable plugin" not in transcript
    assert not (tmp_path / "executed").exists()
