"""Derive a shared coverage workload without replacing project configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import cast

from benchmarks.benchmark_harness import benchmark_environment, run_checked
from benchmarks.benchmark_provenance import JSONValue

_QUERY = """
import json, sys
import coverage
value = coverage.Coverage(config_file=sys.argv[1] or True)
print(json.dumps({'config_file': value.config.config_file,
                  'patch': value.get_option('run:patch'), 'version': coverage.__version__}))
"""


def _configuration(
    python: Path, root: Path, config_file: Path | None, environment: Mapping[str, str]
) -> dict[str, JSONValue]:
    result = run_checked(
        [str(python), "-B", "-c", _QUERY, str(config_file) if config_file else ""],
        cwd=root,
        env=environment,
        timeout=60,
    )
    record = json.loads(result.stdout)
    if not isinstance(record, dict):
        raise RuntimeError("Coverage.py returned no configuration observation")
    return cast("dict[str, JSONValue]", record)


def prepare_subprocess_config(
    python: Path,
    root: Path,
    destination: Path,
    config_file: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, JSONValue]:
    """Use selected Coverage.py discovery and a controller-owned lossless TOML editor."""
    import tomlkit  # noqa: PLC0415 - optional benchmark preparation dependency

    env = benchmark_environment() if environment is None else environment
    original = _configuration(python, root, config_file, env)
    selected = original["config_file"]
    if not isinstance(selected, str) or not selected.endswith(".toml"):
        raise RuntimeError(
            "shared subprocess overlay requires an actual TOML coverage configuration"
        )
    source = Path(selected).resolve()
    destination = destination.resolve()
    if source == destination:
        raise RuntimeError("derived coverage configuration must not replace its source")
    content = source.read_bytes()
    document = tomlkit.parse(content.decode("utf-8"))
    patches = original["patch"]
    if not isinstance(patches, list) or any(not isinstance(value, str) for value in patches):
        raise RuntimeError("Coverage.py returned no valid subprocess patch observation")
    run = document["tool"]["coverage"].setdefault("run", tomlkit.table())
    if "subprocess" not in patches:
        run["patch"] = [*run.get("patch", []), "subprocess"]
    rendered = tomlkit.dumps(document).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(rendered)
    derived = _configuration(python, root, destination, env)
    expected = [*patches, *([] if "subprocess" in patches else ["subprocess"])]
    if derived["patch"] != expected:
        raise RuntimeError("derived subprocess patch differs from the declared workload")
    return {
        "kind": "shared-coverage-subprocess",
        "source_config": str(source),
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "config_path": str(destination),
        "config_sha256": hashlib.sha256(rendered).hexdigest(),
        "original_patch": patches,
        "patch": derived["patch"],
        "coverage_version": original["version"],
        "editor": "tomlkit",
        "editor_version": version("tomlkit"),
    }
