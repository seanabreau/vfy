"""Watch root reloads retain notifications for overlapping source directories."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform != "linux", reason="inotify descriptor sharing is Linux-specific")
@pytest.mark.parametrize("expand", [False, True])
def test_overlapping_root_reload_rejects_edit_revert_results(tmp_path: Path, expand: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("external/pkg/app.py", "VALUE = 1\n")
    project.write(
        "proof.py",
        "from pathlib import Path\n"
        "from vfy.native import WatchSession\n"
        "external = Path('external').resolve()\n"
        "package = external / 'pkg'\n"
        f"old, new = (package, external) if {expand!r} else (external, package)\n"
        "session = WatchSession([str(old)], debounce_ms=0)\n"
        "session.set_roots([str(new)])\n"
        "session.begin_run()\n"
        "source = package / 'app.py'\n"
        "source.write_text('VALUE = 2\\n')\n"
        "source.write_text('VALUE = 1\\n')\n"
        "session.wait_for_batch(1000)\n"
        "assert session.finish_run(0, True), 'edit/revert result must be obsolete'\n"
        "assert str(source) in session.wait_for_batch(1000)['paths']\n"
        "session.stop()\n",
    )
    result = project.run_python("proof.py")
    result.expect(0)
