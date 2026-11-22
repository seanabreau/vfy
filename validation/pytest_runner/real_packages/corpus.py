"""Compare stock pytest with ``vfy test`` on pinned real-package test suites.

``manifest.json`` lists each package's repository, commit, ``uv pip install``
arguments, and optional pytest arguments, environment, and setup commands.
Each package runs in its own virtual environment under ``CORPUS_WORK``
(by default, a user-specific directory in the system temporary directory).
The work root and its ancestors must not contain pytest configuration files;
choose another ``CORPUS_WORK`` if they do. Project configuration inside each
checkout remains in effect. The former checkout-local default ``work/`` is
left untouched. Preparation clears and rebuilds the managed environments on
every run:

```console
python corpus.py prepare NAME...
python corpus.py run --wheel dist/vfy-*.whl [--only pytest|vfy] [--jobs N] [NAME...]
python corpus.py run --bare --vfy-source python --only vfy --pytest-results DIR [NAME...]
python corpus.py report DIRECTORY [--pytest-results DIR]
```

``--bare`` runs ``vfy test`` in a second environment, ``.venv-bare``, that holds
the full environment's exact distributions except pytest, its plug-ins, and
the pytest dependencies nothing else needs (see ``prune.py``).
``--pytest-results`` compares with pytest runs recorded in another directory.

``run`` executes ``python -m pytest -q -p no:randomly -p no:cacheprovider``
and ``python -m vfy test --no-cov -q`` and compares their exit statuses and
final outcome counts and ordered JUnit identities and outcomes. A matching
failing test run is labeled ``baseline_not_green``; incomplete runs do not count
as clean matches. JUnit does not establish full phase or diagnostic parity.
Warning counts, output text, coverage, and timing are not compared. Runs record
the requested inventory, recipe, interpreter, dependencies, command, and vfy
artifact. Missing or stale evidence and non-green comparisons fail the command.
Single-route runs without a baseline check execution only, not parity.
``CORPUS_VFY_ARGS`` adds options to the explicit independent engine route.
``--vfy-workers`` requires the native result to attest the requested worker count;
``--vfy-scheduling`` selects its native scheduler.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.process_tree import run_captured

HERE = Path(__file__).resolve().parent
_USER = (
    str(os.getuid())
    if hasattr(os, "getuid")
    else re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser())
)
_DEFAULT_WORK = "CORPUS_WORK" not in os.environ
WORK = Path(os.environ.get("CORPUS_WORK", str(Path(tempfile.gettempdir()) / f"vfy-corpus-{_USER}")))
SOURCES = WORK / "src"
PYTEST = "pytest==9.1.1"
TIMEOUT = int(os.environ.get("CORPUS_TIMEOUT", "2400"))
_COUNT = re.compile(r"(\d[\d,]*) (subtests \w+|\w+)")
_ANSI_STYLE = re.compile(r"\x1b\[[\d;]*m")
_SUMMARY = re.compile(
    r"(?:no tests ran|\d[\d,]* (?:subtests \w+|\w+)"
    r"(?:, \d[\d,]* (?:subtests \w+|\w+))*)"
    r" in \d+(?:\.\d+)?s(?: \((?:\d+ days?, )?\d+:\d{2}:\d{2}\))?"
)
_OUTCOMES = {
    "passed",
    "failed",
    "skipped",
    "xfailed",
    "xpassed",
    "error",
    "errors",
    "deselected",
    "subtests passed",
    "subtests failed",
    "subtests skipped",
}

Entry = dict[str, Any]
INVENTORY = "corpus-run.json"

# Run in the target interpreter before vfy installs its compatibility facade.
_PROBE = """
import hashlib, importlib.metadata, importlib.util, json, platform, re, sys
from pathlib import Path
origins = {}
files = {}
for name in ('vfy', 'vfy._native'):
    spec = importlib.util.find_spec(name) if importlib.util.find_spec('vfy') else None
    if spec is not None and spec.origin is not None:
        path = Path(spec.origin).resolve()
        origins[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if name == 'vfy':
            files = {str(item.relative_to(path.parent.parent)):
                     hashlib.sha256(item.read_bytes()).hexdigest()
                     for item in sorted(path.parent.rglob('*'))
                     if item.is_file() and item.suffix in {'.py', '.so', '.pyd'}}
print(json.dumps({
    'python': sys.version, 'platform': platform.platform(),
    'distributions': {re.sub(r'[-_.]+', '-', dist.metadata['Name']).lower(): dist.version
                      for dist in importlib.metadata.distributions()},
    'pytest_available': any(importlib.util.find_spec(name) is not None
                            for name in ('pytest', '_pytest')),
    'vfy_origins': origins,
    'vfy_files': files,
}))
"""

_CONFIG_FILES = (
    "pytest.ini",
    ".pytest.ini",
    "pytest.toml",
    ".pytest.toml",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)


def _validate_work_root() -> None:
    """Reject ancestor configuration that pytest could apply to a checkout."""
    sources = SOURCES.resolve()
    for directory in (sources, *sources.parents):
        for name in _CONFIG_FILES:
            path = directory / name
            if path.is_file():
                raise ValueError(
                    f"corpus work root has an ancestor configuration file: {path}; "
                    "set CORPUS_WORK to a directory outside that configuration"
                )


def _ensure_default_work() -> None:
    """Create the default work directory privately or reject an unsafe existing one."""
    if not _DEFAULT_WORK:
        return
    if WORK.is_symlink():
        raise ValueError(f"default corpus work directory is a symlink: {WORK}")
    WORK.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = WORK.stat()
    if not stat.S_ISDIR(info.st_mode) or (
        hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise ValueError(f"default corpus work directory is not private to this user: {WORK}")


def _checkout_path(entry: Entry) -> Path:
    directory = SOURCES / str(entry["name"])
    if not directory.resolve().is_relative_to(SOURCES.resolve()):
        raise ValueError(f"corpus checkout resolves outside the source directory: {directory}")
    return directory


def manifest() -> dict[str, Entry]:
    """Return manifest entries by package name."""
    entries = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in entries}


def _run(
    command: list[str] | str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    *,
    subprocess_umask: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=isinstance(command, str),
        umask=-1 if subprocess_umask is None else subprocess_umask,
        check=False,
    )


def _checkout(entry: Entry) -> Path:
    _validate_work_root()
    _ensure_default_work()
    directory = _checkout_path(entry)
    if not (directory / ".git").exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        clone = _run(["git", "clone", "-q", "--filter=blob:none", entry["url"], str(directory)])
        if clone.returncode:
            raise RuntimeError(clone.stdout)
    checkout = _run(["git", "checkout", "-q", "-f", entry["commit"]], cwd=directory)
    if checkout.returncode:
        raise RuntimeError(checkout.stdout)
    return directory


def _venv(*, bare: bool) -> str:
    return ".venv-bare" if bare else ".venv"


def _clear_caches(directory: Path) -> None:
    """Remove harness-owned state before package versioning or test execution."""
    for cache in (".vfy", ".pytest_cache"):
        shutil.rmtree(directory / cache, ignore_errors=True)


def _failure(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout or f"command failed with exit status {completed.returncode}"


def prepare(entry: Entry, *, bare: bool = False) -> str:
    """Clone, create the environment, and install; return an error transcript or ``""``.

    A bare environment installs the full environment's exact distributions,
    then removes pytest, its plug-ins, and its unused dependencies, so
    ``vfy test`` runs as it would in a project without pytest.
    """
    if bare:
        return _prepare_bare(entry)
    directory = _checkout(entry)
    _clear_caches(directory)
    python = directory / _venv(bare=bare) / "bin/python"
    version = entry.get("python", "3.13")
    venv = _run(["uv", "venv", "-q", "--clear", "--python", version, _venv(bare=bare)], directory)
    if venv.returncode:
        return _failure(venv)
    for command in entry.get("pre", []):
        setup = _run(command, directory)
        if setup.returncode:
            return _failure(setup)
    pins = [] if entry.get("pytest_pinned_by_project") else [PYTEST]
    install = _run(
        ["uv", "pip", "install", "-q", "--python", str(python), *entry["install"], *pins],
        directory,
        {**os.environ, **entry.get("env", {})},
        1800,
    )
    return _failure(install) if install.returncode else ""


def _prepare_bare(entry: Entry) -> str:
    error = prepare(entry)
    if error:
        return error
    directory = SOURCES / str(entry["name"])
    python = directory / _venv(bare=True) / "bin/python"
    freeze = _run(["uv", "pip", "freeze", "--python", str(directory / ".venv/bin/python")])
    if freeze.returncode:
        return _failure(freeze)
    version = entry.get("python", "3.13")
    venv = _run(["uv", "venv", "-q", "--clear", "--python", version, _venv(bare=True)], directory)
    if venv.returncode:
        return _failure(venv)
    # vfy comes from --wheel or --vfy-source, never from the full environment.
    requirements = directory / _venv(bare=True) / "requirements.txt"
    requirements.write_text(
        "".join(
            f"{line}\n"
            for line in freeze.stdout.splitlines()
            if not re.match(r"vfy\b", line) and not line.startswith(("warning:", "Using "))
        ),
        encoding="utf-8",
    )
    options = ["-q", "--no-deps", "--python", str(python)]
    install = _run(
        ["uv", "pip", "install", *options, "-r", str(requirements)],
        directory,
        {**os.environ, **entry.get("env", {})},
        1800,
    )
    if install.returncode:
        return _failure(install)
    pruned = _run([str(python), "-I", str(HERE / "prune.py"), str(directory)])
    if pruned.returncode:
        return _failure(pruned)
    removed = pruned.stdout.split()
    (directory / _venv(bare=True) / "pruned.txt").write_text(pruned.stdout, encoding="utf-8")
    if removed:
        uninstall = _run(["uv", "pip", "uninstall", "-q", "--python", str(python), *removed])
        if uninstall.returncode:
            return _failure(uninstall)
    return ""


def summary(text: str) -> dict[str, int]:
    """Read final pytest-style outcome counts, rejecting a truncated final summary."""
    for raw_line in reversed(text.splitlines()):
        line = _ANSI_STYLE.sub("", raw_line).strip().strip("=").strip()
        if _SUMMARY.fullmatch(line) is None:
            first = _COUNT.match(line)
            if line.startswith("no tests ran") or (
                first is not None and first[2] in _OUTCOMES | {"warning", "warnings"}
            ):
                return {}
            continue
        counts: dict[str, int] = {}
        for number, word in _COUNT.findall(line):
            outcome = {"error": "errors", "warning": "warnings"}.get(word, word)
            if outcome not in _OUTCOMES | {"warnings"} or outcome in counts:
                return {}
            counts[outcome] = int(number.replace(",", ""))
        counts.pop("warnings", None)
        return counts
    return {}


def _recipe(entry: Entry) -> str:
    recipe = dict(entry)
    clock = _collection_clock(entry)
    if clock is not None:
        recipe["collection_clock_sha256"] = clock["sha256"]
    return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()


def _subprocess_umask(entry: Entry) -> int | None:
    value = entry.get("umask")
    if value is None and "umask" not in entry:
        return None
    if (
        os.name != "posix"
        or not isinstance(value, str)
        or re.fullmatch(r"0[0-7]{3}", value) is None
    ):
        raise ValueError("manifest umask must be a four-digit POSIX octal mask")
    return int(value, 8)


def _collection_time(entry: Entry) -> str | None:
    value = entry.get("collection_time")
    if value is None and "collection_time" not in entry:
        return None
    if not isinstance(value, str):
        raise ValueError("collection_time must be a naive ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("collection_time must be a naive ISO datetime string") from error
    if parsed.tzinfo is not None:
        raise ValueError("collection_time must be a naive ISO datetime string")
    return value


def _collection_clock(entry: Entry) -> dict[str, str] | None:
    value = _collection_time(entry)
    if value is None:
        return None
    return {
        "time": value,
        "sha256": hashlib.sha256((HERE / "corpus_clock.py").read_bytes()).hexdigest(),
    }


def _configure_collection_clock(entry: Entry, environment: dict[str, str]) -> None:
    value = _collection_time(entry)
    if value is None:
        return
    existing_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (str(HERE), existing_path)))
    existing_plugins = environment.get("PYTEST_PLUGINS")
    environment["PYTEST_PLUGINS"] = ",".join(filter(None, (existing_plugins, "corpus_clock")))
    environment["VFY_CORPUS_COLLECTION_CLOCK"] = value


def _configure_path_environment(entry: Entry, checkout: Path, environment: dict[str, str]) -> None:
    paths = entry.get("env_paths", {})
    if not isinstance(paths, dict):
        raise ValueError("manifest env_paths must be a mapping")
    root = checkout.resolve()
    requested = entry.get("env", {})
    for name, value in paths.items():
        if not isinstance(name, str) or not name or not isinstance(value, str) or not value:
            raise ValueError("manifest env_paths must contain nonempty string keys and values")
        if name in requested:
            raise ValueError(f"manifest specifies {name} in both env and env_paths")
        path = Path(value)
        resolved = (root / path).resolve()
        if path.is_absolute() or not resolved.is_relative_to(root):
            raise ValueError(f"manifest env_paths value for {name} must remain inside the checkout")
        environment[name] = str(resolved)


def _recorded_environment(entry: Entry, environment: dict[str, str]) -> dict[str, str | None]:
    names = ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONHASHSEED", "LANG", "LC_ALL", "TZ"]
    if _collection_time(entry) is not None:
        names.extend(["PYTHONPATH", "VFY_CORPUS_COLLECTION_CLOCK"])
    names.extend(entry.get("env_paths", {}))
    return {name: environment.get(name) for name in names}


def _artifact(wheel: str | None, source: Path | None) -> dict[str, object] | None:
    if wheel is not None:
        with zipfile.ZipFile(wheel) as archive:
            files = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist()
                if name.startswith("vfy/") and Path(name).suffix in {".py", ".so", ".pyd"}
            }
        return {
            "wheel": wheel,
            "sha256": hashlib.sha256(Path(wheel).read_bytes()).hexdigest(),
            "files": files,
        }
    if source is not None:
        return {
            "source": str(source),
            "files": {
                str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((source / "vfy").rglob("*"))
                if path.is_file() and path.suffix in {".py", ".so", ".pyd"}
            },
        }
    return None


def _artifact_matches(outcome: dict[str, Any]) -> bool:
    artifact = outcome.get("artifact")
    provenance = outcome.get("provenance", {})
    origins = provenance.get("vfy_origins", {})
    if not artifact or not artifact.get("files") or set(origins) != {"vfy", "vfy._native"}:
        return False
    files = provenance.get("vfy_files")
    if files != artifact["files"]:
        return False
    root = Path(origins["vfy"]["path"]).parent
    if artifact.get("source") and root != (Path(artifact["source"]) / "vfy").resolve():
        return False
    for name, origin in origins.items():
        path = Path(origin["path"])
        if not path.is_relative_to(root):
            return False
        relative = str(Path("vfy") / path.relative_to(root))
        if files.get(relative) != origin["sha256"]:
            return False
        if name == "vfy._native" and path.suffix not in {".so", ".pyd"}:
            return False
    return True


def _cases(path: Path) -> list[list[object]]:
    """Project ordered JUnit identities and outcomes, as in check_native_project."""
    return [
        [
            case.attrib["classname"],
            case.attrib["name"],
            [
                [child.tag, child.attrib.get("message", "")]
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ],
        ]
        for case in ET.parse(path).getroot().iter("testcase")
    ]


def _execute(
    entry: Entry,
    route: str,
    output: Path,
    *,
    bare: bool,
    vfy_source: Path | None,
    vfy_workers: int | None = None,
    vfy_scheduling: str | None = None,
) -> dict[str, object]:
    _validate_work_root()
    directory = _checkout_path(entry)
    execution_directory = (directory / entry.get("cwd", ".")).resolve()
    if not execution_directory.is_relative_to(directory.resolve()):
        raise ValueError(
            f"corpus working directory resolves outside the checkout: {execution_directory}"
        )
    venv = directory / _venv(bare=bare and route == "vfy")
    environment = {**os.environ, **entry.get("env", {})}
    environment.pop("VIRTUAL_ENV", None)
    for variable in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        if variable not in entry.get("env", {}):
            environment.pop(variable, None)
    environment["PATH"] = f"{venv / 'bin'}:{environment['PATH']}"
    if route == "vfy" and vfy_source is not None:
        environment["PYTHONPATH"] = str(vfy_source)
    _configure_path_environment(entry, directory, environment)
    _configure_collection_clock(entry, environment)
    python = str(venv / "bin/python")
    subprocess_umask = _subprocess_umask(entry)
    probe = _run(
        [python, "-c", _PROBE],
        execution_directory,
        environment,
        60,
        subprocess_umask=subprocess_umask,
    )
    if probe.returncode:
        raise RuntimeError(f"environment probe failed: {_failure(probe)}")
    provenance = json.loads(probe.stdout)
    provenance["pruned"] = (
        (venv / "pruned.txt").read_text().split() if bare and route == "vfy" else []
    )
    provenance["recipe"] = _recipe(entry)
    if subprocess_umask is not None:
        provenance["umask"] = entry["umask"]
    clock = _collection_clock(entry)
    if clock is not None:
        provenance["collection_clock"] = clock
    provenance["environment"] = _recorded_environment(entry, environment)
    if bare and route == "vfy" and provenance["pytest_available"]:
        raise RuntimeError("bare environment can still import pytest or _pytest")
    xml = (output / f"{entry['name']}.{route}.xml").resolve()
    native = (output / f"{entry['name']}.native.json").resolve()
    xml.unlink(missing_ok=True)
    if route == "pytest":
        options = ["-q", "-p", "no:randomly", "-p", "no:cacheprovider"]
        command = [
            python,
            "-m",
            "pytest",
            *options,
            *entry.get("pytest_args", []),
            f"--junitxml={xml}",
        ]
    else:
        native.unlink(missing_ok=True)
        extra = shlex.split(os.environ.get("CORPUS_VFY_ARGS", ""))
        arguments = entry.get("vfy_args", entry.get("pytest_args", []))
        command = [
            python,
            "-m",
            "vfy",
            "test",
            *extra,
            "--no-cov",
            "-q",
            *arguments,
            *([f"--workers={vfy_workers}"] if vfy_workers is not None else []),
            *([f"--native-scheduling={vfy_scheduling}"] if vfy_scheduling is not None else []),
            "--engine=native",
            f"--junitxml={xml}",
            f"--native-result-json={native}",
        ]
    _clear_caches(directory)
    started = time.monotonic()
    try:
        completed = run_captured(
            command,
            cwd=execution_directory,
            env=environment,
            timeout=entry.get("timeout", TIMEOUT),
            subprocess_umask=subprocess_umask,
        )
        status: int | str = completed.returncode
        text = completed.stdout + completed.stderr
    except subprocess.TimeoutExpired as error:
        status = "timeout"
        text = "".join(
            stream.decode(errors="replace") if isinstance(stream, bytes) else stream or ""
            for stream in (error.stdout, error.stderr)
        )
    elapsed = round(time.monotonic() - started, 2)
    (output / f"{entry['name']}.{route}.log").write_text(text, encoding="utf-8")
    result: dict[str, object] = {
        "exit": status,
        "counts": summary(text),
        "seconds": elapsed,
        "command": command,
        "provenance": provenance,
    }
    try:
        result["cases"] = _cases(xml)
        if route == "vfy":
            record = json.loads(native.read_text(encoding="utf-8"))
            result["engine"] = record["engine"]
            result["execution_topology"] = record.get("execution_topology")
            if result["engine"] != "vfy-independent-v1":
                result["evidence_error"] = "independent engine did not produce the result"
    except (OSError, ValueError, KeyError, ET.ParseError) as error:
        result["evidence_error"] = str(error)
    return result


def _install_vfy(entry: Entry, wheel: str, *, bare: bool) -> str:
    python = str(SOURCES / entry["name"] / _venv(bare=bare) / "bin/python")
    options = [
        "--python",
        python,
        "--reinstall-package",
        "vfy",
        "--no-deps",
        "--no-cache",
        "--link-mode=copy",
    ]
    install = _run(["uv", "pip", "install", "-q", *options, wheel])
    return "" if install.returncode == 0 else _failure(install)


def _job(entry: Entry, routes: list[str], options: argparse.Namespace) -> None:
    result: dict[str, object] = {"name": entry["name"], "run_id": options.run_id}
    output = Path(options.out)
    try:
        error = prepare(entry, bare=options.bare and "vfy" in routes)
        if not error and options.wheel is not None and "vfy" in routes:
            error = _install_vfy(entry, options.wheel, bare=options.bare)
        if error:
            result["prepare_error"] = error[-3000:]
        else:
            for route in routes:
                result[route] = _execute(
                    entry,
                    route,
                    output,
                    bare=options.bare,
                    vfy_source=options.vfy_source,
                    vfy_workers=options.vfy_workers,
                    vfy_scheduling=options.vfy_scheduling,
                )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["prepare_error"] = repr(error)[-3000:]
    (output / f"{entry['name']}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    finished = ", ".join(key for key in result if key != "name")
    sys.stdout.write(f"{entry['name']}: {finished}\n")


def _route(directory: Path, name: str, route: str) -> dict[str, Any] | None:
    path = directory / f"{name}.json"
    log = directory / f"{name}.{route}.log"
    if not path.exists() or not log.exists():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    inventory = directory / INVENTORY
    if inventory.exists() and result.get("run_id") != json.loads(inventory.read_text())["run_id"]:
        return None
    if route not in result:
        return None
    return {
        **result[route],
        "counts": summary(log.read_text(encoding="utf-8")),
        "inventory_verified": inventory.exists(),
        "vfy_workers": json.loads(inventory.read_text()).get("vfy_workers")
        if inventory.exists()
        else None,
        "artifact": (
            json.loads(inventory.read_text()).get("artifact") if inventory.exists() else None
        ),
    }


def _completed(outcome: dict[str, Any]) -> bool:
    counts = outcome["counts"]
    executed = sum(counts.get(word, 0) for word in ("passed", "failed", "xfailed", "xpassed"))
    return bool(
        executed > 0
        and (
            (outcome["exit"] == 0 and not counts.get("failed") and not counts.get("errors"))
            or (outcome["exit"] == 1 and bool(counts.get("failed") or counts.get("errors")))
        )
    )


def _verified(outcome: dict[str, Any], route: str) -> bool:
    workers = outcome.get("vfy_workers") if route == "vfy" else None
    topology = outcome.get("execution_topology")
    worker_pids = topology.get("worker_pids") if isinstance(topology, dict) else None
    topology_verified = workers is None or (
        isinstance(workers, int)
        and not isinstance(workers, bool)
        and workers > 0
        and isinstance(topology, dict)
        and topology.get("requested_workers") == workers
        and topology.get("active_workers") == workers
        and isinstance(worker_pids, list)
        and len(worker_pids) == workers
        and all(
            isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 for pid in worker_pids
        )
        and len(set(worker_pids)) == workers
    )
    return bool(
        bool(outcome.get("cases"))
        and bool(outcome.get("inventory_verified", False))
        and bool(outcome.get("provenance"))
        and not outcome.get("evidence_error")
        and topology_verified
        and (
            route != "vfy"
            or (outcome.get("engine") == "vfy-independent-v1" and _artifact_matches(outcome))
        )
    )


def report(output: Path, pytest_results: Path | None = None) -> bool:
    """Require complete current evidence for every requested package and route."""
    inventory_path = output / INVENTORY
    inventory = json.loads(inventory_path.read_text()) if inventory_path.exists() else None
    names = (
        inventory["names"]
        if inventory
        else [
            result["name"]
            for path in sorted(output.glob("*.json"))
            if isinstance(result := json.loads(path.read_text()), dict) and "name" in result
        ]
    )
    requested = inventory["routes"] if inventory else ["pytest", "vfy"]
    comparing = len(requested) == 2 or pytest_results is not None
    required = ["pytest", "vfy"] if comparing else requested
    matched = successful = 0
    for name in names:
        routes = {
            route: _route(pytest_results or output if route == "pytest" else output, name, route)
            for route in required
        }
        if any(outcome is None for outcome in routes.values()):
            verdict = "incomplete (missing or stale route)"
        elif not all(_completed(outcome) for outcome in routes.values() if outcome is not None):
            verdict = "incomplete"
        elif not inventory or not all(
            _verified(outcome, route) for route, outcome in routes.items() if outcome is not None
        ):
            verdict = "unverified"
            vfy_outcome = routes.get("vfy")
            if vfy_outcome and vfy_outcome.get("vfy_workers") is not None:
                verdict += (
                    f" (requested workers={vfy_outcome['vfy_workers']}, "
                    f"observed topology={vfy_outcome.get('execution_topology')!r})"
                )
        elif not comparing:
            outcome = routes[required[0]]
            assert outcome is not None
            verdict = (
                "execution passed (parity not checked)"
                if outcome["exit"] == 0
                else "execution failed"
            )
            successful += verdict.startswith("execution passed")
        else:
            stock, vfy = routes["pytest"], routes["vfy"]
            assert stock is not None
            assert vfy is not None
            compatible = all(
                stock["provenance"].get(key) == vfy["provenance"].get(key)
                for key in (
                    "recipe",
                    "python",
                    "platform",
                    "environment",
                    "collection_clock",
                    "umask",
                )
            )
            excluded = {"vfy", *vfy["provenance"].get("pruned", [])}
            compatible &= {
                name: version
                for name, version in stock["provenance"]["distributions"].items()
                if name not in excluded
            } == {
                name: version
                for name, version in vfy["provenance"]["distributions"].items()
                if name not in excluded
            }
            same = all(stock[key] == vfy[key] for key in ("exit", "counts", "cases"))
            if not compatible:
                verdict = "incompatible provenance"
            elif stock["exit"] != 0:
                verdict = "baseline_not_green (match)" if same else "baseline_not_green (differ)"
            else:
                verdict = "match" if same else "differ"
                matched += same
                successful += same
        sys.stdout.write(f"{name} | {verdict}\n")
    sys.stdout.write(
        f"\n{matched} of {len(names) if comparing else 0} compared packages have a clean match\n"
    )
    return bool(inventory and names and successful == len(names))


def main() -> int:
    """Run the command-line interface."""
    parser = argparse.ArgumentParser(description="Compare stock pytest with vfy test.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("names", nargs="+")
    run_parser = commands.add_parser("run")
    artifact_parser = run_parser.add_mutually_exclusive_group()
    artifact_parser.add_argument("--wheel")
    run_parser.add_argument("--only", choices=["pytest", "vfy"])
    run_parser.add_argument("--jobs", type=int, default=2)
    run_parser.add_argument("--vfy-workers", type=int, help="require this many vfy workers")
    run_parser.add_argument("--vfy-scheduling", choices=["file", "scope", "cases", "striped"])
    run_parser.add_argument("--out", type=Path, default=WORK / "results")
    run_parser.add_argument(
        "--bare", action="store_true", help="run vfy where pytest and its plug-ins are absent"
    )
    artifact_parser.add_argument(
        "--vfy-source", type=Path, help="import vfy from this source directory instead of a wheel"
    )
    run_parser.add_argument("--pytest-results", type=Path, help="compare with these pytest runs")
    run_parser.add_argument("names", nargs="*")
    report_parser = commands.add_parser("report")
    report_parser.add_argument("directory", type=Path)
    report_parser.add_argument("--pytest-results", type=Path)
    args = parser.parse_args()
    entries = manifest()
    if args.command == "report":
        return 0 if report(args.directory, args.pytest_results) else 1
    names = args.names or [name for name, entry in entries.items() if not entry.get("skip")]
    if len(set(names)) != len(names) or any(name not in entries for name in names):
        parser.error("package names must be unique entries from manifest.json")
    try:
        _validate_work_root()
        _ensure_default_work()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.command == "prepare":
        failed = False
        for name in names:
            error = prepare(entries[name])
            failed |= bool(error)
            sys.stdout.write(f"{name}: {error or 'ready'}\n")
        return int(failed)
    routes = [args.only] if args.only else ["pytest", "vfy"]
    if args.vfy_workers is not None and args.vfy_workers < 1:
        parser.error("--vfy-workers must be positive")
    if args.vfy_scheduling is not None and args.vfy_workers is None:
        parser.error("--vfy-scheduling requires --vfy-workers")
    if "vfy" not in routes and (args.vfy_workers is not None or args.vfy_scheduling is not None):
        parser.error("--vfy-workers and --vfy-scheduling require the vfy route")
    if "vfy" in routes and not (args.wheel or args.vfy_source):
        parser.error("--wheel or --vfy-source is required for the vfy route")
    if args.vfy_source is not None:
        args.vfy_source = args.vfy_source.resolve()
    if args.wheel is not None:
        args.wheel = str(Path(args.wheel).resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    args.run_id = uuid.uuid4().hex
    artifact = _artifact(args.wheel, args.vfy_source)
    (args.out / INVENTORY).write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": args.run_id,
                "names": names,
                "routes": routes,
                "artifact": artifact,
                "vfy_workers": args.vfy_workers,
                "vfy_scheduling": args.vfy_scheduling,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as executor:
        list(executor.map(lambda name: _job(entries[name], routes, args), names))
    return 0 if report(args.out, args.pytest_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
