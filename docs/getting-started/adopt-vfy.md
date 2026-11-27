# Adopt vfy in an existing project

Add vfy to a project that uses pytest with pytest-cov or Coverage.py, then
compare test execution and coverage in separate runs. Start from the project
root on Linux or macOS with CPython 3.12 or later. The examples use uv and the
`tests` directory. Use the same checkout, interpreter, test dependencies, and
services for each comparison.

## Add vfy to a supported test environment

For a library that supports Python versions below 3.12, mark the development
dependency so uv can resolve it alongside the library’s existing
`requires-python` range. Select a supported interpreter for the vfy job.

```console
uv add --dev --python 3.12 "vfy; python_version >= '3.12'"
uv run --python 3.12 vfy --version
```

Keep the existing jobs that test older Python versions. If you already require
Python 3.12 or newer, use `uv add --dev vfy`. For a pip-managed test
environment, add `vfy; python_version >= "3.12"` to your test requirements and
run the examples without `uv run --python 3.12` in a supported environment.
If your test matrix includes other operating systems, add a `sys_platform`
marker for Linux and macOS or use a dependency group installed only by the vfy
job.

## Compare test execution

Check [suite compatibility](../guides/native-adoption.md) before trying the
native engine. Keep pytest and all test dependencies installed while you compare
the two runners with coverage disabled.

The examples clear inherited pytest arguments with `PYTEST_ADDOPTS=` and
`-o addopts=`. Copy the test selectors, plug-in options, and other settings
from your usual command and both sources of inherited options into each run.
Omit the inherited coverage flags. If you request file reports such as JUnit
XML, give each command its own output paths.

<!-- doc-test: compare-runners -->
```console
PYTEST_ADDOPTS= uv run --python 3.12 pytest -o addopts= tests -v
PYTEST_ADDOPTS= uv run --python 3.12 vfy test --engine=native -o addopts= --no-cov tests -v
```

Compare collected test IDs, outcomes, exit statuses, and the behavior your
fixtures and plug-ins provide. A runner comparison can stay in its own job
alongside the existing coverage job.

If a difference appears against an older pytest pin, repeat the comparison in
a separate environment with vfy's
[pytest reference version](../about/compatibility.md#versions). This helps
identify differences between pytest releases while keeping the original job
available for comparison.

## Compare coverage under pytest

Run the same tests under pytest with each collector. If your coverage
configuration defines the source roots, use bare `--cov` to keep them. Carry
over the existing branch mode and other coverage settings. Pass a custom
configuration path with `--cov-config` for the plug-ins and `--rcfile` for
the coverage commands. This example uses pytest-cov 7.1, a 93% threshold, and
zero decimal places. Set the threshold and precision explicitly on both
commands, including when they come from coverage configuration in your current
job.

Store comparison databases and reports in a directory separate from your
existing job's data. Use a distinct database and report path for each run.
Clear inherited options as above, then reapply report options with the new paths.

<!-- doc-test: prepare-coverage-directories -->
```console
mkdir -p .vfy-adoption
```

<!-- doc-test: compare-collectors -->
```console
PYTEST_ADDOPTS= COVERAGE_FILE=.vfy-adoption/pytest-cov.data uv run --python 3.12 pytest -o addopts= -p pytest_cov --cov --cov-report=term-missing --cov-report=xml:.vfy-adoption/pytest-cov.xml --cov-fail-under=93 --cov-precision=0 tests -v
PYTEST_ADDOPTS= COVERAGE_FILE=.vfy-adoption/vfy.data uv run --python 3.12 pytest -o addopts= -p no:pytest_cov -p vfy.pytest_plugin --cov --cov-report=term-missing --cov-report=xml:.vfy-adoption/vfy.xml --cov-fail-under=93 --cov-precision=0 tests -v
```

### Compare Coverage.py commands

If your current job uses `coverage run -m pytest`, compare these commands
with the same configuration and pytest options. If your job combines shards
before reporting, run its combine step with each run's `--data-file`. The XML
commands export the reports, and the next section checks the coverage gate.

<!-- doc-test: compare-coverage-commands -->
```console
PYTEST_ADDOPTS= uv run --python 3.12 coverage run --data-file=.vfy-adoption/coverage-py.data -m pytest -o addopts= tests -v
PYTEST_ADDOPTS= uv run --python 3.12 vfy coverage run --data-file=.vfy-adoption/vfy-run.data -m pytest -o addopts= tests -v
uv run --python 3.12 coverage xml --data-file=.vfy-adoption/coverage-py.data --fail-under=0 -o .vfy-adoption/coverage-py.xml
uv run --python 3.12 vfy coverage xml --data-file=.vfy-adoption/vfy-run.data --fail-under=0 -o .vfy-adoption/vfy-run.xml
```

## Check the coverage gate

Compare the measured files, missing lines and branches, report totals, and
pass or fail status at your CI threshold. Check each exported report with the
service that consumes it.

Use the pinned Coverage.py version from your existing job to report on both
databases. Then run the vfy reporter on the same candidate database. Keep the
report configuration, precision, and threshold the same and record both
reporter versions. For the `coverage run` comparison, use
`.vfy-adoption/coverage-py.data` and `.vfy-adoption/vfy-run.data`.

<!-- doc-test: compare-reporters -->
```console
uv run --python 3.12 coverage report --data-file=.vfy-adoption/pytest-cov.data --fail-under=93 --precision=0 --show-missing
uv run --python 3.12 coverage report --data-file=.vfy-adoption/vfy.data --fail-under=93 --precision=0 --show-missing
uv run --python 3.12 vfy coverage report --data-file=.vfy-adoption/vfy.data --fail-under=93 --precision=0 --show-missing
```

The first two commands show how the collected data affects the report. The
last two show whether reporting alone changes the result. Each standalone
report exits with status 2 below the threshold.

## Switch CI one change at a time

Keep the existing required jobs while you check the comparison results. You
can switch only the collector by using the vfy coverage plug-in under pytest,
or add `vfy test --no-cov` for test execution while the original coverage job
continues to run. If you want a single `vfy test` job for both, compare the
combined run against your existing CI job before making it required.

Use the [CI recipes](../guides/ci.md) for full-suite runs and artifact uploads.
To revert a change, restore the original job command, plug-in flags, and
artifact paths. Keep the original pytest-cov dependency and lockfile entries
until you finish the comparison.
