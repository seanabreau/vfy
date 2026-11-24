# Native pytest testing

Use these methods to compare an installed native engine with pinned pytest and
real projects. [Testing vfy](testing.md#test-layers) lists suites and release
requirements; the [CLI reference](../reference/cli.md#vfy-test) lists
supported options and plug-in restrictions.

`vfy test` defaults to independent execution and one worker. Use
`--engine=pytest-native` to exercise the installed-pytest compatibility route.
Internal differential checks use the hidden `--engine=pytest` override.
Default-route tests omit engine and worker overrides and verify independent
execution records.

## Run focused tests

After [setting up the development environment](https://github.com/seanabreau/vfy/blob/main/CONTRIBUTING.md#setup),
run these commands from the repository root:

```console
scripts/cargo-test.sh --locked -p vfy-pytest
cargo test --locked -p vfy-core
.venv/bin/python -m pytest -q \
    tests/integration/test_native_*.py \
    tests/integration/test_watch_native_workers.py \
    tests/integration/test_watch.py::test_native_watch_discards_obsolete_evidence_and_publishes_recovery \
    tests/integration/test_pytest_config_projection.py
```

Rust tests cover engine rules such as report ordering, JUnit identities, and
XML escaping. Python integration tests run temporary projects through
`VfyProject` and compare public behavior with pinned pytest.

On Linux with CPython 3.12, CI is configured to run each pytest version listed
under [release validation](testing.md#release-validation) as a separate job. The
pytest 9.1.0 job first uses the declared minimum pytest 9.1.0 and pluggy 1.5.0,
checking version boundaries, protocol wrappers, warning filters, and warnings
retained after control exceptions. Each version then runs with pluggy 1.6.0, and
the pytest 9.2.0 job also checks pluggy 1.7.0. To reproduce the checks in a
disposable environment with vfy installed, run every version or name one:

```console
bash scripts/check_native_pytest_versions.sh
bash scripts/check_native_pytest_versions.sh 9.2.0
```

## Find a test

Use the [suite map](testing.md#test-layers) to choose a layer. Native public
checks are `tests/integration/test_native_*.py`; watch and mutation suites have
separate entry points. Typed-plan tests live in `vfy-core`, and
`test_native_dynamic_bindings.py` checks bindings and pre-fixture failures
through `VfyProject`. Recorder and corpus-driver tests are under
`tests/repository/`.

Cache checks in `test_native_cache_selection.py` cover `--lf`, `--ff`, `--nf`,
`--sw`, `--sw-skip`, and `--sw-reset`, including stale state, collection errors,
and recovery. Keyboard interrupt preserves prior native cache files.
`--cache-show` and `--cache-clear` are unsupported.

## Required adoption subset

The regular [CI workflow](https://github.com/seanabreau/vfy/blob/main/.github/workflows/ci.yml)
checks pinned iniconfig, sortedcontainers, pluggy, colorama, and Rich projects
on Linux CPython 3.12. See [CI change classification](testing.md#ci-change-classification)
for when this gate is required. It reuses the tier's Linux wheel and build manifest.

`validation/pytest_runner/check_native_adoption_matrix.py --required-subset`
compares stock pytest with explicit `vfy test --engine=pytest-native` using one and two
workers. All five projects require complete ordered case and collection
identities, matching setup/call/teardown outcomes and exceptions, exit statuses,
warnings, diagnostics, and captured output. Route checkouts use equal-length
paths so path widths do not alter rendered output. The checker binds the
candidate to the supplied wheel, build manifest, and current source revision
and tree, and refuses uncommitted source files. Ignored build and environment
directories remain permitted. Colorama covers its pytest-discoverable unittest
cases; the other projects use their pinned pytest suites.

The pinned Rich workload retains all 981 tests and applies the same
[reviewed patch](https://github.com/seanabreau/vfy/blob/main/validation/pytest_runner/patches/rich-deterministic-workload.patch)
to every outcome and coverage checkout:

- The broken-pipe test closes the reader before the demo starts, retains the
  direct demo precheck, and requires producer status 1.
- The auto-refresh test writes background frames to `StringIO` because Console
  capture is thread-local. The refresh thread and update loop run; main-thread
  capture must include `Step 9`.
- The recursive traceback test raises `RecursionError` after 20 `foo` calls.
  Hidden-frame and truncation assertions remain, and captured output compares
  literally, including the hidden-frame count.

Preparation verifies the pinned revision and `git apply --check`. Each route
retains `workload-patch.json` with the patch path and hash and source hashes
before and after preparation.

Warning comparisons remove exactly one trailing formatter newline from native
comparison copies when present; two or more trailing newlines remain literal.
Raw records remain unchanged, and the results retain each applied rule and the
literal differences. Warning order and multiplicity still have to match.

Rich has six reviewed rules for volatile call-phase stdout at revision
`9d8f9a372cc5916fd4781fec207ced7ddac2f08f`. They affect comparison copies only;
raw execution records and transcripts remain unchanged. The allowed spans are:

| Volatile value | Exact test scope |
| --- | --- |
| Render duration, two spans | `test_console.py::test_brokenpipeerror` |
| SWIG object address, one span | `test_inspect.py::test_inspect_swig_edge_case` |
| LRU wrapper address, one span | `test_inspect.py::test_qualname_in_slots` |
| Log clock label or equivalent padding, two spans | `test_logging.py::test_exception` |
| Log formatter timestamp, two spans | `test_logging.py::test_exception` |
| Log clock label, one span | `test_logging.py::test_exception_with_extra_lines` |

The [rules](https://github.com/seanabreau/vfy/blob/main/validation/pytest_runner/adoption_observations.py)
check each span's surrounding text and occurrence count; missing or extra spans
fail the comparison. Every other captured or diagnostic literal remains in the
comparison. These adoption rules do not change the
[upstream corpus's literal accounting contract](pytest-test-suite.md#comparison-scope).

Coverage runs use stock pytest with vfy's plug-in and native execution with
one and two workers. Every route must exit with status 0, report the complete
ordered case inventory, match the stock coverage run's case outcomes, and retain
nonempty schema-7 data and JSON reports.
Pinned Coverage.py 7.15.4 reanalyzes each route's own data and must reproduce its
normalized report. All three routes use vfy's collector: Coverage.py is a
report-analysis oracle, not an independent collection oracle. For the pinned
sortedcontainers workload, the harness adds an autouse fixture that seeds each
of its three random stress tests to 0 and restores the previous random state.
This gives each test the same inputs across worker partitions; the tracked
upstream files remain unchanged. The harness records the fixture hash.

| Project | Coverage compared across routes |
| --- | --- |
| iniconfig, sortedcontainers, colorama | Exact files, lines, arcs, contexts, and normalized JSON reports |
| pluggy | Exact aggregate files, lines, arcs, and normalized JSON reports. Context differences remain in the artifacts: stock pytest itself executes the measured Pluggy framework, while native execution has different framework calls. |
| Rich | Matching source-file inventories and the same-run report check above. Thread scheduling and import or worker execution state can change hits and contexts, which remain in the artifacts. |

This untimed subset does not establish broad plug-in, platform, or performance
compatibility.

CI retains route transcripts, reports, execution records, coverage data, and
failure diagnostics under `target/native-adoption/`, including on failure.
To reproduce from the repository root, supply a release wheel and its build
manifest created at build time, then run:

```console
.venv/bin/python validation/pytest_runner/check_native_adoption_matrix.py \
    --wheel dist/vfy-*.whl \
    --build-manifest dist/adoption-build.json \
    --output target/native-adoption \
    --python-version 3.12 \
    --required-subset
```

## Pytest-mock adoption projects

The adoption matrix also includes two projects that explicitly load
pytest-mock 3.15.1. These optional entries compare complete ordered execution
records through stock pytest and native one- and two-worker routes. Their
reviewed coverage contracts are:

| Project | Pinned revision | Scope and environment | Coverage contract |
| --- | --- | --- | --- |
| Poetry Core | [b52c25c](https://github.com/python-poetry/poetry-core/tree/b52c25c451bbaed9f309d45fc6e6e35c704e55bf) | Complete constraints, JSON, packages, pyproject, SPDX, utilities, VCS, version, core version, and factory unit suites. Build and integration suites are outside this entry. | Aggregate lines, arcs, tracers, and reports must match. Serial schema-7 data was also observed to match exactly. |
| pyproject-api 1.9.1 | [651b632](https://github.com/tox-dev/pyproject-api/tree/651b63254b5558399909cf3e041d5d4c71b6c802) | Full suite; `--cov-config=/dev/null` isolates coverage from upstream covdefaults settings. The report oracle uses the same explicit configuration. | Exact schema-7 data and reports. |

Poetry Core initializes its grammar and marker parser caches lazily in each
worker. Parallel workers can attribute that initialization to different
first-use tests. Its reviewed aggregate-only contract retains those context
differences in the report while requiring identical measured lines, arcs,
tracers, and normalized reports. Every route's report is also checked against
Coverage.py using that route's own database.

Comparison copies normalize known checkout roots in parametrized test IDs and
coverage context labels. Captured output also normalizes the requested pytest
temporary roots, including each native worker's root. Raw execution records and
coverage databases remain unchanged.

Use the wheel and build manifest described above:

```console
.venv/bin/python validation/pytest_runner/check_native_adoption_matrix.py \
    --wheel dist/vfy-*.whl \
    --build-manifest dist/adoption-build.json \
    --output target/native-adoption/mock-projects \
    --python-version 3.12 \
    --project poetry-core --project pyproject-api
```

Platformdirs and Requests also request pytest-mock in the matrix, but require
other runtime plug-ins. Supporting mock fixtures does not establish adoption
parity for those suites.

Poetry Core's builder tests share files in their source fixtures. Run its full
configured suite with one worker; the automated entry above excludes those
builders so its two-worker comparison does not race over the shared files.

## Pytest-xdist adoption projects

The optional xdist matrix requests the same pytest-xdist 3.8.0 worker counts
(0 and 2) and `--dist=loadfile` on both routes. Stock pytest uses `-n`;
native `vfy test` uses pytest-xdist's `--numprocesses`. On the vfy CLI,
`-n` selects vfy's own file-group workers.
The pinned workloads are Requests-cache 1.3.3's complete upstream deployment
unit phase, Black's official `no_jupyter` phase, and Chardet 7.6.0's complete
configured suite with its pinned external test-data corpus.

The checker compares JUnit identities, outcomes, diagnostic bodies, properties,
and captures. Coverage phases compare schema-7 data and normalized reports and
check each route's report against Coverage.py 7.15.4. Raw warnings and logs
are retained; this checker does not compare the corpus gate's full warning and
phase records. Native routes also require Rust engine records with complete
test phases and matching worker execution counts. A configured workload is not
a claim that the current wheel passed it.

```console
.venv/bin/python validation/pytest_runner/check_native_adoption_matrix.py \
    --wheel dist/vfy-*.whl \
    --build-manifest dist/adoption-build.json \
    --output target/native-adoption/xdist \
    --python-version 3.12 \
    --xdist-project requests-cache --xdist-project black \
    --xdist-workers 0 --xdist-workers 2
```

Chardet includes an unmarked timing assertion that failed in an instrumented
stock baseline. Use `--xdist-project chardet --xdist-phase outcomes` to compare its complete
test outcomes separately; retain any coverage-phase failure as distinct evidence.
Selecting `--xdist-phase coverage` also runs the outcomes baseline and requires
coverage to preserve its test inventory and outcomes on each route.
This optional matrix does not change the required five-project CI subset.

## Installed-wheel CI

The [native pytest workflow](https://github.com/seanabreau/vfy/blob/main/.github/workflows/native-pytest.yml)
is configured for Linux and macOS with Python 3.12–3.15. It installs matching artifacts
in separate reference and candidate environments, runs the integration suites
and focused differential checkers, then compares the
[pinned upstream corpus](pytest-test-suite.md).

The installed-wheel lanes discover every `tests/integration/test_native_*.py`
suite, including new fixture, reporting, worker, and version regressions.
Watch and mutation suites run separately through their explicit entry points.

The workflow also runs a pinned Rich project check with the candidate vfy
wheel. It executes the 981-case suite through pytest with vfy's pytest
plug-in, then through native `vfy test` with one and two workers. Every route
uses vfy's coverage collector. The checker compares ordered JUnit case
identities and outcomes across routes, then reanalyzes each route's own schema-7
data with pinned Coverage.py 7.15.4 and compares complete normalized per-file
coverage records and totals. Coverage.py is the report-analysis oracle, not an
independent collector: the check does not claim collection parity or require
threaded runs to observe identical branch hits. It does not compare diagnostic
transcripts or measure performance.

The check then applies a reviewed source-breaking edit in the disposable native
checkout. Native execution must report a real call-phase test failure and exit
with status 1. The checker restores the source, reruns the affected tests, and
requires status 0 and a clean checkout. This verifies the failure-and-repair
sequence through the installed native command. Each edit stage gets an isolated
Python bytecode cache so same-size, same-second edits cannot reuse stale `.pyc`
files. The check does not test import-cache invalidation.

### Reproduce the Rich project check

From the repository root, prepare a candidate wheel at
`target/native-pytest/dist/` and use the locked Rich dependencies:

```console
uv venv --python 3.12 target/native-pytest/rich-environment
uv pip sync --python target/native-pytest/rich-environment/bin/python \
    --require-hashes benchmarks/locks/rich.txt
uv pip install --no-deps \
    --python target/native-pytest/rich-environment/bin/python \
    target/native-pytest/dist/vfy-*.whl
uv run --no-sync python -c \
    'from pathlib import Path; from benchmarks.prepare_projects import checkout_project; from benchmarks.project_manifest import manifest; checkout_project(manifest("rich"), Path("target/native-pytest/upstream-rich"))'
uv run --no-sync python validation/pytest_runner/check_native_project.py \
    --upstream-root target/native-pytest/upstream-rich \
    --python target/native-pytest/rich-environment/bin/python \
    --vfy-wheel target/native-pytest/dist/vfy-*.whl \
    --output target/native-pytest/results/native-rich
```

The checker verifies that the installed package matches the supplied wheel and
that the project checkout matches the pinned revision. It writes route records
and normalized reports under `target/native-pytest/results/native-rich/`; the
source edit is confined to a fresh checkout and restored before success. CI
runs this check in eight installed-wheel lanes: Ubuntu and macOS with Python
3.12, 3.13, 3.14, and 3.15.

`tests/repository/test_native_project.py` tests the checker itself. Other
checkers in `validation/pytest_runner/` cover failure limits, warnings,
plug-in restrictions, reports, and installation. CI retains their results.
The separate ecosystem workflow compares coverage engines through stock pytest;
this Rich check applies only to the pinned suite and routes above.
