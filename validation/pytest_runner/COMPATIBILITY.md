# Native pytest compatibility

`vfy test --engine=pytest-native` uses installed pytest for collection on Linux and
macOS with a [supported stable pytest release](../../docs/about/compatibility.md#versions).
The real-project matrix in this directory uses pytest 9.1.1. Pytest
handles configuration, collection, imports, and assertion rewriting; Rust
executes test and fixture phases.

The engine supports synchronous and bundled asyncio tests and fixtures,
unittest cases, text doctests, capture, logging, cache selection, progress,
JUnit XML, vfy coverage, and pytest-xdist 3.8.0 scheduling. Windows native
execution, arbitrary third-party runtime plug-ins, and unsupported item types
are outside its scope.
The vfy CLI uses `-n`, `-j`, or `--workers` for its own file-group workers;
pass `--numprocesses` to use pytest-xdist scheduling.
Preflight rejects unsupported integrations before test bodies run. See the
[CLI reference](../../docs/reference/cli.md#vfy-test) for routing and options.

The [test map](../../docs/about/native-pytest-testing.md) lists public and
installed-wheel checks. The [upstream corpus gate](../../docs/about/pytest-test-suite.md)
compares 4,229 parity cases and one observed outer-xdist skip, with a separate
Hypothesis lifecycle exclusion; nested pytester projects run stock pytest.

Use vfy's CLI for product runs. Loading `vfy._pytest.native_pytest`
directly is reserved for focused plug-in checks.
