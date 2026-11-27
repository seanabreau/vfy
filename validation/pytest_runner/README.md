# Test execution comparisons

The default independent engine runs without installed pytest:

```console
uv run vfy test --no-cov tests
```

Execution uses one worker by default. On Linux or macOS, `-j 4` enables four
independent workers that claim case batches as they finish; `-n` and `--workers`
are aliases.
Use `python -m pytest -n 4` for the stock pytest-xdist comparison.
The optional `--engine=pytest-native` frontend uses installed pytest for collection and
requires `vfy[pytest]` with a
[supported stable release](../../docs/about/compatibility.md#versions).
The [compatibility note](COMPATIBILITY.md) describes that frontend's limits.
The pinned stock comparisons use pytest 9.1.1.

Use the checks in this directory to compare an installed wheel with pinned
pytest and project suites before measuring performance.

- [Test guide](../../docs/about/native-pytest-testing.md): focused suites and CI.
- [Corpus guide](../../docs/about/pytest-test-suite.md): pinned source, comparison limits, and reproduction.
- [Installed-wheel workflow](../../.github/workflows/native-pytest.yml): build and environment setup.

For serial runner measurements, see
[benchmark instructions](../../benchmarks/README.md#native-serial-pytest).

## Compare independent workers on a real project

Use a release wheel, its build manifest, and fresh work and output directories.
This diagnostic runs one complete pinned project with seven paired rounds in
rotating order:

```console
.venv/bin/python validation/pytest_runner/benchmark_native_xdist.py \
    --wheel /path/to/vfy.whl --build-manifest /path/to/build.json \
    --project iniconfig --workers 4 --independent-workers 1 4 \
    --timing-route stock-xdist-0 --timing-route native-j1 \
    --timing-route stock-xdist-4 --timing-route native-j4 \
    --timed-evidence full --mode diagnostic --warmups 1 --runs 7 \
    --work-root /path/to/project-work --output /path/to/project-results
```

Preflights compare the full configured inventory, JUnit, phase records, and
warnings before timing. Each independent sample repeats the phase check. The
iterator-parametrization deprecation has one declared representation rule:
verified framework source locations map to the public `Metafunc.parametrize`
operation, preserving warning order, count, category, and complete message.
Project warning locations and other diagnostics remain literal. Raw diagnostic
differences and normalization receipts are retained alongside the semantic
comparison. Unknown emitters or custom formatting remain blocked.

Diagnostic samples do not establish published speed claims. Publication uses
the full harness on the release commit with its required sampling protocol.

The untimed adoption matrix includes Click 8.5.0's configured pytest suite, pinned at
`8b19813f2bfca99f1018a587a8cf54fc959f2e5d`. It compares stock pytest with one and
two native workers, then checks branch coverage after outcome parity passes.
Click's upstream configuration deselects stress tests.
Aggregate lines, arcs, and reports must match; cyclic finalizers can run under
different test contexts.
Comparison copies normalize the `CliRunner` address in one expected-xfail
diagnostic; raw execution records retain it.
Use an installed release wheel and its build manifest:

```console
.venv/bin/python validation/pytest_runner/check_native_adoption_matrix.py \
    --wheel dist/<wheel-filename>.whl --build-manifest dist/adoption-build.json \
    --project click --output target/native-adoption-click --python-version 3.14
```

## Time bounded project suites

From a clean, committed checkout with an installed release wheel and its build
manifest, run the [project timing driver](benchmark_native_projects.py):

```console
.venv/bin/python -m validation.pytest_runner.benchmark_native_projects \
    --wheel dist/<wheel-filename>.whl --build-manifest dist/adoption-build.json \
    --python-version 3.12 --cases 100 --warmups 3 --runs 15 --coverage \
    --work-root ../native-project-work --output ../native-project-timings
```

The driver selects at most 100 evenly spaced cases from each package's
configured stock inventory before comparing native results, without filtering
by outcome. Strict outcome checks gate each supported route; coverage also needs
its declared data contract and the same-run Coverage.py report comparison.
Unsupported routes remain blocked in the result. Coverage timings use vfy
on both stock pytest and the native engine.

Use `--full-suite` instead of `--cases 100` to retain configured selectors and
benchmark the full stock inventory with the same parity gates and sampling
protocol.

Benchmark preparation applies public Hypothesis seed 0 to Attrs and Dateutil
tests after collection, preserving their existing hooks and profiles. Prepared
snapshots retain the exact setup diff and seed metadata. The adoption matrix
keeps strict coverage contracts. Its sortedcontainers recipe seeds three random
stress tests individually and restores the global random state so worker
partitioning does not change their inputs.

Commands run serially with three warmups and 15 measured samples in rotated
order. Each command restores prepared sources and fixtures; source bytecode
and pytest caches are cold. Measured commands retain common JUnit reports and
check ordered cases, outcomes, and status outside the timer. Work directories,
raw artifacts, hashes, and summaries are retained at the supplied paths.
Use `--validate-only` with a separate output directory to check scopes and
routes without timing.

## Time complete generated suites

The [scaling driver](benchmark_native_scaling.py) compares stock pytest and one
native worker without coverage, running complete parameter families and distinct
functions at every requested count:

```console
.venv/bin/python -m validation.pytest_runner.benchmark_native_scaling \
    --python .venv/bin/python --wheel dist/<wheel-filename>.whl \
    --build-manifest dist/build.json --counts 1000 10000 50000 \
    --warmups 3 --runs 15 --output ../native-scaling
```

Every run checks ordered JUnit outcomes and actual body or fixture traces.
Default 1,000-case controls verify fixture teardown and captured payloads;
`--controls` with no values omits them. Three warmups and 15 measured samples
run serially in rotated order, retaining all values and medians. Bytecode stays
warm; history and temporary state reset outside timing.

Each fresh Linux collector times child spawn, pipe drain and reap, excluding
its own startup. The same child supplies user/system CPU and peak RSS; RSS is
the largest process, not summed concurrent memory. Outer wrapper wall is
separate. Common JUnit and trace persistence remain part of child cost.

Results distinguish the controller commit from the artifact build commit and
verify installed wheel bytes before and after execution. Drift, mismatches or
incomplete samples prevent publication. `--validate-only` runs gates alone;
`--mode diagnostic --warmups 0 --runs 2` permits a short, unpublished probe.
