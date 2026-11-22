# Measure coverage with vfy

Find an untested branch, add its test, and require a coverage threshold. This
tutorial runs your tests with vfy's independent engine on Linux or macOS, then
exports the saved coverage as JSON and XML.

## Set up the example

You need Linux or macOS, CPython 3.12+, and uv;
[Installation](installation.md) covers the requirements.

If you completed the selection tutorial, continue in that example and skip the
setup commands below. Both tests still pass and the negative-input branch is
still uncovered. This coverage workflow does not need a Git baseline.

Otherwise, create a standalone project and install vfy from PyPI:

```console
uv init --bare --vcs none vfy-coverage-example
cd vfy-coverage-example
uv add --dev vfy
```

uv records the resolved dependencies in `uv.lock` and installs a prebuilt wheel
on supported platforms.

Add the following configuration to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[tool.coverage.run]
branch = true
source = ["src"]

[tool.coverage.report]
show_missing = true

[tool.vfy.test]
source = ["src"]
```

Create `src/` and `tests/` directories. Save this as `src/bounds.py`:

```python
from __future__ import annotations


def clamp_low(value: int) -> int:
    if value < 0:
        return 0
    return value
```

Save this as `src/formatting.py`:

```python
from __future__ import annotations


def format_count(number: int) -> str:
    return f"{number}"
```

Save this as `tests/test_bounds.py`:

```python
from __future__ import annotations

from bounds import clamp_low


def test_positive_value() -> None:
    assert clamp_low(5) == 5
```

Save this as `tests/test_formatting.py`:

```python
from __future__ import annotations

from formatting import format_count


def test_format_count() -> None:
    assert format_count(42) == "42"
```

Add a `.gitignore` so generated files stay out of the selection tutorial's
Git baseline:

```gitignore
.venv/
__pycache__/
.pytest_cache/
.coverage
.coverage.*
.vfy/
coverage.json
coverage.xml
```

The example tests `clamp_low(5) == 5` and a separate formatting function.
`clamp_low` returns zero for negative inputs, but that branch has no test.
The configuration adds `src` to the test import path and enables branch coverage.

## Collect and inspect

Run both tests and save coverage:

<!-- doc-test: collect -->
```console
uv run vfy test --engine=native --all --cov=src --cov-branch --cov-report= -v
```

Both tests pass. `--all` runs the full configured suite, and `--cov-report=`
saves coverage data without printing a report. The output ends like this
(abridged):

```text
tests/test_bounds.py::test_positive_value PASSED                         [ 50%]
tests/test_formatting.py::test_format_count PASSED                       [100%]
...
============================== 2 passed in 0.36s ===============================
```

Print the report from the saved data:

<!-- doc-test: report -->
```console
uv run vfy coverage report --show-missing
```

<!-- doc-test: missing-output -->
```text
Name                Stmts   Miss Branch BrPart  Cover   Missing
---------------------------------------------------------------
src/bounds.py           5      1      2      1    71%   6
src/formatting.py       3      0      0      0   100%
---------------------------------------------------------------
TOTAL                   8      1      2      1    80%
```

Line 6, `return 0`, never ran. Coverage counts statements and
branch exits: 7 of 8 statements and 1 of 2 branch exits ran, for 80%. The report
exits 0 because no threshold was requested.

## Make the coverage gate fail

Require 100% coverage:

<!-- doc-test: fail-gate -->
```console
uv run vfy coverage report --fail-under 100
```

The report shows 80%, so the command exits with status **2**.

## Cover the missing branch

Replace `tests/test_bounds.py` with:

<!-- doc-test: complete-tests -->
```python
from __future__ import annotations

from bounds import clamp_low


def test_positive_value() -> None:
    assert clamp_low(5) == 5


def test_negative_value() -> None:
    assert clamp_low(-1) == 0
```

Collect again after any change. Reports read saved coverage and don't rerun
tests.

<!-- doc-test: recollect -->
```console
uv run vfy test --engine=native --all --cov=src --cov-branch --cov-report= -v
uv run vfy coverage report --show-missing --fail-under 100
```

All three tests pass. The report shows 100% with no missing lines or branch
exits, and both commands exit with status 0.

## Export reports

Write JSON and Cobertura XML files to the example directory:

<!-- doc-test: export -->
```console
uv run vfy coverage json -o coverage.json --pretty-print
uv run vfy coverage xml -o coverage.xml
```

The JSON lists executed and missing lines and branches for each file. The XML
uses the Cobertura format. Both reports read `.coverage` without changing it.

To use this in your own project, [install vfy](installation.md), then run
`uv run vfy test --engine=native --all --cov=src` with your source directory.
For the optional installed-pytest workflow, follow
[collection under pytest](../guides/collection.md#collect-with-stock-pytest).

**Next:** [Run the tests an edit affects](selection.md), starting with a fresh
example if you added the test above. For
CI, see [CI and quality gates](../guides/ci.md); for parallel vfy tests, see
[workers](../guides/run-tests.md#workers).
