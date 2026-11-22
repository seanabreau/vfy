# Testing vfy

Find the suite for your change in the [test layers](#test-layers) table. Rust
tests check the algorithms. Python integration tests exercise the public APIs
and compare supported behavior with pinned upstream versions.

## Run the required checks

[CONTRIBUTING.md](https://github.com/seanabreau/vfy/blob/main/CONTRIBUTING.md)
lists environment setup and the full pre-PR checklist. Use this page to choose
a suite and understand what it checks.

Linux CI is configured to run the Rust tests with `cargo-insta` and `nextest`. It rejects
pending or unreferenced snapshots:

```console
scripts/cargo-insta-test.sh --locked --workspace --no-default-features
```

The configured Rust matrix also uses `nextest` on Linux and macOS.
Installed-wheel jobs check CPython and PyO3 behavior. These configurations do
not establish that a particular revision passed; check its job results.

`tests.support.vfy_project.VfyProject` runs temporary projects, Git baselines, subprocesses,
and schema-7 reads. It isolates ambient Git, Coverage.py, pytest, and home
configuration, and a test can override the environment explicitly. Commands time
out after a configurable 120 seconds and keep both streams. Cleanup terminates the
POSIX process group, or uses Windows `taskkill` while the parent is alive.
Descendants that leave the group, or outlive their Windows parent, may survive.

## Python contracts

The repository typing gate covers Python 3.12–3.15, because
version guards select different standard-library APIs, such as `sys.monitoring`
events. Read-only `sources/` and semantic fixture inputs are outside the typing
gate. Fixtures keep their behavioral harnesses.

Native signatures are checked by `tests/repository/test_native_typing.py`.
`tests/typing/consumer.py` has positive type assertions and named expected
diagnostics for invalid calls. After you install a built wheel, run
`uv run --no-sync python scripts/check_wheel_typing.py` to repeat the consumer
checks outside the checkout. The wheel lanes run this check on their installed
interpreters.

Unexpected warnings and strict xfail successes fail vfy's own pytest suite.
Child projects set their own warning and xfail policy when they test those
behaviors on purpose.

## Test layout

Rust tests live beside their owning modules. Run installed-package contracts with `uv run pytest -q tests/integration` and repository
checks with `uv run pytest -q tests/repository`. Shared process helpers live in
`tests/support/`; reviewed source inputs live in `tests/fixtures/`.

## Test layers

| Change | Owning checks | Focused command |
| --- | --- | --- |
| Rust algorithms, ordering, state, persistence, and merge atomicity | Tests beside the owning module; property tests check algebra and round trips | `scripts/cargo-test.sh --locked --workspace --no-default-features` |
| Report aggregation, filtering, escaping, and rendering | Rust report tests and reviewed snapshots | `scripts/cargo-insta-test.sh --locked -p vfy-core` |
| Python source analysis | Literate analysis fixtures and direct Rust harness | `cargo test --locked -p vfy-core analysis` |
| Semantic collection and reports | Reviewed semantic fixtures and public integration tests | `uv run pytest -q tests/integration/test_semantic_fixtures.py tests/integration/test_semantic.py` |
| Coverage.py API and schema-7 compatibility | API, data, and analysis differentials | `uv run pytest -q tests/integration/test_api.py tests/integration/test_data_compat.py tests/integration/test_analysis_compat.py` |
| Native pytest execution, workers, and lifecycle | [Native pytest testing](native-pytest-testing.md) | Use the focused command on that page |
| Public CLI, pytest-cov, selection, and watch | Integration tests through `VfyProject` | `uv run pytest -q tests/integration` |
| Diagnostics and invalid configuration | Streams, exit statuses, and absence of work | `uv run pytest -q tests/integration/test_cli_diagnostics.py` |
| Built-in asyncio | Differential tests against pytest-asyncio 1.4.0 | `uv run pytest -q tests/integration/test_asyncio.py` |
| Built-in HTML and metadata | Reports, hooks, redaction, and publication | `uv run pytest -q tests/integration/test_pytest_html.py` |
| Installed artifacts | Fast Linux wheel lane and full platform wheel matrix | CI `wheel-fast` or `wheel` job |
| Repository health | Locked licenses, advisories, unused code, and workflow security | CI supply-chain jobs |
| Performance | Parity-gated committed harness and retained samples | [Benchmark commands](https://github.com/seanabreau/vfy/blob/main/benchmarks/README.md) |

## Focused development loop

Use the table above for the smallest owning suite. For a CLI iteration, narrow
it with `uv run pytest -q tests/integration/test_cli.py -k vfy_test`.
Before handoff, run the [full pre-PR checklist](https://github.com/seanabreau/vfy/blob/main/CONTRIBUTING.md#before-a-pull-request).
`-k` is only an iteration aid; release validation uses complete suites.

## CI change classification

`scripts/ci_change_plan.py` classifies each change conservatively:

- Documentation-only changes skip the platform jobs.
- Ordinary code and test changes keep the Linux wheel lane.
- Collector, PyO3, dependency, build, workflow, and unknown changes run the full
  platform matrix.
- Updates to `main` always run the full matrix.

`tests/repository/test_workflows.py` checks that every workflow job that does
not call a reusable workflow declares `timeout-minutes`.

The `CI summary` gate requires the change classification and every selected job
to succeed. Missing, cancelled, or skipped required jobs fail it.

Every non-documentation tier also requires the Linux CPython 3.12
[native adoption subset](native-pytest-testing.md#required-adoption-subset). It
reuses the Linux wheel that the job built. Documentation-only changes skip that
job explicitly, and the summary requires that skipped status.

The same required job is configured to compare the pinned iniconfig,
itsdangerous, and h11 suites on CPython 3.13 through stock pytest and
`vfy test --engine=native`. The independent route uses the same wheel in a
second environment without pytest. The real-package corpus gate checks ordered
JUnit identities and outcomes, exit status, and outcome counts; it rejects
missing, stale, empty, or incompatible evidence. It retains commands, dependency
versions, artifact hashes, reports, and transcripts under
`target/native-adoption/independent/`. This check does not compare full report
phases, diagnostics, warnings, or coverage. A configured gate does not establish
that a particular wheel passed it.

## Measuring CI resource use

When you change CI tiering, keep before-and-after completed-run samples. Each
sample records the commit, lane, wall-clock duration, and billed runner minutes.
Store them in a JSON file:

```json
{
  "samples": [
    {
      "revision": "<commit>",
      "lane": "quality",
      "duration_seconds": 123.4,
      "runner_minutes": 18.75
    }
  ]
}
```

Summarize each file with `scripts/ci_metrics.py`:

```console
uv run python scripts/ci_metrics.py ci-metrics/before.json --label before
uv run python scripts/ci_metrics.py ci-metrics/after.json --label after
```

Keep the raw records with the measurement note. Report an improvement only when
both sample sets use the same workflow revision window and runner classes.
Otherwise, describe the result as an observation, not a speed claim.

## Review discipline

Use the [testing instructions](https://github.com/seanabreau/vfy/blob/main/.agents/skills/testing/SKILL.md)
to choose the owning layer, an independent oracle, and the smallest regression
fixture. Performance changes also follow the
[measurement instructions](https://github.com/seanabreau/vfy/blob/main/.agents/skills/coverage-performance/SKILL.md).

## Semantic coverage in depth

Semantic changes need independent checks of evaluation, CPython event mapping,
persistence, and public reports. Ordinary line and branch differentials must
still pass with decisions disabled.

### Rust invariants

`crates/vfy-core/src/semantic/logic/tests.rs` and `crates/vfy/src/semantic/tests.rs`
cover interpreter-free rules: tri-state short-circuit evaluation, `not` polarity,
masking MC/DC pairs, stable source analysis, and pragma exclusions. Reviewed truth
tables and generated logic trees (at most four conditions and four levels of
nesting) compare the Rust evaluator with an independent reference oracle. Every
observation pair is checked for each condition. The oracle checks behavior. It does
not copy the evaluator's implementation.

### Literate fixtures

`tests/fixtures/semantic/*.md` is the review surface for supported Python syntax.
Each fixture has one executable Python block and one expected JSON block.
`tests/integration/test_semantic_fixtures.py` runs each source through the real
collector and compares normalized definitions and observation vectors. Expected
files omit temporary paths and source digests, and keep the kind, source line,
expression, logic tree, vector, and outcome. The fixtures cover short-circuit
and chained conditions, control-flow forms,
tri-state observations, exclusions, exceptioning truth tests, generator
suspension, and atomic-condition source ranges. They check that collection does
not repeat truth tests, fabricate outcomes, or retain incomplete vectors.

The Rust evidence API is tested end to end. Each reported pair is checked against
the same Rust proof used for the aggregate percentage, including context
filtering and invalid short-circuit rows. Python report tests check that they
adapt Rust rows. They do not carry a second MC/DC algorithm.

Adding a syntax form means adding a fixture with a hand-reviewed truth table. Do
not add another large string to a Python test. A fixture failure is a semantic
regression until the implementation or the language contract is reviewed.
Never regenerate expected JSON blindly.

### Public-contract tests

The Python suite keeps fixtures focused and tests each boundary separately:

- schema-7 compatibility and atomic semantic persistence;
- shard combine, path aliases, source-digest conflicts, append replacement, stale
  data, contexts, and optimization-level behavior;
- CLI, pytest options, JSON shape, terminal output, and threshold exit codes;
- Coverage.py and pytest-cov differential behavior for ordinary line and branch
  modes, which must not change when decisions are disabled;
- mutation-testing boundaries: verified passing baselines, stale coverage data,
  imported aliases, nested and decorated callables, fixture setup, call, and
  teardown, async and generator owners, xfail semantics, timeouts, fallback, and
  resumable per-mutation cache entries.

## Release validation

Release publication requires the complete Python suite against installed ABI3
wheels on Linux CPython 3.12–3.15, and on macOS ARM64 CPython
3.12, 3.14, and 3.15. The same commit must pass the Rust workspace tests on Linux.

The Linux CPython 3.12 wheel checks are configured to run pytest 9.1.0, 9.1.1, and 9.2.0. Each version runs the native
version-boundary suite without xdist, then installs pytest-xdist 3.8.0 for the
broader native suite's stock-pytest comparisons.
They also run pinned pytest 9.1.1 with the pytest-xdist 3.6.1 minimum for
direct-pytest coverage integration. The built-in asyncio check runs without
pytest-asyncio installed.

### Native engine release criteria

Native `vfy test` is experimental. Removing that label requires the supported
[test workflows](../guides/run-tests.md) to pass against stock pytest on
Linux and macOS, on every advertised CPython version and
[supported pytest release](compatibility.md). The native installed-wheel matrix
covers CPython 3.12–3.15. Verify
installation and execution on every advertised platform and interpreter.
Unsupported workflows must fail with an actionable diagnostic, without a stock
pytest retry. Add compatibility proof before widening the pytest range.

The version-boundary suite also checks locked pytest 9.1.1 without xdist; the
[version-check method](native-pytest-testing.md#run-focused-tests) lists the
complete matrix. Corpus and real-project comparisons use 9.1.1.

| Area | Required proof |
| --- | --- |
| Test behavior | Rust invariants, plus public `VfyProject` differentials against pinned pytest for success, failure, teardown, interruption, and repeated sessions |
| Plan and bindings | Rust typed-plan validation, plus public tests for dynamic fixtures, binding resolution, warnings, report locations, and exception translation |
| Installed artifacts | Clean-wheel `vfy test` runs on every advertised platform and Python version |
| Existing projects | Pinned suites run natively. Collection, outcomes, diagnostics, coverage, and requested reports are compared. Exclusions are recorded. |
| Failure handling | Earlier coverage and reports are kept where promised, resources are released, and the documented exit status is returned. No stock retry. |
| First use | Clean install, then the documented adoption example. Configured options are honored or rejected explicitly. |
| Performance | Parity-gated measurements of the exact candidate artifact, using the committed harness. All samples, workload, versions, and environment are kept. |

On the candidate, install a wheel without Rust. Through the public CLI, run a
file, node ID, parametrized case, and `-k` selection. Check successful and failed
runs, assertion details, source locations, exit status, help, and quiet output.
Record configuration changes, rejected plug-ins, diagnostic differences, and
real-project exclusions. Keep user feedback separate from automated results.
Each claimed workflow needs public-boundary proof: the
[corpus comparison](pytest-test-suite.md#comparison-scope) covers outer sessions,
uses stock pytest for nested pytester projects, and does not check complete
captured-output parity. See the [adoption scope](../guides/native-adoption.md#check-the-supported-scope)
for unsupported workflows and plug-ins.

For an edit-loop claim, measure one test, one file, affected-test selection, and
larger suites. Measure watch from saved edit to displayed result, including
debounce, collection, and reporting. Separate first invocation from warm-cache
runs, and execution time from total command latency. Compare outcomes and
coverage with a fresh full run before interpreting timings. Use the committed
benchmark harness; correctness gates alone do not establish performance.

Find comparison methods in
[Native pytest testing](native-pytest-testing.md) and publication steps in the
[release checklist](https://github.com/seanabreau/vfy/blob/main/CONTRIBUTING.md#release-checklist).

## Ecosystem comparisons

The scheduled ecosystem workflow (`.github/workflows/ecosystem.yml`) defines
pinned `attrs`, `click`, and `httpx` smoke comparisons against Coverage.py and
vfy. It compares normalized files, lines, arcs, contexts, and report totals.
Its separate recorded-project matrix has these lanes:

| Project | Python | Configured check |
| --- | --- | --- |
| Rich | 3.12, 3.13 | Require the parity comparison to pass |
| Rich | 3.14 | Require a known schema-7 rejection |
| Requests | 3.12, 3.13, 3.14 | Require the parity comparison to pass |
| Datasette | 3.12 | Require a known report-generation or source-parsing rejection |

The diagnostic lanes require a nonzero result matching their expected error;
a passing diagnostic job does not establish successful parity. Jobs retain
reports, databases, resolved dependencies, logs, and semantic differences, and
publish no timings. This configured matrix is separate from the
[historical benchmarks](benchmarks.md); check the candidate's retained job
results for execution evidence.

The workflow does not block releases. A case can become a required CI check
after its expected behavior and comparison method are reviewed.

## Executable documentation

`tests/integration/test_documentation.py` copies the source, tests, and config
from `examples/coverage/` into temporary projects. It runs the coverage,
selection, mutation, Python API, and CI examples through `VfyProject`.
`test_documented_semantic.py` checks the semantic guide’s missing-input example,
including the condition vectors and failing and passing gates:

```console
uv run pytest -q tests/integration/test_documentation.py tests/integration/test_documented_semantic.py
```

Mark an executable block with `<!-- doc-test: name -->` immediately before its
fence. Names are unique within each page. Console blocks have one `$ ` command
per line. Tests substitute the installed interpreter for `uv run vfy` and
`uv run pytest`, so wheel tests exercise the wheel under review. Python blocks
are run or written as shown. CI recipes use inline `run:` commands. The
extractor rejects multiline commands rather than skipping them. Job conditions
and artifact transport still need a real Actions run.

Keep expected lines, branches, tests, and statuses independently reviewable.
Unmarked API fragments are partial examples.

Documentation has two validation boundaries:

- The strict Zensical build checks the pages under `docs/`: navigation, links,
  and anchors.
- Repository entry points such as `README.md`, `CONTRIBUTING.md`, benchmark notes,
  and changelogs sit outside the site. They need a separate link review.

The build cannot catch clipped tables, unreadable diagrams, or an unbalanced
feature grid. Check changed pages at desktop and mobile widths, in both color
schemes. The required CI workflow runs these tests for documentation-only changes.
Code changes run them in the integration and installed-wheel suites.

The contributor fixture under `examples/coverage/` builds vfy from the
containing checkout and locks its Python dependencies. The user tutorials use
[PyPI installation](../getting-started/installation.md); this fixture validates
the local source under review. Run `uv lock --project examples/coverage`
after changing dependency requirements, then `uv sync --locked` from the
example directory. Keep the example inside a reviewed checkout so its relative
source path remains valid.

The separate installation smoke copies the fixture into temporary projects
and points their vfy source at the checkout under review. It resolves that local
source, installs a noneditable wheel in a fresh environment, verifies the
recorded source directory and installed package and extension origins, and
executes the actual `uv run` commands from the coverage, selection, mutation,
and semantic guides:

```console
uv run python scripts/check_tutorial_install.py
```

It checks 80% coverage, the failing threshold, 100% after adding the missing test,
JSON/XML exports, affected selection, combined coverage, and the full-run
fallback. It also checks a surviving mutation becomes protected after a stronger
assertion, a missing input brings condition and MC/DC coverage to 100%, and
upstream pytest-html and pytest-metadata remain active with vfy coverage.
This contributor check requires Linux or macOS, Git, and the
[source-build prerequisites](https://github.com/seanabreau/vfy/blob/main/CONTRIBUTING.md#setup).
The documentation CI job runs it separately from candidate-build tests; passing
one does not establish the other.

## Measuring Rust execution coverage

Measure Rust unit tests and Python integration tests together. The Python tests
load a Rust extension built with `-C instrument-coverage`, including in child
processes. Use a separate build directory, absolute `LLVM_PROFILE_FILE` paths
containing `%p` and `%m`, and the LLVM tools that match the pinned Rust compiler.
Include the Rust test executables and the extension. Exclude test sources and
dependencies.

Compare unit-only and combined reports against the same source-map inventory.
Review uncovered lines and regions by behavior, not against a percentage target.
Region coverage is not branch coverage. Record the interpreter, platform, build
flags, test results, and skipped cases with each measurement. Normal-exit profiles
can omit counters from deliberately terminated processes. Measure each CPython
version in the supported CI matrix, because collector paths are versioned.

`tests/integration/test_collector_edge_cases.py` covers branch-event
retirement, non-LIFO suspensions, file tracers, `cover_pylib`, and encoding
edges. `tests/integration/test_collector_interleaving.py` compares threaded calls, and
generator instances resumed out of suspension order, with pinned Coverage.py. The
same cases independently check semantic observations. Rust merge properties
assert the expected context-partitioned union as well as the merge algebra.
Semantic persistence tests compare complete stored records and their ordering.
