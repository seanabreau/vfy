"""Install the owned mutation import resource before application startup."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

from vfy import native
from vfy.mutation import MutationSpec, _install_spec


def main(
    owner: native._pytest.NativeIndependentInvocation,
    startup: Callable[[], None],
) -> int:
    """Use the same pre-bound native invocation for import and execution."""
    finder = None
    if payload := owner.mutation_input:
        finder = _install_spec(
            MutationSpec.from_mapping(json.loads(payload)), owner.patch_mutation_source
        )
    try:
        startup()
        from .entry import (  # noqa: PLC0415 - mutation precedes application imports
            main as run,
        )

        return run(sys.argv[1:], invocation=owner)
    finally:
        if finder is not None and finder in sys.meta_path:
            sys.meta_path.remove(finder)


if __name__ == "__main__":
    owner = globals().pop("_vfy_worker_invocation", None)
    startup = globals().pop("_vfy_worker_startup", None)
    if not isinstance(owner, native._pytest.NativeIndependentInvocation) or not callable(startup):
        raise TypeError("fresh mutation bootstrap requires its native invocation and startup")
    raise SystemExit(main(owner, startup))
