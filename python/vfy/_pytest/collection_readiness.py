"""Report collected tests that a native run would reject or could not set up."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _pytest.main import Session

UNSUPPORTED_ITEMS_PREFIX = "vfy: native execution does not run "
MISSING_FIXTURES_PREFIX = (
    "vfy: collected tests request fixtures that no loaded plug-in or conftest.py defines: "
)


def report_collection_readiness(session: Session) -> None:
    """Print the failures a native run of the collected tests would hit first.

    A collection-only run executes no test body or fixture. It can still name
    item types native execution does not run, and fixtures that no loaded
    plug-in or ``conftest.py`` defines, usually because native execution omits
    the installed plug-in that provides them.
    """
    # The CLI imports the prefixes without loading the pytest extension.
    from vfy.native import _pytest  # noqa: PLC0415

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    unsupported = _pytest.native_unsupported_items(session.items)
    if unsupported:
        noun = "item" if len(unsupported) == 1 else "items"
        reporter.write_line(
            f"{UNSUPPORTED_ITEMS_PREFIX}{len(unsupported)} collected {noun}, "
            f"such as {unsupported[0]}"
        )
    missing: Counter[str] = Counter()
    for item in session.items:
        info = getattr(item, "_fixtureinfo", None)
        if info is not None:
            missing.update(
                name
                for name in info.names_closure
                if name != "request" and name not in info.name2fixturedefs
            )
    if missing:
        names = ", ".join(
            f"{name} ({count} {'test' if count == 1 else 'tests'})"
            for name, count in sorted(missing.items())
        )
        reporter.write_line(f"{MISSING_FIXTURES_PREFIX}{names}")
