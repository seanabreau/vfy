# Mutation testing

Use mutation testing to check whether your pytest tests detect a small source
change, such as replacing `>=` with `>`. vfy runs the replacement without
editing your checkout. If the selected tests pass, the mutation **survived**:
review whether it changes behavior before adding a test.

The default native engine needs Linux, `--workers=1`, and serial test
isolation. For macOS, parallel mutations, or file isolation, install `vfy[pytest]`
and add `--engine=pytest-native` after `vfy mutate` in the commands below. This route follows the
[native test limits](native-adoption.md#check-the-supported-scope). Mutation runs
reject `--continue-on-collection-errors`, because collection must succeed before
a test failure can be blamed on a mutation.

!!! note "Alpha workflow"

    Mutation testing is Alpha. Results, the `vfy.mutation` JSON shape, and operators
    can change.

## Run a focused mutation review

Create an empty project directory with `src/` and `tests/` subdirectories.
Install [vfy](../getting-started/installation.md) there, then add this pytest
configuration to `pyproject.toml`:

<!-- doc-test: mutation-config -->
```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

Run all commands below from that project directory. No Git baseline is needed:
`--all` selects the source set explicitly.

### Start with a weak assertion

Save this function as `src/sign.py`. It returns 1 for nonnegative integers
and -1 for negative integers:

<!-- doc-test: mutation-source -->
```python
from __future__ import annotations


def sign(value: int) -> int:
    if value >= 0:
        return 1
    return -1
```

Save these tests as `tests/test_sign.py`:

<!-- doc-test: mutation-weak-tests -->
```python
from sign import sign


def test_negative() -> None:
    assert sign(-1) == -1


def test_zero_has_unit_magnitude() -> None:
    assert abs(sign(0)) == 1
```

The second assertion checks magnitude. Both 1 and -1 satisfy it, so it misses
an incorrect sign at zero. Collect fresh per-test coverage:

<!-- doc-test: mutation-baseline -->
```console
uv run vfy test --all --source src --cov=src --cov-context=test --cov-report=
```

Both tests pass. This writes `.coverage` and state under `.vfy/`. Both
branches ran:

<!-- doc-test: mutation-coverage -->
```console
uv run vfy coverage report --fail-under 100
```

The report shows 100% coverage and exits 0. That says which code ran, not whether
the assertions would catch a wrong result.

### Find the surviving change

Run one boundary mutation and save the report:

<!-- doc-test: mutation-focused -->
```console
uv run vfy mutate --all --source src --operator comparison_boundary --max-mutations 1 --no-cache --fail-on-survivor --format json --pretty-print --output mutations.json
```

**This command intentionally exits 2:** `--fail-on-survivor` detected a surviving
mutation. Open `mutations.json`. Its single mutation contains these fields
(abridged):

```json
{
  "line": 5,
  "operator": "comparison_boundary",
  "original": ">=",
  "replacement": ">",
  "status": "survived",
  "killer": null,
  "tests": ["tests/test_sign.py::test_negative", "tests/test_sign.py::test_zero_has_unit_magnitude"]
}
```

The changed condition is `value > 0`. At zero it returns -1 instead
of 1, but the magnitude assertion still passes. vfy applies the replacement
without editing `src/sign.py`. It verifies the unchanged source with the
same tests before evaluating mutations; this is the mutation baseline.
The default native engine selects the tests whose saved coverage reached
this function body. Its `tests` field records that selected plan. Full-scope
execution can have an empty list; `unreached` mutations run no tests.

### Make the assertion detect the bug

Replace `tests/test_sign.py` with:

<!-- doc-test: mutation-strong-tests -->
```python
from sign import sign


def test_negative() -> None:
    assert sign(-1) == -1


def test_zero_is_nonnegative() -> None:
    assert sign(0) == 1
```

Refresh coverage so the saved test contexts include the new boundary test:

<!-- doc-test: mutation-refresh -->
```console
uv run vfy test --all --source src --cov=src --cov-context=test --cov-report=
```

Both tests pass against the original source. Repeat the same mutation:

<!-- doc-test: mutation-protected -->
```console
uv run vfy mutate --all --source src --operator comparison_boundary --max-mutations 1 --no-cache --fail-on-survivor --format json --pretty-print --output mutations.json
```

The command exits 0 and replaces `mutations.json`. The same `>=` to `>` mutation
is now `protected`; `killer` names
`tests/test_sign.py::test_zero_is_nonnegative`. That test fails on the
mutated source because -1 is not 1. Coverage is still 100%, but the assertion now
checks the behavior that the weak test missed.

`--no-cache` makes both commands evaluate the mutation again. For your own code,
start with one location or operator and review survivors: some replacements
are equivalent to the original behavior and need no artificial test. To review
files changed relative to Git's `HEAD`, use:

```console
uv run vfy mutate --changed --source src --fail-on-survivor
```

## Choose the mutation scope

Pick one scope and narrow it with a location or an operator family:

| Need | Option | Behavior |
| --- | --- | --- |
| Changed files | `--changed` | Mutate changed Python files. This is the default. |
| Committed PR changes | `--base REF` | Mutate whole Python files changed since the merge base of `REF` and `HEAD`, including the working tree. |
| Complete source set | `--all` | Mutate every measured or supplied Python file. |
| Source roots | `--source PATH` | Add a source root. Repeat for more roots. |
| One location | `PATH:LINE` | Limit the catalog to one source location. |
| One operator family | `--operator NAME` | Limit to certain operators. Repeat to include several. |
| Cap the run | `--max-mutations N` | Run at most `N` catalog entries. |

`--changed` and `--all` can't be combined. If Git can't list the changed files,
vfy uses the Python files you supplied or the ones it measured, and the report
says so.

```console
uv run vfy mutate --operator comparison_boundary src/sign.py:5
uv run vfy mutate --all --source src \
    --operator boolean_operator --format json --pretty-print
```

See the [operator appendix](#mutation-operators) for replacements and limits.

## Review committed pull-request changes

On a clean CI checkout, `--changed` compares against `HEAD`, so it misses commits
already on the PR branch. Fetch the base branch and pass `--base`. On GitHub
Actions, set checkout to `fetch-depth: 0`. In a shallow local checkout, fetch the
missing history first.

<!-- doc-test: mutation-pr-fetch -->
```console
git fetch origin main
```

Collect full coverage on the current checkout, using the
[baseline command above](#run-a-focused-mutation-review). Then run:

<!-- doc-test: mutation-pr-run -->
```console
uv run vfy mutate --base origin/main --source src --fail-on-survivor --format json --output build/mutations.json
```

vfy mutates whole changed files, not just the lines you edited. It doesn't run
tests on the base revision. Staged and unstaged edits, new untracked Python files,
and rename destinations are included. Deleted files are skipped. Test files and
ignored directories are still excluded.

`--base` implies `--changed`, so passing both is allowed. It can't be combined
with `--all` or `PATH:LINE`. If the reference is missing, the merge base is
missing or ambiguous, or Git fails, the command stops before the baseline runs and
before any saved results change. It never falls back to all sources. If the
catalog has no supported operations, it also stops before collection. Fetch the
history you need, or pick a base that shares exactly one common ancestor with
`HEAD`.

JSON records the requested base, both resolved commits, and absolute selected
paths under `scope.requested_base`, `scope.base_commit`,
`scope.merge_base_commit`, and `scope.selected_files`.

## Use vfy on a mutmut project

Install [vfy with pytest support](../getting-started/installation.md) in your
project's test environment. From the project root, use the same source paths,
test selectors, and pytest options as your mutmut run. vfy reads your pytest
configuration; it does not import `[tool.mutmut]` settings.

```console
uv run vfy mutate --engine=pytest-native --all --source src \
    --test-isolation serial -j 4 tests/
```

Replace `src` and `tests/` with your project's mutation scope. vfy applies
mutations without editing your source files. The tools generate different
mutation catalogs. Serial isolation shares fixtures across test files within
each mutation, as mutmut does. Check the [native compatibility limits](native-adoption.md#check-the-supported-scope)
before switching an existing workflow.

## How vfy runs each mutation

Automatic baselines collect coverage for test selection. Report thresholds
apply when your pytest options request coverage or specify a threshold.

vfy validates each replacement before testing it; failed validation is
`invalid`. Changes inside a function or method (*callable-body* mutations) run
the tests whose recorded contexts reached that location. The independent engine
preserves collection order; the optional `--engine=pytest-native` mode ranks tests
by test history.
For measured source files, no recorded context means `unreached`; test names
are not used to guess reachability. With a verified baseline, a supplied source
file excluded by coverage `omit` settings uses the full requested test suite.
Missing measurements for that file cannot establish which tests reach it.

Native mutation runs use Python tracebacks (`--tb=native`) for the clean baseline
and mutation children. Explicit traceback styles and requests for locals, full
tracebacks, or xfail tracebacks take precedence. pytest's failure-reporting hooks
still run before fixture teardown.
To share a passing `vfy test` baseline, use the same explicit traceback style
for both commands, such as `--tb=native`.

On Linux, the independent engine can collect the unchanged suite once and fork
a new child for each eligible body mutation. Each child creates and finalizes its
own fixtures. Admission requires a supported callable, no collection execution
of its body, and an idle runtime with only vfy-owned process resources. A denied
fork, or a callable whose bytecode was inspected or replaced during collection,
runs the selected tests in a fresh interpreter. A body whose selection cannot be
validated, a body executed during collection, and a one-line definition run the
full requested scope in a fresh interpreter.

Selected and forked execution runs only the tests whose coverage reached the
mutated code. A mutant whose effect only a test that never calls it observes,
such as through shared global state, can survive where a full-suite run would
kill it.

A cached independent result is reused only when the collection, selected tests,
source, configuration, arguments, and declared environment inputs are unchanged. Cache lookup still
collects the unchanged suite, but a valid hit does not execute its test bodies.
If collection changes those inputs, vfy executes the mutation without publishing
a reusable result.

Changes to module, class, or function definitions (*import-time* mutations) run
the full requested pytest scope in a fresh process. Line contexts cannot identify
which tests consume a default argument, decorator result, or other import-time effect.
For files included in coverage collection, if the saved data records no executed
line from any test or from collection, no test imported the file, and its
import-time mutations are `unreached`. A package's untested `__main__.py` is a common example.

Some parametrized test IDs change on every collection, for example IDs built
from the current time. When pytest no longer collects a test ID recorded in the
coverage contexts, vfy prints a `Note:`. Each mutation that test reached also runs
every collected test whose ID is not in the contexts. A mutation that needs a
fresh process runs the full requested scope instead. Stable `ids=` values keep
these runs selective and allow their results to be reused from the mutation cache.

### Isolate test files

With `--engine=pytest-native` in a single-threaded POSIX process with `fork()`, vfy
runs each test file in its own process by default:

```console
uv run vfy mutate --engine=pytest-native --all --source src -j 10
```

Each file gets a new fork of the collected session, so session fixtures are
created and finalized separately per file. Changes to Python globals do not
carry between files. Use `--test-isolation serial` when different files need
to share session fixtures or global state:

```console
uv run vfy mutate --engine=pytest-native --all --source src --test-isolation serial -j 10
```

This runs up to ten mutations concurrently. Each mutation's tests execute
serially in one process.

When the caller has additional Python threads or the host lacks POSIX `fork()`,
omitting `--test-isolation` selects `serial`. Explicit `--test-isolation file`
requires a single-threaded POSIX process with `fork()`.

vfy resolves this choice at command entry. Threads started later during project
collection can prevent safe file forks and cause the run to fail. vfy keeps the
chosen isolation mode throughout the run; use `--test-isolation serial` for
suites whose collection starts background threads.

The clean baseline uses the same isolation mode as the mutation tests. Worker
crashes and timeouts count as incomplete results, not as protection. File
isolation does not support `--tb=line`; the baseline gets the same `--tb`
option.

`-j` limits executing tests across mutation children and test-file groups.
In file isolation, that limit also covers fresh pytest supervisors during
collection. The controller's initial collection runs outside the limit.

vfy runs inherited pytest worker defaults serially inside each mutation. Use
`-j` to choose mutation concurrency. An explicit pytest `-n 0` is accepted;
an explicit parallel pytest worker request is rejected.

## Read results and exit statuses

The text report prints a summary and lists every result that needs review:

| Result | Meaning |
| --- | --- |
| `protected` | A selected test failed after the source was changed. |
| `survived` | The selected tests passed with the change in place. Read the replacement. If the behavior matters, add a test that tells the two apart. |
| `unreached` | No recorded test context reaches the operation, or no test imports its module. Add a test that does. |
| `invalid` | The replacement failed validation. |
| `error` | Mutation execution couldn't start, the process returned an unexpected result, or its exit status was unavailable because project code reaped it. |
| `timeout` | A mutation worker or test-file group exceeded its execution watchdog. |
| `unstable` | The clean baseline wasn't verified, so a test failure can't be blamed on the mutation. |

Without `--fail-on-survivor`, the report exits `0` for ordinary results, including
survivors and unreached operations. It exits `1` for `invalid`, `error`,
`timeout`, or `unstable`. With `--fail-on-survivor`, any `survived` result exits
`2`. Infrastructure results still exit `1`.

Set the mutation watchdog limit with `--timeout SECONDS` (default: 30), and
the worker count with `-j/--workers` (`1`, a positive number, `auto`, or `logical`):

```console
uv run vfy mutate --engine=pytest-native --changed --source src \
    --timeout 60 --workers 2 --fail-on-survivor
```

Clean baseline collection has a separate budget of at least five minutes;
increasing `--timeout` above that also raises the baseline budget. Mutation
testing collects private coverage for selection even when `--no-cov` disables
pytest coverage reports. Mutations in files excluded by the coverage settings
run the full requested test scope.

The default `--timeout-policy adaptive` can reduce that limit using clean-test
history. For collected callable-body mutations, the watchdog covers the selected tests
together, not each test separately. vfy sums their recorded `duration_ms`
values from `--history` and calculates the limit in seconds:

```text
min(timeout, max(1, 10 × recorded_milliseconds / 1000 + 0.5))
```

For example, selected tests with 250 ms of recorded duration get **3 seconds**,
even with `--timeout 60`. Increasing the ceiling alone does not increase that
adaptive limit. Missing test durations contribute zero; if the total is zero,
vfy uses the configured ceiling. Fresh-process mutation execution, including
import-time mutations, uses the configured ceiling without this history-based
reduction.

Use `--timeout-policy fixed` to give each mutation the configured `--timeout`
without reducing it based on clean-test history. This is useful when a plausible
mutation makes a quick test substantially slower. The watchdog still rejects a
mutation that exceeds the configured limit:

```console
uv run vfy mutate --engine=pytest-native --all --source src \
    --timeout-policy fixed --timeout 60
```

The policy can also be set with `timeout_policy = "fixed"` under
`[tool.vfy.mutate]`. Changing it invalidates cached mutation results.

## Save JSON and resume interrupted work

Write a machine-readable `vfy.mutation` report for CI or review tools:

```console
uv run vfy mutate --changed --source src \
    --format json --pretty-print --output build/mutations.json
```

The report identifies the exact mutation list and records each mutation's
location, replacement, selected tests, outcome, and timing. With caching enabled,
finished results are saved atomically to `.vfy/mutations.json`; reruns skip
them. Mutation IDs include the source hash, operator, byte range, and replacement,
so edited source cannot reuse an old result.

The verified baseline is `.vfy/mutation-baseline.json`. Changes to source,
tests, pytest configuration, test history, or coverage data invalidate it. Use
`--no-cache` to rule out cached results when investigating a flaky result.

Keep `.vfy/` out of version control.

## Use mutation testing in CI

The [CI guide](ci.md#schedule-mutation-testing) has a workflow: fetch the PR base
history, collect a passing baseline in the same job, and publish the JSON.
`--all` reviews the complete source set; `--base REF` reviews changed files.

## Understand the limits

- A survivor is a review prompt, not proof of a missing test. vfy does not
  infer equivalent replacements. Neither mutation nor [semantic coverage](semantic.md)
  proves behavior for untested inputs.
- The clean baseline must match the mutation run's source, tests, pytest
  arguments, configuration, runtime, and coverage data. Any change to them
  invalidates the baseline and the cache.
- `unreached`, `timeout`, `error`, and `unstable` results mean vfy couldn't
  tell whether the tests detect the change.
- Requires CPython 3.12+.

## Troubleshoot a mutation run

- **No test contexts:** collect with `vfy test --all --cov-context=test`, then
  rerun the mutation command.
- **The baseline was rejected:** run the passing full baseline again after you
  change the source, tests, pytest configuration, runtime, or coverage data.
- **A mutation is `unreached`:** check the location with
  `vfy explain PATH:LINE`, then add or run a test that reaches it.
- **A mutation times out:** inspect the replacement and selected tests for a
  hang or expected slowdown. Check their recorded durations in `--history` and
  the [adaptive watchdog](#read-results-and-exit-statuses); raising `--timeout`
  may leave the effective limit unchanged. Use `--timeout-policy fixed` to apply
  the configured limit directly. Recollect current clean-test timings
  when the history is stale. Keep timeout results visible in CI; they do not
  establish that an assertion detected the mutation.
- **The catalog is too large:** use `--changed`, a `PATH:LINE`, `--operator`, or
  `--max-mutations`.

For baseline and selection rules, see [Test selection](test-selection.md).

<a id="mutation-operators"></a>

## Appendix: mutation operators

Each operator replaces one source range. The table lists the change and the
limits to consider when reviewing its result.

| Operator | Replacement | Review limits |
| --- | --- | --- |
| `comparison_negation` | `== ↔ !=`, `< ↔ >=`, `<= ↔ >`, `> ↔ <=`, `is ↔ is not`, `in ↔ not in` | Comparisons inside excluded pragma regions; parser-invalid replacements |
| `comparison_chain` | The second and later operators in a chained comparison use the same negation pairs as `comparison_negation` | Only non-first links change; the parser does not synthesize missing chain operands |
| `comparison_boundary` | `< ↔ <=`, `> ↔ >=` | Survivors can reflect inputs that never reach the boundary |
| `boolean_operator` | `and ↔ or` | Side-effectful operands can make an equivalent or order-sensitive mutant |
| `remove_not` | `not expr → expr` | Truthiness conversions remain the test's responsibility |
| `binary_operator` | `+ → -`, `- → +`, `* → /`, `/ → *`, `% → *`, `// → /` | Matrix, numeric-overflow, and type-specific equivalent cases are not inferred |
| `boolean_flip` | `True ↔ False` | Callable-body constants with no recorded test context are `unreached` |
| `integer_plus_one` | Integer literal `n → n + 1` | Only decimal literals parsed as signed 128-bit integers are eligible; floats, complex, hex, octal, binary, and increment overflow are excluded |
| `return_none` | `return expr → return None` | Literal Boolean, numeric, and `None` returns are excluded. An expression that evaluates to `None` can give an equivalent replacement. |
| `remove_call` | Standalone call → `pass` | Calls whose return value is used are not rewritten |
| `raise_to_pass` | `raise ... → pass` | A raise in module or class initialization needs a fresh-process run |
| `break_to_continue` | `break → continue` | Only explicit loop-control statements are considered |
| `continue_to_break` | `continue → break` | Only explicit loop-control statements are considered |

Syntax-invalid input produces no mutation catalog. vfy validates each
replacement before running it. A setup failure never counts as a protected
mutant. Check survivors for equivalence before adding tests.
