# Test asyncio code

`vfy test` runs `async def` tests and fixtures with coverage through its built-in
adapter. You don't need pytest-asyncio. The default engine autoloads an
installed AnyIO pytest plug-in, as it does other installed plug-ins, but
third-party async backends are outside its compatibility contract; see
[AnyIO with direct pytest](#use-hypothesis-or-anyio-with-direct-pytest).

## Run an async test

Install [vfy](../getting-started/installation.md) in your test environment.
`vfy test` needs Linux or macOS; [check plug-in compatibility](native-adoption.md) first.
Run the commands below from the project root. Save this as `tests/test_asyncio.py`:

```python
import asyncio

import vfy
import pytest


@vfy.async_fixture
async def queue():
    queue = asyncio.Queue()
    queue.put_nowait(42)
    yield queue


@pytest.mark.asyncio
async def test_read(queue):
    assert await queue.get() == 42
```

```console
uv run vfy test --all
```

The run prints a coverage report and ends with the pass count:

```text
============================== 1 passed in 0.05s ===============================
```

Each worker has its own event loop; tests within a worker run one at a time.
For parallel execution and coverage, see
[worker options](run-tests.md#workers).

## Use existing pytest-asyncio tests

1. Remove pytest-asyncio from the project and CI dependencies.
2. Replace `pytest_asyncio.fixture` with `vfy.async_fixture`.
3. Keep `@pytest.mark.asyncio` and your `asyncio_*` settings.
4. Remove any explicit `-p pytest_asyncio` or `-p pytest_asyncio.plugin` arguments.
5. For direct pytest, run `uv run pytest -p vfy.asyncio_plugin`.

`vfy.asyncio.fixture` and `vfy.asyncio.is_async_test` are aliases for
mechanical migrations. Use `vfy.async_fixture` in new code.

Strict mode is the default. In auto mode, a plain `@pytest.fixture` works for
coroutine fixtures:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

```python
import asyncio

import pytest


@pytest.fixture
async def queue():
    return asyncio.Queue()
```

Don't load the adapter alongside pytest-asyncio.

## Fixtures and loop scopes

vfy finishes async-generator teardown before it closes the loop, then cancels any
remaining tasks during runner cleanup:

```python
import asyncio
import vfy


@vfy.async_fixture
async def server():
    instance = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
    yield instance
    instance.close()
    await instance.wait_closed()
```

`scope` sets how long a fixture's value is cached. `loop_scope` sets how long the
event loop lives, and must be at least as broad as `scope`. To share one loop between
a fixture and a test:

```python
import asyncio

import vfy
import pytest


@vfy.async_fixture(scope="module", loop_scope="module")
async def queue():
    return asyncio.Queue()


@pytest.mark.asyncio(loop_scope="module")
async def test_round_trip(queue):
    await queue.put(42)
    assert await queue.get() == 42
```

Scopes are `function`, `class`, `module`, `package`, and `session`. Tests default to
function scope. A loop is shared only when you ask for it.

Set `asyncio_default_fixture_loop_scope = "fixture-scope"` to use each async
fixture's caching scope when its declaration omits `loop_scope`.

vfy's adapter acquires the `event_loop_policy` fixture only when a runner needs it,
so synchronous tests do not use it. Override this session fixture to supply a
custom policy. A pytest-asyncio compatibility alias activated before vfy's adapter
keeps pytest-asyncio's autouse policy behavior.

To run each test on custom event loops with `vfy test --engine=pytest-native` or direct
pytest, add a hook to `conftest.py`. Each test runs once per factory, and
`@pytest.mark.asyncio(loop_factories=["debug"])` picks the factories. The default
independent engine rejects the hook with a usage error (exit status 4) and the
marker argument with a collection error (exit status 2); both recommend
`--engine=pytest-native`:

```python
import asyncio


def pytest_asyncio_loop_factories(config, item):
    return {"debug": lambda: asyncio.new_event_loop()}
```

## Use Hypothesis or AnyIO with direct pytest

For an existing AnyIO suite, keep its plug-ins and pytest as the runner. To add
vfy coverage, use:

```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=src tests
```

Replace `src` and `tests` with your source and test paths. AnyIO execution is
unsupported by `vfy test`, including `asyncio_mode = "auto"`. Test selection and
`vfy mutate` are not suitable for suites that depend on it.

To combine vfy's asyncio adapter with Hypothesis or AnyIO under direct pytest,
install the other packages and run `uv run pytest -p vfy.asyncio_plugin`.
Do not load pytest-asyncio alongside this adapter. `vfy test` supports only the
[documented plug-in adapters](../about/compatibility.md#versions).

```python
import pytest
from hypothesis import given, strategies as st


@pytest.mark.asyncio
@given(st.integers())
async def test_round_trip(value):
    assert int(str(value)) == value
```

In strict mode, AnyIO can run Trio tests next to asyncio tests:

```python
import pytest


@pytest.fixture
def anyio_backend():
    return "trio"


@pytest.mark.anyio
async def test_trio_backend(anyio_backend):
    assert anyio_backend == "trio"
```

Don't mark one test for two async backends.

## Troubleshooting

- **Async fixture warning in strict mode:** use `@vfy.async_fixture`, or set
  `asyncio_mode = "auto"`.
- **Conflicting plug-in error:** remove pytest-asyncio, every explicit pytest-asyncio
  plug-in argument, and every matching `PYTEST_PLUGINS` entry.
- **Unexpected loop identity:** set the test marker's `loop_scope` to match the
  fixture's. The fixture's caching scope doesn't pick the test's loop.
- **Direct pytest skips async tests:** add `-p vfy.asyncio_plugin`, or use `vfy test`.

Asyncio settings (`asyncio_mode`, `asyncio_debug`, and the two default loop-scope
keys) are in the [configuration reference](../reference/configuration.md#pytest-asyncio-settings).
For the tests behind this support, see [compatibility](../about/compatibility.md#built-in-asyncio).
