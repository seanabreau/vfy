# Check your pytest suite

Use this guide to check a suite’s configuration, collection, fixtures, and
plug-ins before switching CI to `vfy test`.

<a id="check-the-supported-scope"></a>

## What is supported

On Linux and macOS, vfy’s default native engine runs pytest-style tests
without installed pytest. Check the [compatibility matrix](../about/compatibility.md)
for the test features and plug-ins your suite uses. The optional
[`--engine=pytest-native` route](../about/pytest.md#select-an-execution-engine)
uses installed pytest for collection and vfy for execution.

## Check your suite

Run from the project root. Add `--engine=pytest-native` to inspect the native
compatibility route instead of the default native engine:

```console
uv run vfy diagnostics
uv run vfy diagnostics --collect -- tests
```

Diagnostics reports visible configuration, plug-in, and hook conflicts with
locations and suggested fixes. It lists which installed plug-ins vfy
loads and which it omits. The static scan can include nested `conftest.py`
files outside the chosen test path; `--collect` checks that path by importing
modules and running collection hooks. It does not run test bodies or validate
fixture behavior and outcomes. A clean diagnostic result is not a compatibility
guarantee. Pass run options after `--`.

## Compare one suite before you change CI

Use the same environment and services for both full runs:

<!-- doc-test: compare-execution -->
```console
uv run pytest tests -q
uv run vfy test --engine=native --no-cov tests -q
```

`tests` limits both commands to that directory. `--no-cov` runs every collected
test without coverage. Do not combine `--all` with a path or other pytest
selector. To check the native compatibility route, install `vfy[pytest]` and add
`--engine=pytest-native` to the second command.

Compare collection, outcomes, exit status, skips, xfails, warnings, fixture
effects, failure messages, and teardown. Reduce differences to a small case and
check [Compatibility](../about/compatibility.md). For coverage, reports, workers,
and the edit loop, use [Run tests with vfy](run-tests.md).

Check plug-in behavior with a test that needs it. The `native` and `pytest-native` engines admit different plug-ins, so run `vfy diagnostics --collect` and the
comparison with the **same** engine. Pass `--engine=pytest-native` to check pytest collection with vfy execution.
For example, supported Hypothesis releases
provide `--hypothesis-profile` and `--hypothesis-seed` under the independent
engine. The `pytest-native` engine omits Hypothesis's pytest plug-in, so `@given` tests
can run there without those options or the plug-in's checks. Matching passing
tests alone does not establish that a plug-in's safeguards are active. See the
[version and plug-in scope](../about/compatibility.md#versions).

Keep test dependencies installed for this comparison. For example,
pytest-regressions supplies the `data_regression` fixture; removing it leaves
tests that request the fixture unable to run. A collector plug-in can have a
less visible effect: pytest-mypy-plugins collects YAML test cases, so removing
it can reduce the collected count without failing the remaining tests.
pytest-mypy-plugins requires pytest as the runner.

If `--engine=pytest-native` rejects a custom `pytest.Item` or a project
`pytest_runtest_call` hook, compare the default native engine before
changing the suite. It supports public custom item execution and call hooks;
the optional native route has a narrower item and phase-hook scope. Keep the
same test dependencies and compare the collected count and outcomes with stock
pytest. If a required plug-in is still omitted or needs pytest internals, use
pytest with vfy coverage as shown below. [Troubleshooting](../help/troubleshooting.md#vfy-rejects-a-test-option-or-plug-in)
covers inherited options and missing fixtures.

If your suite depends on AnyIO or another unsupported runtime plug-in, keep
pytest as the runner and use vfy for coverage:

```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src tests
```

Replace `src` and `tests` with your source and test paths. This keeps pytest's
installed plug-ins active. Native affected-test selection and mutation testing
require a suite whose execution plug-ins vfy supports.

## Load supported plug-ins

Native vfy provides coverage, asyncio, and test-results HTML support. It
disables general installed plug-in autoloading and loads its supported
adapters. It also loads an installed plug-in when a static scan of its module
finds only fixtures and hooks that native execution runs unchanged, such as
pytest-datadir. The scan follows hook reexports within the same package.
Unresolved imports, import cycles, and hook decorators or names that cannot
be resolved statically leave a plug-in omitted. Explicitly loading an unsupported runtime
plug-in is rejected. vfy does not fall back to pytest. An explicit `-p pytest_cov`,
including one in configured `addopts`, is replaced by vfy's coverage adapter, and
pytest's built-in plug-in names such as `-p warnings` are accepted. See
[Compatibility](../about/compatibility.md#versions) for versions and supported
features.

### Use pytest-xdist

Installed xdist loads when its options are requested. Explicit loads through
`-p xdist.plugin`, `PYTEST_PLUGINS`, or a root `conftest.py` also work. Use
`--numprocesses` for xdist workers; `-n` controls native workers. See
[distributed execution](run-tests.md#use-xdist-for-distributed-runs)
and [coverage across processes](collection.md#collect-workers-and-subprocesses).

### Use pytest-mock fixtures

An installed supported pytest-mock autoloads. Disable it with
`-p no:pytest_mock`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, or
`--disable-plugin-autoload`. Explicit `-p pytest_mock` or `-p pytest_mock.plugin`,
`PYTEST_PLUGINS`, a root `pytest_plugins` declaration, and pytest `addopts` work.
The plug-in must come from the interpreter's active installation locations,
including enabled user site; a project-local or `PYTHONPATH`-only copy is rejected.
Unsupported versions fail before collection.

The `mocker`, `class_mocker`, `module_mocker`, `package_mocker`, and
`session_mocker` fixtures keep their scopes and undo patches at teardown,
including failures and early stops. Patching, spying, stubbing, and assertion
messages come from pytest-mock; each native worker has its own scoped fixtures.

### Use pytest-randomly

Native execution loads installed pytest-randomly 5.0.0 and preserves its test
ordering and reseeding between setup, call, and teardown. Use
`--randomly-seed=0` to reproduce an order. The original
`--randomly-dont-reset-seed` and `--randomly-dont-reorganize` options also apply.

### Use pytest-timeout

An installed supported pytest-timeout autoloads. Disable it with
`-p no:timeout` or either autoload opt-out above. As under pytest,
`-p no:pytest_timeout` leaves the `timeout` entry point enabled.
Unsupported versions fail before collection.

Native execution runs pytest-timeout's own timer hooks, so `--timeout`,
`timeout` in configuration, `PYTEST_TIMEOUT`, `@pytest.mark.timeout`,
and `func_only` behave as under pytest, including in native workers.
`--session-timeout` and `session_timeout` require `--workers=1`; native file
workers reject them before collection because their session stop state is local
to each worker. Only the `signal` method is supported: native output capture would
discard the `thread` method's stack dump before it exits the process. vfy rejects
`--timeout-method=thread`, `timeout_method = thread`, and markers that request
it before the affected timer starts. Embedded pytest runs must start signal
timers in the main thread, since pytest-timeout substitutes the thread method
outside it.

### Use pytest-benchmark

An installed pytest-benchmark 5.3 autoloads. Disable it with
`-p no:benchmark` or either autoload opt-out above. Other versions are omitted,
so their `benchmark` fixture is unavailable natively.

pytest-benchmark's own fixture, options, markers, and reports run unchanged:
`benchmark(...)`, `benchmark.pedantic(...)`, `@pytest.mark.benchmark`,
`--benchmark-disable`, `--benchmark-skip`, `--benchmark-only`,
`--benchmark-json`, the results table, and `conftest.py` implementations of
pytest-benchmark's hooks such as `pytest_benchmark_update_json`. vfy marks the
fixture of a test that skips or fails as expected, as pytest-benchmark does
from its report hook, so no unused-fixture warning follows. Native execution
does not run the plug-in's check of `@pytest.mark.benchmark` arguments:
unknown arguments still fail the test when the fixture is created, but
positional arguments are ignored.

Measurement needs one process. As pytest-benchmark does under pytest-xdist,
each native worker runs benchmarked functions once without timing them, and
vfy prints a note when it used workers. Pass `--workers=1` to measure.

### Use Platformdirs fixtures

The installed Platformdirs pytest entry point autoloads `platformdirs_isolated`.
Explicit `-p platformdirs.pytest_plugin` and a root
`pytest_plugins = ["platformdirs.pytest_plugin"]` declaration also work. For a
declaration, use `vfy test -p no:platformdirs` or either autoload opt-out above
to prevent duplicate registration through the entry point. Admission checks the
reviewed fixture module's contents: a later release with the same module works,
but changed contents fail before collection. This adapter provides fixtures;
other execution hooks are rejected.

### Use pytester

Load pytest's `pytester` fixture with `-p pytester` or a root
`conftest.py` declaration such as `pytest_plugins = ["pytester"]`.
The helper plug-in `pytester_assertions` is also accepted. Nested pytester runs
use stock pytest.

<a id="project-phase-hooks"></a>

## Setup and teardown hooks

The native runner supports plain `pytest_runtest_setup` and
`pytest_runtest_teardown` hooks in `conftest.py`, including `tryfirst` and
`trylast` ordering. Register them before tests start. Hooks registered later, and
wrappers of these two hooks, are rejected.

`pytest_runtest_protocol` hooks from `conftest.py` and admitted plug-ins run
around each native test in pluggy's order. Register them before execution starts.
Wrappers, including legacy
`hookwrapper=True` hooks, receive `item` and `nextitem` and the protocol's
result. A plain hook that returns a result would replace pytest's test protocol,
so native execution reports it instead of running the test.
Wrappers that suppress a protocol exception require `--workers=1`; native
parallel workers reject that recovery because it can omit assigned test reports.
These hooks also run with xdist's disabled-worker setting,
`--numprocesses=0`. Distributed xdist runs reject project and compatible
third-party protocol hooks; use vfy's native file workers or run those workflows
directly with pytest.

pytest still runs
`pytest_sessionstart` and the supported collection hooks in `conftest.py`:
`pytest_collection`, `pytest_collect_file`, `pytest_ignore_collect`,
`pytest_pycollect_makemodule`,
`pytest_pycollect_makeitem`, `pytest_generate_tests`,
`pytest_make_parametrize_id`, `pytest_itemcollected`, `pytest_deselected`,
`pytest_collection_modifyitems`, and `pytest_collection_finish`. The native
runner builds its test list after that.

The test list is fixed after collection. Inserting, removing, or reordering
`session.items` during the run is not supported, and vfy does not catch every
such change. Changes to live test functions, argument values, fixture names a
test requests, and skip or xfail markers take effect at the matching point in the
run. Replacing pytest's fixture definitions during the run is not supported.
