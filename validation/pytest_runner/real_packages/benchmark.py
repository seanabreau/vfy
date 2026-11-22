"""Run sequential diagnostic timings only after matching corpus evidence.

Samples omit the corpus-added JUnit and native-result files. Each sample checks
exit status and terminal counts, not individual outcomes or phases. Only the
harness-owned .vfy and .pytest_cache directories are reset between commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from benchmarks.benchmark_harness import rotated_orders, summarize  # noqa: E402
from scripts.process_tree import run_captured  # noqa: E402
from scripts.pytest_execution_record import write_atomic  # noqa: E402
from validation.pytest_runner.real_packages import corpus  # noqa: E402

ROUTES = ("pytest", "vfy-full", "vfy-bare")
_BENCHMARK_TABLE = re.compile(r"\bbenchmark[^\n]*?:\s*[1-9]\d*\s+tests\b")


def _observer_arguments(arguments: list[str]) -> bool:
    for index, value in enumerate(arguments):
        if value.split("=", 1)[0] in {
            "--junitxml",
            "--junit-xml",
            "--native-result-json",
            "--html",
            "--json-report",
            "--json-report-file",
        }:
            return True
        if (
            value == "-p"
            and index + 1 < len(arguments)
            and arguments[index + 1] == "pytest_execution_probe"
        ):
            return True
        if value == "-ppytest_execution_probe":
            return True
    return False


def _without_addopts_overrides(options: list[str]) -> list[str]:
    """Drop raw overrides already expanded according to pytest's last-value rule."""
    result = []
    index = 0
    while index < len(options):
        value = options[index]
        if (
            value in {"-o", "--override-ini"}
            and index + 1 < len(options)
            and options[index + 1].partition("=")[0].strip() == "addopts"
        ):
            index += 2
            continue
        attached = (
            value.removeprefix("--override-ini=")
            if value.startswith("--override-ini=")
            else value[2:]
            if value.startswith("-o")
            else ""
        )
        if attached.partition("=")[0].strip() == "addopts":
            index += 1
            continue
        result.append(value)
        index += 1
    return result


def _check_addopts(
    python: Path,
    cwd: Path,
    route: str,
    command: list[str],
    env: dict[str, str],
    *,
    subprocess_umask: int | None = None,
) -> list[str]:
    """Use vfy's configuration projection to inspect the selected pytest options."""
    arguments = command[3:] if route == "pytest" else command[4:-1]
    engine = "pytest" if route == "pytest" else "native"
    program = (
        "import json, sys; "
        "from vfy.run.engine import TestEngine, _resolve_pytest_configuration; "
        "c = _resolve_pytest_configuration("
        "json.loads(sys.argv[1]), engine=TestEngine(sys.argv[2])); "
        "print(json.dumps(c.effective_args))"
    )
    completed = subprocess.run(
        [str(python), "-c", program, json.dumps(arguments), engine],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
        umask=-1 if subprocess_umask is None else subprocess_umask,
    )
    if completed.returncode:
        raise ValueError(f"could not resolve pytest configuration: {completed.stderr.strip()}")
    raw = json.loads(completed.stdout)
    if not isinstance(raw, list):
        raise ValueError("pytest configuration did not return an argument list")
    options = [str(value) for value in raw if isinstance(value, str)]
    if len(options) != len(raw):
        raise ValueError("pytest configuration returned a non-string argument")
    options = _without_addopts_overrides(options)
    if _observer_arguments(options) or any(
        name.strip() == "pytest_execution_probe"
        for name in env.get("PYTEST_PLUGINS", "").split(",")
    ):
        raise ValueError("selected pytest configuration requests an observer or result file")
    if "--no-cov" not in options and any(
        option == "--cov" or option.startswith("--cov=") for option in options
    ):
        raise ValueError("selected pytest configuration enables coverage in the timed command")
    return options


def _benchmark_table(text: str) -> bool:
    return _BENCHMARK_TABLE.search(corpus._ANSI_STYLE.sub("", text)) is not None


def _check_benchmark_options(
    options: list[str], env: dict[str, str], distributions: dict[str, str], bare: dict[str, str]
) -> None:
    """Reject modes that can hide work missing from the bare replacement."""
    if "pytest-benchmark" in distributions and "pytest-benchmark" not in bare:
        hidden = {"--no-summary", "--benchmark-quiet", "-pno:terminal", "-pno:terminalreporter"}
        if any(
            option.split("=", 1)[0] in hidden or option.startswith("--benchmark-cprofile")
            for option in options
        ) or any(
            flag == "-p" and name in {"no:terminal", "no:terminalreporter"}
            for flag, name in pairwise(options)
        ):
            raise ValueError(
                "benchmark output is suppressed or profiled; measurement cannot be ruled out"
            )
    if (
        "pytest-codspeed" in distributions
        and "pytest-codspeed" not in bare
        and ("--codspeed" in options or "CODSPEED_ENV" in env)
    ):
        raise ValueError("CodSpeed measurement is enabled but the bare replacement cannot measure")


def _timed_command(route: str, outcome: dict[str, Any], python: Path) -> list[str]:
    raw = outcome.get("command")
    if not isinstance(raw, list):
        raise ValueError(f"{route} receipt has no command list")
    command = [str(argument) for argument in raw if isinstance(argument, str)]
    if len(command) != len(raw):
        raise ValueError(f"{route} receipt contains a non-string argument")
    if not command or Path(command[0]) != python:
        raise ValueError(f"{route} receipt does not use the prepared interpreter {python}")
    suffixes = ("--junitxml=",) if route == "pytest" else ("--junitxml=", "--native-result-json=")
    if len(command) < len(suffixes) or any(
        not argument.startswith(prefix)
        for argument, prefix in zip(command[-len(suffixes) :], suffixes, strict=True)
    ):
        raise ValueError(f"{route} receipt lacks the expected corpus result-file arguments")
    return command[: -len(suffixes)]


def _require_stock_match(
    name: str, route: str, stock: dict[str, Any], outcome: dict[str, Any]
) -> None:
    if any(outcome[key] != stock[key] for key in ("exit", "counts", "cases")):
        raise ValueError(f"{name} {route}: ordered JUnit outcomes or counts differ from pytest")
    provenance = outcome["provenance"]
    reference = stock["provenance"]
    if any(
        provenance.get(key) != reference.get(key)
        for key in ("recipe", "python", "platform", "environment", "collection_clock", "umask")
    ):
        raise ValueError(f"{name} {route}: interpreter or environment differs from pytest")
    excluded = {"vfy", *provenance.get("pruned", [])}
    if {k: v for k, v in provenance["distributions"].items() if k not in excluded} != {
        k: v for k, v in reference["distributions"].items() if k not in excluded
    }:
        raise ValueError(f"{name} {route}: dependency versions differ from pytest")


def _check_recorded_benchmarks(
    name: str, full: Path, stock: dict[str, Any], bare: dict[str, Any]
) -> None:
    stock_distributions = stock["provenance"]["distributions"]
    bare_distributions = bare["provenance"]["distributions"]
    if "pytest-benchmark" in stock_distributions and "pytest-benchmark" not in bare_distributions:
        for report_route in ("pytest", "vfy"):
            if _benchmark_table((full / f"{name}.{report_route}.log").read_text(encoding="utf-8")):
                raise ValueError(
                    f"{name}: {report_route} measured benchmarks but the bare replacement runs once"
                )


def _route_gate(
    name: str, entry: dict[str, Any], full: Path, bare: Path, wheel: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    outcomes = {
        "pytest": corpus._route(full, name, "pytest"),
        "vfy-full": corpus._route(full, name, "vfy"),
        "vfy-bare": corpus._route(bare, name, "vfy"),
    }
    for route, outcome in outcomes.items():
        if outcome is None or not corpus._completed(outcome) or outcome["exit"] != 0:
            raise ValueError(
                f"{name} {route}: missing, stale, incomplete, or non-green corpus result"
            )
        if not corpus._verified(outcome, "pytest" if route == "pytest" else "vfy"):
            raise ValueError(f"{name} {route}: corpus evidence is unverified")
        if outcome["provenance"].get("recipe") != corpus._recipe(entry):
            raise ValueError(f"{name} {route}: manifest recipe changed")
        if outcome["provenance"].get("pytest_available") is not (route != "vfy-bare"):
            raise ValueError(f"{name} {route}: unexpected pytest availability")
        if route != "pytest" and any(
            outcome["artifact"].get(key) != wheel.get(key) for key in ("sha256", "files")
        ):
            raise ValueError(f"{name} {route}: corpus artifact differs from requested wheel")
        directory = bare if route == "vfy-bare" else full
        report_route = "pytest" if route == "pytest" else "vfy"
        expected_suffix = [f"--junitxml={(directory / f'{name}.{report_route}.xml').resolve()}"]
        if route != "pytest":
            expected_suffix.append(
                f"--native-result-json={(directory / f'{name}.native.json').resolve()}"
            )
        if outcome["command"][-len(expected_suffix) :] != expected_suffix:
            raise ValueError(f"{name} {route}: receipt points to different report files")
        try:
            cases = corpus._cases(directory / f"{name}.{report_route}.xml")
        except ET.ParseError as error:
            raise ValueError(f"{name} {route}: invalid JUnit report") from error
        if cases != outcome["cases"]:
            raise ValueError(f"{name} {route}: JUnit report differs from recorded cases")
        if route != "pytest":
            native = json.loads((directory / f"{name}.native.json").read_text(encoding="utf-8"))
            if native.get("engine") != outcome["engine"]:
                raise ValueError(f"{name} {route}: native result engine differs from receipt")
    stock = outcomes["pytest"]
    assert stock is not None
    for route in ("vfy-full", "vfy-bare"):
        outcome = outcomes[route]
        assert outcome is not None
        _require_stock_match(name, route, stock, outcome)
    bare_outcome = outcomes["vfy-bare"]
    assert bare_outcome is not None
    _check_recorded_benchmarks(name, full, stock, bare_outcome)
    return {route: outcome for route, outcome in outcomes.items() if outcome is not None}


def _environment(entry: dict[str, Any], venv: Path) -> dict[str, str]:
    raw = entry.get("env", {})
    if not isinstance(raw, dict):
        raise ValueError("manifest environment must be a mapping")
    requested = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    if len(requested) != len(raw):
        raise ValueError("manifest environment must contain string keys and values")
    if requested.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        raise ValueError("manifest disables plug-in autoload; diagnostic requires it unset")
    environment = {**os.environ, **requested}
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        if name not in requested:
            environment.pop(name, None)
    environment["PATH"] = f"{venv / 'bin'}:{environment['PATH']}"
    corpus._configure_path_environment(entry, venv.parent, environment)
    corpus._configure_collection_clock(entry, environment)
    return environment


def _identity(project: Path, entry: dict[str, Any]) -> dict[str, str]:
    def git(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(project), *arguments], check=True, capture_output=True, timeout=60
        ).stdout

    head = git("rev-parse", "HEAD").decode().strip()
    if head != entry["commit"]:
        raise ValueError(f"{entry['name']}: checkout revision differs from manifest")
    return {
        "head": head,
        "tracked_diff_sha256": hashlib.sha256(git("diff", "--binary", "HEAD")).hexdigest(),
    }


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _tooling_identity() -> dict[str, Any]:
    revision = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    files = (
        Path(__file__).resolve().relative_to(ROOT),
        Path(corpus.__file__).resolve().relative_to(ROOT),
        Path("scripts/process_tree.py"),
        Path("scripts/pytest_execution_record.py"),
        Path("benchmarks/benchmark_harness.py"),
    )
    return {
        "controller_revision": revision,
        "sha256": {str(path): _digest(ROOT / path) for path in files},
    }


def _receipt_hashes(name: str, full: Path, bare: Path) -> dict[str, str]:
    files = (
        full / corpus.INVENTORY,
        full / f"{name}.json",
        full / f"{name}.pytest.log",
        full / f"{name}.vfy.log",
        full / f"{name}.pytest.xml",
        full / f"{name}.vfy.xml",
        full / f"{name}.native.json",
        bare / corpus.INVENTORY,
        bare / f"{name}.json",
        bare / f"{name}.vfy.log",
        bare / f"{name}.vfy.xml",
        bare / f"{name}.native.json",
    )
    return {str(path.resolve()): _digest(path) for path in files}


def _probe(
    python: Path, cwd: Path, env: dict[str, str], outcome: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    mask = corpus._subprocess_umask(entry)
    result = subprocess.run(
        [str(python), "-c", corpus._PROBE],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
        umask=-1 if mask is None else mask,
    )
    current = json.loads(result.stdout)
    expected = outcome["provenance"]
    if expected.get("umask") != entry.get("umask"):
        raise ValueError("subprocess umask changed")
    if expected.get("collection_clock") != corpus._collection_clock(entry):
        raise ValueError("collection clock helper or time changed")
    for key in (
        "python",
        "platform",
        "distributions",
        "pytest_available",
        "vfy_origins",
        "vfy_files",
    ):
        if current.get(key) != expected.get(key):
            raise ValueError(f"installed environment changed: {python} ({key})")
    recorded_env = corpus._recorded_environment(entry, env)
    if recorded_env != expected.get("environment"):
        raise ValueError(f"execution environment changed: {python}")
    return current


def _sample(
    name: str,
    entry: dict[str, Any],
    project: Path,
    cwd: Path,
    output: Path,
    route: str,
    settings: dict[str, Any],
    expected_counts: dict[str, int],
    phase: str,
    index: int,
) -> dict[str, Any]:
    corpus._clear_caches(project)
    for cache in (project / ".vfy", project / ".pytest_cache"):
        if cache.exists() or cache.is_symlink():
            raise ValueError(f"could not clear harness cache {cache}")
    destination = output / name / f"{phase}-{index:02d}-{route}"
    destination.mkdir(parents=True, exist_ok=False)
    command = settings["command"]
    names = {
        "PATH",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "LANG",
        "LC_ALL",
        "TZ",
        "VFY_CORPUS_COLLECTION_CLOCK",
        *entry.get("env", {}),
        *entry.get("env_paths", {}),
    }
    (destination / "command.json").write_text(
        json.dumps(
            {
                "argv": command,
                "cwd": str(cwd),
                "environment": {name: settings["env"].get(name) for name in sorted(names)},
                "umask": entry.get("umask"),
                "codspeed_environment_present": "CODSPEED_ENV" in settings["env"],
            },
            indent=2,
        )
        + "\n"
    )
    started = time.perf_counter_ns()
    try:
        completed = run_captured(
            command,
            cwd=cwd,
            env=settings["env"],
            timeout=float(entry.get("timeout", 180)),
            subprocess_umask=corpus._subprocess_umask(entry),
        )
    except subprocess.TimeoutExpired as error:
        for stream in ("stdout", "stderr"):
            value = getattr(error, stream) or b""
            data = value if isinstance(value, bytes) else value.encode()
            (destination / f"{stream}.txt").write_bytes(data)
        row = {"phase": phase, "index": index, "route": route, "exit": "timeout"}
        (destination / "result.json").write_text(json.dumps(row, indent=2) + "\n")
        raise ValueError(f"{name} {route}: sample timed out; see {destination}") from error
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    (destination / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (destination / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    counts = corpus.summary(completed.stdout + completed.stderr)
    row = {
        "phase": phase,
        "index": index,
        "route": route,
        "elapsed_ms": elapsed_ms,
        "exit": completed.returncode,
        "counts": counts,
    }
    (destination / "result.json").write_text(json.dumps(row, indent=2) + "\n")
    if settings.get("benchmark_table_guard") and _benchmark_table(
        completed.stdout + completed.stderr
    ):
        raise ValueError(f"{name} {route}: sample measured benchmarks; see {destination}")
    if completed.returncode != 0 or counts != expected_counts:
        raise ValueError(
            f"{name} {route}: sample status or outcome counts changed; see {destination}"
        )
    return row


def _run_package(
    args: argparse.Namespace,
    name: str,
    entry: dict[str, Any],
    wheel: dict[str, Any],
    output: Path,
    vfy_extra: list[str],
) -> dict[str, Any]:
    project = corpus._checkout_path(entry)
    cwd = (project / entry.get("cwd", ".")).resolve()
    if not cwd.is_relative_to(project.resolve()):
        raise ValueError(f"{name}: working directory resolves outside checkout")
    outcomes = _route_gate(name, entry, args.full_results, args.bare_results, wheel)
    receipt_hashes = _receipt_hashes(name, args.full_results, args.bare_results)
    identity = _identity(project, entry)
    routes: dict[str, dict[str, Any]] = {}
    bare_distributions = outcomes["vfy-bare"]["provenance"]["distributions"]
    for route in ROUTES:
        venv = project / (".venv-bare" if route == "vfy-bare" else ".venv")
        python = venv / "bin/python"
        env = _environment(entry, venv)
        command = _timed_command(route, outcomes[route], python)
        if route != "pytest" and command[4 : 4 + len(vfy_extra)] != vfy_extra:
            raise ValueError(f"{name} {route}: receipt command differs from attested vfy arguments")
        options = _check_addopts(
            project / ".venv/bin/python",
            cwd,
            route,
            command,
            env,
            subprocess_umask=corpus._subprocess_umask(entry),
        )
        _probe(python, cwd, env, outcomes[route], entry)
        distributions = outcomes[route]["provenance"]["distributions"]
        if route != "vfy-bare":
            _check_benchmark_options(options, env, distributions, bare_distributions)
        routes[route] = {
            "command": command,
            "env": env,
            "python": python,
            "benchmark_table_guard": (
                "pytest-benchmark" in distributions and "pytest-benchmark" not in bare_distributions
            ),
        }
    records: list[dict[str, Any]] = []
    try:
        for phase, count in (("warmup", args.warmups), ("sample", args.runs)):
            for index, order in enumerate(rotated_orders(ROUTES, count)):
                records.extend(
                    _sample(
                        name,
                        entry,
                        project,
                        cwd,
                        output,
                        route,
                        routes[route],
                        outcomes[route]["counts"],
                        phase,
                        index,
                    )
                    for route in order
                )
    finally:
        corpus._clear_caches(project)
        if _identity(project, entry) != identity:
            raise ValueError(f"{name}: tracked checkout changed during samples")
        if corpus._recipe(corpus.manifest()[name]) != corpus._recipe(entry):
            raise ValueError(f"{name}: manifest recipe changed during samples")
        if _digest(args.wheel.resolve()) != wheel["sha256"]:
            raise ValueError("wheel changed during samples")
        if _receipt_hashes(name, args.full_results, args.bare_results) != receipt_hashes:
            raise ValueError(f"{name}: corpus evidence changed during samples")
        for route in ROUTES:
            _probe(routes[route]["python"], cwd, routes[route]["env"], outcomes[route], entry)
    return {
        "recipe": corpus._recipe(entry),
        "source": identity,
        "artifact_sha256": wheel["sha256"],
        "receipt_sha256": receipt_hashes,
        "samples": records,
        "summary": {
            route: summarize(
                [r["elapsed_ms"] for r in records if r["route"] == route and r["phase"] == "sample"]
            )
            for route in ROUTES
        },
    }


def run(args: argparse.Namespace) -> bool:
    corpus.WORK = args.work.resolve()
    corpus.SOURCES = corpus.WORK / "src"
    corpus._validate_work_root()
    entries = corpus.manifest()
    names = args.names or [name for name, entry in entries.items() if not entry.get("skip")]
    if not names or len(set(names)) != len(names) or any(name not in entries for name in names):
        raise ValueError("package names must be unique manifest entries")
    if args.warmups < 0 or args.runs < 1:
        raise ValueError("warmups must be nonnegative and runs must be positive")
    output = args.out.resolve()
    if output.is_relative_to(corpus.WORK):
        raise ValueError("output must be outside the corpus work directory")
    output.mkdir(parents=True, exist_ok=False)
    wheel = corpus._artifact(str(args.wheel.resolve()), None)
    assert wheel is not None
    results: dict[str, Any] = {
        "diagnostic_only": True,
        "per_sample_case_parity": False,
        "preflight_scope": (
            "ordered JUnit identities/outcomes and terminal counts; not phases or warnings"
        ),
        "source_identity_scope": "pinned HEAD and tracked diff before and after timing",
        "sample_state": "remove .vfy and .pytest_cache before each command",
        "benchmark_gate": (
            "rejects visible pytest-benchmark measurements and configured summary suppression "
            "or profiling; enabled CodSpeed is rejected. Arbitrary custom hooks can hide work."
        ),
        "warmups": args.warmups,
        "runs": args.runs,
        "requested_names": names,
        "packages": {},
    }
    inventory_path = corpus.WORK / "run-inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    manifest_path = Path(corpus.__file__).with_name("manifest.json")
    if (
        inventory.get("autoload_disabled_unset") is not True
        or inventory.get("wheel_sha256") != wheel["sha256"]
        or inventory.get("manifest_sha256") != _digest(manifest_path)
        or inventory.get("harness_sha256") != _digest(Path(corpus.__file__))
        or not set(names).issubset(inventory.get("names", []))
    ):
        raise ValueError(
            "corpus run inventory does not attest wheel, manifest, and autoload policy"
        )
    vfy_extra = shlex.split(inventory["vfy_extra_args"])
    blocked = {name for flag, name in pairwise(vfy_extra) if flag == "-p"}
    if not {"no:randomly", "no:cacheprovider"} <= blocked:
        raise ValueError("vfy corpus recipe does not block the same two stock plug-ins")
    inventory_hash = _digest(inventory_path)
    results["run_inventory_sha256"] = inventory_hash
    results["artifact_revision_reported"] = inventory.get("source_revision")
    results["artifact_revision_source"] = "corpus run inventory; wheel bound by sha256"
    tooling = _tooling_identity()
    results["tooling"] = tooling
    results["tooling_unchanged"] = True
    eligible = []
    for name in names:
        try:
            _route_gate(name, entries[name], args.full_results, args.bare_results, wheel)
        except (OSError, ValueError, KeyError, TypeError) as error:
            results["packages"][name] = {"status": "ineligible", "reason": str(error)}
        else:
            eligible.append(name)
            results["packages"][name] = {"status": "pending"}
    write_atomic(output / "results.json", results)
    for name in eligible:
        try:
            row = _run_package(args, name, entries[name], wheel, output, vfy_extra)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            results["packages"][name] = {"status": "ineligible", "reason": str(error)}
        else:
            results["packages"][name] = {"status": "passed", **row}
        if _tooling_identity() != tooling:
            results["tooling_unchanged"] = False
            write_atomic(output / "results.json", results)
            raise ValueError("benchmark tooling or controller revision changed during sampling")
        write_atomic(output / "results.json", results)
    if _digest(inventory_path) != inventory_hash:
        raise ValueError("corpus run inventory changed during sampling")
    return all(row["status"] == "passed" for row in results["packages"].values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--full-results", type=Path, required=True)
    parser.add_argument("--bare-results", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("names", nargs="*")
    raise SystemExit(0 if run(parser.parse_args()) else 1)


if __name__ == "__main__":
    main()
