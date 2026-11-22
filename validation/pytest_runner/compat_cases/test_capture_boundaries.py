"""Exercise phase-local streams, descriptor writes, and nested capture fixtures."""

# Prints are the public behavior under differential test.
# ruff: noqa: T201

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import pytest


def test_descriptor_output_across_native_read_chunks() -> None:
    payload = ("é" * 7000 + "tail\n").encode()
    for descriptor in (1, 2):
        remaining = memoryview(payload)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]


@pytest.fixture
def noisy_fixture() -> Iterator[None]:
    print("fixture setup stdout", flush=True)
    print("fixture setup stderr", file=sys.stderr, flush=True)
    yield
    print("fixture teardown stdout", flush=True)
    print("fixture teardown stderr", file=sys.stderr, flush=True)


def test_phase_streams(noisy_fixture: None) -> None:
    print("test stdout", flush=True)
    print("test stderr", file=sys.stderr, flush=True)
    os.write(1, b"descriptor stdout\n")
    os.write(2, b"descriptor stderr\n")


def test_capfd_read_resets(capfd: pytest.CaptureFixture[str]) -> None:
    os.write(1, b"first stdout\n")
    os.write(2, b"first stderr\n")
    assert capfd.readouterr() == ("first stdout\n", "first stderr\n")
    assert capfd.readouterr() == ("", "")
    print("second stdout", flush=True)
    assert capfd.readouterr() == ("second stdout\n", "")


def test_capsys_keeps_descriptor_capture(capsys: pytest.CaptureFixture[str]) -> None:
    print("python stdout")
    print("python stderr", file=sys.stderr)
    os.write(1, b"outside capsys stdout\n")
    assert capsys.readouterr() == ("python stdout\n", "python stderr\n")


def test_capfd_disabled_restores_outer_streams(capfd: pytest.CaptureFixture[str]) -> None:
    with capfd.disabled():
        print("uncaptured capfd stdout", flush=True)
        os.write(2, b"uncaptured capfd stderr\n")
    assert capfd.readouterr() == ("", "")


def test_capsys_disabled_restores_outer_streams(capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        print("uncaptured capsys stdout", flush=True)
    assert capsys.readouterr() == ("", "")


def test_subprocess_descriptor_inheritance() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; os.write(1,b'child stdout\\n'); os.write(2,b'child stderr\\n')",
        ],
        check=True,
    )


def test_invalid_bytes_are_replaced() -> None:
    os.write(1, b"invalid: \xff\n")


def test_stdin_read_is_rejected() -> None:
    with pytest.raises(OSError, match="pytest: reading from stdin"):
        sys.stdin.read()


def test_next_case_has_no_previous_output(capfd: pytest.CaptureFixture[str]) -> None:
    assert capfd.readouterr() == ("", "")


@pytest.fixture
def noisy_failed_setup() -> None:
    print("failed setup stdout", flush=True)
    os.write(2, b"failed setup stderr\n")
    raise RuntimeError("controlled noisy setup failure")


def test_failed_setup_preserves_streams(noisy_failed_setup: None) -> None:
    raise AssertionError("the body must not execute after setup failure")


@pytest.fixture
def noisy_failed_teardown() -> Iterator[None]:
    yield
    print("failed teardown stdout", flush=True)
    os.write(2, b"failed teardown stderr\n")
    raise RuntimeError("controlled noisy teardown failure")


def test_failed_teardown_preserves_streams(noisy_failed_teardown: None) -> None:
    print("successful body stdout", flush=True)
