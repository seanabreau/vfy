# FAQ

## How do you pronounce vfy?

Say “verify.” Write it **vfy**, in lowercase.

## What is vfy for?

vfy runs tests, measures coverage, and selects tests for code your changes affect.
Start with [running tests](../guides/run-tests.md), then try
[affected-test selection](../getting-started/selection.md). See
[test execution and pytest compatibility](../about/pytest.md) for how vfy runs an existing suite.
[Mutation testing](../guides/mutation.md) checks whether assertions detect a
source change; [semantic coverage](../guides/semantic.md) finds untested
Boolean conditions.

## Does high coverage mean my tests catch bugs?

No. Coverage shows which code ran, not whether assertions detect incorrect
behavior. A test can execute every line without checking its result.

[Semantic coverage](../guides/semantic.md) identifies missing condition outcomes.
[Mutation testing](../guides/mutation.md) checks whether tests fail after a
specific source change. A surviving mutation is a prompt to review the behavior;
it can be equivalent to the original code. Neither measure proves correctness.

## Does vfy replace pytest?

For supported suites, yes. `vfy test` provides collection, assertions, fixtures,
and execution by default, including the `pytest` API used by your tests.
Only the optional `--engine=pytest-native` and `--engine=pytest` routes require
installed pytest. [Check your suite’s hooks and plug-ins](../guides/native-adoption.md).

## Does vfy require Rust?

No. Installing vfy from PyPI uses a prebuilt wheel on supported platforms, and
running vfy does not need Rust. Rust and a C compiler are needed only when
[building from source](../getting-started/installation.md#install-from-source).

## How does vfy decide which tests to run?

vfy compares uncommitted edits with a passing full run on the current clean Git
commit, using per-test coverage and static imports. The independent engine
selects tests for unfiltered `safe` coverage runs; its other runs execute the
full requested scope. Missing data and unsupported changes also trigger a full run.
See [the selection rules](../guides/test-selection.md).

## Can I trust a selected run?

Yes, for edits to source files. Selected runs rely on past execution, so they
cannot track changes to external services, clocks, environment variables, or
state shared between tests. Use
your usual command with `--all` added for those changes and full-suite CI checks,
keeping its coverage and worker options.
[Selection limits](../guides/test-selection.md#what-selection-does-not-promise)
explain when to run everything.

## How does performance compare?

Performance depends on the workload, worker count, and coverage settings.
The [benchmark guide](../about/benchmarks.md) explains how to check that the
tools produce equivalent results before comparing their timings.

## How do I see which tests cover a line?

After collecting per-test coverage with `vfy test`, run
`uv run vfy explain PATH:LINE`. It reads saved data, runs no tests, and lists
the tests that reached the line. Recollect after edits to keep it current.
See [explain](../guides/test-selection.md#explain) for decision data and JSON.

## Which features are ready to try?

vfy is Alpha, so interfaces may change. vfy 0.0.1 supports Linux and macOS.
See [Compatibility](../about/compatibility.md) for feature and plug-in limits.

## Can Coverage.py open vfy data?

Yes, tested Coverage.py releases read vfy's schema-7 line and branch data.
Line and branch runs cannot be mixed in one data file.
[Data files](../reference/data-files.md) explains the format;
[Compatibility](../about/compatibility.md) lists the tested APIs and plug-ins.

## Where can I find the details?

- [Parallel workers](../guides/run-tests.md#workers) and [subprocess coverage](../guides/collection.md#collect-workers-and-subprocesses)
- [Test-result HTML](../guides/run-tests.md#test-results-html), separate from [source coverage HTML](../guides/collection.md#generate-reports-and-enforce-a-threshold)
- [Semantic coverage](../guides/semantic.md) and its [plug-in limits](../about/compatibility.md#semantic-decision-coverage)
- [Python 3.12 monitoring and bundled source parser](../about/architecture.md)
- [Configuration validation](../reference/configuration.md) and [raw benchmark records](../about/benchmarks.md)
