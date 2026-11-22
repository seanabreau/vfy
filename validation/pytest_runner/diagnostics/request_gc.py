"""Check whether a test instance retaining its request is collectible."""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--engine", choices=("pytest", "pytest-native"), required=True)
arguments = parser.parse_args()

root = Path(os.environ["NATIVE_GC_PROBE_ROOT"]).resolve()
root.mkdir(parents=True, exist_ok=True)
(root / "tracker.py").write_text("refs = []\n", encoding="utf-8")
(root / "test_cycle.py").write_text(
    """import weakref

import pytest
import tracker


class TestCycle:
    @pytest.mark.parametrize("value", [1])
    def test_request_cycle(self, request, value):
        assert value == 1
        self.request = request
        tracker.refs.append(weakref.ref(self))
""",
    encoding="utf-8",
)
sys.path.insert(0, str(root))
tracker = importlib.import_module("tracker")

os.chdir(root)
pytest_arguments = [
    "-q",
    "--capture=no",
    "-c",
    os.devnull,
    "--rootdir",
    str(root),
    "-p",
    "no:cacheprovider",
]
if arguments.engine == "pytest-native":
    pytest_arguments.extend(("-p", "vfy._pytest.native_pytest"))
pytest_arguments.append("test_cycle.py")
status = pytest.main(pytest_arguments)
collected = gc.collect()


def retained_request_view() -> dict[str, object] | None:
    instance = tracker.refs[0]()
    if instance is None:
        return None
    request = instance.request
    node = request.node
    return {
        "nodeid": node.nodeid,
        "params": getattr(getattr(node, "callspec", None), "params", {}),
        "instance_is_test_instance": request.instance is instance and node.instance is instance,
        "session_matches_node": request.session is node.session,
    }


request_view = retained_request_view()
sys.stdout.write(
    "GC_PROBE "
    + json.dumps(
        {
            "pytest_version": pytest.__version__,
            "exit_status": int(status),
            "weakrefs": len(tracker.refs),
            "alive_after_gc": sum(reference() is not None for reference in tracker.refs),
            "gc_collected": collected,
            "request_view": request_view,
        },
        sort_keys=True,
    )
    + "\n"
)


def release_session_items() -> None:
    instance = tracker.refs[0]()
    if instance is None:
        return
    session = instance.request.session
    session.items.clear()
    session._collection_cache.clear()


release_session_items()
collected_after_release = gc.collect()
sys.stdout.write(
    "GC_PROBE_AFTER_SESSION_RELEASE "
    + json.dumps(
        {
            "alive_after_gc": sum(reference() is not None for reference in tracker.refs),
            "gc_collected": collected_after_release,
        },
        sort_keys=True,
    )
    + "\n"
)

raise SystemExit(1 if any(reference() is not None for reference in tracker.refs) else 0)
