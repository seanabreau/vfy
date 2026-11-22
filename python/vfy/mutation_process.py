"""Validate and execute one source mutation in its fresh pytest interpreter."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .mutation import MutationSpec, _patch_bytes


def main() -> int:
    """Keep source compilation and its audit hooks outside the controller."""
    spec = MutationSpec.from_mapping(json.loads(os.environ["VFY_MUTATION"]))
    try:
        source = Path(spec.path).read_bytes()
        compile(_patch_bytes(source, spec), spec.path, "exec", dont_inherit=True)
    except (OSError, UnicodeError, ImportError, SyntaxError) as error:
        Path(os.environ["VFY_MUTATION_VALIDATION_ERROR"]).write_text(str(error), encoding="utf-8")
        return 3

    import pytest  # noqa: PLC0415 - validate before pytest can import project code

    return int(pytest.main())


if __name__ == "__main__":
    raise SystemExit(main())
