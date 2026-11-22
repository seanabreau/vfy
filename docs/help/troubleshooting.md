# Troubleshooting

Start with `uv run vfy diagnostics`. It shows the runtime, configuration, and
default native engine; add `--json` for a bug report. Use `--engine=pytest-native`
to inspect the optional installed-pytest route. Run the commands below from your
project root.

```console
uv run vfy diagnostics
uv run vfy diagnostics --json
```

## vfy rejects a test option or plug-in

Choose the collection command that matches the failing engine and test scope:

```console
uv run vfy diagnostics --collect -- tests
uv run vfy diagnostics --engine=pytest-native --collect -- tests
```

The first command checks the default native engine; the second checks the
optional native route, which requires `vfy[pytest]`. Collection imports test
modules and runs collection hooks, but not test bodies. Compare the collected
count with pytest, then run the full suite to check fixtures and outcomes.

If vfy reports an unrecognized option such as `--testmon`, check `addopts` in
`pytest.ini`, `pyproject.toml`, or `tox.ini`, and `PYTEST_ADDOPTS`. Remove
pytest-testmon options from vfy's invocation; they do not configure vfy's
[affected-test selection](../guides/test-selection.md). Keep them on a separate
pytest command if that workflow still uses pytest-testmon. A clean Git commit
and a passing `vfy test --all` create vfy's selection baseline.

An omitted plug-in can leave a requested fixture undefined, even when the
package is installed. Native `vfy diagnostics --engine=pytest-native --collect` names
missing fixtures and counts the tests requesting them. Independent collection
can succeed before fixture setup fails, so also run the tests. For example,
tests using pytest-aiohttp's `aiohttp_client` fixture need pytest execution if
vfy does not admit that plug-in. Keep the plug-in and run
`uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src tests` to collect
vfy coverage while pytest runs its hooks. Replace `src` and `tests` with your
paths. [Suite adoption](../guides/native-adoption.md#check-your-suite) explains
how to compare results before changing CI.

If the native route rejects a custom `pytest.Item` or a
`pytest_runtest_call` hook in `conftest.py`, try the independent engine with the
same dependencies. It supports public custom items and call hooks; compare its
collection and outcomes with pytest before adopting it. AnyIO tests and plug-ins
that need pytest internals stay on pytest with vfy coverage. Narrowing vfy's
scope with `--ignore` omits those tests from the run and disables affected-test
selection for that invocation; it does not validate the whole suite.

## vfy ran the whole suite when I expected a few tests

Independent selection needs an unfiltered `safe` coverage run and a passing
full run on a clean Git checkout. Explicit selectors, watch, other policies,
and `--no-cov` run the full requested scope. vfy also runs the full
requested scope when it cannot justify a subset and prints `Note: <reason>`.
Check that reason before rebuilding the baseline. See [full-run reasons](../guides/test-selection.md#when-vfy-runs-the-full-suite).

To establish a baseline, commit your changes and run the full suite with the
same coverage and worker options you will use for selected runs. For example:

```console
uv run vfy test --engine=native --all --cov=src   # on a clean Git checkout
```

## A selected run fails

Read the failing test output and rerun that test on its own, or run everything.
A failed selected run preserves the passing baseline but writes fresh partial
coverage; reports from that failure do not include reused coverage for skipped
tests. A passing selected run merges fresh and saved coverage.

```console
uv run vfy test --engine=native --cov=src tests/test_bounds.py::test_positive_value
uv run vfy test --engine=native --all --cov=src
```

## Check the installed build

Print the package version, build profile, and parser backend:

```console
uv run vfy --version
uv run python -c "from vfy.native import build_profile, parser_backend; print(build_profile(), parser_backend())"
```

Use a release build for performance measurements; a debug build is suitable
for development. Include this output when reporting a build or import problem.

<a id="coverage-tool-id-is-already-in-use"></a>

## `sys.monitoring coverage tool id N is already in use`

Another collector already holds CPython's coverage tool ID, often Coverage.py,
SlipCover, or an auto-loaded pytest-cov. Run only one collector. Under pytest, turn
pytest-cov off:

```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src
```

With the Python API, pair every `start()` with `stop()`, preferably in `try/finally`
or `with coverage.collect()`.

## No data file

Check the data path first: the run may have written elsewhere or left shards.
Check whether `COVERAGE_FILE` points somewhere else and whether the effective
`data_file` is the one you expect. Confirm that the measured interpreter exited
normally, then inspect the data:

```console
uv run vfy coverage debug data --data-file .coverage
uv run vfy coverage combine --strict --keep
```

Run `combine` only for parallel `.coverage.*` shards.

## A source file is missing

vfy reports a file only if it was imported or is under `source`. Check the
resolved settings and where each one came from:

```console
uv run vfy config show
```

To discover unimported files, set `source = ["src"]` under
`[tool.coverage.run]` in `pyproject.toml`. Check whether `include` or `omit`
filters exclude the file; see [file selection](../guides/collection.md#choose-files-and-collection-mode). For virtual sources,
register the same file-tracer plug-in during reporting that was used during
collection.

## Totals differ from Coverage.py

A report table can't show where the engines differ. Keep separate data files and
compare them in this order: `measured_files()`, then `lines()` or `arcs()` for one
differing file, then `analysis2()` statements and exclusions, then `branch_stats()`,
then context filters, then the report-format summaries.

```console
uv run vfy coverage debug data --data-file .coverage
```

## Cannot combine files

The shards don't match. Common causes: one shard has line data and another branch
data, a shard uses a schema other than 7, two plug-ins claim different tracers for
one file, the output path was also an input, or `--strict` found no files. Use
`--keep` while you investigate so the input shards stay available.

```console
uv run vfy coverage combine --keep
```

## Invalid configuration

Run validation to locate the invalid setting, then correct its name, type, or
value using the [configuration reference](../reference/configuration.md).
Use `uv run vfy config show` if the error refers to an unexpected file.

```console
uv run vfy config validate
```

## `vfy coverage check` reports no semantic data

Check that the command reads the data file from your decision-enabled run.
Line or branch data cannot gain semantic observations during reporting.
Recollect with `--cov-decisions` for tests or `vfy coverage run --decisions`
for a script or module, then check. Pass `--data-file PATH` if collection used
another path.

```console
uv run vfy test --all --cov=src --cov-decisions --cov-report=term-missing
uv run vfy coverage check --data-file .coverage
```

## `vfy coverage check` says the source is stale

A measured source file changed after collection. The check compares saved
source digests with the current files, so even an edit outside a Boolean
expression can make the data stale. Recollect from the current checkout, then
check. Data from different source digests cannot be combined.

```console
uv run vfy test --all --cov=src --cov-decisions
uv run vfy coverage check
```

## A semantic threshold fails even though line coverage is high

Write the semantic report to JSON and inspect the missing outcomes and witness
pairs. Line, decision, condition, and MC/DC thresholds are independent; high
line coverage does not satisfy the other gates. Add tests for the missing
cases, then recollect. See [semantic coverage](../guides/semantic.md) for
interpreting the report:

```console
uv run vfy coverage check --format json --output build/decisions.json
```

## Semantic mode reports a plug-in error

Decision mode rejects Coverage.py file-tracer plug-ins, because their virtual source
can't map to CPython decision bytecode. Collect line and branch coverage for that
plug-in instead: remove `--cov-decisions` and any configured `run:decisions = true`
([compatibility contract](../about/compatibility.md#semantic-decision-coverage)).

```console
uv run vfy test --all --cov=src
```

## A downstream service rejects XML, JSON, or LCOV

Keep the rejected file and the service's error message. Compare the fields your
service requires with
[Compatibility](../about/compatibility.md), and regenerate the XML at a fixed path
to inspect it.

```console
uv run vfy coverage xml -o build/coverage.xml
```
