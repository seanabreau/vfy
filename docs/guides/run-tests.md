# Run tests with vfy

`vfy test` runs your tests with coverage. Its [default engine](../about/pytest.md)
collects and runs pytest-style tests without installing pytest. For an existing
suite, check its hooks and plug-ins with the [adoption guide](native-adoption.md).

## Run the test suite

From your project root:

```console
uv run vfy test --all --cov=src --cov-branch --cov-report=term-missing
uv run vfy test --no-cov -q
```

Replace `src` with your source directory. The first command writes `.coverage`
and reports missing lines. The second runs tests without touching coverage data.
Failing tests give a failing exit status.

The default engine selects affected tests for safe, unfiltered coverage runs.
Filtered, no-coverage, and watch runs execute the requested scope. See
[baseline setup and selection rules](test-selection.md).

## Pass pytest arguments

Pass pytest options with the vfy options, in any order:

```console
uv run vfy test --maxfail=1 -q -m smoke
uv run vfy test tests/test_bounds.py -k test_positive_value
```

vfy accepts the supported pytest arguments described in
[pytest compatibility](../about/pytest.md). With `--engine=pytest-native`, vfy disables
general plug-in autoloading, loads the plug-ins it supports, and omits other
installed plug-ins; see [Load supported plug-ins](native-adoption.md#load-supported-plug-ins).
Selectors such as `-m`, `-k`, and file or node IDs turn off selection, so vfy
runs the tests you asked for.

With pytest 8.2 or later, `@args.txt` reads one argument per line. vfy
checks those arguments the same way as command-line arguments.

## Workers

`vfy test` uses one worker by default. Set the count with `-n`, `-j`, or
`--workers` and a positive number, `auto`, or `logical`. These aliases work
before or after pytest targets and flags. `-n 1` uses one worker. `-n 0` runs
serially; the native engine requires pytest-xdist for this mode, while the
independent engine accepts it without pytest-xdist.
`auto` uses the CPUs this process can use; `logical` uses the logical
CPU count. The limit is 256 workers.

```console
uv run vfy test -n 4
uv run vfy test tests -q --workers 4
```

With `file` scheduling, a file's tests stay in one worker, so the worker count
can't exceed the number of file groups. Independent runs start every requested
worker together and collect in each. A worker with no assigned tests finishes a
collect-only session: hooks run, no test body does, and the worker exits with
status 0. A collection failure reaches every started worker.

Eligible `pytest-native` parallel runs collect once in the controller and fork workers.
Other `pytest-native` runs and all default native workers start fresh interpreters. Each worker
has its own fixture lifetimes. For `pytest-native` runs, set
`VFY_WORKER_START=exec` when tests depend on per-process import state. The
[worker reference](../reference/configuration.md#native-worker-start)
lists the fork requirements and fallback rules.

### Choose a scheduling mode

Set `--native-scheduling` with the native or independent engine when the default
grouping does not suit your tests. The native engine defaults to `file`.
Independent runs with several workers default to `demand`: each worker claims
another batch when it finishes its current work. Serial independent runs retain
collection order.

| Mode | Tests that stay together |
| --- | --- |
| `file` (native default) | Every test in a file |
| `scope` | Each class, plus each file's module-level tests |
| `demand` (native parallel default) | Workers claim case batches as they finish |
| `cases` | A fixed contiguous range of independent cases |
| `striped` | Interleaved independent cases, preserving each worker's order |

```console
uv run vfy test -j 4 --native-scheduling=scope
```

Use `file` when classes share module state. Module, class, package, and session
fixtures run separately in each worker. File and scope modes balance work by
recorded test durations.

### Worker limits

- Each worker has its own test session and fixture lifetimes.
- Collection must agree across workers before tests start.
- `--maxfail` is shared. Tests already running may finish after the limit.
- A worker crash or invalid coverage shard leaves the previous data in place.
- Failing tests still save valid coverage. `--no-cov-on-fail` skips the reports,
  not the data write.
- `--stepwise` requires one native worker.
- With `--engine=pytest-native`, file logging (`--log-file` or `log_file`) and live CLI
  logging need one worker. `-j auto` and `-j logical` run serially when either is
  on. An explicit count above one is rejected.

### Use xdist for distributed runs

Install `vfy[pytest]` and pytest-xdist, then use `--engine=pytest-native` with xdist's
long `--numprocesses` option and `--dist` for upstream scheduling
(`-n` is vfy's own `-j`):

```console
uv run vfy test --engine=pytest-native --numprocesses=2 --dist=loadscope
```

xdist chooses which worker runs each test, and vfy runs the tests in each
worker. Don't combine `--numprocesses` with more than one native worker. Stepwise
options require native serial execution, even with `--numprocesses=0`. See
[supported xdist versions](../about/compatibility.md#versions) and
[plug-in loading](native-adoption.md#use-pytest-xdist).

The `load`, `loadscope`, `loadfile`, `loadgroup`, `worksteal`, and `each` modes
retain xdist scheduling, worker fixtures, hooks, and restarts. Scoped fixtures
survive scheduling batches. `--dist=each` runs all tests in each worker, keeps
per-worker JUnit and HTML results, and combines coverage. `--numprocesses=0`
runs serially in the controller, preserving supported test protocol and report
hooks.

<a id="test-results-html"></a>

## Write test results HTML

Write an HTML report of test outcomes, captured output, and run metadata.
Run from the project root:

```console
uv run vfy test --all --html=reports/tests.html
```

Open `reports/tests.html` in a browser to inspect the results. Styles go in
`reports/assets/`. Use `--css PATH` for your own styles, or
`--self-contained-html` to embed the styles in one file:

```console
uv run vfy test --all --html=tests.html --self-contained-html
```

This report covers test results. For source coverage, use `--cov-report=html`.

### Add metadata

```console
uv run vfy test --html=tests.html --metadata Build local
uv run vfy test --html=tests.html --metadata-from-json='{"Commit":"abc123"}'
```

Use `--metadata-from-json-file PATH` to read a JSON object from a file. Tests can
update the `metadata` fixture. In `conftest.py`, project hooks can write to the
session dictionary:

```python
from vfy.pytest_metadata import metadata_key


def pytest_configure(config):
    config.stash[metadata_key]["Build"] = "local"
```

With native workers, a later file batch takes precedence when metadata keys overlap.
Session-finish metadata is included.

To redact values, list key patterns in `environment_table_redact_list`. Each pattern
matches metadata keys from the start of the key:

```ini
[pytest]
environment_table_redact_list =
    TOKEN
    .*SECRET.*
```

### Add attachments and customize the report

Tests attach text, JSON, URLs, HTML, and images with the `extras` fixture.
Image helpers such as `html_extras.png()` accept base64 content, a path, or a URL.
Base64 images are embedded with `--self-contained-html` and saved under `assets/`
otherwise. Content that is not valid base64 remains an external reference in both modes.
Generated asset names and extensions can differ from pytest-html; use the image
references in the report. The helpers do not accept pytest-html’s `extension=`
argument.

For example, in a test file:

```python
from vfy.pytest_html import extras as html_extras


def test_response(extras):
    extras.append(html_extras.json({"status": "ok"}, name="Response"))
```

Attachments work in serial runs and with native workers. For worker controls, see
[worker options](#workers). To write each row as its test
finishes, use `-o generate_report_on_test=true` with `-j 1`. Streaming with more than
one worker is rejected. With `--maxfail`, the report includes the finished tests and
their fixture teardown.

The `pytest_html_report_title` and `pytest_html_results_summary` hooks change the title
and summary. `pytest_html_results_table_header` adds column headings, but can't change
the built-in columns. vfy inserts hook HTML as written.

### Report limits

The report options also work in pytest configuration or `PYTEST_ADDOPTS`.
See [compatibility targets](../about/compatibility.md#pytest-html-and-metadata). vfy does not support
per-test row hooks, imports of pytest-html's internal objects, or the interactive report
UI. For those, use direct pytest with pytest-html.

vfy writes the report after the run. If writing fails, it restores the previous
files. If the report fails, the exit status stays the same and vfy prints an error.
If restoring the old files also fails, the error names the backup that was kept. A run
that reuses saved results without running any test keeps the existing report.

## Run only affected tests

[How vfy selects tests](test-selection.md) covers creating a baseline and running
after edits.

## See which tests reach a line

Use [`vfy explain PATH:LINE`](test-selection.md#explain) to inspect saved
per-test coverage without running tests.

## Async tests

vfy supports asyncio tests and fixtures. See
[Test asyncio code](asyncio.md) for modes, loop scopes, and configuration.
Third-party async backends are outside the native compatibility contract.

## Rerun on save

Run the suite, then rerun after source changes settle:

```console
uv run vfy test --watch -j auto
```

Each run starts a fresh test process. Edits during a run queue another run. If
inputs change before a run saves its results, that run leaves the last accepted
coverage and reports in place.

The default engine runs the full requested scope on every watch run. With
`--engine=pytest-native`, watch selects affected tests; after a failed or
obsolete run, it runs the full requested scope before selection resumes. Use
`--all --watch` to run that scope every time. Watch rejects
test node IDs, `PATH:LINE` targets, and the `balanced` and `aggressive` policies.

### Watched inputs

Watch observes the configured source roots and project inputs. While idle, it
runs periodic metadata checks to catch changes the filesystem backend misses. An
edit that keeps the file size and modification time may go unnoticed until another
change happens.

## Investigate a failure

To inspect one failing test, run its node ID. To confirm an observed failure, run:

```console
uv run vfy test --engine=pytest-native --confirm-failures
```

Confirmation runs natively in a fresh serial process and isolates its coverage
and reports from the primary run. A passing confirmation does not change the original result or exit
status. For baseline and selection problems, see
[Troubleshooting](../help/troubleshooting.md).
