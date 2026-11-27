# Configuration reference

vfy reads Coverage.py settings from `[tool.coverage]` and vfy settings from
`[tool.vfy]`, in `pyproject.toml` or another configuration file. To see what
vfy loaded, run `uv run vfy config show`.
`uv run vfy config validate` checks types and supported values without running
tests.

## Discovery and precedence

Project commands (`test`, `explain`, `coverage check`, `mutate`, `config`,
`diagnostics`, and `cache`) use the nearest `pyproject.toml` with a
`[tool.vfy]` table. They search the current directory, then its parents, and
do not merge settings from ancestor projects. If no vfy project is found, they
fall back to Coverage.py discovery.

Other `vfy coverage` commands use Coverage.py discovery. vfy checks
`COVERAGE_RCFILE` first, then the current directory for these files, in order:

1. `.coveragerc`
2. `.coveragerc.toml`
3. `setup.cfg` with Coverage.py settings
4. `tox.ini` with Coverage.py settings
5. `pyproject.toml` containing `[tool.coverage]`

Use `--config PATH` before the command to choose a file. A file named
`pyproject.toml` stores vfy settings under `[tool.vfy]`. Other TOML files use the
same `[tool.vfy]` and `[tool.coverage]` tables. Like Coverage.py, vfy also reads
a bare section such as `[run]` from an explicit TOML file, `COVERAGE_RCFILE`, or
`.coveragerc.toml` when that file has no `[tool.coverage.run]`; a discovered
`pyproject.toml` ignores bare sections. Coverage commands
also accept `--rcfile PATH`, which takes precedence over `--config`. A missing
explicit file is an error. `--isolated` ignores explicit configuration and
discovery.

Explicit command-line values override configuration, even when they equal the
default, such as `--workers auto`, `--timeout 30`, or
`--history .vfy/test-history.sqlite3`. Omitted options use configuration, then
defaults. Explicit Python constructor arguments override loaded run settings.
`config show`, `config validate`, and `diagnostics` report which file they used,
including a Coverage.py file found by fallback.

INI files support `DEFAULT` values, indented multiline values, and Coverage.py's
shell-style environment substitution: `$WORD`, `${WORD}`, `${WORD?}`,
`${WORD-default}`, and `$$`. TOML strings support the same substitutions.
Booleans, numbers, and lists keep their types.

## TOML example

This partial example enables branch and semantic collection. Omitted settings
retain their defaults; collection otherwise uses line mode and text reports use
zero decimal places.

```toml
[tool.coverage.run]
branch = true
source = ["src"]

[tool.coverage.report]
fail_under = 90

[tool.vfy.run]
decisions = true

[tool.vfy.test]
selection = "safe"
workers = 1
```

## Coverage.py `[run]` settings

| Setting | Type | Meaning |
| --- | --- | --- |
| `branch` | boolean | Collect branch arcs |
| `concurrency` | list | Accepts `thread` and `multiprocessing`; see [concurrency](#concurrency) |
| `context` | string | Static context |
| `cover_pylib` | boolean | Without a source filter, also measure the Python installation's standard library; the `Coverage(cover_pylib=...)` argument overrides it |
| `core` | `sysmon`, `ctrace`, or `pytrace` | Accepted as a tracer preference; collection always uses `sys.monitoring`, which also records dynamic contexts. Other values are rejected |
| `data_file` | path | Main data-file basename |
| `disable_warnings` | list of warning names | Suppress the listed collection warnings, such as `include-ignored` or `no-data-collected` |
| `dynamic_context` | `test_function` or `none` | `test_function` records each `test*` or `runTest` function's lines under its qualified name, such as `tests.test_app.TestApp.test_save`. A context starts only while no dynamic context is active on that thread and ends when that function returns or yields. This switcher runs before dynamic-context plug-ins. `none` disables the built-in switcher |
| `include` | list of globs | Include matching paths. If `source` or `source_pkgs` is set, it takes precedence and collection emits the `include-ignored` warning |
| `omit` | list of globs | Exclude matching files from collection, including unexecuted files under a source root |
| `parallel` | boolean | Write process-specific shards when collection starts |
| `patch` | list | Accepts `subprocess`, which enables automatic Python child collection, and `_exit`, which saves data before `os._exit()`, including stopped instances. The `_exit` hook remains installed for the process lifetime; save errors preserve the requested exit status. `execv` and `fork` are rejected, including through `Coverage.set_option()`. |
| `plugins` | list of modules | Import modules defining `coverage_init` |
| `relative_files` | boolean | Store relative measured paths when collecting |
| `sigterm` | boolean | Save data before preserving normal SIGTERM termination |
| `source` | list of paths or package names | Restrict collection and discover unimported files. An entry that is not an existing path names an importable package or module. Parent packages are not executed during configuration. |
| `source_pkgs` | list of package names | Restrict collection by importable package name, and discover unimported files in imported regular packages. Parent packages are not executed during configuration. A module name selects only that module. |

Coverage file patterns treat braces literally: `*a{b,c}.py` matches
`a{b,c}.py`. `*` and `?` match within a path component, while `**/` matches
zero or more directories. A leading `*/` and a trailing `/*` match across
directories. Patterns without a slash match filenames anywhere in the tree.
`?` and bracket classes match Unicode filename characters.
On Windows, case-insensitive file patterns follow the active Python
interpreter's Unicode case tables.
Backslashes act as path separators. Embedded `**`, such as `*a**b.py`,
raise `ConfigError` when collection or reporting compiles the patterns.
Unmatched brackets also raise `ConfigError`. An invalid character range such as
`[z-a]` raises Python's `re.error` instead; its message and regex position
can differ from Coverage.py.
These rules also apply to report and semantic include/omit settings.

Use a TOML list or Python sequence for brace patterns containing commas;
Coverage INI lists split on commas.

Dotted source packages whose parents extend or replace their search paths during
import are matched when imported.

### Concurrency

Collection always measures every thread, so `thread` needs no extra setup.

`multiprocessing` measures processes that `multiprocessing` starts with the
`fork`, `forkserver`, or `spawn` start method. It implies `parallel`: the
parent and each child write a process-specific shard, and the child writes its
shard when the process target returns. Combine the shards with
`vfy coverage combine`; `pytest --cov` and `vfy test` combine them before
reporting. Children use the parent's data file, configuration, and collection
settings. A configuration file is not required, unlike Coverage.py.
The forkserver and resource-tracker helper interpreters are not measured.

`greenlet`, `eventlet`, and `gevent` are rejected because collection does not
attribute greenlet switches. Unknown values are also rejected.

## Coverage.py `[report]` settings

| Setting | Type | Meaning |
| --- | --- | --- |
| `contexts` | list of regexes | Select observed contexts |
| `exclude_lines` | list of regexes | Replace the default exclusions; remove matching statements and suites |
| `exclude_also` | list of regexes | Add patterns to the default exclusions |
| `fail_under` | number | Minimum total percentage |
| `format` | `text`, `markdown`, or `total` | Default `report` output format |
| `include` | list of globs | Include matching report files |
| `ignore_errors` | boolean | Skip missing or malformed source files instead of failing |
| `omit` | list of globs | Exclude matching report files |
| `partial_branches` | list of regexes | Treat matching branch origins as intentional partials |
| `partial_also` | list of regexes | Add patterns to the default partial-branch set |
| `precision` | integer | Decimal places in percentages |
| `show_missing` | boolean | Show missing ranges |
| `skip_covered` | boolean | Hide fully covered files |
| `skip_empty` | boolean | Hide files without statements |
| `sort` | `name`, `stmts`, `miss`, `branch`, `brpart`, `cover` | Default `report` ordering; case-insensitive, with optional `-` prefix for descending order |

## `[tool.vfy.run]`

| Setting | Type | Meaning |
| --- | --- | --- |
| `decisions` | boolean | Collect semantic decision, condition, and MC/DC evidence |

These are vfy-only run settings. Keeping them out of `[tool.coverage.run]`
leaves that table portable.

## `[tool.vfy.test]`

These settings control test execution and selection. CLI arguments take
precedence. Baseline, plan, and audit options have no configuration keys; see
[CI selection controls](cli.md#ci-selection-options).

| Setting | Type | Meaning |
| --- | --- | --- |
| `history` | path | Timing and bounded outcome history; default `.vfy/test-history.sqlite3` |
| `selection` | `safe` (default), `balanced`, or `aggressive` | `balanced` and `aggressive` can skip affected tests. `vfy test --all` uses `safe` regardless of this setting. See [selection policies](../guides/test-selection.md#selection-policies). |
| `source` | list of paths | Source roots used for affected-test analysis |
| `workers` | integer from 1 to 256, `"auto"`, or `"logical"` | Defaults to `1`, or `"auto"` with `--engine=pytest`. Capacity and scheduling limits are in the [CLI reference](cli.md#workers) |

<a id="test-history"></a>

The history file keeps `duration_ms` for selected and full-run estimates, and up
to 20 recent outcomes per test. It is project-local and separate from `.coverage`.
A `.sqlite3` suffix selects SQLite; other suffixes use JSON. SQLite updates the
tests in the current run and stores each run identifier once. JSON updates rewrite
the complete history. Set `--history .vfy/test-history.json` or the `history`
configuration key to use JSON explicitly.

## `[tool.vfy.mutate]`

| Setting | Type | Meaning |
| --- | --- | --- |
| `history` | path | Timing and context history; default `.vfy/test-history.sqlite3` |
| `source` | list of paths | Source roots to mutate |
| `timeout` | positive finite number | Mutation timeout limit in seconds (default: 30); the adaptive policy can lower the effective limit |
| `timeout_policy` | `adaptive` or `fixed` | `adaptive` (default) reduces collected-mutation limits using clean-test history; `fixed` uses `timeout` directly |
| `workers` | `auto`, `logical`, or integer from 1 to 256 | Mutation worker policy. Independent execution requires and defaults to `1`; `--engine=pytest-native` defaults to `auto` |

The mutation timeout covers a selected group of tests, not each individual
test. With the default `adaptive` policy, collected callable-body mutations use
a limit based on summed history durations in milliseconds. The `fixed` policy
and fresh-process mutations use the configured timeout directly.
See the [timeout calculation](../guides/mutation.md#read-results-and-exit-statuses)
for the formula and a 250 ms example. Raising `timeout` may not change the
adaptive limit.

## `[tool.vfy.check]`

These settings configure `vfy coverage check` and pytest semantic thresholds.
CLI arguments take precedence.

| Setting | Type | Meaning |
| --- | --- | --- |
| `contexts` | list of regexes | Restrict observations by context |
| `exclude_decisions` | list of regexes | Exclude matching decision expressions or kinds |
| `fail_under_conditions` | number from 0 to 100 | Minimum atomic-condition percentage |
| `fail_under_decisions` | number from 0 to 100 | Minimum decision-outcome percentage |
| `fail_under_mcdc` | number from 0 to 100 | Minimum masking MC/DC percentage |
| `format` | `text` or `json` | Default semantic report format |
| `include` | list of globs | Include matching source files |
| `omit` | list of globs | Omit matching source files |
| `output` | path | Default semantic report destination; stdout when unset |

Without `--source`, `vfy coverage check` adds Python files from `[run] source`.
There is no `history` key; `--history` defaults to `.vfy/test-history.sqlite3`.

## `[paths]`

Each key defines an ordered group of equivalent source roots. The first entry is
the local reporting root. Later entries are literal or wildcard worker roots.
During `combine`, `Coverage.combine`, and remote xdist handoff, vfy replaces
the first matching remote prefix, but only when the mapped local path exists.
Groups are tried in configuration order.

## Format sections

| Section | Settings |
| --- | --- |
| `[html]` | `directory` (default `htmlcov`), `show_contexts` (default false), `skip_covered`, `skip_empty`, `title` (default `Coverage report`) |
| `[xml]` | `output` (default `coverage.xml`), `package_depth` (default `99`) |
| `[json]` | `output` (default `coverage.json`), `pretty_print` and `show_contexts` (default false) |
| `[lcov]` | `output` (default `coverage.lcov`) |
| `[annotate]` | `directory` |

`[html]` `skip_covered` and `skip_empty` override the `[report]` settings when
set. Otherwise they inherit them.

## Plug-in options

In `setup.cfg` and `tox.ini`, use a `coverage:` section for each plug-in module:

```ini
[coverage:run]
plugins = myproject.coverage_plugin

[coverage:myproject.coverage_plugin]
template_root = templates
```

TOML may use nested plug-in tables. Options are passed unchanged to the module's
`coverage_init` function.

## Unsupported settings

Unknown sections and keys are errors. An unsupported Coverage.py setting cannot
silently change what a coverage gate measures.

## Pytest asyncio settings

These are pytest configuration names, not vfy coverage settings. vfy
recognizes them when the asyncio plug-in is loaded, either by `vfy test` or by
`pytest -p vfy.asyncio_plugin`. The command-line options `--asyncio-mode` and
`--asyncio-debug` override `asyncio_mode` and `asyncio_debug`:

~~~toml
[tool.pytest.ini_options]
asyncio_mode = "strict"
asyncio_debug = false
asyncio_default_fixture_loop_scope = "function"
asyncio_default_test_loop_scope = "function"
~~~

The default mode is `strict`. In `auto` mode, ordinary `@pytest.fixture`
decorators on coroutine fixtures are handled automatically. An unset fixture loop
scope keeps the upstream fixture-caching default. Set it explicitly to avoid the
upstream deprecation warning. Asyncio markers accept `loop_scope` and
`loop_factories`.

See [Test asyncio code](../guides/asyncio.md) for usage examples.

## Environment variables

vfy reads these variables for coverage settings, color output, and native
test execution.

### User-facing variables

| Variable | Meaning |
| --- | --- |
| `COVERAGE_FILE` | Override the coverage data-file basename. An explicit `--data-file` takes precedence. |
| `COVERAGE_CORE` | A nonempty value selects the `sysmon`, `ctrace`, or `pytrace` preference when a `Coverage` object is constructed. `set_option("run:core", ...)` can override it before collection. Collection uses `sys.monitoring`. |
| `COVERAGE_RCFILE` | Select a Coverage.py configuration file during Coverage-style discovery. An empty value is ignored; `/dev/null` (or `NUL` on Windows) disables discovery. |
| `NO_COLOR` | A nonempty value disables ANSI color when `--color auto` is active. |
| `FORCE_COLOR` | A nonempty value enables ANSI color when `--color auto` is active. A nonempty `NO_COLOR` takes precedence. |

`--color always` and `--color never` override the color variables. vfy does
not read pytest's `PYTEST_ADDOPTS` as vfy configuration.

### Managed subprocess variables

These names carry internal state to managed Python children. They are not user
settings and may change. vfy restores the parent environment when the managed
run exits; see [Python subprocesses](../guides/collection.md#python-subprocesses)
for activation and bootstrap behavior.

| Variable | Internal purpose |
| --- | --- |
| `VFY_PROCESS_START` | Enable the isolated `sitecustomize` bootstrap, which runs the application hook after starting collection. |
| `VFY_PROCESS_OPTIONS` | JSON-encoded collection mode and filters. |
| `VFY_DATA_FILE` | Pytest worker data basename. |
| `VFY_BRANCH`, `VFY_DECISIONS` | Pytest worker collection-mode flags. |
| `VFY_REPLAY_MERGE` | JSON file naming the baseline and destination from which a selected run reconstructs complete coverage before reporting. |
| `VFY_CLI_ACTIVE` | Identify CLI-managed execution. |
| `PYTHONPATH` | Prepend the isolated bootstrap directory. |

Selection, confirmation, watch, mutation, profiling, and runner controllers also
pass internal `VFY_*` state. Do not set these handoff variables manually.

### Native worker start

With more than one worker (`-j N` above one, `-j auto`, or `-j logical`), native
runs can import and collect the suite once in the controller, then fork the
workers from that snapshot. Otherwise each worker starts a fresh interpreter and
collects the suite itself. Forked workers inherit imported modules, conftest
state, and collected items. Each worker initializes its own fixtures. Import side
effects and collection hooks run only in the controller.

A run forks when all of these hold:

- Linux with CPython and the GIL enabled.
- Coverage is off (`--no-cov`), or on through vfy's native coverage without
  Coverage.py plug-ins. Each forked worker writes its own coverage shard; the
  controller measures import and collection and combines every shard.
- `--capture=fd` (the default), `--capture=sys`, or `-s`.
- No HTML report, xdist, mutation isolation, watch run, live or file logging,
  tracing, or profiling.
- Before any worker starts, after collection: one live OS thread, no
  subprocesses, no unowned open descriptors, no initialized fixtures, only plain
  synchronous test functions, stock synchronous unittest cases, stock doctests,
  and only reviewed lifecycle hooks and cleanup callbacks.

vfy checks the options before collection. If they reject the run, fresh workers
collect the suite, and the controller imports no test modules. If the options pass
but a later collected-state check rejects the run, fresh workers import the project
again, so the controller and each worker both import it. Both fallbacks are silent.
Matching outcomes, reports, and exit status is the supported contract for suites
within the [import and process-state limits](#worker-import-and-process-state-limits)
below; suites that depend on import side effects can observe differences.
To see why a run did not
fork, set `VFY_WORKER_START=fork`, which fails with a diagnostic instead.

| Variable | Values and effect |
| --- | --- |
| `VFY_WORKER_START` | Unset: fork when eligible, otherwise fresh workers. `exec`: always fresh workers. `fork`: require a collected snapshot; rejection exits `4`. |
| `VFY_WORKER_COLLECTION` | `0`: controller collection before fresh-worker collection. `1`: worker-owned collection with at least two explicit workers. Either bypasses automatic fork selection. |
| `VFY_FORK_STARTUP_SECONDS` | Spawn and compile barrier deadline: 1–300 seconds; default `120`. |
| `VFY_NATIVE_SCHEDULING` | `file` (pytest-native default), `scope`, `cases`, `striped`, or `demand` (native parallel default); assignment details below. |
| `VFY_NATIVE_STARTUP_GRACE_MS` | `demand` first-batch head start: integer 0–60000 milliseconds; default `0`. |

<a id="worker-import-and-process-state-limits"></a>

Some results differ from a fresh or serial run:

- Lazy imports are credited to the test that triggers them in each worker, as with
  fresh workers. A module that several workers import lazily is credited to one
  test per worker.
- A line that runs only in a cyclic-garbage finalizer is credited to whichever
  test runs the collector. A fork also inherits the controller's collector counters.
- When vfy measures pluggy itself, the tail of the `pytest_runtestloop` hook
  dispatch is not credited to the worker's last test, because a forked worker calls
  the run loop directly.
- An automatically selected fork joins non-daemon threads and discards daemon
  threads at exit. With `VFY_WORKER_START=fork`, tests must finish their
  threads before worker cleanup.
- Without `--basetemp`, automatic forks use pytest's default numbered base
  directory for `tmp_path`; explicitly requested forks use a directory owned by
  the run. With `--basetemp`, each worker uses a separate subdirectory there.
- Forked workers do not run `atexit` callbacks, including callbacks registered
  by tests; callbacks registered during import run only in the controller.
  Custom random-generator state and process IDs saved at import are inherited;
  CPython's global random generator reseeds at fork. A suite that depends on any
  of these differences should set `VFY_WORKER_START=exec`.

Collection-only runs, help, and continuation after collection errors
keep controller collection either way. Mutation hosts keep controller collection;
explicit worker collection rejects them and can't be combined with collected
snapshots.

### Explicit collected-snapshot workers

`VFY_WORKER_START=fork` requests the fork on Linux with CPython and the GIL
enabled, and reports every rejection. It supports JUnit, retained case captures,
ordinary fixtures, stock doctests, synchronous stock unittest items, and supported
runtime hooks.

An explicit fork requires at least two explicit workers and controller-owned
collection. It has these rules:

- Imports, supported configuration and session initialization, test generation,
  selection, and collection-finish hooks run once, in the controller.
- Runtime threads must be joined before worker cleanup, and `tmp_path` uses a
  directory owned by the run.
- The runtime PID differs from a PID saved at import. Custom random generators
  inherit their state; CPython's global random generator reseeds at fork as usual.
- Collection callbacks don't run again in workers. Workers inherit collected
  objects, including unittest instances, without rerunning their constructors.
- Imported libraries and at-fork callbacks must be safe in this model. At-fork
  callbacks must not block, start threads or descendants, or change the worker
  bootstrap. One live OS thread doesn't prove that dormant native thread pools or
  retained objects are safe. The supervisor's startup watchdog can't interrupt a
  controller blocked in a before-fork or parent-side at-fork callback.
- The controller writes history and result files after it merges worker records.
  Native hook compatibility checks still apply. Custom conftest sessionstart hooks
  are unsupported. Unreviewed sessionfinish, unconfigure, or runtest-loop hooks and
  config cleanup callbacks are rejected before test bodies run. Permitted finish and
  unconfigure hooks run in each worker and in the controller.
- Children exit with `os._exit` after that lifecycle, so interpreter shutdown and
  `atexit` callbacks don't run in them. Fixtures and tests must join runtime threads
  before cleanup.
- A rejected `fork` request does not fall back to fresh interpreters.

An explicit request rejects active threads or subprocesses, unowned open
descriptors, initialized fixtures, async items (including
`IsolatedAsyncioTestCase`), custom unittest item or collector classes, Coverage.py
plug-ins, an enclosing coverage session, a capture manager other than pytest's
stock `fd`, `sys`, or `no`, HTML, incremental publication, xdist, mutation
isolation, live or file logging, tracing, and profiling. The default route falls
back to fresh workers for each of these.

Child process groups are registered before
bootstrap and test code runs. An interruption or failure kills those groups and
reaps the direct children. Forking does not guarantee lower runtime or memory use:
reference counting, garbage collection, compilation, and test execution can make
inherited pages private.

For assignment policies, see [CLI scheduling](cli.md#workers). Independent
parallel runs default to `demand`, with workers claiming case batches as they
finish. Serial runs retain collection order. Set `--native-scheduling` or
`VFY_NATIVE_SCHEDULING` to choose another policy; the command-line option takes
precedence.

With the optional `pytest-native` engine, `demand` requires a collected fork and at
least two explicit workers, and cannot use worker-owned collection. It compiles
the suite before forking, and workers claim batches as they finish. Module and
class fixture scopes stay together, and a package fixture keeps the whole suite in
one batch.

For that collected fork, `VFY_NATIVE_STARTUP_GRACE_MS` gives the worker that
claims the first batch a head start under `demand` scheduling. All workers are ready before the period
starts; the others wait until that batch finishes or the period expires. A grace
period can reduce simultaneous cold initialization, but it can also delay useful
parallel work. A nonzero grace value without `demand` scheduling is an error, and so is one
with the independent engine, which starts fresh workers instead of forking.
