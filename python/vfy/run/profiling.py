"""Optional diagnostic profile shared by the run modules."""

from __future__ import annotations

import os

_diagnostic_profile = None
if os.getenv("VFY_PROFILE_DIR"):
    from vfy.diagnostic_profile import DiagnosticProfile

    _diagnostic_profile = DiagnosticProfile("controller")


def _profile_begin() -> int:
    return _diagnostic_profile.begin() if _diagnostic_profile is not None else 0


def _profile_end(name: str, started: int) -> None:
    if _diagnostic_profile is not None:
        _diagnostic_profile.end(name, started)
