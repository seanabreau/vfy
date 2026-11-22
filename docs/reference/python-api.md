# Python API

`Coverage` and `CoverageData` provide Coverage.py-compatible collection, data,
and reporting API, plus decision coverage methods. Signatures are checked
against Coverage.py 7.15.4; see [Compatibility](../about/compatibility.md) for
behavioral limits.

## Public surface

vfy's stable contract is the `vfy` command, its configuration, its documented
output and data formats, and these Python names:

- `vfy.Coverage`, `vfy.CoverageData`, and `vfy.VfyError`;
- the exceptions in `vfy.errors`;
- `vfy.semantic.DecisionEvidence`;
- `vfy.plugin.CoveragePlugin`, `vfy.plugin.FileTracer`, and
  `vfy.plugin.FileReporter`, the [Coverage.py plug-in protocols](#coverage-plugins);
- `vfy.async_fixture`, `vfy.asyncio.fixture`, and `vfy.asyncio.is_async_test`;
- the pytest plug-in names passed to `-p`: `vfy.pytest_plugin`,
  `vfy.pytest_reporting`, and `vfy.asyncio_plugin`.

Every other `vfy` module and attribute is internal and may change without
notice, including `vfy.cli`, `vfy.data`, `vfy.analysis`, and the native extension.

Import the classes and exceptions from these paths:

<!-- doc-test: public-imports -->
```python
from vfy import Coverage, CoverageData, VfyError
from vfy.errors import (
    ConfigError,
    DataError,
    NoDataError,
    NoSource,
    NotPython,
    UnsupportedFeatureError,
)
from vfy.semantic import DecisionEvidence
```

## Collection example

Run this from the [tutorial project](../getting-started/quickstart.md#set-up-the-example).
Save it as `api_example.py` and run `uv run python api_example.py`. It measures
both branches of `clamp_low`, saves `.coverage.api`, reads it back, and prints
`[1, 4, 5, 6, 7]`. Exit status is 0.

<!-- doc-test: api-collection -->
```python
from pathlib import Path
import runpy

from vfy import Coverage, CoverageData

coverage = Coverage(data_file=".coverage.api", branch=True, source=["src"], config_file=False)
with coverage.collect():
    bounds = runpy.run_path("src/bounds.py")
    assert bounds["clamp_low"](-1) == 0
    assert bounds["clamp_low"](5) == 5
coverage.save()

data = CoverageData(basename=".coverage.api")
data.read()
print(sorted(data.lines(str(Path("src/bounds.py").resolve()))))
```

The remaining examples are fragments. They assume a configured `coverage`
object and application-provided `run_code` or `module_or_path`.

## `Coverage`

### Constructor

The call below leaves `data_file` unset, allowing `COVERAGE_FILE` or
configuration to select the destination. Other keyword arguments show their
defaults.

```python
Coverage(
    data_suffix=None,
    cover_pylib=None,
    auto_data=False,
    timid=None,
    branch=None,
    config_file=True,
    source=None,
    source_pkgs=None,
    source_dirs=None,
    omit=None,
    include=None,
    debug=None,
    concurrency=None,
    check_preimported=False,
    context=None,
    messages=False,
    plugins=None,
    decisions=False,  # vfy extension; omitted from compatibility introspection
)
```

- Omit `data_file` to use `COVERAGE_FILE`, then `[run] data_file`, then
  `.coverage`. An explicit `data_file=".coverage"` overrides those settings.
- `source` accepts existing paths and importable package or module names. A
  name such as `pkg` can select a package in a `src` layout. A `source_pkgs`
  name that was never imported is not an error at construction; as in
  Coverage.py, the data-saving step emits a `CoverageWarning` from
  `vfy.errors` with the slug `module-not-imported`, and `no-data-collected`
  when nothing was measured.
- Pass `data_file=None` for in-memory coverage. `save()` writes no database in
  this mode.
- `timid=True` raises `ValueError`.
- `concurrency` accepts `"thread"` and `"multiprocessing"`, individually or as
  an iterable. A name Coverage.py does not define raises `ConfigError`. The
  Coverage.py names `eventlet`, `gevent`, and `greenlet` raise `ValueError`.
- `decisions=True` enables decision, condition, and masking MC/DC collection, and
  implies branch collection. It is accepted at runtime.
  `inspect.signature(Coverage)` keeps the Coverage.py signature.

<a id="collect"></a>

### Collect data

```python
coverage.start()
try:
    run_code()
finally:
    coverage.stop()
coverage.save()
```

Or use the context manager:

```python
with coverage.collect():
    run_code()
```

- `Coverage.current()` returns the innermost active instance in the current thread.
- `switch_context(name)` changes the static context while collection is active
  and raises `VfyError` when collection is not started.
- `get_data()` and `save()` include measurements so far without stopping
  collection, so you can call them as checkpoints. A checkpoint validates before
  it clears pending data.
- `stop()` releases monitoring masks and callbacks. If source or plug-in
  discovery fails, calling `stop()` again retries with the collected measurements.

### Analyze source

```python
filename, statements, missing, formatted = coverage.analysis(module_or_path)
filename, statements, excluded, missing, formatted = coverage.analysis2(module_or_path)
stats = coverage.branch_stats(module_or_path)
```

`Morf` accepts a path string, `os.PathLike`, a module, or an object exposing
`__file__`. A module whose `__file__` is `None` raises `TypeError`.
`branch_stats()` maps each origin line to `(possible_exits, taken_exits)`.

```python
analysis(morf: Morf) -> tuple[str, list[int], list[int], str]
analysis2(morf: Morf) -> tuple[str, list[int], list[int], list[int], str]
branch_stats(morf: Morf) -> dict[int, tuple[int, int]]
```

### Configure exclusions and options

```python
coverage.exclude(r"raise NotImplementedError")
coverage.exclude(r"pragma: no branch", which="partial")
coverage.get_exclude_list()
coverage.clear_exclude()

coverage.set_option("report:precision", 2)
coverage.get_option("report:precision")
```

`which` accepts `"exclude"` (the default) or `"partial"`; other values raise
`ValueError`. Invalid exclusion regexes raise `re.error`. Unsupported option
names raise `vfy.errors.ConfigError`; registered plug-in sections accept
`<plugin>:<option>` names. `get_option("paths")` returns the `[paths]` table, and
`get_option("run:data_file")` returns the configured name as written.

```python
exclude(regex: str, which: str = "exclude") -> None
clear_exclude(which: str = "exclude") -> None
get_exclude_list(which: str = "exclude") -> list[str]
get_option(option_name: str) -> Any
set_option(option_name: str, value: Any) -> None
```

`report:exclude_lines` and
`report:partial_branches` replace their default patterns. `report:exclude_also`
and `report:partial_also` add to them.

### Write reports

Report methods: `report`, `html_report`, `json_report`, `xml_report`,
`lcov_report`, and `annotate`. They include active and unsaved in-memory data.

`report(skip_covered=True, skip_empty=True)` hides file rows while preserving
the aggregate total and returned percentage in each output format.

- `report` takes `output_format="text"`, `"markdown"`, or `"total"`, and
  `sort="name"`, `"stmts"`, `"miss"`, `"branch"`, `"brpart"`, or `"cover"`.
- `json_report` includes function and class regions for Python source.
- `annotate(directory=...)` uses Coverage.py's flattened filenames to distinguish
  sources in different directories. With no directory, it writes beside each
  source with the suffix `,cover`.
- `lcov_report` replaces nonprintable filename characters with `?`. A missing
  branch arm has hit value `0` when another outgoing arm was executed, and `-`
  when no outgoing arm was executed.
- Constructor `include` and `omit` replace the `[report]` patterns; `[run]`
  patterns select collection only.
- An omitted argument uses its configured value, including output paths,
  precision, and JSON context display. `xml_report` also uses `xml:package_depth`.
  XML paths are relative to the matching `run:source` root. `run:relative_files`
  controls whether those roots stay relative.

Every report method raises `NoSource` when measured source is missing and
`NotPython` when a file cannot be parsed. Pass `ignore_errors=True` to skip those
files. If that leaves no measured files, the method raises `NoDataError`.

The signatures below omit `self`.

```python
report(morfs: Iterable[Morf] | None = None,
       show_missing: bool | None = None, ignore_errors: bool | None = None,
       file: TextWriter | None = None, omit: Sequence[str] | None = None,
       include: Sequence[str] | None = None, skip_covered: bool | None = None,
       contexts: Sequence[str] | None = None, skip_empty: bool | None = None,
       precision: int | None = None, sort: str | None = None,
       output_format: str | None = None) -> float
html_report(morfs: Iterable[Morf] | None = None, directory: str | None = None,
            ignore_errors: bool | None = None, omit: Sequence[str] | None = None,
            include: Sequence[str] | None = None, extra_css: str | None = None,
            title: str | None = None, skip_covered: bool | None = None,
            show_contexts: bool | None = None, contexts: Sequence[str] | None = None,
            skip_empty: bool | None = None, precision: int | None = None) -> float
json_report(morfs: Iterable[Morf] | None = None, outfile: str | None = None,
            ignore_errors: bool | None = None, omit: Sequence[str] | None = None,
            include: Sequence[str] | None = None, contexts: Sequence[str] | None = None,
            pretty_print: bool | None = None, show_contexts: bool | None = None) -> float
xml_report(morfs: Iterable[Morf] | None = None, outfile: str | None = None,
           ignore_errors: bool | None = None, omit: Sequence[str] | None = None,
           include: Sequence[str] | None = None, contexts: Sequence[str] | None = None,
           skip_empty: bool | None = None) -> float
lcov_report(morfs: Iterable[Morf] | None = None, outfile: str | None = None,
            ignore_errors: bool | None = None, omit: Sequence[str] | None = None,
            include: Sequence[str] | None = None, contexts: Sequence[str] | None = None) -> float
annotate(morfs: Iterable[Morf] | None = None, directory: str | None = None,
         ignore_errors: bool | None = None, omit: Sequence[str] | None = None,
         include: Sequence[str] | None = None, contexts: Sequence[str] | None = None) -> None
```

### Lifecycle methods

```python
start() -> None
stop() -> None
collect() -> Iterator[None]
current() -> Coverage | None
sys_info() -> list[tuple[str, object]]
switch_context(new_context: str | None) -> None
get_data() -> CoverageData
load() -> None
save() -> None
erase() -> None
combine(data_paths: Iterable[str] | None = None, strict: bool = False,
        keep: bool = False) -> None
```

- `load()` and `save()` read and write the configured file.
- `erase()` removes the data file. In parallel mode, it also removes shards
  when the data file is the main file. The `data_suffix` constructor argument
  sets parallel mode when supplied. Multiprocessing also enables parallel mode.
- `combine()` merges shards and applies configured `[paths]` aliases.
- `get_data()` returns the `CoverageData` instance.
- `sys_info()` returns engine, parser, build, Python, mode, and plug-in details.

### Semantic report

```python
coverage = Coverage(source=["src"], decisions=True)
with coverage.collect():
    run_code()
coverage.save()

decision_data = coverage.get_decision_data()
report = coverage.decision_report(
    format="json",
    output="build/vfy-decisions.json",
    pretty=True,
)
print(report.decision_percent, report.condition_percent, report.mcdc_percent)

for pair in decision_data.evidence_records():
    print(pair.condition, pair.left_context, pair.right_context)
```

`get_decision_data()` returns read-only Boolean definitions, condition ranges,
tri-state observations, named path outcomes, and `DecisionEvidence` pairs
showing each condition independently changing a decision outcome (masking MC/DC).
Named outcomes count toward `decision_percent` but have no condition or MC/DC
obligations.

`decision_report()` accepts keyword-only arguments: `format="text"` or
`"json"`, `output=None`, `pretty=False`, and filters `include`, `omit`,
`contexts`, `source`, and `exclude_decisions` (each defaults to `None`).
It always returns a structured report; it writes a rendering only when `output`
is supplied. An unsupported format raises `ValueError`. Evidence condition
indexes are zero-based.

```python
get_decision_data() -> DecisionData
decision_report(*, include: Sequence[str] | None = None,
                omit: Sequence[str] | None = None,
                contexts: Sequence[str] | None = None,
                source: Sequence[str] | None = None,
                exclude_decisions: Sequence[str] | None = None,
                format: str = "text", output: str | None = None,
                pretty: bool = False) -> DecisionReport
```

`DecisionData` exposes tuple properties `definitions`, `observations`,
`path_definitions`, and `path_observations`, plus these methods:

```python
has_data() -> bool
evidence_records(decisions: Iterable[str] = (),
                 contexts: Iterable[str] = ()) -> tuple[DecisionEvidence, ...]
```

`DecisionEvidence` contains `decision`, zero-based `condition`, and each
observation's context, vector, and outcome as `left_context`, `left_vector`,
`left_outcome`, `right_context`, `right_vector`, and `right_outcome`.

`DecisionReport.rows` is a tuple of per-decision rows. Report totals are
`decisions_covered`, `decisions_total`, `conditions_covered`, `conditions_total`,
`mcdc_covered`, and `mcdc_total`. The percentage properties return 100 when their
total is zero.

Reports raise `NoDataError` when no semantic data matches and `DataError` for
stale source observations before writing output. This includes a stale decision
in a file whose other decisions match the current source. Obtain reports through
`decision_report()`; constructing one by hand is not public API.

#### Semantic JSON

The Alpha JSON rendering uses `schema: "vfy.semantic"`; this shape may change.
`totals` contains `decisions`, `conditions`, and `mcdc` objects with `covered`
and `total`, plus their percentages under `percent`.

`files` contains one entry per decision, so paths can repeat. Each entry has
`path`, `decision` (the key), `source_digest`, `line`, `kind`, `expression`,
`logic`, `conditions_definition`, `vectors`, `outcomes`, the three covered/total
objects, and `missing` descriptions.

Each condition definition contains a zero-based `index`, `line`, `column`,
`end_line`, and `end_column`. Each Boolean vector contains `context`, `vector`
(`0` = false, `1` = true, `-` = unevaluated), and a Boolean `outcome`. Each named
outcome contains `label`, `covered`, and `contexts`. Named outcomes have zero
condition and MC/DC totals.

## `CoverageData`

```python
data = CoverageData(
    basename=None,
    suffix=None,
    no_disk=False,
    warn=None,
    debug=None,
)
```

`basename=None` uses `.coverage`. `suffix=True` adds a hostname, process ID,
and random token; a string suffix is appended literally. `no_disk=True` makes
`read()` and `write()` no-ops.

### Read, write, and combine

```python
base_filename() -> str
data_filename() -> str
read() -> None
write() -> None
erase(parallel: bool = False) -> None
update(other_data: CoverageData, map_path: Callable[[str], str] | None = None,
       aliases: PathMapper | None = None) -> None
combine(data_paths: Iterable[str] | None = None, *, strict: bool = False,
        keep: bool = False) -> None
```

`read()` clears the in-memory dataset when the file is absent. `erase()` clears
it and removes the selected file; `parallel=True` also removes the basename and
its shards. `combine()` writes the merged file and removes shards unless
`keep=True`; `strict=True` raises `NoDataError` when no shards are available.

`update()` remaps every measured path with `map_path`, or with the `map(path)`
method of `aliases` when `map_path` is omitted. Mapping conflicts raise `DataError`.

`dumps()` and `loads(data)` use a private format for matching vfy builds. See
[Data files](data-files.md#private-serialized-bytes) for merge and serialization
failure guarantees.

### Query

```python
has_arcs() -> bool
measured_files() -> set[str]
measured_contexts() -> set[str]
lines(filename: str) -> list[int] | None
arcs(filename: str) -> list[tuple[int, int]] | None
contexts_by_lineno(filename: str) -> dict[int, list[str]]
file_tracer(filename: str) -> str | None
```

Unknown filenames return `None` from `lines()`, `arcs()`, and `file_tracer()`,
and `{}` from `contexts_by_lineno()`. Lines and arcs are sorted. For line-only
data, `arcs()` returns `[]`. `lines()`, `arcs()`, and `contexts_by_lineno()` use
the current query-context selection.
Context selection leaves `has_arcs()`, `measured_files()`, `measured_contexts()`,
and `file_tracer()` unchanged.

### Add and select data

```python
add_lines(line_data: Mapping[str, Collection[int]]) -> None
add_arcs(arc_data: Mapping[str, Collection[tuple[int, int]]]) -> None
add_file_tracers(file_tracers: Mapping[str, str]) -> None
touch_file(filename: str, plugin_name: str = "") -> None
touch_files(filenames: Collection[str], plugin_name: str = "") -> None
purge_files(filenames: Collection[str]) -> None
set_context(context: str | None) -> None
set_query_context(context: str) -> None
set_query_contexts(contexts: list[str] | None) -> None
dumps() -> bytes
loads(data: bytes) -> None
close(force: bool = False) -> None
sys_info() -> list[tuple[str, str]]
```

`set_context()` labels subsequent additions; `None` selects the empty label.
`set_query_context()` selects one exact context. `set_query_contexts()` selects
contexts using regex searches. Both `None` and `[]` clear the filter.
Invalid regexes raise `re.error`.

`purge_files()` removes measurements in memory; `write()` persists the removal.
`purge_files()` raises `DataError` before the first addition of lines or arcs.
`write()` leaves an unused, unsuffixed data file untouched. An empty arc set
registers no measured file.
A later collection records fresh measurements. `close()` is a compatibility
no-op: vfy keeps no SQLite connection open. Adding line records to branch
data, or branch records to initialized line data, raises `DataError`. Empty
additions establish the collection mode; purging measurements preserves it.
`add_file_tracers()` rejects a conflicting batch without changing any files.
Coverage.py 7.15.4 can retain earlier entries from the same batch on conflict.
`touch_file()` and `touch_files()` reject conflicting plug-in names with
`DataError` and retain the existing tracer. Touching files requires an established
collection mode, including an empty addition. A rejected touch batch leaves all
files unchanged.

<a id="coverage-plugins"></a>

## Coverage plug-ins

Import `CoveragePlugin`, `FileTracer`, and `FileReporter` from `vfy.plugin`.
Coverage plug-ins use these protocols; pytest plug-ins use pytest's separate
registration system.

Pass initializer callables to `Coverage(plugins=[initializer])`. Each receives
an object with these registration methods:

```python
add_file_tracer(plugin: CoveragePlugin) -> None
add_configurer(plugin: CoveragePlugin) -> None
add_dynamic_context(plugin: CoveragePlugin) -> None
add_noop(plugin: CoveragePlugin) -> None
```

For configuration-based registration, an importable module provides
`coverage_init(registration, options)`. List its module name in `run:plugins`;
its plug-in option section supplies `options`. The module must remain importable
when saved data is reported: tracer identities are persisted with measured files.

### Plug-in protocol

Signatures omit `self`:

```python
file_tracer(filename: str) -> FileTracer | None
file_reporter(filename: str) -> FileReporter | Literal["python"]
find_executable_files(src_dir: str) -> Iterable[str]
configure(config: object) -> None
dynamic_context(frame: FrameType) -> str | None
sys_info() -> Iterable[tuple[str, object]]
```

`file_tracer()` returns `None` to leave a file to ordinary Python collection.
A registered file-tracer plug-in must implement `file_reporter()`: return
`"python"` for Python source or a custom reporter for other source. The base
method raises `NotImplementedError`. `find_executable_files()` discovers
unexecuted plug-in source beneath each source root.
Report `include` and `omit` patterns match each custom reporter's `filename`.
vfy obtains reporter objects before filtering and skips excluded sources
before calling their source and analysis methods.
Report output displays `relative_filename()`. Measurements use the mapped
`filename`; annotation without a directory writes beside that filename.

Register `configure()` with `add_configurer()`; it receives the `Coverage`
instance and can use `get_option()` and `set_option()`. Register
`dynamic_context()` with `add_dynamic_context()`; a non-`None` label starts a
frame-scoped context. With a static label, the stored label is `static|dynamic`.
Per-test contexts append `nodeid|phase` to the static context.

### File-tracer protocol

```python
source_filename() -> str
has_dynamic_source_filename() -> bool
dynamic_source_filename(filename: str, frame: FrameType) -> str | None
line_number_range(frame: FrameType) -> tuple[int, int]
```

Implement `source_filename()` for a fixed source path. For frame-dependent
paths, return `True` from `has_dynamic_source_filename()` and implement
`dynamic_source_filename()`; `None` skips that frame's source. A line range is
inclusive; the default is `(frame.f_lineno, frame.f_lineno)`. `(-1, -1)` skips
the event.

### File-reporter protocol

Construct a reporter with `FileReporter(filename)` and implement `lines()`.
The other methods have defaults shown by their contracts:

```python
relative_filename() -> str
source() -> str
lines() -> set[int]
excluded_lines() -> set[int]
arcs() -> set[tuple[int, int]]
no_branch_lines() -> set[int]
exit_counts() -> dict[int, int]
translate_lines(lines: Iterable[int]) -> set[int]
translate_arcs(arcs: Iterable[tuple[int, int]]) -> set[tuple[int, int]]
source_token_lines() -> Iterable[list[tuple[str, str]]]
code_region_kinds() -> Iterable[tuple[str, str]]
code_regions() -> Iterable[CodeRegion]
arc_description(start: int, end: int) -> str
missing_arc_description(start: int, end: int,
                        executed_arcs: Iterable[tuple[int, int]] | None = None) -> str
```

The default `source()` reads UTF-8 text from `filename`; `relative_filename()`
returns that filename. Exclusions, arcs, no-branch lines, exit counts, and code
regions default to empty collections. Translation methods return their inputs
as sets. Token lines default to plain text. A `CodeRegion` provides `kind`,
`name`, `start`, and `lines` attributes.

### Fixed-source example

This standalone example compiles Python under a generated filename, maps its
measurements back to the original source, and reports that source. Save it as
`plugin_example.py` in a project with [vfy installed](../getting-started/installation.md)
and run `uv run python plugin_example.py`.
It writes `example.py`, `example.py.generated`, and `.coverage`, and prints a
100% text report.

```python
from pathlib import Path

from vfy import Coverage
from vfy.plugin import CoveragePlugin, FileTracer


class GeneratedTracer(FileTracer):
    def __init__(self, source_filename: str) -> None:
        self._source_filename = source_filename

    def source_filename(self) -> str:
        return self._source_filename


class GeneratedPlugin(CoveragePlugin):
    def file_tracer(self, filename: str) -> FileTracer | None:
        if filename.endswith(".py.generated"):
            return GeneratedTracer(filename.removesuffix(".generated"))
        return None

    def file_reporter(self, filename: str) -> str:
        return "python"


def initialize(registration):
    registration.add_file_tracer(GeneratedPlugin())


source = Path("example.py").resolve()
source.write_text("answer = 40 + 2\n", encoding="utf-8")
generated = Path(str(source) + ".generated")
generated.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
coverage = Coverage(plugins=[initialize], config_file=False)
with coverage.collect():
    exec(compile(generated.read_text(encoding="utf-8"), str(generated), "exec"), {})
coverage.save()
coverage.report(morfs=[str(source)])
```

Validate a plug-in against Coverage.py using measured filenames, translated
lines/arcs, discovered files, exclusions, no-branch lines, rendered source,
contexts, and saved tracer identities. Package-specific results are recorded in
[Compatibility](../about/compatibility.md#plug-ins).

## Exceptions

`VfyError` is exported from `vfy`. Import `ConfigError`, `DataError`,
`NoDataError`, `UnsupportedFeatureError`, `NoSource`, `NotPython`, and the
`CoverageWarning` category from `vfy.errors`, as in the imports above.

## Built-in asyncio pytest support

`vfy.async_fixture` is the main decorator. `vfy.asyncio.fixture` and
`vfy.asyncio.is_async_test` support mechanical migrations. Importing these
does not start coverage or enable a pytest plug-in. Direct pytest runs need
`-p vfy.asyncio_plugin`.

See [Test asyncio code](../guides/asyncio.md) for migration, examples, defaults,
and loop factories. See [pytest asyncio settings](configuration.md#pytest-asyncio-settings)
for the settings this reads.
