"""Opt-in phase records for the edit-loop benchmark."""

from __future__ import annotations

import atexit
import json
import os
import time
from pathlib import Path
from typing import Any


class DiagnosticProfile:
    """Record coarse process phases without affecting ordinary commands."""

    def __init__(self, process: str) -> None:
        self.process = process
        self.started_ns = time.perf_counter_ns()
        self.phases: list[dict[str, Any]] = []
        spawn = os.getenv(f"VFY_PROFILE_{process.upper().replace('-', '_')}_SPAWN_NS")
        if spawn is not None:
            self.phases.append(
                {
                    "name": "startup",
                    "elapsed_ms": (self.started_ns - int(spawn)) / 1_000_000,
                }
            )
        atexit.register(self.write)

    @staticmethod
    def begin() -> int:
        return time.perf_counter_ns()

    def end(self, name: str, started_ns: int) -> None:
        self.phases.append(
            {
                "name": name,
                "elapsed_ms": (time.perf_counter_ns() - started_ns) / 1_000_000,
            }
        )

    def write(self) -> None:
        directory = Path(os.environ["VFY_PROFILE_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "process": self.process,
            "pid": os.getpid(),
            "elapsed_ms": (time.perf_counter_ns() - self.started_ns) / 1_000_000,
            "phases": self.phases,
        }
        target = directory / f"{self.process}.json"
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
