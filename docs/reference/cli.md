# CLI reference

The `vfy` command-line interface: global options, commands, options, defaults,
and exit statuses. Run `uv run vfy COMMAND --help` in an installed project
environment for command-specific help.

Watch mode, selection policies, CI baseline options, semantic checking, and
mutation testing are Alpha.

```text
vfy [GLOBAL_OPTIONS] COMMAND ...
```

| Global option | Meaning |
| --- | --- |
| `-V`, `--version` | Print `vfy VERSION` |
| `--config PATH` | Use this vfy configuration file |
| `--isolated` | Ignore configuration files and environment-selected config |
| `--color [auto\|always\|never]` | Colored output (default: `auto`). `--color` without a value means `always`. See [environment variables](configuration.md#user-facing-variables) for `NO_COLOR` and `FORCE_COLOR`. |
| `-v`, `--verbose` | Write the configuration source and effective execution settings to stderr. Repeating the option has no further effect. |
| `-q`, `--quiet` | Show only requested output; reports and errors still appear |

`--quiet` and `--verbose` cannot be combined. Verbose output does not change
reports or where they go. To control pytest's output, pass pytest arguments,
such as `vfy --quiet test -q`.

| Command | Use it to |
| --- | --- |
| `test` | Run tests with coverage by default, selecting affected tests for safe, unfiltered runs. |
| `explain` | List the tests that reach `PATH:LINE`, from saved coverage |
| `coverage` | Collect, report, and check coverage; manage saved data |
| `mutate` | Run mutation analysis |
| `config` | Show or validate resolved configuration |
| `diagnostics` | Print runtime and integration diagnostics |
| `cache` | Inspect or clean the project cache |

For config file selection, environment variables, and overrides, see
[configuration discovery and precedence](configuration.md#discovery-and-precedence).

## `vfy test`

```text
vfy test [--engine native|pytest-native|pytest]
        [--data-file PATH] [--rcfile PATH] [--all] [--watch] [--no-replay]
        [--history PATH] [(-n|-j|--workers) (N|auto|logical)]
        [--source PATH]
        [--native-scheduling file|scope|cases|striped|demand]
        [--baseline PATH] [--export-baseline PATH] [--plan-output PATH]
        [--plan-only] [--audit-selection] [--confirm-failures]
        [TARGET ...] [PYTEST_ARGS ...]
```

`vfy test` runs tests with coverage. With `--no-cov`, it runs them without
collecting coverage. Such runs leave saved coverage untouched, skip coverage
database reads and replay preparation, and still record test outcomes and
durations.

The default native engine selects affected tests for unfiltered `safe`
coverage runs. Eligible function-body edits use
[scoped collection](../guides/test-selection.md#scoped-collection). A test
inventory change found during collection triggers a full run. This engine also
supports `--plan-only` and
`--plan-output`. Other independent workflows, including
`--no-cov`, `--cov-append`, explicit selectors, watch, and other selection policies, run the full
requested scope. The `pytest-native` and `pytest` routes require `vfy[pytest]`.

| Execution condition | Behavior |
| --- | --- |
| Valid passing baseline for current `HEAD` | Select tests affected by uncommitted changes using the configured policy (`safe` by default). A passing selected run merges fresh coverage; a failed selected run preserves the baseline. |
| No changes with a valid baseline | Independent execution runs the requested scope. The `pytest-native` and `pytest` routes restore complete coverage without running tests. |
| `--all` | Run every test in the requested scope; a passing clean-checkout run replaces the baseline. Uses `safe` selection regardless of configured policy and rejects pytest selection controls such as `-k`, `-m`, file/node targets, and `PATH:LINE` on the command line or in `PYTEST_ADDOPTS`. Static selectors in configured `addopts` define the suite instead. |
| No valid baseline or a safety fallback | Run the full requested scope and print a `Note:` with the reason. |
| `PATH:LINE` target | With `pytest-native` or `pytest`, run the fastest recorded test reaching the one-based source line; `explain` lists all matching tests. |
| `balanced` or `aggressive` selection | With `pytest-native` or `pytest`, select by changed line with fewer safeguards; either policy can skip affected tests. |

Selectors supplied through `-o addopts=...` also count as command-line controls.

A failed selected run prints `Passing baseline preserved because the selected run
failed`. An unchanged `pytest-native` or `pytest` run prints `No code changed; complete
coverage restored without running pytest`.

Safety fallbacks include `--no-cov`, pytest selectors or explicit file/node
ID targets, changed invocation or settings, unavailable Git
state, and untracked, deleted, renamed, test, or `conftest.py` changes. Changing
the interpreter optimization level (`-O`, `-OO`, or `PYTHONOPTIMIZE`) invalidates
both local replay and portable CI baselines. `--cov-append` runs the full
requested suite and preserves existing coverage. See the complete
[fallback conditions](../guides/test-selection.md#when-vfy-runs-the-full-suite)
and [selection policies](../guides/test-selection.md#selection-policies).

| Test option | Default and effect |
| --- | --- |
| `--engine native\|pytest-native\|pytest` | Defaults to `native`, regardless of installed pytest. It supports affected-test selection for unfiltered `safe` coverage runs, plan inspection, selectors, history, watch, and vfy workers. `--baseline`, `--export-baseline`, `--audit-selection`, and `--confirm-failures` require explicit `pytest-native` or `pytest` execution and `vfy[pytest]`. `vfy mutate` accepts `native` (default) or `pytest-native`. Native mutations require Linux, one worker, and serial isolation. |
| `--data-file PATH`, `--rcfile PATH` | Data defaults to `.coverage` or `COVERAGE_FILE`. vfy settings come from `--config` or [discovery](configuration.md#discovery-and-precedence); `--rcfile` names the pytest and Coverage.py configuration file for the run. |
| `--history PATH` | `.vfy/test-history.sqlite3`, unless configured; stores durations and outcomes. See [history storage](configuration.md#test-history) for SQLite and JSON choices. |
| `--source PATH` | Repeatable source roots for affected-test analysis. Uses test configuration, then coverage run source. |
| `--no-replay` | Disable baseline replay; incompatible with baseline import. |

Coverage-enabled `vfy test` supplies `--cov-context=test` unless explicitly
configured. Line targets and test reachability in `explain` need saved per-test contexts;
ordinary line coverage alone cannot identify test node IDs. Pairs of semantic observations
can still appear in `explain` without per-test contexts.

`vfy test` options and pytest options can be interleaved, with no `--` separator.
Global vfy options such as `--config` go before `test`. The independent engine
reads configuration, collects tests, and rewrites asserts itself. The explicit
`pytest-native` and `pytest` routes use installed pytest for these operations.
The independent engine's `--version` or `-V` response and session header identify
the actual vfy version. Explicit `pytest-native` and `pytest` routes retain installed
pytest's version response and header. The independent pytest API exposes its
compatibility version separately for plug-ins.
Unsupported options fail before execution. See
[Compatibility](../about/compatibility.md). `vfy test` returns pytest's exit
status.

Pytest 8.2+ argument files (`@args.txt`) use one argument per line. Nested
paths resolve from the invocation directory. Expansion is limited to 64 levels,
1,024 files, 8 MiB, and 100,000 arguments.

If a selected test ID no longer exists at collection, the run fails before test
bodies start and keeps the passing baseline.

JUnit output replaces its target atomically. The target's parent directory must
be writable, including when the target is a symlink.

### Pytest coverage options

`vfy test` accepts these coverage controls through the selected engine.
Stock pytest uses the explicitly loaded `vfy.pytest_plugin`. See
[collection](../guides/collection.md) for plug-in loading and runnable workflows.

`vfy test --help` summarizes common coverage options and shows an example.
With the optional `vfy[pytest]` extra installed, get stock pytest and coverage option help with
`uv run python -m pytest -p no:pytest_cov -p vfy.pytest_plugin --help`.
vfy supports the subset described in [Compatibility](../about/compatibility.md).

| Option or fixture | What it does |
| --- | --- |
| `--cov[=SOURCE]` | Adds a path or an importable package or module to measure. Repeat it for more. `--cov=` with an empty value disables source filtering. |
| `--cov-reset` | Clears the sources added earlier on the command line |
| `--cov-report=TYPE[:DEST]` | Repeatable `term`, `term-missing`, `annotate`, `html`, `xml`, `json`, `markdown`, `markdown-append`, or `lcov` reports |
| `--cov-report=` | Saves coverage data without writing a report |
| `--cov-config=PATH` | Uses this Coverage.py-style configuration file (default: `.coveragerc`) |
| `--cov-branch` | Measures branch coverage |
| `--cov-append` | Appends to existing coverage data; disables selected-run replay |
| `--cov-decisions` | Collects decision, condition, and MC/DC data; implies branch coverage |
| `--cov-decision-report=TYPE[:DEST]` | `term` (default) or `json[:PATH]`; JSON defaults to `vfy-decisions.json` |
| `--cov-fail-under-decisions=MIN`, `--cov-fail-under-conditions=MIN`, `--cov-fail-under-mcdc=MIN` | Independent semantic percentage gates |
| `--cov-fail-under=MIN` | Makes pytest exit with status 1 if coverage is below `MIN` |
| `--cov-precision=N` | Sets rounding for the threshold and the number of decimals in the terminal report |
| `--cov-context=test` | Labels each test's `nodeid|setup`, `nodeid|run`, and `nodeid|teardown` phases |
| `--no-cov-on-fail` | Skips the report when tests fail |
| `--no-cov` | Turns off coverage for a run that has it configured |
| `cov` fixture | Gives you the active `vfy.Coverage` object |
| `no_cover` fixture | Leaves the test's call phase out of coverage |
| `@pytest.mark.no_cover` | Leaves the marked test's call phase out of coverage |

Without `--cov-report`, vfy writes the terminal report. `term` and `term-missing`
accept `:skip-covered`. File reports accept a destination after `:`, and Markdown goes
to `coverage.md` by default. `markdown` is a vfy addition. Machine-readable formats
can differ from pytest-cov in the ways listed in
[Compatibility](../about/compatibility.md#analysis-and-reports).

### Workers

`-n`, `-j`, and `--workers` set the number of test workers. Each accepts an
integer from 1 to 256, `auto`, or `logical`. Execution defaults to one worker,
except with `--engine=pytest`, which uses automatic parallelism when pytest-xdist is installed.
`-n 0` runs serially. The native engine requires pytest-xdist for this mode;
the independent engine accepts it without pytest-xdist. These aliases also work
after pytest targets and flags.

Each worker has its own fixture lifetimes. With `--engine=pytest-native`, runs collect
once in the controller and fork their workers when they can, including runs with coverage and the
default `--capture=fd`. Other runs start fresh workers (see
[native worker start](configuration.md#native-worker-start)).

With `--engine=pytest-native` and pytest-xdist 3.8.0 installed, pytest's
`--numprocesses` and `--dist` use
upstream xdist scheduling, with native execution inside the workers. Combining
xdist with native file-group capacity above one is rejected.
`--stepwise` requires one native worker and does not load xdist.

`--native-scheduling` chooses how the shared Rust scheduler assigns tests to
native or independent workers:

| Value | Assignment |
| --- | --- |
| `file` (native default) | Each file runs in one worker. Use this when tests in a file share state. |
| `scope` | Each test class, and each file's module-level functions, runs in one worker. Matches xdist `loadscope`. |
| `demand` (native parallel default) | Workers claim another case batch as they finish. |
| `cases` | Fixed contiguous case ranges. Can split one parametrized family across workers. |
| `striped` | Cases interleaved across workers. Each worker keeps collection order. |

Native capacity is limited to 256 workers and capped by file groups or cases,
depending on the scheduler. Unknown test durations use the upper-middle recorded
duration; without history, tests have equal weight.

File and scope scheduling balance workers by the durations in `--history`.
Each worker initializes and finalizes its own fixtures. `demand`, `cases`, and `striped`
can initialize a file's module, class, package, or session fixtures in several workers;
use `file` when tests share module state. Independent workers collect in fresh
interpreters before assignment. Every requested independent worker starts
together; a worker left without tests finishes a collect-only session. Serial
independent runs retain collection order. The optional `pytest-native` engine requires
a collected fork for `demand` scheduling.

These scheduling options cannot be used with xdist. Native file-group
execution with file or live CLI logging requires one worker, and automatic worker
selection runs serially for these options. See
[Workers](../guides/run-tests.md#workers) for capacity and process
behavior.

### Watch mode

`--watch` runs immediately, then starts a fresh test process after source changes
settle. An obsolete run keeps the last accepted coverage and reports. A failed or
obsolete run needs a full recovery run before selection resumes. `--all --watch`
always runs the full scope. Watch rejects node IDs, `PATH:LINE` targets, and the
`balanced` and `aggressive` policies. Ctrl-C exits with status `2`. See
[Rerun on save](../guides/run-tests.md#rerun-on-save).

<a id="ci-selection-options"></a>

### CI selection controls

| Option | Behavior |
| --- | --- |
| `--confirm-failures` | Rerun failed tests in a fresh serial process. The primary result, coverage, and reports are kept. |
| `--export-baseline PATH` | Export a passing full run as a portable CI baseline. Requires `--all`. |
| `--baseline PATH` | Import a compatible ancestor baseline and select affected tests. An incompatible input causes a full run. |
| `--plan-output PATH` | Save the finalized selection plan. |
| `--plan-only` | Build and finalize a plan without running tests. Writes to stdout unless `--plan-output` is set. |
| `--audit-selection` | Run the full scope and check whether the plan skipped a failing test. |

`--plan-only` and `--plan-output` also work with the independent engine without
installed pytest. The other options require explicit `--engine=pytest-native` or
`--engine=pytest` execution with `vfy[pytest]` installed. These controls support only the
`safe` policy and cannot be combined with `--watch`. `--audit-selection` and
`--plan-only` are mutually exclusive. Baseline import cannot be combined with
`--all` or `--no-replay`. `--export-baseline` cannot be combined with
`--baseline` or `--plan-only`. `--cov-append` is not supported. See
[CI selection](../guides/ci.md#ci-selection-options).

## `vfy explain`

```text
vfy explain [--data-file PATH] [--history PATH] [--json] PATH:LINE
```

Lists tests that reached a line in saved per-test coverage, without running tests.
Collect test reachability with `uv run vfy test --all` (which supplies
`--cov-context=test`). Pairs of semantic observations can be reported without test
contexts. It reports reachability and
recorded durations, not dependency paths.
`PATH:LINE` is a source path and a one-based line number, such as
`src/parser.py:42`.

Output starts with `Tests reaching PATH:LINE: N`. Then come the estimated
durations of the selected and full suites, when recorded, and one line per test,
fastest first, with its recorded duration. The selected estimate describes the
fastest reaching test; the full estimate covers all matching tests. If the line
is part of a decision
recorded with `--cov-decisions`, a `MC/DC observation pairs: N pair(s)` block follows,
with one `condition N:` line per pair of observations. Condition numbers start
at one. For example, the [bounds tutorial](../guides/semantic.md#fix-a-gap)
records this pair for `x >= 0` before the upper-bound test is added:

```text
MC/DC observation pairs: 1 pair(s)
  condition 1: test_bounds.py::test_below_lower_bound|run (0-) -> test_bounds.py::test_inside_bounds|run (11)
```

Context labels can also be application labels rather than test IDs.

`--json` writes an object with `location`, `tests` (each with `nodeid` and
`duration_ms`), `selection` (`reasons`, `affected_files`, `affected_functions`,
and `affected_branches`), and `semantic_evidence`. The text output omits
`selection`.

- `--data-file PATH`: coverage data to read (default: `.coverage`, or
  `$COVERAGE_FILE`). No data file or matching shards is an error.
- `--history PATH`: test history with recorded durations (default: the
  `history` key under `[tool.vfy.test]`, which defaults to
  `.vfy/test-history.sqlite3`).
- `--json`: write JSON instead of text.

Loading saved coverage combines discovered shards into the selected data file
and removes consumed shards after a successful replacement. It can therefore
change saved data even though it runs no tests.

If the line has neither recorded test reachability nor pairs of semantic
observations, the command fails with
`no historical test reaches PATH:LINE`. See
[How vfy selects tests](../guides/test-selection.md#explain).

## `vfy coverage`

```text
vfy coverage run [OPTIONS] TARGET [ARGUMENTS...]
vfy coverage report [OPTIONS]
vfy coverage html [OPTIONS]
vfy coverage json [OPTIONS]
vfy coverage xml [OPTIONS]
vfy coverage lcov [OPTIONS]
vfy coverage annotate [OPTIONS]
vfy coverage combine [OPTIONS] [PATH...]
vfy coverage erase [OPTIONS]
vfy coverage debug data [OPTIONS]
vfy coverage check [--strict] [OPTIONS]
```

These commands use Coverage.py-compatible data, configuration, output formats,
schema-7 files, path aliases, and report thresholds. Semantic checking is a
vfy extension. vfy does not install or shadow a `coverage` executable or
package.

Output goes to the command's output option, the configured destination, or the
command's default. `-` means stdout. Diagnostics and confirmations go to stderr.

`coverage run` accepts `-a/--append`, `--branch`, `--decisions`,
`-p/--parallel-mode`, `-m/--module`, repeatable `--source`, `--include`, and
`--omit`, `--context`, `--data-file`, and `--rcfile`. `--decisions` implies
`--branch`. `--append` loads existing data before the target runs.
`--parallel-mode` writes a shard. The exit status is the target's.

| Command | Options in addition to `--data-file` and `--rcfile` |
| --- | --- |
| `report` | `--include`, `--omit`, `--contexts`, `--precision`, `--fail-under`, `--ignore-errors`, `-m/--show-missing`, `--skip-covered/--no-skip-covered`, `--skip-empty/--no-skip-empty`, `--sort=COLUMN`, `--format text / markdown / total` |
| `json` | `--include`, `--omit`, `--contexts`, `--precision`, `--fail-under`, `--ignore-errors`, `-o/--output`, `--pretty-print`, `--show-contexts`, `-q/--quiet` |
| `xml`, `lcov` | `--include`, `--omit`, `--contexts`, `--precision`, `--fail-under`, `--ignore-errors`, `-o/--output`, `-q/--quiet` |
| `html` | `--include`, `--omit`, `--contexts`, `--precision`, `--fail-under`, `--ignore-errors`, `-d/--directory`, `--title`, `--skip-covered/--no-skip-covered`, `--skip-empty/--no-skip-empty`, `-q/--quiet` |
| `annotate` | `--include`, `--omit`, `--contexts`, `--precision`, `--ignore-errors`, `-d/--directory` |
| `combine` | `--keep`, `--strict`, and optional shard files or directories |
| `erase` | `--parallel` to also remove discovered shards |
| `debug data` | No additional options. Prints `path`, `schema` (always `7`), `has_arcs`, `files` (the file count), and the line count of each measured file. |

As in Coverage.py, `-q/--quiet` after `html`, `json`, `xml`, `lcov`, or `combine`
has the same effect as the global `--quiet`.

Report commands load saved coverage by combining discovered shards into the
selected data file and removing consumed shards after successful replacement.
`report` writes to stdout. `combine` is transactional and honors configured
`[paths]` aliases. `erase` removes the selected data file and, with `--parallel`,
its shards.

`report --skip-covered` and `--skip-empty` hide file rows while preserving
the aggregate total and `--fail-under` result in each output format.
Sort columns are `name`, `stmts`, `miss`, `branch`, `brpart`, and `cover`.
Use `--sort=-cover` to list the highest coverage first.

`--fail-under` compares the total rounded to `--precision`. At the default
precision of zero, 79.66% passes `--fail-under=80`. A threshold of 100 requires
every line and branch to be covered.

## `vfy coverage check`

```text
vfy coverage check [--strict] [OPTIONS]
```

Checks decision, atomic-condition, and masking MC/DC evidence from a
decision-enabled run. It accepts `--data-file`, plus
repeatable `--include`, `--omit`, `--contexts`, `--source`, and
`--exclude-decisions`; `--format text|json`, `-o/--output`, `--pretty-print`, and
`--history`; and the independent thresholds `--fail-under-decisions`,
`--fail-under-conditions`, and `--fail-under-mcdc`.

`--history` defaults to `.vfy/test-history.sqlite3`; `[tool.vfy.test]` `history` does not apply.
`--strict` sets each threshold you do not specify to 100%. Explicit thresholds
take precedence. Missing or stale data exits with status `1`. An unmet threshold
exits with status `2`. The command does not run tests or change source code.

## `vfy mutate`

```text
vfy mutate [OPTIONS] [PATH:LINE] [PYTEST_ARGS ...]
```

Mutation testing applies one supported operation at a time to changed Python
files by default. It first checks that the baseline passes, then runs tests
against each replacement. Callable-body mutations use recorded test reachability.
Import-time mutations run the full requested pytest scope in fresh processes.
Results are saved as they finish by atomically replacing `.vfy/mutations.json`.
The checkout is never modified.

| Option | Default and effect |
| --- | --- |
| `--changed`, `--all`, `--base REF` | Changed files by default; choose one scope. `--base` selects current files changed since the merge base. |
| `--engine native\|pytest-native` | `native` by default; `pytest-native` requires `vfy[pytest]`. |
| `--source PATH`, `--operator NAME` | Repeatable source roots or operator filters. |
| `--history PATH` | `.vfy/test-history.sqlite3`, unless configured. |
| `--max-mutations N` | `0` means no limit. |
| `--timeout SECONDS` | Positive finite mutation timeout limit in seconds; `30` unless configured. The adaptive policy can lower the effective limit; see the [timeout calculation](../guides/mutation.md#read-results-and-exit-statuses). |
| `--timeout-policy adaptive\|fixed` | `adaptive` (default) reduces collected-mutation limits using recorded clean-test durations. `fixed` uses the configured timeout directly. |
| `-j`, `--workers N\|auto\|logical` | Independent execution defaults to `1` and requires one worker. Explicit `--engine=pytest-native` defaults to `auto` unless configured. |
| `--test-isolation serial\|file` | Independent execution uses `serial` and rejects `file`. With explicit `--engine=pytest-native`, defaults to `file` for a single-threaded POSIX process with `fork()`; otherwise `serial`. `file` forks test files under the shared worker limit. The collected backend admits up to three fresh pytest supervisors, each collecting under one slot of that limit, reserving an outer scheduling position for body mutations where capacity permits; see [file isolation](../guides/mutation.md#isolate-test-files). |
| `--no-cache` | Skip the mutation cache. |
| `--format text\|json`, `-o/--output PATH`, `--pretty-print` | Text to stdout by default; `-` also selects stdout. |
| `--fail-on-survivor` | Exit `2` when a mutation survives. |
| `--data-file PATH`, `--rcfile PATH` | Select coverage data and the pytest and Coverage.py configuration file passed to test runs. vfy settings come from `--config`. |

An optional `PATH:LINE` narrows the catalog. Remaining arguments go to pytest.
Fresh-process mutations run the selected engine in a direct child; timeout and
interruption cleanup covers its process group and registered test-file groups. See
[file isolation](../guides/mutation.md#isolate-test-files) for fixture boundaries
and the shared worker budget.

File isolation creates session fixtures separately for each test file; changes
to Python globals do not carry between files. Select `--test-isolation serial`
when files need shared session state. An explicit `--test-isolation file`
requires a single-threaded POSIX process with `fork()`; only an omitted option
selects `serial` automatically when these conditions are unavailable. Baseline
and mutation tests use the same isolation mode.

Isolation is resolved at command entry. Threads first started during project
collection can cause file execution to fail its fork-safety checks; they do not
change the isolation mode midrun. Use `--test-isolation serial` for such suites.

File and node targets, `-k`, and `-m` on the command line restrict both the
passing baseline and mutation tests. A restricted baseline does not become a full-suite passing
baseline for `vfy test`; changing the pytest scope invalidates mutation-cache
results for the earlier scope. `--all` selects source files, so it can be
combined with these pytest selectors:

```console
vfy mutate --all --source src -q tests/test_sign.py::test_zero_is_nonnegative
```

`--base REF` selects whole current Python files changed since the merge base of
`REF` and `HEAD`. That includes committed PR changes and staged, unstaged, and
untracked files. It implies `--changed` and rejects `--all` and `PATH:LINE`. A
missing reference or missing common history fails before baseline collection, so
fetch the base history first. JSON scope metadata records both resolved commits
and the selected files. Without `--base`, changed scope compares against `HEAD`.
See [PR mutation testing](../guides/mutation.md#review-committed-pull-request-changes).

## `vfy config`

```text
vfy config show
vfy config validate
```

`show` prints JSON with `source` (the file used, or `null`), `settings`, and
`provenance`, which records how the file was found. If no `[tool.vfy]` table
exists, the source is the Coverage.py file found by fallback. `validate` checks
types and supported values without running tests. It covers selection modes,
workers, positive finite timeouts, timeout policies, check formats, and
percentage ranges.

## `vfy diagnostics`

```text
vfy diagnostics [--engine=native|pytest-native] [--json] [--collect] [-- PYTEST_ARGS ...]
```

Reports the Python version and platform, the Ruff parser and profile, pytest and
xdist availability, pytest-cov coexistence, the configuration source, cache
state, and Rust-build diagnostics. The default `native` engine works
without installed pytest. Diagnostics resolves the selected engine's test
configuration and arguments without importing project code. Plug-in and hook
support remains unknown until collection runs. JSON records the selected
`engine` and its `test_readiness`.

`--engine=pytest-native` inspects the optional installed-pytest route. It also checks
statically visible plug-in declarations and runtime hooks in conftest files.
Without importing project code, the scan identifies literal `wrapper=True` and
`hookwrapper=True` on setup and teardown hooks, including `specname` aliases.
The installed plug-in inventory shows the native load plan: loaded by an
adapter or because every hook in its module runs unchanged, replaced by a vfy
adapter, or omitted. This is a plan, not proof that
collection or execution succeeded. Native startup also names installed plug-ins
it omits compared with pytest autoloading.
Findings from nested conftest files come from a project-wide scan and may not
apply to the selected test paths. Xdist options and hooks are accepted with
pytest-xdist 3.8.0. Other versions get an installation diagnostic. `--json`
output is suitable for bug reports.

`--collect` runs collection with the selected engine in a child process.
Test modules and collection hooks run; test bodies do not. The output
reports the exact collected item count. Collection does not prove that tests
pass, so run `vfy test` with the same engine to check fixtures and outcomes. Pass
invocation-specific pytest options after `--` so diagnostics can inspect them.
Native diagnostics also retains `native_readiness` in JSON for that route.

## `vfy cache`

```text
vfy cache dir
vfy cache clean
```

`dir` prints the `.vfy` path in the current directory. `clean` removes that directory.
Files outside it, such as the project-root `.coverage` file and Coverage.py
shards, are not changed. Cleanup rejects a symlink or directory junction at
`.vfy` and does not follow links inside it. A missing cache is a successful
no-op. Filesystem errors are reported. A partly completed deletion cannot be
rolled back.

## Exit statuses

| Status | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Configuration, data, or filesystem error |
| `2` | Argument parsing error, failed coverage or survivor gate, or Ctrl-C in watch mode |
| `4` | Test configuration or usage error, including a rejected explicit fork request (see [native worker start](configuration.md#native-worker-start)) |

`vfy test` uses pytest-compatible exit statuses. Status `4` indicates a test
configuration or usage error from the selected engine. `vfy coverage run`
returns the target program's exit status.
