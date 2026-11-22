"""Publish worker collection completion after pytest finishes unconfigure."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vfy.native import _pytest

from .worker_completion import _NativeWorkerCompletionRecorder


def main() -> None:
    # Match -m pytest's public argv while the completion envelope uses this launcher.
    sys.argv[0] = str(Path(pytest.__file__).with_name("__main__.py"))
    recorder = _NativeWorkerCompletionRecorder()
    status = pytest.main(sys.argv[1:], plugins=[recorder])
    records: list[tuple[str, str, int, str]] = []
    if recorder.config is not None:
        # Pytest installs assertion rewriting before loading its native plugin.
        from .native_pytest import _session_warnings_key  # noqa: PLC0415

        records = recorder.config.stash.get(_session_warnings_key, [])
    _pytest.NativeParallelWorker.complete_collection(
        os.environ["VFY_NATIVE_PARALLEL_DIR"],
        int(os.environ["VFY_NATIVE_PARALLEL_INDEX"]),
        status,
        recorder.infrastructure_failed or recorder.config is None,
        records,
        recorder.exit_reason,
    )
    raise SystemExit(status)


if __name__ == "__main__":
    main()
