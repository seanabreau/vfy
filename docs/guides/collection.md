# Collect and report coverage

Collect lines and branches, save `.coverage`, and generate reports without
rerunning tests. Install [vfy](../getting-started/installation.md) in your
CPython 3.12+ test environment. Run commands from the project root and replace
`src` with your source directory. Coverage and [test execution](native-adoption.md) support Linux and macOS.

## Run tests with coverage

Check [suite compatibility](native-adoption.md) before adopting vfy.
`vfy test` supports a limited set of runtime plug-ins.

```console
uv run vfy test --all --cov=src --cov-branch --cov-report=term-missing
```

This runs the full suite, saves `.coverage`, and prints missing lines and
branches. `vfy test` collects coverage by default and records per-test contexts.
For affected-test runs, follow [test selection](test-selection.md); keep the same
collection settings between the baseline and selected runs.

## Collect a script or module

```console
uv run vfy coverage run --branch --source src app.py
uv run vfy coverage run --branch --source src -m pytest
```

Use your entry point in place of `app.py`. Each command saves `.coverage` and
returns the program's exit status. Arguments after the script or module name
pass through unchanged. Existing Coverage.py jobs can replace `coverage` with
`vfy coverage` for run, report, combine, erase, and file-report commands.

### Choose files and collection mode

`--source` includes unexecuted source files in reports. Repeat it for more
roots and use `--omit` to exclude files within those roots.

```console
uv run vfy coverage run --source src --omit '*/generated/*' -m pytest
```

To select executed files by glob, use `--include` on its own. When both
`--source` and `--include` are set for collection, `--source` takes precedence
and vfy emits the `include-ignored` warning. You can still use `--include` on
a later report to narrow its output.

Without an explicit source or include filter, vfy skips the standard library
and installed packages. `[run] cover_pylib = true` or the Python API's
`cover_pylib=True` also measures the standard library; installed packages remain
excluded. Collection defaults to lines.
`--branch` records arcs, including function entry and exit; a data file contains
lines or arcs, not both. For condition outcomes and short-circuit paths, use
`--decisions` (or pytest's `--cov-decisions`), which also enables branches. See
[semantic coverage](semantic.md) for collection, JSON output, and gates.

### Append or erase data

Runs replace saved data by default. To accumulate compatible runs from two scripts (replace their paths with yours):

```console
uv run vfy coverage run --append --source src scripts/check_units.py
uv run vfy coverage run --append --source src scripts/check_integration.py
uv run vfy coverage erase --parallel
```

The last command deletes the main database and its shards; omit `--parallel` to
delete only the main file. Both `vfy test` and stock pytest with vfy's coverage
plug-in accept `--cov-append`. Appending with `vfy test` runs the full requested
scope without test selection. Combine only data from the same source
revision and collection mode.

## Generate reports and enforce a threshold

After collection:

```console
uv run vfy coverage report --show-missing --precision 1 --fail-under 95
uv run vfy coverage html --directory htmlcov --title "Project coverage"
uv run vfy coverage json -o coverage.json --pretty-print --show-contexts
uv run vfy coverage xml -o coverage.xml
uv run vfy coverage lcov -o coverage.lcov
uv run vfy coverage annotate --directory annotations
```

The first command prints totals and fails with status 2 below 95%; invalid
configuration or data exits 1. pytest's `--cov-fail-under=95` instead fails with
status 1. The remaining commands write the named files. All formats share
exclusions, context filters, and totals. Use `--cov-report=` during a test run to
save data for a later report step.

Filter reports with `--include`, `--omit`, or repeatable `--contexts`; hide files
without statements with `--skip-empty`. Terminal reports also support
`--format=markdown` and `--format=total`. See the
[CLI reference](../reference/cli.md#vfy-coverage) for flags and sort values,
and [Python API](../reference/python-api.md) for report methods.

??? info "Report content and source limits"

    JSON contains line and branch totals in Coverage.py's format-3 shape plus
    `functions` and `classes`. XML contains rates, counts, package groups, and branch
    condition coverage; check it with the service that consumes it. LCOV contains
    per-file line hits, branches, and summary counts. Annotated source prefixes are
    `> ` for executed statements, `! ` for missing statements, `- ` for excluded
    statements, and two spaces for non-executable source.

    HTML writes an index, stylesheet, and one page per file. Each file is replaced
    atomically; a failed write can leave a mix of old and new files. Reports read
    source at its current path. A file-reporter plug-in can supply virtual source;
    test reports for generated or deleted files before making them a gate.

## Find which tests reach a line

Per-test contexts connect measured lines to test node IDs. Selection and mutation
testing use that mapping; ordinary coverage or one static run label cannot
identify the reaching tests. `vfy test` records contexts automatically. For stock
pytest, collect a full suite explicitly:

```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src --cov-context=test --cov-report=
uv run vfy explain src/service.py:42
uv run vfy coverage report --contexts '^tests/unit/'
```

Replace `src/service.py:42` with an executable source line. `explain` lists tests
that reached it, fastest first when durations are available; it does not run
those tests or explain selection decisions. With semantic data, it also shows the
pairs of recorded runs that demonstrate a condition changing a decision's
outcome (MC/DC; see [semantic coverage](semantic.md)). If neither reaching tests
nor such pairs are recorded, it exits with an error.

Contexts label each test phase as `nodeid|setup`, `nodeid|run`, and
`nodeid|teardown`, for phases that ran. Schema-7 data and combined xdist shards
retain them. Report filters are regular expressions; they filter lines and arcs
before calculating totals and leave saved data unchanged.

To label an entire run, use `vfy coverage run --context unit ...`.
With per-test contexts too, the label is `unit|nodeid|phase`. For programmatic
labels and dynamic frame contexts, see the [Python API](../reference/python-api.md#collect).

## Reuse Coverage.py configuration

vfy reads `.coveragerc`, `.coveragerc.toml`, `setup.cfg`, `tox.ini`, and
`pyproject.toml`. `COVERAGE_FILE` chooses the data basename; `COVERAGE_RCFILE`
chooses the configuration. For example:

```toml
[tool.coverage.run]
branch = true
source = ["src"]

[tool.coverage.report]
show_missing = true
fail_under = 90
```

```console
uv run vfy config show
uv run vfy config validate
```

These print resolved settings and validate their types and supported values.
Unknown sections and keys are errors. Keep vfy-only semantic settings under
`[tool.vfy.*]`. See [configuration](../reference/configuration.md) for discovery,
precedence, path aliases, exclusions, format destinations, and plug-in options.

vfy reads and writes Coverage.py 7 schema-7 SQLite data. To compare engines,
run the same suite with separate `COVERAGE_FILE` values, then compare files,
lines or arcs, contexts, exclusions, totals, and artifacts. Private APIs and
`dumps()` bytes are outside Coverage.py compatibility.

## Collect workers and subprocesses

With pytest-xdist 3.8.0 installed:

```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin -n 2 --cov=src --cov-branch --cov-context=test --cov-report=term-missing
```

The controller combines worker shards into `.coverage` before reporting; a
complete run needs no separate combine step. `vfy test --engine=pytest-native` can use
xdist with `--numprocesses=2 --dist=loadfile`; see [worker options](run-tests.md#workers)
for restrictions on combining worker modes. A worker crash or malformed shard
preserves the previous database and report. Failing tests still save valid data.

### Python subprocesses

```console
uv run vfy coverage run --parallel-mode app.py
uv run vfy coverage report
```

Replace `app.py` with your program. Python children inherit collection bootstrap
settings. Spawned and exec'd children save a shard on normal exit; forked children
restart the collector and save their own shard. The report combines discovered
shards. Application `sitecustomize` hooks run once under their usual module name,
before `usercustomize`; CPython handles their exceptions normally. Code imported
by an application startup hook is measured when it matches the collection filters.
Preserve the child's Python environment; do not set vfy's managed
[environment variables](../reference/configuration.md#environment-variables) yourself.

Collection saves on normal exit, including plain `SystemExit`. Set `[run]
sigterm = true` to save before preserving SIGTERM's usual exit status. SIGKILL,
`os._exit`, interpreter crashes, and power loss cannot save pending data.

### Recover interrupted shards

```console
uv run vfy coverage combine --strict --keep
uv run vfy coverage debug data --data-file .coverage.worker
```

`--strict` errors when no shards exist; `--keep` retains inputs. vfy validates
all shards before replacing `.coverage`; failure leaves data and shards unchanged.
Replace `.coverage.worker` with a real shard path. If a worker is missing, check
plug-in loading (add `-p xdist.plugin` when autoload is disabled), child environment,
and `.coverage.*` files before combining. Standalone runs need `--parallel-mode`
to write the parent as a shard.

## Use a Coverage.py plug-in

Install the plug-in in the collection and reporting environment and configure its
module under `[run] plugins`. File tracers, file reporters, configurers, and dynamic
contexts use the [Coverage.py plug-in API](../reference/python-api.md#coverage-plugins).
These differ from pytest plug-ins loaded with `-p`.

Reports reload the tracer named in saved data, so the module must remain
importable. Compare your plug-in's virtual filenames, translated lines and arcs,
unexecuted-file discovery, exclusions, source rendering, contexts, and saved
tracer records against Coverage.py before changing CI. See the
[plug-in checks](../about/compatibility.md#coverage-plugin-evidence) for the
protocol tests and third-party package
reports.

## Collect with stock pytest

Keep your existing pytest runner and measure coverage with vfy:

<!-- doc-test: stock-coverage -->
```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src --cov-branch
```

This saves `.coverage` and prints a terminal report. pytest's `-p` option loads
`vfy.pytest_plugin`; `-p no:pytest_cov` blocks pytest-cov. vfy does not
activate automatically, so plain `pytest --cov` uses pytest-cov when installed.
Other plug-ins, including pytest-html and pytest-metadata, load as usual.
vfy's own HTML/metadata replacements are a separate opt-in:
`-p vfy.pytest_reporting`. See [reporting compatibility](../about/compatibility.md#pytest-html-and-metadata)
before enabling them.

For repeated runs, add the flags to `pyproject.toml`, preserving existing
`addopts`, or set them in `PYTEST_ADDOPTS` for your shell or CI job:

```toml
[tool.pytest.ini_options]
addopts = "-p no:pytest_cov -p vfy.pytest_plugin"
```

pytest reads these flags before installed plug-ins load. See
[compatibility](../about/compatibility.md) for supported versions.

For flags, report destinations, and the `cov` and `no_cover` fixtures, see
[pytest coverage options](../reference/cli.md#pytest-coverage-options).
