"""Stage upstream JUnit serialization for Rust's atomic report publication."""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from io import StringIO
from typing import Protocol, TextIO, cast

import pytest
from _pytest import junitxml

from vfy.native import _pytest


class _OpenXMLFile(Protocol):
    def __call__(
        self, file: str, mode: str = "r", *, encoding: str | None = None
    ) -> AbstractContextManager[TextIO]: ...


@contextmanager
def atomic_junit_report(config: pytest.Config) -> Iterator[None]:
    """Capture LogXML's completed output without changing its model or report path."""
    reporter = config.stash.get(junitxml.xml_key, None)
    if reporter is None:
        yield
        return
    namespace = vars(junitxml)
    had_open = "open" in namespace
    previous_open = namespace.get("open")
    # The upstream module's open lookup follows Python's text-file protocol.
    open_file = cast(_OpenXMLFile, namespace.get("open", builtins.open))
    with StringIO() as buffer:

        @contextmanager
        def capture_open(
            file: str, mode: str = "r", *, encoding: str | None = None
        ) -> Iterator[TextIO]:
            if file == reporter.logfile and mode == "w":
                yield buffer
                _pytest.publish_junit_xml(file, buffer.getvalue())
            else:
                with open_file(file, mode, encoding=encoding) as stream:
                    yield stream

        # Intercept open so LogXML and terminal output keep the requested path.
        namespace["open"] = capture_open
        try:
            yield
        finally:
            if had_open:
                namespace["open"] = previous_open
            else:
                del namespace["open"]
