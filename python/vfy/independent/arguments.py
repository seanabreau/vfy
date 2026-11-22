"""Argument declarations shared by the bootstrap and plugin protocol parser."""

from __future__ import annotations

import argparse
import os
from typing import override


class _VersionAction(argparse.Action):
    """Record a version request; the verbose form is not implemented and is refused."""

    @override
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, 0):
            parser.error("--version and -V may be given once; the verbose form is not supported")
        setattr(namespace, self.dest, 1)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vfy test --engine=native", allow_abbrev=False)
    # Cache-aware plugins inspect this disabled mode even without requesting --ff.
    parser.set_defaults(failedfirst=False)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("-q", "--quiet", action="count", default=0)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--verbosity", dest="verbose", type=int, default=0)
    parser.add_argument(
        "-V", "--version", action=_VersionAction, nargs=0, dest="version", default=0
    )
    parser.add_argument("-x", "--exitfirst", action="store_const", const=1, dest="maxfail")
    parser.add_argument("--maxfail", type=int, default=0)
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--no-summary", action="store_true", default=False)
    parser.add_argument(
        "--code-highlight", dest="code_highlight", choices=("yes", "no"), default="yes"
    )
    parser.add_argument("-r", "--report-chars", dest="reportchars", default="fE")
    parser.add_argument("--disable-warnings", "--disable-pytest-warnings", action="store_true")
    parser.add_argument(
        "--tb",
        dest="tbstyle",
        choices=("auto", "long", "short", "no", "line", "native"),
        default="auto",
    )
    parser.add_argument(
        "--assert", dest="assertmode", choices=("rewrite", "plain"), default="rewrite"
    )
    parser.add_argument("--durations", type=int, default=None)
    parser.add_argument("--durations-min", type=float, default=None)
    parser.add_argument("--full-trace", "--fulltrace", dest="fulltrace", action="store_true")
    parser.add_argument("-l", "--showlocals", action="store_true", default=False)
    parser.add_argument("--no-showlocals", dest="showlocals", action="store_false")
    parser.add_argument("--capture", choices=("fd", "sys", "no", "tee-sys"), default="fd")
    parser.add_argument("-s", action="store_const", const="no", dest="capture")
    parser.add_argument("-k", dest="keyword")
    parser.add_argument("-m", dest="markexpr")
    parser.add_argument("-W", "--pythonwarnings", action="append", default=[])
    for name in (
        "level",
        "format",
        "date-format",
        "auto-indent",
        "cli-level",
        "cli-format",
        "cli-date-format",
    ):
        parser.add_argument(f"--log-{name}")
    parser.add_argument("-p", dest="plugins", action="append", default=[])
    parser.add_argument("-c", "--config-file")
    parser.add_argument("--rootdir")
    parser.add_argument(
        "--import-mode", choices=("prepend", "append", "importlib"), default="prepend"
    )
    parser.add_argument("-o", "--override-ini", action="append", default=[])
    parser.add_argument("--collect-in-virtualenv", action="store_true")
    parser.add_argument("--ignore", action="append", default=[])
    parser.add_argument("--ignore-glob", action="append", default=[])
    parser.add_argument("--deselect", action="append", default=[])
    parser.add_argument("--strict-config", action="store_true", default=None)
    parser.add_argument("--strict-markers", action="store_true", default=None)
    parser.add_argument("--strict", action="store_true", default=None)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--continue-on-collection-errors", action="store_true")
    parser.add_argument(
        "--keepduplicates",
        "--keep-duplicates",
        action="store_true",
        dest="keepduplicates",
        default=False,
    )
    parser.add_argument("--native-result-json")
    parser.add_argument("--basetemp")
    parser.add_argument("--junitxml", "--junit-xml")
    parser.add_argument("--junitprefix", "--junit-prefix")
    parser.add_argument("--workers", "-j", type=int, default=1)
    workers = parser.add_argument(
        "-n", dest="workers", type=process_count, default=argparse.SUPPRESS
    )
    vars(workers)["_vfy_native_worker_alias"] = True
    return parser


def process_count(value: str) -> int:
    """Read pytest-xdist's process count; ``0`` keeps tests in this process."""
    if value in {"auto", "logical"}:
        return os.cpu_count() or 1
    count = int(value)
    if count < 0:
        raise argparse.ArgumentTypeError("process count must not be negative")
    return max(count, 1)
