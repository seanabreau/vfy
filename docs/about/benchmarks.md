# Benchmarks

Measure vfy with the [benchmark harness](https://github.com/seanabreau/vfy/blob/main/benchmarks/README.md). Each workload defines the commands and the outputs it compares.

## How to read benchmark evidence

Check the engine, Python version, machine, selected tests, coverage settings and worker count before comparing times. The reporting workload compares saved coverage against Coverage.py before sampling. Selection benchmarks compare rerun outcomes and reconstructed coverage with a full run. Mutation benchmarks verify the common mutations and the tests that detect them.

Use release wheels and pin the workload revision and dependency lock. Record all samples from both tools, with their execution order, warmups, median and spread. Elapsed time includes only the stages named in the workload. A process peak and the memory used by an entire worker tree measure different things.

## Run a comparison

Follow the [setup and report recipe](https://github.com/seanabreau/vfy/blob/main/benchmarks/README.md#getting-started) to check parity and collect samples. The [native benchmark recipes](https://github.com/seanabreau/vfy/blob/main/benchmarks/native-pytest.md) cover installed-wheel test execution.

<a id="readme-chart"></a>

## README chart workloads and methodology

The chart compares each tool with vfy on the same workload. Time is median elapsed command time. Memory is the median sampled peak proportional set size across the process tree. Each pair starts at zero and uses its own time and memory scale.

| Package | Workload | Comparison |
| --- | --- | --- |
| Packaging | Full suite of 62,435 tests across four workers | vfy with file scheduling and pytest-xdist |
| Rich | Both tools select the same 1 of 981 tests after an edit to `Emoji.__repr__` | vfy and pytest-testmon |
| flake8-simplify | 69 shared mutations against 226 tests with one mutation worker | vfy and mutmut |

The Linux ARM64 runs used CPython 3.12.14 and the vfy 0.0.1 release wheel. The Linux container shared its host with the local test suite during sampling. All three vfy commands use `--engine=pytest-native`: pytest collects the tests and vfy executes them. Packaging checks JUnit outcomes before timing and again for each measured run. The mutation comparison checks the shared mutation outcomes before timing and throughout sampling.

For Rich, each pair starts from fresh full-suite histories and the same edit to `rich/emoji.py`. The timer covers the affected rerun, including JUnit output from both tools and vfy’s reconstructed coverage report. Full-suite baseline creation is outside the timer. The harness compares selected test outcomes with a full pytest-cov run and checks vfy’s JSON report and aggregate coverage database after the edit. Per-test context attribution differs from the full run. The Rich lock pins pytest-testmon 2.2.0, pytest-cov 7.1.0 and Coverage.py 7.16.2.

### Measured medians

| Package | Tool | Time (s) | Peak tree PSS (MiB) |
| --- | --- | ---: | ---: |
| Packaging | vfy | 12.05 | 583 |
| Packaging | pytest-xdist | 22.69 | 1641 |
| Rich | vfy | 0.39 | 60 |
| Rich | pytest-testmon | 0.73 | 45 |
| flake8-simplify | vfy | 1.59 | 69 |
| flake8-simplify | mutmut | 28.49 | 138 |

Each tool ran three warmups. Packaging and Rich contributed 15 measured pairs each and flake8-simplify contributed 30. The order alternated within each workload. The [paired samples, confidence intervals, build identity and upstream revisions](https://github.com/seanabreau/vfy/blob/main/benchmarks/results/workflow-evidence.json) accompany the chart.

### Reproduce the comparisons

Build the release wheel and record the imported extension with the [native benchmark setup](https://github.com/seanabreau/vfy/blob/main/benchmarks/native-pytest.md). Run these commands from the vfy checkout on Linux. Set the paths to your wheel, its build manifest, the upstream checkouts and fresh output directories.

```sh
python validation/pytest_runner/benchmark_native_xdist.py \
  --wheel "$VFY_WHEEL" --build-manifest "$VFY_BENCHMARK_BUILD_MANIFEST" \
  --project packaging --python-version 3.12 --workers 4 --native-workers 4 \
  --native-scheduling file --timing-route stock-xdist-4 --timing-route pytest-native-j4 \
  --capture=no --override-upstream-capture --tree-resources \
  --work-root "$PACKAGING_WORK" --output "$PACKAGING_RESULTS" \
  --mode publication --warmups 3 --runs 15
```

For Rich, prepare the pinned checkout and its locked environment, then run the affected-test comparison. The [selection protocol](https://github.com/seanabreau/vfy/blob/main/benchmarks/README.md#real-project-selection-protocol) describes the edit and the full-suite checks.

```sh
python -m benchmarks prepare projects rich "$RICH_WORK" --python python3.12
python -m benchmarks.benchmark_real_selection --project rich --engine pytest-native \
  --root "$RICH_WORK/project" --python "$RICH_WORK/environment/bin/python" \
  --preparation-manifest "$RICH_WORK/artifacts/manifest.json" --tree-resources \
  --artifacts "$RICH_RESULTS/artifacts" --output "$RICH_RESULTS/result.json" \
  --mode publication --warmups 3 --runs 15
```

For flake8-simplify, use the [resolved dependency lock](https://github.com/seanabreau/vfy/blob/main/benchmarks/results/flake8-simplify.lock) and install the project from the pinned checkout so its tests can read the distribution metadata. Install mutmut from its pinned checkout, the vfy wheel and the locked dependencies in the same environment. The mutmut checkout is `41353df71526c8309e77521b9342a539dd434479`. The [mutation setup](https://github.com/seanabreau/vfy/blob/main/benchmarks/README.md#compare-another-mutmut-adopter) describes the required inputs.

```sh
"$BENCH_PYTHON" -m benchmarks.benchmark_adopter_mutation \
  --root "$FLAKE8_SIMPLIFY" --revision d02de77a453dfcb81b67a0239d76fa1be610e658 \
  --expected-tests 226 --vfy-engine pytest-native --workers 1 \
  --mutmut "$MUTMUT" --mutmut-repository "$MUTMUT_CHECKOUT" \
  --lock "$MUTATION_LOCK" --output "$MUTATION_RESULTS" --warmups 3 --runs 30
```

Run each comparison with `--validate-only` and a separate output directory to check correctness before collecting samples.
