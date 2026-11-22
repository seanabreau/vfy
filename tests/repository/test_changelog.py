from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_release_notes_cli_prints_the_versioned_entry(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "changelog_section.py"
    shutil.copyfile(ROOT / "scripts" / "changelog_section.py", script)
    changelogs = tmp_path / "changelogs"
    changelogs.mkdir()
    (changelogs / "0.0.x.md").write_text(
        "# Changelog\n\n## [0.0.2]\n\nOther release.\n\n"
        "## [0.0.1]\n\n### Added\n\n- Selected release.\n\n"
        "## [0.0.0]\n\nEarlier release.\n",
        encoding="utf-8",
    )
    (changelogs / "1.0.x.md").write_text("## [1.0.0]\n\nUnrelated series.\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), "0.0.1"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "### Added\n\n- Selected release.\n"
