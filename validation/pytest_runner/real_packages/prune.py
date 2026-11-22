"""List pytest, its plug-ins, and its unused pytest-only dependencies for removal.

Run with the target environment's interpreter. The distribution installed from
the checkout given as the first argument is kept even when it is a pytest
dependency, such as pluggy or packaging testing themselves. Uses pytest's
installed packaging dependency to evaluate environment markers. Prints the
removed distribution names, one per line.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import sys
from pathlib import Path

from packaging.requirements import Requirement

# pytest's own runtime dependencies. General-purpose libraries such as
# packaging and pygments stay installed: test suites import them directly.
_PYTEST_DEPENDENCIES = {"iniconfig", "pluggy"}


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(distribution: importlib.metadata.Distribution) -> set[str]:
    names: set[str] = set()
    for line in distribution.requires or ():
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        names.add(_canonical(requirement.name))
    return names


def _local_project(distribution: importlib.metadata.Distribution, checkout: Path) -> bool:
    text = distribution.read_text("direct_url.json")
    if not text:
        return False
    url = json.loads(text).get("url", "")
    return (
        isinstance(url, str)
        and url.startswith("file://")
        and Path(url.removeprefix("file://")).resolve() == checkout
    )


def main() -> None:
    """Print the distributions to remove for the checkout in ``sys.argv[1]``."""
    checkout = Path(sys.argv[1]).resolve()
    found = list(importlib.metadata.distributions())
    distributions = {_canonical(dist.metadata["Name"]): dist for dist in found}
    keep = {_canonical(dist.metadata["Name"]) for dist in found if _local_project(dist, checkout)}
    requirements = {name: _requirements(dist) for name, dist in distributions.items()}
    removed = {"pytest"} - keep
    changed = True
    while changed:
        changed = False
        for name, required in requirements.items():
            if name not in removed and name not in keep and required & removed:
                removed.add(name)
                changed = True
    remaining = set(distributions) - removed
    for name in sorted(_PYTEST_DEPENDENCIES & remaining - keep):
        if not any(name in requirements[other] for other in remaining if other != name):
            removed.add(name)
    sys.stdout.write("".join(f"{name}\n" for name in sorted(removed & set(distributions))))


if __name__ == "__main__":
    main()
