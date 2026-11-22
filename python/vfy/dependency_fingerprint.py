"""Installed-distribution identity used by safe test replay."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import threading


def _identity_from_text(contents: str) -> tuple[str | None, str | None]:
    name = None
    version = None
    for line in contents.splitlines():
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if field.lower() == "name":
            name = value.strip()
        elif field.lower() == "version":
            version = value.strip()
        if name is not None and version is not None:
            break
    return name, version


def _distribution_identity(
    distribution: importlib.metadata.Distribution,
) -> tuple[str, str]:
    path = getattr(distribution, "_path", None)
    if path is not None:
        for filename in ("METADATA", "PKG-INFO"):
            try:
                with open(os.path.join(os.fspath(path), filename), encoding="utf-8") as stream:
                    name = None
                    version = None
                    for line in stream:
                        field, separator, value = line.partition(":")
                        if not separator:
                            continue
                        if field.lower() == "name":
                            name = value.strip()
                        elif field.lower() == "version":
                            version = value.strip()
                        if name is not None and version is not None:
                            return name, version
            except (OSError, TypeError, UnicodeError):
                continue
    read_text = getattr(distribution, "read_text", None)
    if callable(read_text):
        for filename in ("METADATA", "PKG-INFO"):
            contents = read_text(filename)
            if contents is not None:
                name, version = _identity_from_text(contents)
                if name is not None and version is not None:
                    return name, version
    metadata = distribution.metadata
    return (
        str(metadata.get("Name") or distribution.name),
        str(metadata.get("Version") or distribution.version),
    )


def _compute() -> str:
    distributions = []
    for distribution in importlib.metadata.distributions():
        name, version = _distribution_identity(distribution)
        distributions.append((name.lower(), version))
    distributions.sort()
    payload = json.dumps(distributions, separators=(",", ":"), ensure_ascii=True)
    return "installed-dependencies:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PendingDependencyFingerprint:
    """Own one asynchronous distribution scan for a single invocation."""

    def __init__(self) -> None:
        self._value: str | None = None
        self._error: BaseException | None = None
        self._consumed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._value = _compute()
        except BaseException as exc:  # noqa: BLE001 - transfer the worker failure to its owner
            self._error = exc

    def take(self) -> str:
        """Join and consume this invocation's fingerprint or its error."""
        if self._consumed:
            raise RuntimeError("dependency fingerprint was already consumed")
        self._consumed = True
        self._thread.join()
        error, self._error = self._error, None
        value, self._value = self._value, None
        if error is not None:
            raise error
        if value is None:
            raise RuntimeError("dependency fingerprint worker returned no value")
        return value

    def close(self) -> None:
        """Join before releasing invocation-owned state, including on error."""
        self._thread.join()
        self._error = None
        self._value = None


def installed_dependency_fingerprint() -> str:
    """Return the current name/version identity of installed distributions."""
    return _compute()
