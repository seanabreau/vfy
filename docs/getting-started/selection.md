# Run affected tests

Run two tests, edit one function, then run only the test that reached it. vfy
combines the selected test's new coverage with saved coverage for the skipped
test. You inspect that report and find which test reached a source line.

The tutorial needs **Linux or macOS**, CPython 3.12+, uv, and Git for the
selection baseline. Install `vfy` from PyPI; see [Installation](installation.md).

## Set up the example

Follow [Set up the example](quickstart.md#set-up-the-example). All commands
below run from that project's root.

If you already completed the coverage tutorial, create a fresh example using a
different directory name in its `uv init` command. Start with the original two
tests so the negative-input branch remains uncovered.

The project has two source files and two tests. One asserts `clamp_low(5) == 5`;
the other asserts `format_count(42) == "42"` without calling `clamp_low`.
The `value < 0` branch in `clamp_low` has no test yet. The configuration adds
`src` to pytest's import path and enables branch coverage.

## Create a clean Git baseline

A **passing baseline** is a saved passing full run on a clean Git commit. vfy
uses it to learn which tests reach which lines.

In the example directory, initialize a separate Git repository and commit the
starting project. This keeps the tutorial baseline scoped to the example.
Configure your Git name and email first if you have not. The included
`.gitignore` already ignores `.vfy/`, where vfy keeps its baseline.

<!-- doc-test: git-baseline -->
```console
git init
git add .
git commit -m "Start coverage example"
```

<!-- doc-test: baseline -->
```console
uv run vfy test --engine=native --all -j 1 --cov=src --cov-branch --cov-report= -v
```

Both tests pass, and vfy saves the passing baseline in `.vfy/`. The
negative-input branch is still untested: a baseline requires passing tests,
not full coverage.

## Edit `bounds.py`

Replace `src/bounds.py` with this equivalent version. Leave the change
uncommitted so vfy can compare it with the baseline commit:

<!-- doc-test: edit-bounds -->
```python
from __future__ import annotations


def clamp_low(value: int) -> int:
    if value < 0:
        return 0
    return int(value)
```

Run the tests:

<!-- doc-test: select -->
```console
uv run vfy test --engine=native -j 1 --cov=src --cov-branch --cov-report= -v
```

vfy prints a summary on stderr. The numbers and paths are illustrative:

```text
Changed code: /path/to/vfy-coverage-example/src/bounds.py
Affected code: 1 functions, 0 branches
Selected 1 / 2 tests (from the saved baseline; collection follows)
`vfy test --all` runs everything
Estimated selected suite: 0.00s
Estimated full suite: 0.00s
...test output...
Reused 1 tests; complete coverage reconstructed from passing baseline 1a2b3c4d
```

vfy selected **1 of 2 tests**: `tests/test_bounds.py::test_positive_value`.
It reused the formatting test's baseline coverage and exited with status 0.

**Why only one test:** your edit changed the body of `clamp_low`, and an edit
inside a function body selects only the tests that ran that function in the
baseline. `test_positive_value` calls `clamp_low`. `test_format_count` never
does, so vfy skipped it. Edits elsewhere in a Python file select more tests;
the [selection rules](../guides/test-selection.md#how-selection-decides) list
every case.

## Report the combined coverage

<!-- doc-test: selected-report -->
```console
uv run vfy coverage report --show-missing
```

The report combines the selected test's new coverage with the formatting test's
baseline. It covers both files and totals 80%. The negative-input branch is still
untested.

<a id="explain"></a>

## Find the test for a line

`vfy test --engine=native` records per-test coverage automatically.
`vfy explain` reads that saved coverage and lists the tests that reached a line,
without running them. Here, line 7 still belongs to the edited function:

```text
$ uv run vfy explain src/bounds.py:7
Tests reaching src/bounds.py:7: 1
Estimated selected suite: 0.00s
Estimated full suite: 0.00s
  tests/test_bounds.py::test_positive_value  0.001s
```

This command reads the last coverage recording; it does not explain why tests
were selected. Recollect after edits so paths and line numbers stay current.
[Semantic coverage](../guides/semantic.md) can add pairs of recorded inputs
showing how individual conditions affect a result.

Add `--json` for the same data as JSON:

<!-- doc-test: explain -->
```console
uv run vfy explain src/bounds.py:7 --json
```

The output has a `location`, a `selection` object, `semantic_evidence`, and a
`tests` list (abridged; durations vary between runs):

```json
{
  "location": "src/bounds.py:7",
  "tests": [
    {"duration_ms": 0.13, "nodeid": "tests/test_bounds.py::test_positive_value"}
  ]
}
```

## Observe the full-run fallback

Clear vfy's disposable cache and run again:

<!-- doc-test: fallback -->
```console
uv run vfy cache clean
uv run vfy test --engine=native -j 1 --cov=src --cov-branch --cov-report= -v
```

With no passing baseline, vfy runs both tests and prints `Note: no passing
baseline exists yet`. Both tests pass.

To make this edit the new baseline, commit it and run the full suite:

<!-- doc-test: refresh -->
```console
git add src/bounds.py
git commit -m "Convert the bounded value explicitly"
uv run vfy test --engine=native --all -j 1 --cov=src --cov-branch --cov-report= -v
```

## Use this on your own suite

[Check compatibility](../guides/native-adoption.md), ignore `.vfy/`, and
commit your setup changes. On a clean checkout, run
`uv run vfy test --engine=native --all --cov=src`; after each uncommitted edit, run
`uv run vfy test --engine=native --cov=src`. Replace `src` with your source directory and
keep coverage and worker options consistent.

Selection cannot see external services, clocks, environment changes, or state
shared between tests. Use `--all` for those changes and for full-suite CI checks.
Read [the selection rules](../guides/test-selection.md) for details.

Next, [measure coverage and close the missing branch](quickstart.md),
[run in parallel](../guides/run-tests.md#workers), or
[use vfy in CI](../guides/ci.md).
