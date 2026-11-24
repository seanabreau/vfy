# Benchmarks

Run the committed harnesses from the repository root to measure coverage,
reporting, selection, or mutation. Each workload checks its output before timing;
a parity mismatch stops the run. Results apply to the named workload, stage,
build, and machine, and vary across operating systems, filesystems, and hardware.

## Getting started

From the repository root, install the locked development and benchmark
dependencies, build the release extension, and list workloads:

```console
uv sync --locked --group dev --group benchmark
uv run maturin develop --release --skip-install
uv run python -m benchmarks list
```

Validate a report workload, then collect samples under `target/benchmarks/`:

```console
uv run python -m benchmarks validate report --report-formats terminal json
uv run python -m benchmarks run report \
  --files 500 --shards 16 --warmups 3 --runs 15 \
  --report-formats terminal json --output target/benchmarks/report.json
```

## Sampling and retained records

Use release builds, three warmups, and fifteen measured runs per tool (thirty
for mutation). Rotate tool order and report medians, including slower results.
Retain exact revisions, artifact hashes, resolved dependencies, machine details,
parity checks, and every raw sample. Diagnostic records retain their actual counts.

Keep generated artifacts outside the source tree; publish a compact index with
archive links and checksums. Compare the same workload and stage. See
[measurement requirements](../docs/about/benchmarks.md#how-to-read-benchmark-evidence)
and [publication guidance](#publish-a-result).

Build a compact index and paired confidence intervals from completed
resource-sampled mutation results with the recorded benchmark interpreter:

```sh
env -u PYTHONHOME /path/to/benchmark-environment/bin/python \
  scripts/summarize_competitor_benchmarks.py /path/to/evidence/generated.json \
  --date YYYY-MM-DD --output /path/to/evidence/index.json
env -u PYTHONHOME /path/to/benchmark-environment/bin/python \
  scripts/analyze_competitor_benchmark_pairs.py /path/to/evidence/index.json \
  --output /path/to/evidence/index-paired-analysis.json
```

The analysis uses 10,000 paired bootstrap resamples with seed zero and reports
the difference of medians, candidate minus reference, with a 95% percentile
interval. Samples must be matching rounds of the same experiment; ordinal
positions in separate cohorts do not establish pairing. An interval spanning
zero is inconclusive. Warm or cached vfy against cold mutmut remains an unequal
cache comparison even when its interval excludes zero. These intervals
describe observed variability on the recorded machine.

### Measure selection and mutation memory

On Linux, add `--tree-resources` to `edit-loop`, `real-selection`, `mutation`,
or `real-mutation` to retain CPU counters and sampled peak process-tree PSS
for each measured public command. The collector samples the command and its
live descendants at a 20 ms target interval, excluding the collector and
benchmark controller. Each record retains the actual maximum sampling gap,
command, exit status, streams, and collector hash. Brief peaks between samples
can be missed. Shared resident pages count proportionally.

Timing from these runs includes the sampler's CPU cost. Keep it separate from
timing collected without the sampler. Selection samples cover the affected
rerun; generated edit loops also retain cold, unchanged, and failure/repair
commands. Mutation memory covers the public mutation command and its workers.
Real-mutation workflow time additionally includes catalog mapping, test
selection, and result parsing outside that command. The result records both
boundaries. All existing catalog, coverage, and outcome checks remain active.

Use three warmups and fifteen measured runs for selection, and thirty measured
runs for mutation. The pinned-project and generated mutation recipes below
provide the remaining arguments; use a separate result and artifact directory
for each workload and measurement method.

### Compare another mutmut adopter

`benchmark_adopter_mutation` reads the project's original mutmut source filters
and pytest arguments. It requires an unchanged checkout, a resolved hash lock,
and vfy and mutmut installed in the same prepared environment. Set
`VFY_BENCHMARK_BUILD_MANIFEST` to the release wheel's build manifest and run from
the vfy checkout:

```sh
"$BENCH_PYTHON" -m benchmarks.benchmark_adopter_mutation \
  --root "$PACKAGE" --revision "$PACKAGE_REVISION" \
  --expected-tests "$TEST_COUNT" \
  --mutmut "$MUTMUT" --mutmut-repository "$MUTMUT_CHECKOUT" \
  --lock "$REQUIREMENTS_LOCK" --output "$RESULT_DIRECTORY"
```

The runner compares ordered baseline phases and JUnit outcomes, then checks
the entire exact common mutation catalog and independently verifies detecting
tests. Incomplete or different outcomes stop sampling. `--validate-only` runs
these checks without timing; the default measurement uses four concurrent
mutations, serial test isolation, three warmups, and thirty alternating pairs
with process-tree memory sampling. Timers cover the public mutation commands,
including startup and baseline collection; copying, catalog mapping, and result
validation stay outside the timers.

### Compare native workers with pytest-xdist

Use `validation/pytest_runner/benchmark_native_xdist.py` with an identified
release wheel and build manifest. The complete configured inventories are
Rich (981 cases), attrs (1,412), Click (2,016), and Packaging (62,435). Prepare
the pinned checkouts and resolved dependencies from
`check_native_adoption_matrix.py`. Keep complete Git history and tags for
projects whose installed version comes from Git, including attrs.

Run each invocation while builds, tests, and profiling are idle. First use
`--validate-only` for each project with a separate output directory. Then
measure each project with the same four workers, disabled coverage and capture,
and JUnit reporting:

```sh
"$VFY_BENCH_PYTHON" validation/pytest_runner/benchmark_native_xdist.py \
  --wheel "$VFY_BENCH_WHEEL" --build-manifest "$VFY_BENCH_MANIFEST" \
  --project rich --workers 0 4 --native-workers 4 \
  --timing-route stock-xdist-4 --timing-route native-xdist-4 \
  --timing-route native-j4 --rich-clock --capture=no \
  --override-upstream-capture --python-version 3.12 --tree-resources \
  --work-root "$VFY_BENCH_WORK/rich" --output "$VFY_BENCH_RESULTS/memory/rich" \
  --local-checkout "rich=$VFY_BENCH_UPSTREAM/rich" \
  --warmups 3 --runs 15 --mode publication
```

Repeat for `attrs`, `click`, and `packaging`, using each pinned local checkout.
The harness restores the identical prepared snapshot before every child, with
cold source bytecode and pytest caches and warmed environment and OS caches.
It rotates stock xdist, native xdist bridge, and native file-scheduler order
within each measured round. Run Rich again without `--tree-resources`, writing
to `"$VFY_BENCH_RESULTS/timing-only/rich"`; keep its timing samples separate.

### Compare independent execution with pytest

Use the same release wheel, build manifest, prepared locks, and complete project
inventories with explicit `independent-jN` routes. This route uses vfy's own
pytest-compatible frontend and executor, which is also the default for `vfy test`
regardless of whether pytest is installed.
The harness selects `--engine=native` explicitly for these routes. For serial execution:

```sh
"$VFY_BENCH_PYTHON" validation/pytest_runner/benchmark_native_xdist.py \
  --wheel "$VFY_BENCH_WHEEL" --build-manifest "$VFY_BENCH_MANIFEST" \
  --project rich --workers 0 --independent-workers 1 \
  --timing-route stock-xdist-0 --timing-route independent-j1 \
  --rich-clock --capture=no --override-upstream-capture --python-version 3.12 \
  --work-root "$VFY_BENCH_WORK/independent" \
  --output "$VFY_BENCH_RESULTS/independent-preflight" --validate-only
```

After validation, repeat without `--validate-only`, with a new output directory
and `--warmups 3 --runs 15 --mode publication`. Repeat for `attrs`, `click`, and
`packaging`. For equal four-worker commands, use `--workers 4
--independent-workers 4`, `--timing-route stock-xdist-4`, and
`--timing-route independent-j4`. Keep serial and parallel comparisons separate.

The harness's optional `--native-scheduling=file|scope|cases|striped|demand` selects the
shared worker scheduler for both native and independent routes. Omission leaves
the scheduling configuration unchanged. Keep results for different policies
separate; `cases` and `striped` can replicate broad fixtures across workers.

`--timed-evidence=full` is the default. Each timed vfy command writes history to
an explicit destination and writes independent result JSON. After timing, the
harness checks that JSON's engine, phases, warnings and inventory against
preflight. `--timed-evidence=junit` omits explicit history and
result-JSON flags, retaining normal vfy controller and default history behavior.
It checks every timed command's exit, counts and complete JUnit output; it does
not validate each sample's phases or warnings.

Both modes first require complete stock/independent preflight parity: phase
records, exception diagnostics, captures, case/session warnings, inventory and
JUnit. Multiple-worker comparisons disregard case completion order while
preserving phase order and duplicate identities. Click and attrs xfails use
their reviewed address normalizations; raw diagnostics and applied rules remain
in the record.

Run each mode into separate work and output directories. Retain both cohorts,
commands, mode-specific gates, medians and paired 95% intervals, including
slower results. Sequential
cohorts do not support subtracting medians to isolate JSON overhead. A blocked
project returns a nonzero harness status. Coverage is disabled in both modes.

## Workloads

| Workload | Measures | Reference |
| --- | --- | --- |
| `report`, `sysmon-events` | Coverage stages and CPython monitoring costs | [Python harnesses](#python-benchmark-harnesses) |
| `selection-persistence`, `rust-boundary` | Schema-7 I/O and report boundaries | [Python harnesses](#python-benchmark-harnesses) |
| `evidence` | Compact Rust evidence operations | [Rust diagnostics](#rust-engine-diagnostics) |
| `edit-loop` | Affected-test selection after source edits | [Selection methods](../docs/about/benchmarks.md#how-to-read-benchmark-evidence) |
| `ci-selection` | PR selection and baseline loading | [Python harnesses](#python-benchmark-harnesses) |
| `asyncio`, `pytest-html` | Async execution and HTML report adapters | [Python harnesses](#python-benchmark-harnesses) |
| `projects`, `native-coverage`, `static-report`, `workflows` | Real-project coverage, prepared reports, and edit workflows | [Project protocol](#real-project-coverage-protocol) |
| `real-selection` | Real-project affected-test execution | [Selection results](../docs/about/benchmarks.md#how-to-read-benchmark-evidence) |
| `mutation`, `real-mutation` | Generated and pinned-project mutation workflows | [Mutation protocol](#mutation-benchmark) |
| Installed-wheel native execution | Complete commands, generated suites, and small project selections | [Native benchmark recipes](native-pytest.md) |

### Report recipe without shards

The following retained recipe uses the report workload's default shard count:

```console
uv run python -m benchmarks validate report --report-formats terminal json
uv run python -m benchmarks run report \
  --files 500 --warmups 3 --runs 15 --report-formats terminal json \
  --output target/benchmarks/report.json
```

## Python benchmark harnesses

Use `uv run python -m benchmarks run WORKLOAD --help` for workload options;
replace `WORKLOAD` with a name from the table. `validate` runs supported parity
checks without timing, and `prepare` builds inputs for workloads that support it.

`report` preflight compares pytest coverage JSON across tools and checks that
their terminal reports are present. Each pytest warmup and measured run checks
the saved pytest-cov and vfy databases against a frozen, mode-specific
Coverage.py reference without recollecting coverage. Each tool's terminal rows
must match its own preflight; terminal presentation parity across tools is not
claimed. SlipCover's measured check covers terminal rows only, not exact saved
line or branch identities. `validate report` runs both JSON and terminal preflight
without collecting timing samples.

`rust-boundary` offers `--source-profile skewed` and `--partial-cache-probe`
for uneven source sizes and single-source cache invalidation. The edit-loop,
CI-selection, and HTML CLI workloads use native execution; direct pytest is
the outcome and coverage reference.

`ci-selection` times selected vfy, full vfy (`--all`), and full pytest-cov,
excluding CI artifact transfer. Its process RSS probe excludes pytest workers.

## Rust engine diagnostics

Criterion targets measure focused engine operations over prepared data:

```console
cargo bench --locked -p vfy-core --bench engine
cargo bench --locked -p vfy-core --bench evidence
cargo build --locked --profile profiling -p vfy-core
```

For the retained semantic MC/DC workloads:

```console
cargo bench --locked -p vfy-core --bench engine -- \
  semantic_mcdc_proof --sample-size 15 --warm-up-time 3 --measurement-time 10
```

Filter `engine` with `coverage_data_path_queries`, `line_persistence`,
`analysis_cache_report`, `semantic_mcdc_proof`, or `native_`. These measure
individual data, analysis, proof, or capture stages, excluding Python startup.

Compare exact revisions on the same machine with isolated targets:

```console
git worktree add --detach /tmp/vfy-benchmark-base REVISION
CARGO_TARGET_DIR=target/benchmarks/base cargo bench --locked \
  --manifest-path /tmp/vfy-benchmark-base/Cargo.toml \
  -p vfy-core --bench engine -- --noplot --save-baseline=REVISION
CARGO_TARGET_DIR=target/benchmarks/candidate cargo bench --locked \
  -p vfy-core --bench engine -- --noplot --save-baseline=CANDIDATE
```

`REVISION` and `CANDIDATE` are placeholders. Retain each executable hash,
`sample.json`, and `estimates.json`; report `median.point_estimate`. Use distinct
baseline names: Criterion overwrites its default result directories. Shared
build targets can retain another checkout's dependencies.

Criterion CI results are advisory. The optional CodSpeed job uses the same
benchmark bodies and requires the owner to enable `CODSPEED_ENABLED`.

### Native serial pytest

Use the [installed-wheel recipes](native-pytest.md) for plain pytest/native,
stock/native coverage, or baseline/candidate comparisons. They require clean
committed checkouts, installed release wheels, and build manifests. Generated
capture cases write 64 KiB per test.

### Real-project small-suite comparisons

Use the [small-suite recipe](native-pytest.md#small-project-selections) for pinned
projects, restored history, and one- or ten-test selections. It measures complete
commands with warm caches and does not measure RSS.

<a id="real-project-coverage-protocol"></a>

## Real-project coverage protocol

The default `projects` route uses stock pytest and changes the coverage plug-in;
`workflows` uses vfy's internal stock executor for an edit, failure, and repair
sequence on Rich or Datasette. These routes measure coverage under stock pytest.
The opt-in `projects --vfy-engine native` route measures the default vfy test
engine; see [serial coverage](#compare-serial-coverage).
`static-report` runs no tests and uses a prepared schema-7 database.
Rich exercises terminal/XML reports; FastAPI exercises xdist, dynamic contexts,
application imports, and aggregation; Requests spends relatively little time on
coverage; Black provides a branch-enabled, CPU-heavy xdist workflow.

The default coverage comparison changes only the coverage plug-in:

```text
-p pytest_cov.plugin  →  -p vfy.pytest_plugin
```

vfy registers no automatic `pytest_cov` entry point, so ordinary `pytest` discovery does not change. vfy child runs and benchmark commands load `vfy.pytest_plugin` explicitly.

Each project keeps its test paths, xdist settings, coverage source arguments, branch mode, contexts, report formats, and coverage configuration. Pytest plug-in autoloading is disabled, and project-required plug-ins are named explicitly. The harness rotates command order on every measured round.

The manifest lists test-generated files that must start in the same state for each tool, such as FastAPI's `log.txt`. The harness restores their original contents or absence before each validation run, warmup, and measured sequence, and again on exit. This preparation is outside the timer. Other checkout files are left intact.

### Parity checks

Before timing, each plug-in writes a JSON report. The harness normalizes project-relative paths and stops at the first difference in:

- measured file identities;
- executed, missing, or excluded lines;
- executed or missing branch edges; or
- pytest exit status.

The gate also compares exact schema-7 data and `(nodeid, phase, outcome, xfail)`
records for setup, call, and teardown. When the manifest requests XML, it compares
the configured XML output before timing. Terminal presentation is outside the
parity check.

The harness checks collection and its report before it reads timings. For the static stage, it checks the prepared database and source signatures before timing source analysis and JSON reporting.

The differential tests cover cases the synthetic fixture does not: unrelated `tox.ini` discovery, alternate-suite exclusions, `try`/`finally` destinations, function-local annotations at branch entries, and CPython cleanup arcs for `with` statements.

### Prepare a project

After [dependency setup](#getting-started), use a clean, committed vfy checkout. Prepare the pinned Rich checkout, isolated environment, installed release wheel, and artifact manifest:

```console
uv run --no-sync python -m benchmarks prepare projects rich /tmp/vfy-projects/rich \
    --python python3.12
```

Then validate and measure a diagnostic coverage-engine comparison:

```console
uv run --no-sync python -m benchmarks run projects \
    --project rich \
    --root /tmp/vfy-projects/rich/project \
    --python /tmp/vfy-projects/rich/environment/bin/python \
    --preparation-manifest /tmp/vfy-projects/rich/artifacts/manifest.json \
    --artifacts /tmp/rich-engine \
    --mode diagnostic --warmups 1 --runs 5 \
    --output /tmp/rich-engine.json
```

The harness prints the current tool and sample number to stderr, outside the timer. Publication runs need at least three warmups and fifteen samples. The smaller counts above are diagnostic only. The [project manifest](project_manifest.py) lists the accepted preparation keys and pinned upstream revisions. To use another project, change the project key and the preparation root together.

- `--validate-only` runs the comparison without timing.
- `--tests` selects a subset for diagnosis. Label those results as a subset of the project suite.
- `--pytest-args` applies extra options, such as exclusions, to both tools identically. Keep them in the result and its description.

For `workflows`, prepare Rich or Datasette the same way. Then run `benchmarks validate workflows` with the prepared project root, interpreter, manifest, and a separate artifact directory. The controller checks the edit, failure, and repair outcomes without collecting timings. Run `benchmarks run workflows` with the same arguments to measure the sequence.
For Rich, both executors use the reviewed deterministic test fixtures. Each sequence retains their input hashes and derived baseline commit in `rich-test-adaptations.json`.

Use `--phase serial` to run a project's complete suite without xdist. Choose it when test execution depends on which worker imported a module first. FastAPI's debugging tests have this import-state condition. The serial phase records `-n 0` and compares both coverage tools under the same test order. The result records the phase separately from the manifest's default parallel workflow.

The JSON record includes the selected project's exact revision.

### Compare serial coverage

The `native-coverage` benchmark is an additional, explicit workflow comparison:
Coverage.py CTracer and vfy both execute tests through the independent frontend.
Benchmark commands set `COVERAGE_CORE=ctrace`, so the reference keeps the C
tracer on Python 3.14, where Coverage.py otherwise defaults to `sysmon`.
It does not replace the stock pytest-cov workflow below. Packaging's stock
workflow remains blocked because pytest imports `packaging.version` before
pytest-cov starts; upstream instead starts Coverage.py before pytest. Keep that
blocked result separate from this same-frontend comparison.

```console
python -m benchmarks run native-coverage \
  --project packaging --root PREP/project --python PREP/environment/bin/python \
  --preparation-manifest PREP/artifacts/manifest.json \
  --build-manifest ORIGINAL-RELEASE-BUILD.json \
  --artifacts FRESH-ARTIFACTS --output FRESH-RESULT.json \
  --mode diagnostic --warmups 1 --runs 5 --compress-samples \
  --timing-route coverage.py-independent --timing-route vfy-independent
```

All three routes, including the plain independent control, pass complete
preflight. Every timed run repeats phase outcomes and JUnit checks; coverage
routes also repeat exact schema-7 contexts, lines, arcs, tracers and JSON/XML
report checks. The reference preserves the
upstream coverage threshold. These are complete CLI workflows with different
controller paths, so their difference is not isolated collector overhead.
Warnings are not compared by this benchmark. Raw evidence and compression
receipts remain outside the checkout; compression occurs outside the timer.
Add `--coverage-subprocess` only for a declared workload that measures child
processes. It derives one shared TOML configuration for both coverage routes,
retains the original settings and thresholds, and records both configuration
hashes in methodology and provenance. The plain control keeps coverage disabled.
Observer modules are copied byte for byte into a separate artifact directory
under `vfy_benchmark_plugins`; source and staged hashes are recorded. This keeps
upstream packages named `benchmarks` importable and avoids adding an
`__init__.py` at the observer import root that could affect coverage aliases.

The opt-in `--vfy-engine native` route compares complete owned-engine vfy
coverage commands with stock pytest plus pytest-cov and a plain pytest control.
It accepts pinned serial projects without an xdist plug-in, including attrs,
Packaging, and Platformdirs. Each run requires `--phase serial`, explicit
`--context test`, retained artifacts and an original release build manifest,
and must pass the parity gate before timing. The default remains
the stock-pytest vfy coverage-plugin comparison; historical native results keep
their original engine identities.

```console
python -m benchmarks run projects \
  --project attrs --phase serial --vfy-engine native \
  --root PREP/project --python PREP/environment/bin/python \
  --preparation-manifest PREP/artifacts/manifest.json \
  --build-manifest ORIGINAL-RELEASE-BUILD.json \
  --context test --report term --report json:coverage.json \
  --cov-config "$PWD/benchmarks/configs/project-coverage.coveragerc" \
  --artifacts FRESH-ARTIFACTS --output FRESH-RESULT.json \
  --mode publication --warmups 3 --runs 15
```

Prepare one environment using the selected project's committed resolved lock and the exact
retained release wheel; preserve the original build manifest unchanged.
`prepare projects` builds a new wheel, so it does not reuse a retained release.
The result binds the runtime revision separately from the controller revision.
Do not substitute a diagnostic profiling wheel.

Full preflight requires actual independent-engine result evidence, exact
JUnit records and `(nodeid, phase, outcome, xfail)` records, JSON/XML reports when requested, and schema-7
coverage including per-test contexts. Every warmup and measured command repeats
the phase, JUnit, report and coverage gates; the extra independent result JSON
is written only during preflight and its hash is checked before each timed
command. Each attempt retains separate raw streams and outputs. A mismatch
blocks timing or publication. Click's historical context mismatch remains
excluded; do not remove context attribution to admit it. Differences from plain
pytest include engine and controller costs, not only coverage collection.

Use one route for every row in a cross-project coverage chart: serial stock
pytest with pytest-cov or vfy's coverage plug-in, with the same test
selection, context mode, and reports for both tools in each project. This
measures the test and coverage workflow, including process startup and report
writing. Native `vfy test`, pytest-xdist, selection, and mutation have
different boundaries and need separate comparisons.

From the clean benchmark controller, run the same command shape for each
prepared project. Replace `KEY`, `PREP`, `ARTIFACTS`, and `RESULT`; set
`VFY_BENCHMARK_BUILD_MANIFEST` to that project's release-wheel build
manifest. The environment and project checkout come from `benchmarks prepare
projects KEY`:

```console
VFY_BENCHMARK_BUILD_MANIFEST=PREP/KEY/vfy-build.json \
  python -m benchmarks run projects \
    --project KEY --phase serial \
    --cov-config "$PWD/benchmarks/configs/project-coverage.coveragerc" \
    --context test \
    --report term --report json:coverage.json \
    --root PREP/KEY/project \
    --python PREP/KEY/environment/bin/python \
    --preparation-manifest PREP/KEY/artifacts/manifest.json \
    --artifacts ARTIFACTS --mode diagnostic --warmups 0 --runs 2 \
    --output RESULT
```


The shared config enables Coverage.py's subprocess patch for suites that start
Python children. The manifest and explicit command flags determine the source
and reports. The harness passes those choices identically to both coverage
plug-ins and retains the exact command and scope with the raw samples. This
matrix requests terminal and JSON reports.

Add `--resources` to the serial command above to measure peak memory on Linux.
Each measured command runs through a fresh kernel resource collector, followed
by the same outcome and coverage-data checks. Results retain peak RSS in bytes
for every run. Linux `ru_maxrss` reports the largest individual process peak, including waited children. Use `--tree-resources` for sampled process-tree PSS.

Record these project-specific flags with the result:

- Rich: `--pytest-args '-p vfy_benchmark_plugins.rich_clock_plugin' --scope 'full suite with controlled logging clock and stream proxy cleanup'`
  freezes per-test logging timestamps for both plug-ins while retaining the
  complete test suite. The memory series retains each test's temporary
  `FileProxy` objects until teardown, then closes them in every route; cleanup
  time is included. Earlier timing records retain their original fixture versions.
  Use a Rich checkout path without `red`: its logging test checks rendered
  file URLs for that substring. Earlier timing commands use the manifest's
  `json` report destination; the memory commands use `json:coverage.json`.
- attrs: `--pytest-args '-p hypothesis.extra.pytestplugin --hypothesis-seed=0'`
  pins generated examples for both plug-ins and disables Hypothesis's example
  database; the full suite remains selected.
- Packaging: the full configured suite retains its upstream default marker
  selection, which excludes 427 opt-in property tests while running 62,435
  cases. The chart abbreviates this to “Configured suite”; the result retains
  the full scope.
- Black: the configured `no_jupyter` tox test phase runs with
  `BLACK_NUM_WORKERS=1` and `--phase serial` (`-n 0`). It contains 483 passing
  tests, one skipped test, and eight subtests. Both coverage routes request
  per-test contexts and terminal plus JSON reports; the result records the
  wheel and source revisions used for this separate two-run comparison.
- FastAPI: `--source fastapi` replaces the manifest's blanket source argument.
  The serial route runs its full `tests` and `scripts/tests` selection: 3,348
  passed, 17 skipped, five xfailed. The corrected release wheel and separate
  preparation manifest are recorded with the two-run comparison.
- Click: the stock coverage gate matched outcomes, aggregate data, and JSON,
  but one per-test context line at `_NamedTextIOWrapper.close:184` differed.
  It is excluded from the exact-parity coverage chart; its native test
  comparison has a separate passing gate.
- Datasette: `--pytest-args '-m ""' --scope 'all test markers included in serial phase'`
  includes markers excluded by its serial defaults.
- Requests: `--pytest-args '-k "not test_connect_timeout and not test_total_timeout_connect"'`
  with `--scope 'four reserved-IP connect-timeout cases excluded'` on a host
  where those four cases cannot exercise their reserved-IP timeout. This is a
  selected suite, not a full-suite result.

Include a chart row only after every outcome, JSON, schema-7, and requested
output gate and every recorded sample completes. Plain pytest is the
uninstrumented control. Complete-command ratios divide pytest-cov's median by
vfy's; added-overhead ratios divide each tool's cost above the plain median.
Retain all three sample series and document any limits of the outcome checks.

Generate chart summaries from fresh result and parity records with scripts/build_coverage_chart_summary.py.

## Static report protocol

The static stage times source analysis and JSON reporting for Rich, Black, and Datasette. It takes a source checkout, its interpreter, and a Coverage.py schema-7 database from a separate parity-checked workflow. Before timing, the command validates the database against vfy's Rust store. No tests run inside a sample. The result keeps the manifest, dependency-lock digest, database hash, exact report outputs, and per-sample source signatures.

Reuse the Rich preparation from [project preparation](#prepare-a-project). If it does not exist, prepare it with the command below. Preparation requires an empty environment directory. The checkout, environment, and metadata stay under one root, and later commands pass each path explicitly:

```console
uv run --no-sync python -m benchmarks prepare projects rich /tmp/vfy-projects/rich \
  --environment /tmp/vfy-projects/rich/environment \
  --metadata /tmp/vfy-projects/rich/artifacts \
  --python python3.12
```

Run these commands from the vfy checkout. The static controller must run under the prepared interpreter, so source analysis and reporting use the recorded Python version and installed release artifact.

Run the full project parity gate first. It writes the schema-7 database and the report files that the static stage consumes:

```console
/tmp/vfy-projects/rich/environment/bin/python -m benchmarks validate projects \
  --project rich \
  --root /tmp/vfy-projects/rich/project \
  --python /tmp/vfy-projects/rich/environment/bin/python \
  --preparation-manifest /tmp/vfy-projects/rich/artifacts/manifest.json \
  --artifacts /tmp/vfy-projects/rich/artifacts/baseline
```

Run the static stage's diagnostic parity gate with the same interpreter and database:

```console
/tmp/vfy-projects/rich/environment/bin/python -m benchmarks validate static-report \
  --project rich \
  --prepared /tmp/vfy-projects/rich \
  --root /tmp/vfy-projects/rich/project \
  --python /tmp/vfy-projects/rich/environment/bin/python \
  --preparation-manifest /tmp/vfy-projects/rich/artifacts/manifest.json \
  --data-file /tmp/vfy-projects/rich/artifacts/baseline/.vfy-benchmark-pytest-cov \
  --diagnostic
```

After validation, collect publication samples with the same interpreter and prepared data:

```console
/tmp/vfy-projects/rich/environment/bin/python -m benchmarks run static-report \
  --project rich \
  --prepared /tmp/vfy-projects/rich \
  --root /tmp/vfy-projects/rich/project \
  --python /tmp/vfy-projects/rich/environment/bin/python \
  --preparation-manifest /tmp/vfy-projects/rich/artifacts/manifest.json \
  --data-file /tmp/vfy-projects/rich/artifacts/baseline/.vfy-benchmark-pytest-cov \
  --mode publication --warmups 3 --runs 15 \
  --artifacts target/benchmarks/static-report-rich-artifacts \
  --output target/benchmarks/static-report-rich.json
```

Coverage.py source signatures and report parity are checked before timing. vfy's source signature, normalized report files, and totals are checked after every sample. The source gate passes the selected project's exclusion and partial-branch patterns to vfy. A mismatch rejects the run.

<a id="real-project-selection-protocol"></a>

## Real-project selection protocol

The Platformdirs edit changes `return prefix + value` to `return prefix +
value + ""`. The Rich edit changes `return f"<emoji {self.name!r}>"` to
`return "<emoji " + repr(self.name) + ">"`. Both execute different bytecode,
retain the original result, and have directly asserted upstream tests. Each
repetition restores the pinned upstream revision, creates separate full
vfy and testmon histories, applies the edit, and rotates the affected-rerun
order. Rich first applies the same reviewed deterministic pipe, display,
recursion, and clock test adaptations to all commands; a local fixture commit
provides the clean, passing baseline required by native test selection.

### Reproduce selected reruns

To repeat a project, prepare its pinned checkout, hashed-lock interpreter,
release wheel, and preparation manifest, then run the harness:

```console
python -m benchmarks.benchmark_real_selection \
  --project platformdirs \
  --engine pytest-native \
  --root /path/to/platformdirs-checkout \
  --python /path/to/locked-environment/bin/python \
  --preparation-manifest /path/to/preparation/artifacts/manifest.json \
  --artifacts target/benchmarks/selection-rerun/platformdirs-artifacts \
  --output target/benchmarks/selection-rerun/platformdirs.json \
  --mode diagnostic --warmups 0 --runs 2
```

Use `--project rich`, `dotenv`, `sortedcontainers`, or `colorama` with its
matching checkout, interpreter, manifest, and separate local output path.
Prepare Platformdirs, python-dotenv, Sorted Containers, and Colorama with
`--constraints benchmarks/locks/PROJECT-selection.txt`; each selection lock adds
pytest-testmon, and python-dotenv, Sorted Containers, and Colorama have no other
lock. Rich uses its full-suite lock, which already includes pytest-testmon.
The harness defaults to `--engine=native`;
use `--engine=pytest-native` to measure pytest collection with vfy execution. Independent runs use configured default discovery only
after its complete phase outcomes match the manifest-targeted full pytest-cov
oracle. Each result records the selected engine, effective commands and inputs.
To regenerate the published plot data and SVG from both local results:

```console
python scripts/build_selection_chart_summary.py \
  target/benchmarks/selection-rerun/platformdirs.json \
  target/benchmarks/selection-rerun/rich.json \
  --output target/benchmarks/selection-rerun/chart-summary.json
python scripts/render_benchmark_charts.py \
  --summary-json target/benchmarks/selection-rerun/chart-summary.json \
  --output-dir docs/assets
```

<a id="mutation-benchmark"></a>

<a id="real-project-mutation-protocol"></a>

## Real-project mutation protocol

The Packaging benchmark copies `pyproject.toml` for both tools, selects
`tests/test_utils.py` as `testpaths`, removes the upstream `-m "not property"`
filter that conflicts with `vfy mutate --all`, and sets
`coverage.report.fail_under` to zero for this utility-only test run. All 77
selected upstream calls pass before and after adaptation; source under mutation
is unchanged. The result retains the config diffs and hashes. Wider Requests
scopes and Packaging definition-time mutations failed parity; their gates are
retained. The body scopes were declared before timing, and no body mutant was
removed based on status or speed.

Datasette's session fixture registers `TrackEventPlugin` once per pytest
process, while mutmut calls pytest repeatedly in one process. Disposable test
copies unregister the plug-in before its existing registration; mutmut also
copies `docs/`, which an upstream autouse fixture reads. Original and adapted
selected-test outcomes match, and the result retains the diff and file hashes.
Both routes omit inherited pytest plug-ins that these tests do not need.
Requests uses upstream `pyproject.toml` settings in both copied test trees;
its internal certificate symlinks are fingerprinted for the killer proofs.

The harness intersects catalogs by relative path, UTF-8 byte range, original
expression, and replacement. The independent engine uses one worker. For installed pytest, select the native route explicitly and record the worker count and test isolation. Each round starts from a fresh copy of the pinned checkout.

The project revisions are in
[`project_manifest.py`](https://github.com/seanabreau/vfy/blob/main/benchmarks/project_manifest.py);
hashed project locks are
[`requests.txt`](https://github.com/seanabreau/vfy/blob/main/benchmarks/locks/requests.txt),
[`packaging.txt`](https://github.com/seanabreau/vfy/blob/main/benchmarks/locks/packaging.txt),
and [`datasette.txt`](https://github.com/seanabreau/vfy/blob/main/benchmarks/locks/datasette.txt).
The mutmut checkout is pinned at
[`cd2f73da`](https://github.com/boxed/mutmut/tree/cd2f73da310c3fc90cffcb3e6c768cdeac14e18c)
with the hashed [`real-mutation-mutmut.txt`](https://github.com/seanabreau/vfy/blob/main/benchmarks/locks/real-mutation-mutmut.txt)
overlay. Packaging also uses the hashed [click supplement](https://github.com/seanabreau/vfy/blob/main/benchmarks/locks/packaging-mutation-click.txt).

Install the recorded release vfy wheel in the controller and project
environments and set `VFY_BENCHMARK_BUILD_MANIFEST` to its verified build
manifest. On Linux, for a prepared Requests utility checkout, run:

```console
VFY_BENCHMARK_BUILD_MANIFEST=/path/to/vfy-build.json \
  /path/to/controller/bin/python -m benchmarks.benchmark_real_mutation \
  --project requests-utils --root /path/to/prepared-requests/project \
  --python /path/to/requests-env/bin/python \
  --preparation-manifest /path/to/prepared-requests/artifacts/manifest.json \
  --artifacts /path/to/evidence/requests-utils-upstream \
  --mutmut /path/to/requests-env/bin/mutmut \
  --mutmut-python /path/to/requests-env/bin/python \
  --mutmut-repository /path/to/mutmut-at-cd2f73da \
  --vfy-route native \
  --workers 4 --warmups 3 --runs 30 --mode publication --tree-resources \
  --output target/benchmarks/requests-utils-mutation.json
```

Use `--project packaging-utils` and its prepared paths for Packaging. Use
`--project requests` for the selected-function scope or `--project datasette`
with prepared Datasette paths and the same publication sampling. Give each
scope a separate `--artifacts` directory to retain its original upstream
validation. First run `--validate-only` with separate artifacts and output.
Each result's sibling `.artifacts/` directory keeps commands, logs,
parity reports, proofs, and samples. The result binds the wheel and source
revision, mutmut revision, lock digests, machine, and timing distribution.

### Generated fixture and cache states

The `benchmark_1k` fixture compares a verified coverage baseline, cold
mutation, an unchanged cached rerun, and a behavior-preserving edit that
invalidates the cache. These are different workloads; do not combine their
timings with the real-project cold runs. Both tools use the same worker count
and the [pinned mutmut fixture](https://github.com/boxed/mutmut/tree/cd2f73da310c3fc90cffcb3e6c768cdeac14e18c/e2e_projects/benchmark_1k).

The generated-fixture harness selects `vfy mutate --engine=pytest-native`; the
real-project harness defaults to the independent engine. Both omit vfy's
`--test-isolation` option by default. The recorded native cohorts use explicit
native execution. The
benchmark selection `--test-isolation default` also omits it; `serial` and
`file` supply explicit controls to every baseline, mutation, and cache-state
command. The resolved public behavior follows the
[isolation contract](../docs/guides/mutation.md#isolate-test-files).

The harness compares vfy's Ruff parser with mutmut's pinned LibCST visitor
and matches the exact common catalog by relative path, UTF-8 byte range,
original expression, and replacement before timing child-process commands.
It checks IDs, statuses, killer tests, and cache behavior. Errors,
timeouts, unstable baselines, and unapplied mutants cannot count as kills.
Definition-time and warm/cached killer identities must match exactly. For a
fresh baseline that chooses another body-test killer, both the previous and
new killer must pass on clean source and fail during the test call on the
mutant, with passing setup and teardown. The same proof applies when cache
invalidation changes a body mutant's killer. Proofs and parity gates run outside
the timer. Named mutmut commands select the common catalog; unnamed cached
commands may reuse a broader one, so the harness checks that boundary.

Alternate tool order, retain every stage sample, and use a release vfy
wheel with its recorded source revision. Publication requires three warmups,
thirty measured runs, clean source, and complete outcome evidence. Absolute
times depend on the project, catalog, worker count, and machine.

### Reproduce the generated fixture

From the vfy checkout, build with `maturin build --release --locked`,
install that exact wheel, and record its build manifest with
`benchmarks.benchmark_provenance.write_build_manifest`. The real-project
presets [prepare this binding](#prepare-a-project).

```console
git clone https://github.com/boxed/mutmut.git /tmp/mutmut
git -C /tmp/mutmut checkout cd2f73da310c3fc90cffcb3e6c768cdeac14e18c
env -u UV_PROJECT_ENVIRONMENT -u VIRTUAL_ENV \
  uv venv /tmp/mutmut/.venv --python 3.12
env -u UV_PROJECT_ENVIRONMENT -u VIRTUAL_ENV \
  uv sync --project /tmp/mutmut --frozen --no-dev \
  --python /tmp/mutmut/.venv/bin/python
VFY_BENCHMARK_BUILD_MANIFEST=/path/to/build.json \
  /path/to/benchmark-environment/bin/python -m benchmarks run mutation \
  --project /tmp/mutmut/e2e_projects/benchmark_1k \
  --mutmut /tmp/mutmut/.venv/bin/mutmut \
  --mutmut-python /tmp/mutmut/.venv/bin/python \
  --output target/benchmarks/mutation.json
```

The benchmark workflow also runs the pinned named-versus-cached command
boundary test with `MUTMUT_BOUNDARY_REQUIRED=1`. Publication rejects missing
parity, release-build provenance, samples, or infrastructure outcomes.

<a id="pinned-project-comparisons"></a>

## Publish a result

Link the raw records, parity gates, builds, and reproduction commands. State the
measured boundary and sampling method beside any claim; one fixture, machine,
or cache state does not establish performance for another workload.
