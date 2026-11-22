"""Compare full and affected vfy runs after an exact upstream commit diff.

Run only after validating the parent baseline and changed-tree parity. Paths
passed here must refer to a dedicated checkout and an exclusive artifact root.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def junit(path: Path) -> dict[tuple[str, str], str]:
    result = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        key = (case.get("classname", ""), case.get("name", ""))
        if key in result:
            raise ValueError(f"duplicate JUnit identity: {key}")
        result[key] = next(
            (name for name in ("failure", "error", "skipped") if case.find(name) is not None),
            "passed",
        )
    return result


def report(path: Path) -> tuple[object, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["files"], data["totals"]


# Keep setup and sample validation together to preserve the measured protocol.
def main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--engine", choices=("native", "pytest-native"), required=True)
    parser.add_argument("--scheduler", choices=("file", "scope", "demand"), required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--full-junit", type=Path, required=True)
    parser.add_argument("--selected-junit", type=Path, required=True)
    parser.add_argument("--full-report", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--build-log", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--revision", required=True, help="vfy source revision")
    parser.add_argument("--parent", required=True, help="upstream parent revision")
    parser.add_argument("--child", required=True, help="upstream child revision")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=15)
    args = parser.parse_args()
    root, out, baseline = (p.resolve() for p in (args.root, args.artifacts, args.baseline))
    python = args.python.absolute()
    if args.warmups < 3 or args.runs < 15:
        raise ValueError("publication requires three warmups and fifteen runs")
    expected_patch = digest(args.patch.read_bytes())
    if (
        digest(
            subprocess.check_output(["git", "diff", "--binary", args.parent, args.child], cwd=root)
        )
        != expected_patch
    ):
        raise ValueError("patch does not match upstream commits")
    expected_full, expected_selected = junit(args.full_junit), junit(args.selected_junit)
    expected_report = report(args.full_report)
    if not expected_selected or len(expected_selected) >= len(expected_full):
        raise ValueError("expected selection must be a strict subset")
    if any(expected_full.get(key) != value for key, value in expected_selected.items()):
        raise ValueError("selected outcomes differ from full")
    out.mkdir(parents=True, exist_ok=True)
    results = out / "runs"
    results.mkdir(exist_ok=False)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    common = [
        str(python),
        "-m",
        "vfy",
        "test",
        f"--engine={args.engine}",
        f"--native-scheduling={args.scheduler}",
        "-j",
        "4",
        "--source",
        "src/packaging",
        "--cov-branch",
        "--cov-context=test",
        "--cov-fail-under=0",
        "--data-file",
        str(out / "vfy.coverage"),
        f"--cov-report=json:{out / 'vfy.json'}",
        f"--junitxml={out / 'vfy.xml'}",
        "-q",
    ]
    commands = {"full": [*common[:6], "--all", *common[6:]], "selected": common}
    extension = subprocess.check_output(
        [str(python), "-c", "import vfy._native; print(vfy._native.__file__)"], env=env, text=True
    ).strip()
    manifest = json.loads(args.build_manifest.read_text(encoding="utf-8"))
    if (
        manifest["source_revision"] != args.revision
        or manifest["wheel_sha256"] != digest(args.wheel.read_bytes())
        or manifest["extension_sha256"] != digest(Path(extension).read_bytes())
    ):
        raise ValueError("build manifest does not match revision or artifact")
    package_dir = Path(extension).parent
    with zipfile.ZipFile(args.wheel) as wheel:
        members = [
            name for name in wheel.namelist() if name.startswith("vfy/") and not name.endswith("/")
        ]
        for name in members:
            installed = package_dir / name.removeprefix("vfy/")
            if not installed.is_file() or digest(installed.read_bytes()) != digest(
                wheel.read(name)
            ):
                raise ValueError(f"installed wheel payload mismatch: {name}")
    metadata = {
        "vfy_source_revision": args.revision,
        "upstream_parent": args.parent,
        "upstream_child": args.child,
        "patch_sha256": expected_patch,
        "wheel_sha256": digest(args.wheel.read_bytes()),
        "extension_sha256": digest(Path(extension).read_bytes()),
        "lock_sha256": digest(args.lock.read_bytes()),
        "build_log_sha256": digest(args.build_log.read_bytes()),
        "build_manifest_sha256": digest(args.build_manifest.read_bytes()),
        "wheel_payload_files_verified": len(members),
        "python": str(python),
        "python_version": subprocess.check_output([str(python), "--version"], text=True).strip(),
        "uname": list(os.uname()),
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "pythonpath": env["PYTHONPATH"],
        "pytest_disable_plugin_autoload": "1",
    }
    samples = []
    for index in range(args.warmups + args.runs):
        order = ("full", "selected") if index % 2 == 0 else ("selected", "full")
        for role in order:
            existing = root / ".vfy"
            if existing.exists():
                shutil.rmtree(existing)
            shutil.copytree(baseline / ".vfy", existing)
            shutil.copy2(baseline / "vfy.coverage", out / "vfy.coverage")
            for stale in (out / "vfy.xml", out / "vfy.json"):
                stale.unlink(missing_ok=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            if head != args.parent:
                raise RuntimeError("upstream HEAD changed")
            before = subprocess.check_output(["git", "diff", "--binary"], cwd=root)
            if digest(before) != expected_patch:
                raise RuntimeError("upstream diff changed before run")
            start = time.monotonic_ns()
            try:
                completed = subprocess.run(
                    commands[role],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    check=False,
                    timeout=300,
                )
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(
                    commands[role], -1, exc.stdout or b"", exc.stderr or b""
                )
            elapsed = (time.monotonic_ns() - start) / 1e9
            after = subprocess.check_output(["git", "diff", "--binary"], cwd=root)
            label = f"{index:02d}-{role}"
            (results / f"{label}.stdout").write_bytes(completed.stdout)
            (results / f"{label}.stderr").write_bytes(completed.stderr)
            record = {
                "round": index,
                "warmup": index < args.warmups,
                "role": role,
                "seconds": elapsed,
                "exit": completed.returncode,
                "stdout_sha256": digest(completed.stdout),
                "stderr_sha256": digest(completed.stderr),
                "diff_before_sha256": digest(before),
                "diff_after_sha256": digest(after),
            }
            for suffix in ("xml", "json"):
                path = out / f"vfy.{suffix}"
                if path.exists():
                    data = path.read_bytes()
                    record[f"{suffix}_sha256"] = digest(data)
                    with (
                        (results / f"{label}.{suffix}.gz").open("wb") as raw,
                        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream,
                    ):
                        stream.write(data)
            samples.append(record)
            (results / "samples.json").write_text(
                json.dumps(
                    {"metadata": metadata, "commands": commands, "samples": samples}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
            if (
                completed.returncode
                or digest(after) != expected_patch
                or subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=root, text=True
                ).strip()
                != args.parent
            ):
                raise RuntimeError(f"{label}: command failure or source drift")
            expected = expected_full if role == "full" else expected_selected
            if junit(out / "vfy.xml") != expected or report(out / "vfy.json") != expected_report:
                raise RuntimeError(f"{label}: outcome or aggregate report mismatch")
            if role == "selected" and (
                f"Selected {len(expected_selected)} / {len(expected_full)} tests".encode()
                not in completed.stderr
            ):
                raise RuntimeError(f"{label}: selection did not run")
            record["parity"] = "passed"
            (results / "samples.json").write_text(
                json.dumps(
                    {"metadata": metadata, "commands": commands, "samples": samples}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )


if __name__ == "__main__":
    main()
