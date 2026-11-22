"""Check that an installed vfy distribution works without a Rust toolchain."""

from __future__ import annotations

import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from vfy.data import CoverageData as PublicCoverageData
from vfy.native import CollectorCore, CoverageData, TraceStore, analyze_file, parser_backend


def main() -> int:
    backend = parser_backend()
    if backend != "ruff":
        print(f"unexpected parser backend: {backend!r}", file=sys.stderr)
        return 1
    if TraceStore(False).branch is not False:
        print("TraceStore(False).branch is not False", file=sys.stderr)
        return 1
    if CoverageData(False).has_arcs is not False:
        print("CoverageData(False).has_arcs is not False", file=sys.stderr)
        return 1
    analysis = analyze_file(__file__, [], False)
    if analysis.source_missing or not analysis.executable:
        print("analyze_file could not analyze the wheel smoke test", file=sys.stderr)
        return 1
    CollectorCore(False, [], "vfy/native.py", [], [])
    executable = "vfy.exe" if sys.platform == "win32" else "vfy"
    script = Path(sysconfig.get_path("scripts")) / executable
    subprocess.run([script, "--version"], check=True)

    # Exercise the installed command across the real coverage boundary. Import
    # checks alone can pass while a wheel is missing a runtime file or cannot
    # write a readable schema-7 data file.
    with tempfile.TemporaryDirectory(prefix="vfy-wheel-smoke-") as temporary:
        root = Path(temporary)
        (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (root / "run_case.py").write_text(
            "from app import value\nassert value() == 1\n", encoding="utf-8"
        )
        data_file = root / ".coverage"
        command = [sys.executable, "-m", "vfy"]
        subprocess.run(
            [
                *command,
                "coverage",
                "run",
                "--source",
                str(root),
                "--data-file",
                str(data_file),
                str(root / "run_case.py"),
            ],
            cwd=root,
            check=True,
        )
        data = PublicCoverageData(str(data_file))
        data.read()
        if not data.measured_files():
            print("coverage smoke test wrote no measured files", file=sys.stderr)
            return 1
        subprocess.run(
            [
                *command,
                "coverage",
                "report",
                "--data-file",
                str(data_file),
                "--fail-under",
                "100",
            ],
            cwd=root,
            check=True,
        )
        if sys.platform == "win32":
            # Windows wheels provide the coverage commands and API checked above.
            return 0
        (root / "test_app.py").write_text(
            "import pytest\nfrom app import value\n\n"
            "@pytest.fixture\ndef expected():\n    return 1\n\n"
            "@pytest.mark.parametrize('scale', [1, 2])\n"
            "def test_value(expected, scale):\n    assert value() * scale == expected * scale\n",
            encoding="utf-8",
        )
        for engine in ([], ["--engine=native"]):
            subprocess.run(
                [*command, "test", *engine, "--no-cov", "test_app.py"], cwd=root, check=True
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
