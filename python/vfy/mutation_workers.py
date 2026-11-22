"""Process leases shared by mutation children and isolated pytest file groups."""

from __future__ import annotations

import contextlib
import os
import signal
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

INHERITED_LEASE_ENVIRONMENT_NAME = "VFY_MUTATION_WORKER_LEASE"
_WORKER_ENVIRONMENT_NAMES: tuple[str, ...] = (
    "VFY_MUTATION_TEST_ISOLATION",
    "VFY_MUTATION_WORKER_POOL",
    "VFY_MUTATION_WORKERS",
    "VFY_MUTATION_TEST_TIMEOUT",
    INHERITED_LEASE_ENVIRONMENT_NAME,
)


@contextlib.contextmanager
def _worker_environment(settings: Mapping[str, str]) -> Iterator[None]:
    previous = {name: os.environ[name] for name in _WORKER_ENVIRONMENT_NAMES if name in os.environ}
    try:
        for name in _WORKER_ENVIRONMENT_NAMES:
            os.environ.pop(name, None)
        os.environ.update(settings)
        yield
    finally:
        for name in _WORKER_ENVIRONMENT_NAMES:
            os.environ.pop(name, None)
        os.environ.update(previous)


@contextlib.contextmanager
def serial_worker_scope() -> Iterator[None]:
    """Keep inherited file-worker settings out of a resolved serial command."""
    with _worker_environment({}):
        yield


@contextlib.contextmanager
def isolated_worker_pool(capacity: int, timeout: float) -> Iterator[None]:
    """Scope file-worker leases and their pytest settings to one mutation run."""
    with tempfile.TemporaryDirectory(prefix="vfy-mutation-workers-") as directory:
        settings = {
            "VFY_MUTATION_TEST_ISOLATION": "file",
            "VFY_MUTATION_WORKER_POOL": directory,
            "VFY_MUTATION_WORKERS": str(capacity),
            "VFY_MUTATION_TEST_TIMEOUT": str(timeout),
        }
        with _worker_environment(settings):
            yield


def terminate_file_groups(directory: Path, owner: int) -> None:
    """Stop registered file groups after their outer process group is stopped."""
    for registration in directory.glob(f"group-{owner}-*"):
        pid = int(registration.name.rsplit("-", 1)[1])
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        registration.unlink(missing_ok=True)


class WorkerPool:
    """Hold one advisory file lock for each collecting or executing child process."""

    def __init__(self, directory: Path, capacity: int) -> None:
        self.directory = directory
        self.capacity = capacity
        self._leases: set[int] = set()
        self._inherited: int | None = None

    def adopt_inherited_lease(self) -> None:
        """Reuse the lease that a mutation controller passed to this supervisor.

        A fresh supervisor holds that lease while it collects, so collection
        counts against the worker capacity. The next ``acquire`` returns it for
        the first file group. A descriptor that does not refer to one of this
        pool's lease files is not a lease and is left untouched.
        """
        raw = os.environ.pop(INHERITED_LEASE_ENVIRONMENT_NAME, None)
        if raw is None:
            return
        try:
            fd = int(raw)
            held = os.fstat(fd)
        except (ValueError, OSError):
            return
        for index in range(self.capacity):
            try:
                lease = (self.directory / str(index)).stat()
            except OSError:
                continue
            if (lease.st_dev, lease.st_ino) == (held.st_dev, held.st_ino):
                os.set_inheritable(fd, False)
                self._leases.add(fd)
                self._inherited = fd
                return

    def acquire(self) -> int | None:
        if self._inherited is not None:
            fd, self._inherited = self._inherited, None
            return fd
        # The file-isolated runner rejects platforms without POSIX fork before
        # constructing a pool. Keep the optional platform import at this boundary.
        import fcntl  # noqa: PLC0415

        for index in range(self.capacity):
            fd = os.open(self.directory / str(index), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            except BaseException:
                os.close(fd)
                raise
            self._leases.add(fd)
            return fd
        return None

    def release(self, fd: int) -> None:
        self._leases.remove(fd)
        if fd == self._inherited:
            self._inherited = None
        os.close(fd)

    def after_fork(self, *, keep: int | None = None) -> None:
        # Close, rather than unlock, inherited open-file descriptions: unlocking
        # would release the parent's lease while a sibling is still executing.
        # A file worker retains its own lease until its group is terminated,
        # including when the supervisor dies before cleaning up the group.
        for fd in tuple(self._leases):
            if fd != keep:
                self.release(fd)

    def close(self) -> None:
        for fd in tuple(self._leases):
            self.release(fd)
