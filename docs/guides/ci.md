# CI and quality gates

Run the full pytest suite, fail the job below a coverage threshold, and save
reports for review. vfy replaces pytest-cov; pytest remains the runner.

Both coverage workflows on this page run from the root of the
[coverage example](../getting-started/quickstart.md), after you add its
negative-value test. In that project, run `uv add --dev 'vfy[pytest]'` to install
vfy and pytest, then commit `pyproject.toml`, `uv.lock`, the source, and the tests.
Supported runners install a prebuilt wheel. Copy the full-suite workflow below
into `.github/workflows/coverage.yml`.

To run the same steps on your machine:

```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src --cov-branch --cov-report=
uv run vfy coverage report --show-missing --fail-under 100
```

## Run the full suite

<!-- doc-test: ci-full -->
```yaml
name: coverage
on: [push, pull_request]
permissions:
  contents: read
jobs:
  coverage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
      - name: Install
        run: uv sync --locked
      - name: Collect
        run: uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src --cov-branch --cov-report=
      - name: Export XML
        if: always()
        run: uv run vfy coverage xml -o coverage.xml
      - name: Enforce threshold
        if: always()
        run: uv run vfy coverage report --show-missing --fail-under 100
      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        if: always()
        with:
          name: coverage
          include-hidden-files: true
          if-no-files-found: warn
          path: |
            .coverage
            coverage.xml
```

The collection step runs every test. The later steps write `coverage.xml`,
check the 100% coverage threshold, and upload the data. They run even after
a test failure; a passing coverage gate does not clear that failure.

If collection fails before writing data, XML export and the threshold check
also fail. The upload step warns if no artifact exists.

## Split tests across two jobs

This recipe uses the independent engine. First [check your suite](native-adoption.md)
and compare its outcomes with pytest. Use separate jobs when one runner cannot
finish the suite in time. Each job writes a separate coverage file (a shard);
a final job combines the shards before checking coverage.

<!-- doc-test: ci-shards -->
```yaml
name: coverage
on: [push, pull_request]
permissions:
  contents: read
jobs:
  tests:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - id: bounds
            test-file: tests/test_bounds.py
          - id: formatting
            test-file: tests/test_formatting.py
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
      - run: uv sync --locked
      - name: Collect one test file
        env:
          TEST_FILE: ${{ matrix.test-file }}
          COVERAGE_SHARD: .coverage.${{ matrix.id }}
        run: uv run vfy test --data-file "$COVERAGE_SHARD" --cov=src --cov-branch --cov-report= "$TEST_FILE"
      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        if: always()
        with:
          name: coverage-${{ matrix.id }}
          path: .coverage.*
          include-hidden-files: true
          if-no-files-found: error

  report:
    needs: tests
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
      - run: uv sync --locked
      - uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          pattern: coverage-*
          path: shards
          merge-multiple: true
      - name: Combine
        run: uv run vfy coverage combine --strict --keep shards
      - name: Export XML
        if: always()
        run: uv run vfy coverage xml -o coverage.xml
      - name: Enforce threshold
        if: always()
        run: uv run vfy coverage report --show-missing --fail-under 100
      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        if: always()
        with:
          name: combined-coverage
          include-hidden-files: true
          if-no-files-found: warn
          path: |
            .coverage
            coverage.xml
            shards/.coverage.*
```

Each test-file job writes one `.coverage.*` shard, and the report job combines
them. Neither test job checks the threshold, because a shard holds only part of
the data. Make the test jobs and the report job required checks in branch
protection. A failed test job still fails the workflow.

`--keep` keeps the shards after combining, so you can inspect them. All jobs must
share the same OS, checkout, and source paths. Don't combine line and branch data,
or data from different source revisions. If jobs use different checkout roots or
operating systems, set up [path aliases](../reference/configuration.md#paths)
first.

## Enforce semantic coverage thresholds

After [checking compatibility](native-adoption.md), replace the collection
step with this partial example to require decision, condition, and MC/DC
(modified condition/decision) coverage. See [semantic coverage](semantic.md) for
what each measure checks. The test run fails with status 1 for an unmet pytest
coverage threshold. The separate check exits 2 for an unmet semantic threshold
and 1 for missing or stale data.

```yaml
      - name: Test and measure semantic coverage
        run: >-
          uv run vfy test --all --cov=src --cov-branch --cov-decisions
          --cov-report=term-missing
          --cov-decision-report=json:build/decisions.json
          --cov-fail-under=90 --cov-fail-under-decisions=95
          --cov-fail-under-conditions=90 --cov-fail-under-mcdc=85

      - name: Verify semantic report
        if: always()
        run: >-
          uv run vfy coverage check --format text
          --fail-under-decisions 95
          --fail-under-conditions 90
          --fail-under-mcdc 85
```

`vfy coverage check` can also run in a separate job with the uploaded
`.coverage` file and the same source checkout. It reads decision observations
from `.coverage`; `build/decisions.json` is a report for review. For the model
and exit statuses, see [semantic coverage](semantic.md).

## Schedule mutation testing

Add these steps to a PR or scheduled workflow. Use checkout with
`fetch-depth: 0`, and replace `main` with the PR target branch. Mutation testing
does not edit the checkout; `--fail-on-survivor` fails the PR step with status 2
when a mutation survives.

```yaml
      - name: Fetch the mutation comparison branch
        if: github.event_name == 'pull_request'
        run: git fetch origin main
      - name: Collect a passing mutation baseline
        run: >-
          uv run vfy test --all --source src --cov=src
          --cov-context=test --cov-report=
      - name: Mutate PR changes
        if: github.event_name == 'pull_request'
        run: >-
          uv run vfy mutate --base origin/main --fail-on-survivor
          --format json --output build/mutations.json

      # Run this in a nightly or release-review workflow, not on every edit.
      - name: Mutate the complete source tree
        if: github.event_name == 'schedule'
        run: >-
          uv run vfy mutate --all --source src
          --format json --output build/mutations.json
```

The default native engine runs mutations serially on Linux. To run them in
parallel, install `vfy[pytest]` and add `--engine=pytest-native -j auto --tb=native` to
the `vfy mutate` commands. Add `--engine=pytest-native --tb=native` to the `vfy test`
baseline step as well so both commands use the same traceback settings.

The full-suite run in the same job is the passing baseline. Upload
`build/mutations.json` to review survivors and incomplete results later. For
result statuses and limits, see [Mutation testing](mutation.md).

`--changed` only sees uncommitted edits, so a clean PR checkout needs `--base`.
The baseline still comes from the current checkout.

<a id="use-selection-in-ci"></a>

## Select tests in CI

<a id="ci-selection-options"></a>

Install `vfy[pytest]` for native selection and baseline controls. Use
`vfy test --engine=pytest-native --all` for a complete CI run. Without `--all`, a restored
`.vfy/` cache can select a subset of tests, or reuse saved coverage without
running any tests. Use selection only when you intend to skip unaffected tests.

Transfer a passing baseline between checkouts with `--export-baseline` and
`--baseline`. A baseline is the saved per-test coverage
from a passing full run. Export it from a clean checkout:

```console
uv run vfy test --engine=pytest-native --all --cov=src --cov-context=test --export-baseline baseline.json
```

Download that file into the checkout you're testing:

```console
uv run vfy test --engine=pytest-native --cov=src --cov-context=test --baseline baseline.json --plan-output plan.json
```

Keep the source roots, coverage settings, and dependencies the same. The baseline
must come from an ancestor of the commit under test. A common setup exports it
from each passing run on your main branch, stores it as a CI artifact, and
downloads it in pull request jobs. Don't copy `.vfy/` between jobs.

A valid but incompatible baseline makes the run execute the full suite.
A malformed file fails the job before any test runs or any output is replaced.

`--plan-only` writes the plan without running tests and also works with
`--engine=native` without installed pytest; `--plan-output` saves that plan.
Portable baseline controls and `--audit-selection` require an installed pytest
route. `--audit-selection` runs the
full scope and checks whether the selection missed any failures. For all the
options, see [CI selection controls](../reference/cli.md#ci-selection-options).
For limits that come from outside the code, such as external services, see
[test selection](test-selection.md).

## Keep artifacts when a job fails

The coverage recipes above upload `.coverage`, `coverage.xml`, and any shards
with `if: always()`, so the files survive a failed test step. Give any other
upload step the same condition.

For reproducible performance checks, see the [benchmark guide](../about/benchmarks.md).
