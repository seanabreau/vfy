"""Write vfy diagnostics to stderr, apart from child pytest output and report payloads."""

from __future__ import annotations

import argparse
import sys
from typing import NamedTuple


class OutputPolicy(NamedTuple):
    quiet: bool = False
    verbose: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> OutputPolicy:
        return cls(quiet=getattr(args, "quiet", False), verbose=bool(getattr(args, "verbose", 0)))

    def notice(self, text: str) -> None:
        if not self.quiet:
            print(text, file=sys.stderr)

    def diagnostic(self, text: str) -> None:
        if self.verbose:
            self.notice(f"vfy: {text}")
