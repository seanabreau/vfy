"""Coverage path aliases resolve relative destinations through interpreter paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject

_DRIVER = """import importlib, sys
from pathlib import Path
provider, operation, search = sys.argv[1:]
module = importlib.import_module(provider)
sys.path.insert(0, search)
destination = provider + '.coverage'
cov = module.Coverage(config_file=False, data_file=destination)
cov.set_option('paths', {'other': ['.', '*/pkg'], 'source': ['src', '**/src']})
data = (cov.get_data() if operation == 'report' else
        module.CoverageData(basename=destination + '.shard'))
data.add_lines({str(Path('src/pkg/__init__.py').resolve()): [1]})
data.write()
if operation == 'combine':
    data.close(force=True)
    cov.combine(strict=True)
cov.save()
cov.json_report(outfile=provider + '.json')
cov.get_data().close(force=True)
data.close(force=True)
"""


@pytest.mark.parametrize("operation", ["report", "combine"])
@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.parametrize("repeated_package_dir", [False, True])
def test_path_alias_destination_searches_sys_path(
    tmp_path: Path, operation: str, shadow: bool, repeated_package_dir: bool
) -> None:
    project = VfyProject(tmp_path / "pkg" / "project" if repeated_package_dir else tmp_path)
    source = project.write("src/pkg/__init__.py", "VALUE = 1\n")
    search = project.root / "search"
    search.mkdir()
    if shadow:
        project.write("search/__init__.py", "SHADOW = 2\n")
    project.write("driver.py", _DRIVER)
    observations = {}
    for provider in ("coverage", "vfy.api"):
        result = project.run_python("driver.py", provider, operation, str(search))
        result.expect(0)
        report = json.loads((project.root / f"{provider}.json").read_text())
        persisted = project.data(project.root / f"{provider}.coverage")
        observations[provider] = {
            "files": report["files"],
            "totals": report["totals"],
            "persisted_files": sorted(persisted.measured_files()),
        }
        persisted.close(force=True)
    expected = "search/__init__.py" if shadow else "src/pkg/__init__.py"
    assert set(observations["coverage"]["files"]) == {expected}
    expected_data = search / "__init__.py" if shadow and operation == "combine" else source
    assert observations["coverage"]["persisted_files"] == [str(expected_data)]
    assert observations["vfy.api"] == observations["coverage"]
