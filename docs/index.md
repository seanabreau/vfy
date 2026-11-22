---
title: vfy
description: A Python test runner and coverage tool, built in Rust. Run pytest-style tests, measure coverage, and rerun the tests your edits affect.
---

# vfy

**A Python test runner and coverage tool, built in Rust.**

vfy runs your Python tests and measures their coverage:

- **Run pytest-style suites.** `vfy test` collects and runs tests with its own
  engine by default. You can use it without installing pytest.
  Check [compatibility](about/compatibility.md) for supported tests and plug-ins.
- **Select affected tests.** Rerun tests that reached the code you changed and
  reuse coverage from the rest.
- **Measure coverage.** See which lines and branches your tests run, and which
  they miss.
- **Find weak assertions.** Change an operator such as `>=` to `>` in memory
  and check whether your tests catch the difference.
- **Find missing test cases.** See which conditions inside an `and` or `or`
  expression your tests never exercise, even with 100% line coverage.

## Get started

vfy is **Alpha**. On Linux or macOS with CPython 3.12+ and uv, install it and run
your tests:

```console
uv add --dev vfy
uv run vfy test
```

Check [compatibility](about/compatibility.md) for supported tests and plug-ins and
[installation](getting-started/installation.md) for platform requirements.
For a complete example with source files and expected output, follow the
[coverage tutorial](getting-started/quickstart.md).

## Collect and report coverage

Measure your application's lines and branches:

```console
uv run vfy test --cov=src --cov-branch --cov-report=term-missing
```

Replace `src` with your source directory. vfy prints missing lines and branches
and saves `.coverage`. Use the
[coverage guide](guides/collection.md) to generate HTML, JSON, XML, and LCOV
reports from that saved data without rerunning tests.

## Run affected tests

First [check your suite's compatibility](guides/native-adoption.md).

Add `.vfy/` to `.gitignore` and commit your setup. From a clean Git checkout,
run the full suite once:

```console
uv run vfy test --all --cov=src
```

vfy saves the passing run and records which tests reached which code. Edit a
function, leave the change uncommitted, then run:

```console
uv run vfy test --cov=src
```

vfy picks tests using the earlier run's coverage and imports. For example, in
[the two-test tutorial](getting-started/selection.md), changing a bounds-checking
function reruns its test and skips an unrelated formatting test.

Replace `src` with your source directory and keep coverage and worker options
the same between runs. Missing saved data, test or fixture changes, and changes
vfy cannot map to tests cause a full run. Use `--all` for full-suite CI checks
and changes to external services, environment variables, or shared test state.
[How selection works](guides/test-selection.md).

## Check what your tests prove

A line can run without its result ever being checked. vfy includes two ways to
look beyond a line-coverage percentage:

- **[Mutation testing](guides/mutation.md):** vfy makes a small change in memory,
  such as replacing `>=` with `>`, and runs the relevant tests. If they still
  pass, review whether an assertion missed a bug or the change is equivalent.
- **[Decision coverage](guides/semantic.md):** vfy records the conditions inside
  expressions such as `x >= 0 and x < 10`, so you can find inputs your tests
  have not exercised even when both branches ran.

See the guides for their requirements and limits.

## FAQ

### How do you pronounce vfy?

Say “verify.” Write it **vfy**, in lowercase.

### Can I use my existing pytest suite?

Yes. vfy supports pytest-style fixtures, parametrization, and assertions.
[Check compatibility](about/compatibility.md) for the hooks and plug-ins your suite uses.

### Does test selection replace full-suite CI?

No. Keep full-suite CI checks. Use `--all` for changes to external services,
environment variables, or shared test state. [Selection rules](guides/test-selection.md).

[More questions](help/faq.md).

## Reference and help

Use the [CLI reference](reference/cli.md) for commands and options, the
[configuration reference](reference/configuration.md) for project settings, and
the [CI guide](guides/ci.md) for full-suite checks. For help, see
[troubleshooting](help/troubleshooting.md) and the [FAQ](help/faq.md).
