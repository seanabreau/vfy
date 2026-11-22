"""Write the exact installed environment lock used by pytest corpus evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.pytest_execution_record import resolve_executable, resolved_environment, write_atomic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_atomic(args.output.resolve(), resolved_environment(resolve_executable(args.python)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
