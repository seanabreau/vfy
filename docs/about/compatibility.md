# Compatibility

vfy is Alpha. It supports CPython 3.12 and newer. The configured CI matrix
covers 3.12–3.15 on Linux, and 3.12, 3.14, and 3.15 on macOS.

vfy's compatibility tests compare the documented interfaces below with
Coverage.py 7.15.4. Compatibility with future upstream releases requires
new comparisons. vfy does not need Coverage.py at runtime.

For adoption questions, see the [FAQ](../help/faq.md). CPython 3.11 and
older, and PyPy, are unsupported.

## Versions

| Component | Supported range or target | Reference or configured validation |
| --- | --- | --- |
| CPython | `>=3.12` | CI: Linux 3.12–3.15; macOS 3.12/3.14/3.15 |
| Coverage.py oracle | 7.x schema-7/public API | 7.15.4 |
| Optional pytest support | `>=9.1`; required by `pytest-native`/`pytest` engines | Linux CPython 3.12 CI: 9.1.0 (floor), 9.1.1, 9.2.0; locked integration and differential oracle: 9.1.1 |
| Native `vfy test` (default) | Experimental; documented Linux/macOS support; pytest is not required | Recorded validation: Linux; pinned pytest 9.1.1 compatibility reference |
| `vfy test --engine=pytest-native` | Experimental; Linux and macOS; stable pytest `>=9.1` | Explicit compatibility route; worker and watch boundary tests |
| pluggy | `>=1.5,<2` with `vfy[pytest]` | CI floor: 1.5.0 with pytest 9.1.0; locked reference: 1.6.0 |
| Hypothesis | Optional; final 6.x releases from 6.152.1 for independent execution | Locked reference: 6.170.5 |
| pytest-xdist | Optional; native execution supports 3.8.0 | Direct pytest minimum lane: 3.6.1; native scheduler, worker, report, and coverage differentials: 3.8.0. With pytest 9.2.0, xdist 3.8.0 loses the group suffix used by `--dist=loadgroup` |
| pytest-cov | vfy supplies the documented coverage options | 7.1.0 reference; integration environment uses `>=7.1,<8` |
| pytest-html | vfy supplies the documented HTML report features | 4.2.0 reference |
| pytest-metadata | vfy supplies the documented metadata fixture and options | 3.1.1 reference |
| pytest-asyncio | Built into the native engine; installation is optional | 1.4.0, 1.4.1.post1, and 1.5.0 differentials |
| pytest-mock | Optional, `>=3.13,<4` for native execution | Scoped fixtures, patch cleanup, and assertion diagnostics compared with stock pytest |
| pytest-randomly | Optional, `5.0.0` for native execution | Original collection and setup, call, and teardown reseeding hooks; requires the installed upstream module |
| pytest-timeout | Optional, `>=2.4,<2.5` for native execution; `signal` method in the main thread; session timeouts require `--workers=1` | Marker, configuration, command-line, environment, `func_only`, and serial session timeouts compared with stock pytest; reference 2.4.0 |
| pytest-benchmark | Optional, `>=5.3,<5.4` for native execution; measures with `--workers=1` | Fixture, `pedantic`, marker options, disable, skip and only modes, JSON output, and skipped-test cleanup compared with stock pytest; reference 5.3.1 |
| pytest-codspeed | Unmeasured calls run natively; tuning and pedantic calls use installed helpers | The independent adapter provides unmeasured fixtures and markers, including the package’s `BenchmarkFixture` import. Use `--engine=pytest` for measurement, instrumentation, and other plug-in internals, including `--codspeed` and `CODSPEED_ENV`. |
| Independent replacements for absent timeout and benchmark plug-ins | POSIX main-thread signal timeouts, configuration and runtime marker overrides, `func_only`, and `0` to disable; unmeasured benchmark calls, group metadata, skip and only selection | Signal timeout phases and configuration precedence are compared with pytest-timeout 2.4.0, including independent workers. Timers restore the previous signal handler and reject a foreign `ITIMER_REAL` deadline before mutation. A runtime call-only marker replaces an owned protocol timer; the protocol deadline does not resume after the call. Thread timers, session deadlines, and debugger override options require pytest-timeout. Benchmark calls and `pedantic` follow pytest-benchmark's disabled mode; explicit `--benchmark-disable` also permits warmup, sort, and grouping settings. Measurement, profiles, comparisons, and artifacts require the installed plug-in. CodSpeed measurement requires `--engine=pytest`. |
| Platformdirs | Optional, reviewed fixture module (4.12.0–4.12.3) | Fixture isolation and cleanup compared with stock pytest, including native and xdist workers |

See [how vfy runs pytest plug-ins](pytest-plugin-compatibility.md) for provider selection,
feature checks, and the evidence used to extend support.

Package metadata declares dependency ranges; the lockfile pins reference versions.
A configured CI lane is not evidence that the current revision passed. Recorded
results apply only to their stated wheel and revision.

With plug-in autoload enabled, the independent engine supplies built-in
asyncio, mock, unmeasured benchmark, signal timeout, and serial xdist support
when the corresponding plug-in is unregistered. Explicit plug-in blocks
also apply to these fallbacks.

## Native pytest limits

`vfy test --engine=pytest-native` requires a stable pytest release from 9.1 onward. Installed pytest handles configuration, collection,
imports, and assertion rewriting on this route. Rust runs tests and fixtures.
See [execution engines](pytest.md#select-an-execution-engine) for the default
selection rule.

Independent affected-test selection supports unfiltered `safe` coverage runs
using a passing local baseline. Selection binds after project collection hooks;
a changed collected inventory runs the full requested scope. Selected tests
retain collection order. Portable baseline import/export, selection audits,
failure confirmation, and selection under other policies require the installed
pytest routes. See
[selection rules and external inputs](../guides/test-selection.md).

Independent assertion rewriting generates vfy-specific bytecode and temporary
names. Tools that identify pytest rewriting by inspecting its private bytecode
names may not recognize it. The independent `pytest` module has a public package
file and standard import metadata for consumers that inspect its origin.
The `native` and `pytest-native` engines drive test execution from Rust; Python stack inspection therefore does not find pytest execution frames
that would be present with `--engine=pytest`. For example, a stack-based ignore
rule for `_pytest` may behave differently. Use `--engine=pytest` when a tool
requires these pytest implementation details.

The real-package validation harness compares ordered JUnit outcomes, counts, and exit statuses.

Owned independent interpreters bind their native lifetime before activating
application import paths, `.pth` files, `sitecustomize`, or `usercustomize`.
They forward the caller's effective interpreter flags and retain its startup
environment, temporarily withholding `PYTHONPATH` and warning-category imports
until binding. Warning options resolve before site hooks, which can still change
their filters. The original
path and site policy also applies to nested workers. Optimization, warning
options, bytecode-cache settings, and fixed hash seeds follow that policy.
In owned interpreters, `sys.flags.safe_path` and `sys.flags.no_site` remain set
after application paths and site hooks are restored. An explicit caller `-I` or
`-E` continues to control environment handling.

The [native test map](native-pytest-testing.md) and [corpus gate](pytest-test-suite.md)
describe the compatibility checks.

Native behavior and limits:

- `raiseerror()` works with pytest-compatible exceptions. Parametrized JUnit cases
  keep their own properties, including values recorded during fixture teardown.
  `record_xml_attribute` follows the selected JUnit family's attribute rules.
  Independent execution supports ElementTree's registered namespace prefixes,
  including the default prefix. Malformed names leave existing JUnit XML intact.
  Saved attribute recorders follow pytest's reporter lifetime.
  Repeated collection reports for one node share a JUnit testcase and retain
  every error or skip event. Independent multiworker collection and pytest-xdist
  can differ in collection-skip counts.
- Independent fixture requests expose the collected item's live `path` in
  function, class, module, and package scopes. Session scope raises `AttributeError`.
- Finalizers run in reverse registration order. With `--engine=pytest-native`, a
  first-time fixture request during teardown follows the installed pytest
  version's rules.
- Warning filters see marker changes made before the test starts. Failure output
  includes captured setup and teardown output.
- `--capture=fd`, `--capture=sys`, and `--capture=no` work. `--capture=tee-sys` is
  unsupported. Sys capture leaves direct file descriptor writes uncaptured.
  Disabling capture leaves output uncaptured.
- Short summaries keep test IDs and outcome reasons. Native skip summaries list each
  test ID, not a source location.
- Independent execution requires runtime test reports to retain the active item’s
  node ID; rewriting report node IDs is unsupported.
- Independent parameter IDs preserve lone surrogates through pytest's default
  escaping. With
  `disable_test_id_escaping_and_forfeit_all_rights_to_community_support = true`,
  surrogate IDs raise `UnicodeEncodeError` during collection.
- Custom collectors keep their function-generation methods. Package xunit hooks
  share pytest's owner lifetimes. Faulthandler timeouts and deferred thread
  exceptions take part in native lifecycles.
- The independent engine collects and runs a custom `pytest.Item` returned
  directly by `pytest_collect_file`, as pytest-checkdocs does. To select the
  item by file path, its `path` must match the requested file, as in pytest.
- `--engine=pytest-native` supports plain conftest setup and teardown hooks and rejects
  phase hook wrappers. `vfy diagnostics --engine=pytest-native` identifies literal
  wrapper declarations and `specname` aliases without importing project code.
- The independent engine runs supported conftest collection hooks, including
  generated parametrization and per-case deselection callbacks. Installed
  pytest handles these hooks with `--engine=pytest-native`. See the
  [supported hook list](../guides/native-adoption.md#project-phase-hooks).
  Hook specifications snapshot warning options at registration; their public
  `warn_on_impl` and `warn_on_impl_args` fields are writable.
- During the test protocol, the independent engine calls
  `pytest_assertrepr_compare` implementations visible to the current test and
  uses the first non-empty answer before its own rules.
- `Config.inicfg` is a read-only mapping of the configuration file's values and
  raises pytest's deprecation warning; assigning to it is not supported. The
  legacy path objects returned by `tmpdir`, `Config.rootdir`, and
  `Config.inifile` implement the commonly used `py.path.local` methods, not the
  full class.

Native execution uses its own executor. Contexts for pytest and pluggy internals
can therefore differ. When evaluating a suite, compare outcomes and coverage of
the code you own.

Native worker and xdist options, fixture lifetimes, stepwise, and logging limits
are documented under [Workers](../guides/run-tests.md#workers).

The supported runtime plug-ins are listed below.

The optional pytest-mock adapter supports the upstream fixture and configuration
hooks. See [loading instructions](../guides/native-adoption.md#use-pytest-mock-fixtures).
The compatibility script runs pytest-mock 3.13.0, 3.14.0, 3.15.0, 3.16.0, and 3.15.1
against stock pytest. The main differential oracle is 3.15.1.

vfy does not automatically fall back to pytest execution. See the
[adoption guide](../guides/native-adoption.md) for supported projects and the
[test guide](../guides/run-tests.md) for commands.

## Supported interfaces

Supported:

- `vfy coverage` command workflows;
- supported Coverage.py configuration, schema-7 data, shard merging, and contexts;
- include/omit file patterns with literal braces, component wildcards, recursive
  directory wildcards, and invalid embedded `**` diagnostics;
- the documented `Coverage` and `CoverageData` Python API;
- line and branch coverage reports in terminal text, Markdown, HTML, JSON,
  XML, LCOV, and annotated source;
- supported pytest-cov options under stock pytest (with vfy's coverage plug-in),
  native `vfy test`, and Python subprocess coverage;
- Coverage.py's documented public plug-in protocols.

Not supported: private Coverage.py modules, the private `dumps()` byte format,
Python runtimes without `sys.monitoring`, hard process termination, and
undocumented third-party plug-in internals. Output is not byte-identical to
Coverage.py. Semantic decision coverage and mutation testing are vfy
extensions, described below.

## Coverage.py data

| Behavior | Support |
| --- | --- |
| Read schema-7 line and branch data | Supported |
| Write schema-7 data that Coverage.py can read | Supported; direct writes preserve line and branch mode |
| Preserve contexts and file-tracer names | Supported |
| Reject tracer conflicts without changing destination data | Rejected before destination mutation; `add_file_tracers()` is atomic even though Coverage.py 7.15.4 can retain earlier batch entries |
| Merge compatible parallel shards | Supported |
| Reject mixed modes and unsupported schemas | Supported |
| Ordered, existence-gated `[paths]` aliases | Supported; Coverage.py differential target |

Schema 7 is an internal Coverage.py format, not a stable interchange standard.
Supporting another Coverage.py release requires tests against that release. A
matching schema number is not enough.

## Collection

| Behavior | Support |
| --- | --- |
| Line and branch collection matching Coverage.py's default core | Targets Coverage.py’s default core |
| Multiline branch destinations and active snapshots | Supported |
| Collection file filters | `source` and `source_pkgs` take precedence over `[run] include` and emit the suppressible `include-ignored` warning when both filters are configured. `[run] omit` still excludes matching files. |
| Threads and interleaved generators | Supported |
| `[run] dynamic_context` | `test_function` and `none`; Coverage.py 7.15.4 differentials cover qualified names, switcher ordering, nesting, generator resumption, exceptions, and overlapping threads |
| Same-line loops, exhausted and early-closed generators | Supported |
| Exceptions | Supported |
| pytest-xdist normal completion | Supported |
| Spawned or exec'd Python subprocesses | Supported on normal shutdown |
| Application `sitecustomize` in measured subprocesses | Coverage.py 7.15.4 differential for hook identity and absence, single execution, `usercustomize` ordering, measured imports, startup errors, and unresolvable search entries |
| Fork without exec | Supported on POSIX, with isolated child shards |
| Retry collection after failed monitoring initialization | Supported, including forked children |
| Monitoring slot reuse after stopping | Clears code-local masks, including restored fork frames |
| Retry after failed source or plug-in discovery | Preserves collected measurements; Coverage.py discovers at `get_data()`, vfy at `stop()` |
| `[run] sigterm` | Saves, then exits normally for the signal, on POSIX |
| `[run] patch = _exit` | Saves before `os._exit()`, including stopped instances; save errors preserve the requested exit status |
| `[run] core` and `[run] disable_warnings` | `sysmon`, `ctrace`, and `pytrace` are accepted as tracer preferences; collection uses `sys.monitoring` with the branch-arc conventions Coverage.py uses for the requested core. `COVERAGE_CORE` is read when `Coverage` is constructed; `set_option("run:core", ...)` can override it before collection. Any warning names resolve like Coverage.py |
| `[run] concurrency = multiprocessing` | Coverage.py differential for `fork`, `forkserver`, and `spawn` children on POSIX; `greenlet`, `eventlet`, and `gevent` are rejected |
| SIGKILL and unpatched hard interpreter exits | Unrecoverable by design |

The package installs `vfy`, not a `coverage` command. See the
[CLI reference](../reference/cli.md#vfy-coverage) for supported coverage
commands. `check` and `mutate` are vfy extensions.

## pytest-cov

vfy's own plug-in implements the pytest-cov 7.1.0 options, default terminal
report, report destinations, `cov` and `no_cover` fixtures and marker, per-test
phase contexts, append and erase, threshold exit status, Python subprocesses, and
xdist data handoff.

`vfy test` loads this plug-in only in its child run, and disables competing
coverage plug-ins there. Ordinary pytest keeps normal entry-point discovery, so
pytest-cov and vfy can be installed together. See
[pytest integration](../guides/collection.md#collect-with-stock-pytest).

## Analysis and reports

Line statements and exclusions target Coverage.py. Branch analysis covers functions, loops, comprehensions, conditional expressions,
`match` and guards, `with`, `try`, partial conditions, excluded branches, and
custom partial expressions. Branch arcs under unexcluded `elif` and `else` suites
stay reportable beneath an excluded `if` header. Cleanup branches in `finally`
retain explicit destinations inside the suite. Abrupt try and handler exits do
not give cleanup a fallthrough branch; an executable `else` suite can supply a
static continuation.

- For Python source, runner and saved-data reports use the file inventory
  recorded during collection, including unexecuted files that pass the filters.
- `exclude_lines` replaces the default patterns. `exclude_also` keeps them.
- Excluding a function or overload does not apply the default ellipsis pattern to
  adjacent blank or comment lines.
- Source loading honors UTF-8 byte-order marks, encoding declarations, and
  universal newlines. Supported encodings: UTF-8, ASCII, Latin-1, and
  Windows-1252. Other codecs, such as Shift-JIS and ISO-8859 variants other than
  Latin-1, raise an explicit error.

| Format | Status |
| --- | --- |
| Terminal text | Totals, missing ranges, precision, threshold; display filters preserve totals and API percentages |
| Markdown and total API output | Display filters preserve totals and API percentages |
| JSON | Format 3 line and branch data, plus Rust function and class regions |
| Cobertura XML | Coverage.py-compatible line and branch fields, source roots, package depth |
| LCOV | Coverage.py-compatible line and branch fields, including sanitized filenames and zero-hit versus unknown branch arms |
| HTML and annotations | Coverage.py-compatible documented output; annotation directories distinguish sources with the same basename |

Keep `run:relative_files` the same during collection and reporting. If relative
data is reported with that option off, vfy keeps the recorded measurements.
Coverage.py's absolute-path lookup can report those lines as unexecuted.

### Semantic decision coverage

Semantic coverage is an opt-in Alpha extension. Runs without it do not perform
semantic analysis; compatible schema-7 readers ignore its additional tables.

| Surface | Contract |
| --- | --- |
| Collection | `vfy coverage run --decisions`, `vfy test --cov-decisions`, `Coverage(decisions=True)`. Decision mode implies branches. |
| Decisions | Boolean control flow, plus named `match` case selection, exception dispatch, cancellation-aware async, and value selection |
| Evidence | Boolean vectors and masking MC/DC pairs, plus named outcomes attributed to contexts, keyed by source digest and structure |
| Persistence | `vfy_*` tables in schema-7 files, written atomically with ordinary coverage. Compatible Coverage.py readers ignore them. |
| Distributed runs | xdist, subprocess, and fork shards keep contexts. `combine` remaps paths, rejects conflicting source digests, and unions identical evidence atomically. |
| Reports | `vfy coverage check` (terminal and JSON); pytest terminal and JSON; independent decision, condition, and MC/DC thresholds |
| Plug-ins | Dynamic-context plug-ins work. File-tracer plug-ins are rejected in decision mode with an error. |

The report refuses evidence for a changed source digest, so the percentage always
applies to the source that ran. Commands, truth tables, exclusions, and the JSON
schema are in the [semantic decision coverage guide](../guides/semantic.md).

## Python API

The Python names that are public are listed in the
[Python API](../reference/python-api.md#public-surface); other `vfy` modules are
internal. The documented `Coverage` and `CoverageData` interfaces target Coverage.py
7.15.4, including constructor and public-method parameter names. Reports can use
active, unsaved data; updates validate before replacing data.
Empty line additions and purged line data retain their collection mode.
Touching a measured file rejects a conflicting plug-in tracer.

Distributions include `py.typed` and native extension declarations. Consumer
checks cover lazy exports, coverage data, reports, and async decorators.

Missing or malformed measured source raises `NoSource` or `NotPython`.
`ignore_errors` omits those files. If none remain, reporting raises `NoDataError`.

`CoverageData.dumps()` uses a vfy-private format. It is not Coverage.py's
serialized representation. It round-trips only through a matching vfy build,
including semantic evidence and contexts.

## Plug-ins

Supported protocol areas: static and dynamic file tracers; line-range and
line/arc translation; custom file reporters and virtual source; dynamic contexts;
configurers; executable-file discovery; and callable and configuration-module
registration. Default plug-in methods are compared directly with Coverage.py
7.15.4.
Custom-reporter include and omit filters are compared with Coverage.py 7.15.4
through `Coverage.report()`, including reports with every source excluded.
Custom reporter branch totals use `exit_counts()`. Single-exit lines do not
count as branches. Custom reporters' `code_regions()` and `arc_description()` callbacks are not
consumed. Their LCOV output omits function records and uses numeric descriptions
for exit arcs.

<a id="coverage-plugin-evidence"></a>

## Plug-in compatibility checks

The protocol fixtures exercise tracer registration, file and line translation, virtual reporters, dynamic contexts, configuration, and discovery. The [third-party package checks](https://github.com/seanabreau/vfy/blob/main/validation/coverage_plugins/README.md) compare saved coverage and reports against Coverage.py. Run them with your release wheel and test your own plug-in’s collection and reporting behavior.

<a id="pytest-html-and-metadata"></a>

## Pytest result HTML and metadata

The coverage plug-in, `vfy.pytest_plugin`, leaves upstream pytest-html and
pytest-metadata enabled under stock pytest. Their existing hooks and fixtures
continue to use the upstream implementations.

To use vfy's own test-results HTML and metadata under stock pytest, explicitly
load `-p vfy.pytest_reporting` alongside the coverage plug-in. This reporting
plug-in replaces pytest-html and pytest-metadata: it blocks their automatic
loading and rejects earlier explicit loads. Native `vfy test` loads vfy's
reporter automatically.

vfy's reporting plug-in and native `vfy test --html` support a subset of pytest-html
4.2.0 and pytest-metadata 3.1.1 with pytest 9.1.1: metadata, redaction, CSS, `extras`
attachments, and the title, summary, and table-header hooks, including native
workers. `generate_report_on_test` publishes completed rows with one worker.
Parallel runs publish merged results after execution.
Base64 PNG attachments preserve decoded bytes in asset-backed reports and use
data URIs in self-contained reports; image content that is not valid base64
remains an external reference. Generated image asset names and extensions can
differ from pytest-html; use the references in the generated HTML. The image
helpers do not accept pytest-html’s `extension=` argument.

Not supported: per-test table-row hooks, upstream imports, internal objects, and
interactive UI. The output is static HTML. See [Pytest result HTML](../guides/run-tests.md#test-results-html).

## Configuration

The [configuration reference](../reference/configuration.md) defines the supported
Coverage.py configuration. Unknown options fail with an error.

## SlipCover

vfy does not provide the `slipcover` executable or import, `[tool.slipcover]`,
JSON input merge, immediate de-instrumentation controls, or Python 3.9–3.11 and
PyPy instrumentation. These are separate interfaces, not aliases.

## Test selection

Test selection and `vfy explain` are vfy extensions. Passing selected runs
combine fresh coverage with saved coverage for skipped tests; those tests are not
rerun. For eligible body-only edits, replay also retains global coverage outside
the edited bodies, including unchanged helpers called during collection of a
skipped module. `--cov-append` runs the full requested suite and appends to
existing data instead of replaying saved results.

See [selection rules and limits](../guides/test-selection.md),
[watch mode](../guides/run-tests.md#rerun-on-save), and
[CI baseline transfer](../guides/ci.md#use-selection-in-ci).

Coverage and JUnit destinations resolved through pytest `addopts` are staged along
with explicit report options. Publishing into an existing report directory preserves
unrelated files. Obsolete watch runs cannot replace accepted outputs.

## Mutation test isolation

`vfy mutate` is a vfy extension. The default native engine requires Linux,
one worker, and serial isolation. With `--engine=pytest-native`, which follows the
native engine's Linux and macOS limits, a single-threaded POSIX process with
`fork()` uses file isolation by default: each test file gets its own fork of
the collected session, with session fixtures created and finalized per file. Changes to Python globals
do not carry between files. Choose `--test-isolation serial` for suites that
share session fixtures or global state across files.

An omitted isolation option selects `serial` when the caller has additional Python
threads or the host lacks POSIX `fork()`. Explicit `--test-isolation file`
requires a single-threaded POSIX process with `fork()`. The clean baseline and
mutation tests use the same isolation mode. See
[mutation test isolation](../guides/mutation.md#isolate-test-files) for the
shared worker budget and supported traceback options.

## Compatibility evidence

Changes to this contract need a differential or upstream-produced fixture, an
edge-case test, and documentation of the public behavior. The
[testing guide](testing.md) maps the checks to their suites.

## Built-in asyncio

The native engine emits the unset loop-scope warning for explicit asyncio
plugin requests or an enabled pytest-asyncio installation. Built-in support
alone does not require that setting.

vfy implements pytest-asyncio’s strict and auto modes, async
fixtures, loop scopes and factories, event-loop policies, port fixtures, and
Hypothesis integration. The [async guide](../guides/asyncio.md) describes
fixture lifetime and configuration on each engine.
[Release validation](testing.md#release-validation) describes installed-wheel
checks. Direct-pytest coverage integration requires pytest-xdist 3.6.1 or newer;
upstream withdrew 3.6.0 for a
[pytest-cov regression](https://pytest-xdist.readthedocs.io/en/latest/changelog.html#pytest-xdist-3-6-0-2024-04-19).
Native xdist execution targets 3.8.0. Performance has
[separate gates](benchmarks.md#how-to-read-benchmark-evidence).

The owned adapter acquires event-loop policies on demand. A pytest-asyncio
compatibility alias activated first retains its autouse policy fixture.
