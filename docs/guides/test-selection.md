# How vfy selects tests

vfy uses saved coverage and static imports to select the tests affected by your
edits. When it cannot select a subset, it runs the full requested scope and
prints the reason.

The default native engine selects tests in unfiltered `safe` coverage runs
without installed pytest. It checks the collected tests against the baseline and
runs the full requested scope if they differ. When every edit stays inside
function bodies (rule 1 below), it collects only the test files that contain
selected tests; see [scoped collection](#scoped-collection). Independent `--watch`
runs, explicit selectors, and other policies run the full requested scope. The
`pytest-native` and `pytest` engines require `vfy[pytest]`. See
[external inputs](#what-selection-does-not-promise) for what recorded coverage
cannot see.

## How selection decides

vfy compares your working tree with a *passing baseline*: a passing full run on
a clean Git checkout. It finds your edits with `git diff HEAD` and `git status`,
so uncommitted changes count. Git paths resolve from the repository root, including
when the project runs from a subdirectory. Quoted Git filenames retain Unicode
and escaped characters. For each changed Python file:

1. **Eligible edits only inside function bodies.** vfy runs the tests that ran those
   functions. Line numbers for the rest of the file shift, so the saved coverage
   still lines up.
2. **Any other Python edit.** That covers module statements, class bodies,
   `def` lines, decorators, docstrings, and edits vfy can't map to a
   function. vfy runs every test that ran any line of the file, plus tests
   whose test file, or a file they cover, imports the module directly or
   indirectly, including relative imports in package initializers. Imports come
   from Ruff's Python parser, so `importlib` and
   `__import__` are not seen.
3. **The baseline or changed files cannot support either rule.** vfy runs
   the full requested scope and prints `Note: <reason>`. See
   [when vfy runs the full suite](#when-vfy-runs-the-full-suite).

Rule 2 also applies when a body runs during import, collection, or fixture setup
or teardown; uses a decorator outside the supported list; or changes whether
the function is a generator. If the baseline includes decision coverage, vfy
selects by file and imports rather than narrowing to edited functions.

After a passing selected run, vfy combines fresh coverage with saved coverage
for skipped tests, mapping line shifts in edited bodies. It also retains global
coverage outside those bodies, including helpers called only while collecting
a skipped test module. Skipped tests are not
rerun: agreement with a full run depends on unchanged external inputs and test
state. Independent selection preserves project collection order; the installed
pytest routes rank selected tests by recorded duration. Independent selection
also reruns every test that recorded no coverage context in the baseline, such
as a test skipped during setup or one that executes measured code only in an
unmeasured subprocess. Without a context, vfy cannot prove that a body edit
leaves the test unaffected. A failed selected run keeps the passing baseline;
its coverage reports and `--cov-fail-under` check describe the selected run
only, and vfy does not retry the full scope. If vfy cannot record the coverage of
a selected run, it prints why, writes no coverage reports, and exits with
status 1.

<a id="scoped-collection"></a>

### Collect selected test files

Collection is scoped when every edit follows rule 1, no recorded import,
collection, or fixture context ran the edited code, and test files and
`conftest.py` files are unchanged. The independent engine then collects only
the test files that contain selected tests and keeps the baseline's test IDs for
the rest, so those modules are neither imported nor rewritten. Files containing
tests without coverage contexts remain in the collection. Omitted modules' side
effects do not run, as when you pass test files to pytest directly.
`--plan-only` always collects the complete suite.

vfy compares the collected files with the baseline. If they produce different
test IDs, for example from parametrization that reads an ignored file, vfy prints
`Collecting the complete suite to bind the selection` and repeats the run with
complete collection before any test runs. Omitted files are not collected, so a
change to their test IDs that is not a Git change is not detected. Run
`vfy test --all` after changing such inputs.

## Create a baseline

First [check your suite's compatibility](native-adoption.md). From your project
root, add `.vfy/` to `.gitignore` and commit that change. From a clean checkout, run
every test:

```console
uv run vfy test --engine=native --all
```

The baseline saves `.vfy/replay.coverage` (the lines each test ran, including
fixture setup and teardown), `.vfy/test-history.sqlite3` (durations), and an
import graph cache. A failed or interrupted run keeps the last baseline. If you
commit, `HEAD` moves and the next `vfy test` runs the full suite.

## Run after an edit

Keep the same source, coverage, and worker settings as the baseline run:

```console
uv run vfy test --engine=native
```

vfy prints the changed files and the selection. Numbers vary; this output is
illustrative:

```text
Changed code: /path/to/project/src/bounds.py
Affected code: 1 functions, 0 branches
Selected 1 / 2 tests (from the saved baseline; collection follows)
`vfy test --all` runs everything
Estimated selected suite: 0.00s
Estimated full suite: 0.00s
...test output...
Reused 1 tests; complete coverage reconstructed from passing baseline 1a2b3c4d
```

vfy prints the selection before pytest starts, using the saved baseline's test
inventory. If independent collection then selects different tests, vfy prints
the selection it applied, marked `(after collection)`, once the run ends. It
also reports any reasons for a full run that first arose during collection.
The selection denominator includes the collected tests and, under scoped
collection, the baseline IDs of files it did not collect. The installed pytest
routes can collect fewer tests because vfy skips unaffected files before collection.
The reuse summary follows the completed run and counts saved results used to
reconstruct coverage.
The coverage report and `--cov-fail-under` check use the reconstructed complete
coverage and print once, in pytest's terminal summary before the final result
line, as `pytest --cov` does. After a failing selected run, they describe only
the coverage that run measured.

Test failures keep a failing exit status. `--quiet` hides these messages. If
nothing changed, the installed pytest routes restore complete coverage without
running tests; independent execution runs the requested scope.

## When vfy runs the full suite

vfy prints a `Note:` line with the reason:

| What happened | Why a subset isn't safe |
| --- | --- |
| No passing baseline, or `HEAD` moved since it was recorded | There is no per-test coverage for the code you are on |
| Configuration, Python, installed packages or plug-ins, pytest arguments, or worker settings changed | Any test's behavior may have changed |
| A `conftest.py`, a test file (`test_*.py`, `*_test.py`), or anything under `tests/` changed | Which tests exist, or what their fixtures do, may have changed |
| A file is untracked, deleted, or renamed | The baseline can't map it to recorded tests |
| A Git submodule's recorded commit changed, or the submodule is uninitialized, has uncommitted changes, or is checked out at another commit | vfy identifies a submodule only by the commit the repository records for it |
| A changed file has no added executable Python line, such as a non-Python file or a comment or deletion-only edit outside a function | No recorded line ties the change to a test |
| No recorded test reaches the changed code | An empty selection would test nothing |
| The import graph can't be built or resolved for an affected file | Import-based selection can't be checked |
| Coverage contexts in the baseline do not map to collected test identities, such as a static `context =` in `.coveragerc` | Recorded lines cannot be attributed to individual tests |
| Independent collection produces different test identities from the baseline | The saved selection does not cover the current inventory |
| Independent collection contains repeated display identities | Individual cases cannot be selected unambiguously by their recorded names |
| You pass a pytest selector (`-k`, `-m`, `--lf`, `--ff`, `--sw`, `--deselect`, `--ignore`, or a file or node ID) on the command line or in `PYTEST_ADDOPTS` | You chose the tests; pytest applies your selector instead |
| The pytest configuration's `addopts` contains `--lf`, `--ff`, `--nf`, `--sw`, or `--collect-only` | These options depend on cached results or skip execution |
| You pass `--no-cov` | vfy can't record or rebuild per-test coverage |
| Git is unavailable | vfy can't see what changed |

`-k`, `-m`, `--deselect`, `--ignore`, `--ignore-glob`, and file targets in the
pytest configuration's `addopts` define the suite. For example, with
`addopts = -m "not slow"`, `vfy test --engine=pytest-native --all` records a baseline
without the slow tests, and later runs select from that suite. A change to `addopts` invalidates
the baseline.

`--cov-append` runs the full requested suite and appends to existing coverage.

In CI, run `uv run vfy test --engine=pytest-native --all` for a complete result. A restored `.vfy/`
baseline for the same commit would otherwise reuse the saved result without
running tests.

For example, editing a test file as well as `src/bounds.py` prints (abridged):

```text
Running the full suite
Known tests before collection: 2
Note: test discovery or fixtures changed
```

<a id="explain"></a>

## See which tests reach a line

`vfy explain` reads the current coverage file (normally `.coverage`) and
runs no tests. When matching parallel shards exist, it combines them into that
file and removes the consumed shards. Record per-test contexts first:
`vfy test` adds them automatically; direct pytest collection needs `--cov-context=test`. Explain
describes that recording at its saved line numbers, without checking Git or
baseline freshness, so edits can make the answer stale. It lists matching tests
fastest first:

```console
uv run vfy explain src/bounds.py:7
```

Example output:

```text
Tests reaching src/bounds.py:7: 1
Estimated selected suite: 0.00s
Estimated full suite: 0.00s
  tests/test_bounds.py::test_positive_value  0.001s
```

`uv run vfy test --engine=pytest-native src/bounds.py:7` runs the fastest of those tests. For a line in a
decision recorded with `--cov-decisions`, explain also prints
`MC/DC observation pairs`. `--json` returns `location`, `tests`, `selection`,
and `semantic_evidence` for scripts.

The selected duration estimates the fastest matching test; the full duration
sums the matching tests, rather than the project suite. Explain describes
historical reachability, not the reasons for edit selection. It fails with
`no historical test reaches PATH:LINE` when neither test reachability nor semantic
evidence is recorded. Pairs of recorded condition inputs can exist without test contexts.

## Selection in CI

A fresh CI checkout has no local baseline, so it runs the full suite unless
`--baseline PATH` supplies a compatible ancestor baseline. Export one with
`--all --export-baseline PATH`. See the [CI recipes](ci.md#ci-selection-options)
for importing, planning, and auditing.

Independent execution supports local plan inspection without installed pytest:

```console
uv run vfy test --engine=native --plan-only --plan-output plan.json
```

This collects tests and writes the finalized plan without running test bodies
or replacing saved coverage or history. Portable baseline import and export,
selection audits, and failure confirmation require `pytest-native` or `pytest`.

Selection can be slower than a full run for cheap tests or broad changes. Compare
whole-command times with `uv run vfy test --engine=pytest-native --all`; the duration estimates leave out
collection, baseline checks, and coverage rebuilding.

## Selection policies

`safe` is the default and uses the rules above. On the installed pytest routes,
`balanced` and `aggressive` are Alpha. They select by changed line instead of changed file, so they can skip
affected tests:

| Policy | Selects | Safeguards skipped |
| --- | --- | --- |
| **safe** (default) | Eligible body-only edits: recorded callers. Otherwise: every test that ran the file, plus import dependents | None |
| **balanced** | Tests that ran an added or modified line, plus import dependents | The no-executable-line and test-discovery fallbacks |
| **aggressive** | Tests that ran an added or modified line | The same fallbacks, and import analysis |

Every policy runs the full suite if its selection is empty. Set the policy with
`[tool.vfy.test].selection`. `--watch` accepts only `safe`. The
[edit-loop benchmark](../about/benchmarks.md#how-to-read-benchmark-evidence)
compares selected tests and rebuilt coverage with full runs.

<a id="what-selection-does-not-promise"></a>

## External inputs

Selection uses recorded data. It cannot see changes to external services,
generated files, clocks, or nondeterministic fixtures. It also cannot see a value
that one test computes and a later test reads from shared state, such as a
module-level cache filled by an edited function. Use `uv run vfy test --engine=pytest-native --all` when
those inputs change.
