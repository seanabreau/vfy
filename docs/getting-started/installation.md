# Installation

Install vfy in your project to run tests, measure coverage, and run only the
tests affected by your changes.

vfy is **Alpha** and available on PyPI with prebuilt wheels for supported
platforms. You need CPython 3.12+ and
[uv](https://docs.astral.sh/uv/getting-started/installation/).

Installing a wheel does not require Git, Rust, or a C compiler. Affected-test
selection uses Git to compare edits with a passing baseline.

vfy ships wheels for Linux x86_64 and aarch64 (glibc and musl), and macOS x86_64 and arm64.

`vfy test` and `vfy mutate` run tests with vfy's native engine by default,
without installed pytest. Independent mutation testing requires Linux. For
the optional `--engine=pytest-native` route, install `vfy[pytest]`. On that route,
pytest collects the tests and vfy runs them. See [Compatibility](../about/compatibility.md) for supported versions,
plug-ins, and `conftest.py` hooks.

If your project already runs pytest, follow [Adopt vfy in an existing project](adopt-vfy.md)
to keep your Python support range and compare the runner and coverage separately.

## Install from PyPI

From your project directory, next to `pyproject.toml`, run:

```console
uv add --dev vfy
```

This adds vfy to your test environment and records the resolved version in
`uv.lock`. Commit the dependency and lockfile changes to keep installs reproducible.
All examples use `uv run` to run tools from that environment.

If you use pip in an existing virtual environment instead:

```console
python -m pip install vfy
```

Pin the vfy version in your requirements for repeatable installs. With pip,
omit `uv run` from the documentation commands.

## Verify the installation

```console
uv run vfy --version
uv run vfy diagnostics
```

Example output:

```text
vfy 0.0.2
vfy 0.0.2 native diagnostics
Python 3.12.14 (cpython, linux)
pytest: not installed
Independent runtime: available
vfy configuration: valid
...
```

`vfy diagnostics` reports your Python runtime, whether pytest is installed, and
whether vfy can read your configuration. The versions and paths in the output
are examples.

## Run tests with coverage

On Linux or macOS, [check your suite's compatibility](../guides/native-adoption.md),
then run the full suite from your project root:

```console
uv run vfy test --all --cov=src
```

Replace `src` with your source directory. vfy runs your tests, prints a coverage
report, and writes `.coverage`. See [running tests](../guides/run-tests.md) for
selectors, workers, and report options. See [test execution and pytest compatibility](../about/pytest.md) for engine
options and the optional coverage plug-in for pytest.

### Try affected-test selection

Affected-test selection runs only the tests that reach your uncommitted edits.
It works with the default native engine on unfiltered `safe` coverage runs.
[Create a passing baseline](../guides/test-selection.md#create-a-baseline) on a
clean Git commit; the selection guide explains which edits select a subset and
when to run the full suite.

### Try the example project

The tutorials set up a small standalone project and install vfy from PyPI:

- [Run affected tests](selection.md): edit a function, run one of two tests,
  retain whole-project coverage, and look up the test for a line.
- [Measure coverage with vfy](quickstart.md): find a missing branch, add its
  test, and export reports on Linux or macOS.

For the optional pytest coverage integration, follow
[pytest setup](../about/pytest.md#use-vfy-coverage-with-stock-pytest).

## Remove vfy

If you saved activation flags, remove `-p vfy.pytest_plugin` and
`-p no:pytest_cov` from pytest `addopts`, `PYTEST_ADDOPTS`, and CI commands first.
Also remove `-p vfy.pytest_reporting` if you enabled the optional reporter.
Otherwise pytest tries to import the removed plug-in and fails before collection.
Restore your pytest-cov dependency and original coverage command if you used them.

Optionally run `uv run vfy cache clean` before uninstalling to remove saved
selection data from `.vfy/`. This leaves the root `.coverage` file intact.
Then run `uv remove --dev vfy`, or `python -m pip uninstall vfy` for a pip
installation. Run your usual pytest command to check that the original setup works.

## Install from source

For contributor work or an explicit source build, you need Git, Rust 1.95+,
and a C compiler: Xcode Command Line Tools on macOS or your Linux distribution's build tools. Source builds can
take several minutes. Follow the
[development setup](https://github.com/seanabreau/vfy/blob/main/CONTRIBUTING.md#set-up-the-repository)
in a vfy checkout, then build a wheel:

```console
uv run maturin build --release --locked --out dist
```

Install that wheel in a matching platform's project environment:

```console
uv add --dev "/path/to/vfy/dist/<wheel-filename>.whl"
```

Replace the path and filename with the built wheel. The receiving machine needs
a supported CPython version and platform, but no Rust toolchain.
