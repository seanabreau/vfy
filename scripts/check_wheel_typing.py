"""Check consumer types against an installed wheel outside the source checkout."""

from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from pathlib import Path

from vfy import _native


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    extension = Path(_native.__file__).resolve()
    if extension.is_relative_to(root / "python"):
        raise RuntimeError("install the built wheel before checking its consumer types")
    for name in ("_native.pyi", "py.typed"):
        if not extension.with_name(name).is_file():
            raise RuntimeError(f"installed wheel is missing {name}")
    ty = Path(sysconfig.get_path("scripts")) / ("ty.exe" if sys.platform == "win32" else "ty")
    config = tomllib.loads((root / "ty.toml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="vfy-wheel-types-") as temporary:
        project = Path(temporary)
        consumer = project / "consumer.py"
        consumer.write_text(
            (root / "tests" / "typing" / "consumer.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        # A separate project prevents source-tree declarations from masking missing wheel files.
        rules = "\n".join(
            f"{json.dumps(rule)} = {json.dumps(level)}" for rule, level in config["rules"].items()
        )
        (project / "ty.toml").write_text(
            f"[rules]\n{rules}\n[terminal]\nerror-on-warning = true\n", encoding="utf-8"
        )
        completed = subprocess.run(
            [
                str(ty),
                "check",
                "--project",
                str(project),
                "--python",
                sys.executable,
                str(consumer),
            ],
            cwd=project,
            check=False,
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
