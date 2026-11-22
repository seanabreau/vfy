# Test execution and pytest compatibility

By default, `vfy test` collects and runs tests with its native engine.
Your tests can use `import pytest` for supported APIs such as `pytest.fixture`
and `pytest.mark.parametrize`. vfy supplies that module during the run, even
in an environment without pytest installed.

## Select an execution engine

| `vfy test` option | Who collects and runs the tests? | Requires installed pytest? |
| --- | --- | --- |
| `--engine=native` (default) | vfy handles configuration, collection, assertion rewriting, fixtures, and execution. | No |
| `--engine=pytest-native` | Installed pytest handles configuration, collection, imports, and assertion rewriting. vfy runs tests and fixtures and schedules workers. | Yes, install `vfy[pytest]` (pytest 9.1 or newer) |
| `--engine=pytest` | Installed pytest collects and runs the tests. | Yes, install `vfy[pytest]` (pytest 9.1 or newer) |

Installing pytest leaves the default engine unchanged. Python test bodies run
in CPython on all three routes. See [running tests](../guides/run-tests.md) for
selectors and workers, [test selection](../guides/test-selection.md) for
engine-specific options, and the [adoption guide](../guides/native-adoption.md)
for checking a suite’s hooks and plug-ins.

## Configuration and plug-ins

vfy reads pytest configuration and accepts supported pytest selectors and run
options, including paths, node IDs, `-k`, and `-m`. See the
[command reference](../reference/cli.md) for options and limits.

`vfy test` loads its supported plug-in adapters and installed plug-ins whose
hooks it runs unchanged, rather than every installed pytest plug-in. An installed
plug-in may be omitted, and explicitly loading an unsupported runtime plug-in is
rejected. vfy does not automatically switch back to pytest execution. The
[compatibility matrix](compatibility.md) and
[plug-in setup](../guides/native-adoption.md#load-supported-plug-ins) describe
what works.

## Plug-in manager differences

On the independent engine, `config.pluginmanager.register()` validates a
plug-in's hook implementations against the known hook specifications before it
publishes the plug-in. When validation raises `PluginValidationError`, the
plug-in is not registered and a later `register()` call validates it again.
pluggy leaves the plug-in name registered after the same failure, so
`is_registered()` is true and a second `register()` call raises `ValueError`.
vfy keeps its behavior to avoid a partially registered plug-in.

## pytest-xdist compatibility

With `-n`, `-j`, or `--workers`, the independent engine gives each worker
pytest-xdist's worker-facing API, whether or not pytest-xdist is installed:

- the session-scoped `worker_id` fixture returns `gw0`, `gw1`, and so on in a
  worker, and `master` in a serial run;
- the session-scoped `testrun_uid` fixture returns the same 32-character hex id
  in every worker of one run, and a new id in each serial run;
- `config.workerinput` holds `workerid`, `workercount`, `testrunuid`, and
  `mainargv`, which is the controller's `sys.argv`.

When pytest-xdist is installed and loaded, its own fixtures are used. `-p
no:xdist` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD` leave the fixtures unregistered,
as they do for pytest.

## Use vfy coverage with stock pytest

If you need pytest's execution or an unsupported runtime plug-in, you can run
pytest and use vfy only to collect coverage.

Follow the [pytest coverage setup](../guides/collection.md#collect-with-stock-pytest). This path keeps
pytest's installed execution plug-ins active; vfy's native test selection and
mutation workflows require `vfy test`.
