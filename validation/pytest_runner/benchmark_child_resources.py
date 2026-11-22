"""Capture child CPU, wall time, and Linux single-process RSS or process-tree PSS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from itertools import pairwise
from pathlib import Path


def _process_table() -> dict[int, tuple[int, int, int]]:
    """Map PID to (parent, process group, start time) from Linux procfs."""

    processes: dict[int, tuple[int, int, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError):
            continue
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) < 20:
            raise RuntimeError(f"incomplete proc stat for PID {entry.name}")
        processes[int(entry.name)] = (int(fields[1]), int(fields[2]), int(fields[19]))
    return processes


def _rollup_bytes(pid: int, start_time: int) -> tuple[int, int] | None:
    path = Path("/proc") / str(pid)
    try:
        initial = (path / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    initial_fields = initial[initial.rfind(")") + 2 :].split()
    if len(initial_fields) < 20:
        raise RuntimeError(f"incomplete proc stat for PID {pid}")
    if initial_fields[0] in {"Z", "X"}:
        return None
    if int(initial_fields[19]) != start_time:
        return None
    try:
        text = (path / "smaps_rollup").read_text(encoding="utf-8")
    except ProcessLookupError:
        # ESRCH means the task has released its address space during exit; the
        # proc stat entry can still briefly show its old non-zombie state.
        return None
    except FileNotFoundError as error:
        # An exited process contributes no live memory to this snapshot.
        try:
            stat = (path / "stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError):
            return None
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) > 19 and (int(fields[19]) != start_time or fields[0] in {"Z", "X"}):
            return None
        raise RuntimeError(f"smaps_rollup missing for live PID {pid}") from error
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in {"Pss", "Rss"}:
            parts = value.split()
            if len(parts) != 2 or parts[1] != "kB":
                raise RuntimeError(f"invalid {key} in smaps_rollup for PID {pid}")
            values[key] = int(parts[0]) * 1_024
    if set(values) != {"Pss", "Rss"}:
        try:
            current = (path / "stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError):
            return None
        current_fields = current[current.rfind(")") + 2 :].split()
        if len(current_fields) > 19 and (
            current_fields[0] in {"Z", "X"} or int(current_fields[19]) != start_time
        ):
            return None
        raise RuntimeError(f"incomplete smaps_rollup for PID {pid}")
    return values["Pss"], values["Rss"]


def _sample_tree(
    child_pid: int, collector_pid: int, group: int, known: set[tuple[int, int]]
) -> tuple[int, int, int, int]:
    table = _process_table()
    selected: set[int] = set()
    for pid, (_parent, process_group, start_time) in table.items():
        if pid == collector_pid:
            continue
        if pid == child_pid or (pid, start_time) in known or process_group == group:
            selected.add(pid)
    changed = True
    while changed:
        changed = False
        for pid, (parent, _, _) in table.items():
            if pid != collector_pid and parent in selected and pid not in selected:
                selected.add(pid)
                changed = True
    pss = rss = count = 0
    for pid in sorted(selected):
        start_time = table[pid][2]
        known.add((pid, start_time))
        rollup = _rollup_bytes(pid, start_time)
        if rollup is None:
            continue
        pss += rollup[0]
        rss += rollup[1]
        count += 1
    return pss, rss, count, len(known)


def _measure_tree(child: subprocess.Popen[str], interval_ms: int) -> dict[str, object]:
    if interval_ms < 1:
        raise ValueError("sample interval must be positive")
    stop = threading.Event()
    errors: list[Exception] = []
    known: set[tuple[int, int]] = set()
    readings: list[tuple[int, int, int, float]] = []
    collector_pid = os.getpid()
    group = os.getpgrp()

    def sample() -> None:
        try:
            while not stop.is_set():
                observed = time.perf_counter()
                pss, rss, count, _ = _sample_tree(child.pid, collector_pid, group, known)
                if count:
                    readings.append((pss, rss, count, observed))
                stop.wait(interval_ms / 1_000)
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(error)
            stop.set()

    thread = threading.Thread(target=sample, name="tree-memory-sampler")
    thread.start()
    try:
        stdout, stderr = child.communicate()
    finally:
        stop.set()
        thread.join()
    if errors:
        raise RuntimeError("process-tree memory sampling failed") from errors[0]
    if not readings:
        raise RuntimeError("no live child process-tree memory snapshots were captured")
    peak_pss, _, peak_count, _ = max(readings, key=lambda row: row[0])
    return {
        "stdout": stdout,
        "stderr": stderr,
        "peak_tree_pss_bytes": peak_pss,
        "peak_tree_rss_bytes": max(row[1] for row in readings),
        "sample_interval_ms": interval_ms,
        "max_sample_gap_ms": max(
            ((right[3] - left[3]) * 1_000 for left, right in pairwise(readings)),
            default=0.0,
        ),
        "snapshot_count": len(readings),
        "peak_process_count": peak_count,
        "tracked_process_count": len(known),
        "tree_scope": "sum of simultaneous live child and descendant PSS; collector excluded",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--sample-interval-ms", type=int, default=20)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if sys.platform != "linux" or not argv:
        parser.error("Linux resource units and a public child command are required")
    import resource  # noqa: PLC0415 - this collector is Linux-only

    if args.tree and os.getpgrp() != os.getpid():
        parser.error("tree resource collection requires a dedicated process group")
    for path in (args.record, args.stdout, args.stderr):
        if path.exists():
            raise FileExistsError(f"resource artifact already exists: {path}")

    # The outer run_captured creates the process group. This child inherits it,
    # so the existing deadline and interrupt cleanup reaches both processes.
    collector_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    started = time.perf_counter_ns()
    tree_record: dict[str, object] = {}
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        if args.tree:
            tree_record = _measure_tree(child, args.sample_interval_ms)
            stdout = str(tree_record.pop("stdout"))
            stderr = str(tree_record.pop("stderr"))
        else:
            stdout, stderr = child.communicate()
        returncode = child.returncode
    if not isinstance(returncode, int):
        raise RuntimeError("resource child has no reaped exit status")
    # communicate has drained both pipes and reaped the child before this boundary.
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)

    args.stdout.write_text(stdout, encoding="utf-8")
    args.stderr.write_text(stderr, encoding="utf-8")
    record = {
        "schema_version": 1,
        "method": "linux-process-tree-smaps-rollup" if args.tree else "linux-single-child-rusage",
        "collector_sha256": collector_sha256,
        "child_command": argv,
        "child_exit_status": returncode,
        "child_wall_ms": wall_ms,
        "user_cpu_ms": usage.ru_utime * 1_000,
        "system_cpu_ms": usage.ru_stime * 1_000,
        "total_cpu_ms": (usage.ru_utime + usage.ru_stime) * 1_000,
        "peak_rss_bytes": usage.ru_maxrss * 1_024,
        "rss_scope": "largest process RSS, not a sum of concurrent descendants",
        "wall_boundary": "child spawn, capture/drain, and reap; excludes collector startup",
        "cpu_scope": "child and reaped descendants; excludes collector",
        **tree_record,
    }
    args.record.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if returncode < 0:
        number = -returncode
        if number not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
        return 128 + number
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
