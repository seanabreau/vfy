# Architecture

vfy implements Coverage.py and pytest interfaces with Rust coverage and test
engines. Use this page to locate the component responsible for a change and
check the invariants it must preserve.

## Components and ownership

| Component | Owns |
| --- | --- |
| `python/vfy/` | Public Coverage.py and pytest protocols, pytest adapters, stdout, and calls into third-party plug-ins |
| `python/vfy/cli*.py` | CLI parsing, command dispatch, diagnostics, and report output |
| `python/vfy/_pytest/` | Private adapters between installed pytest and the native engine: collection, caches, JUnit, xdist transport, and run preparation |
| `python/vfy/run/` | The `vfy test` command: engine choice, selection and replay, CI baselines, and watch mode |
| `python/vfy/independent/` | The pytest-compatible interfaces the independent engine provides without installed pytest |
| `vfy-core` | The PyO3-free data, analysis, configuration, selection, mutation, and report engine |
| `crates/vfy` (`vfy._native`) | CPython monitoring and object conversion |
| `crates/vfy-pytest` | Test collection and execution, built into the same extension |

Rust also owns coverage collection, scheduling, aggregation, rendering, and
atomic filesystem commits. By default, vfy collects and runs tests with its independent
engine. The optional `--engine=pytest-native` mode uses installed pytest for
collection. Python adapts CPython and plug-in protocols for both engines.

## Invariants

- Monitoring events go straight to Rust. Python is not on the event path.
- One Rust data and report model. Every renderer uses the same totals. No Python
  fallbacks or duplicate algorithms. `pytest.approx` is the exception: its
  tolerance checks call `__eq__`, `__sub__`, and `__abs__` on arbitrary user
  objects, so `python/vfy/independent/approx.py` implements it in Python.
- Validate data and report inputs before replacing destination files. Each file
  replacement is atomic; multi-file publication prepares rollback copies and
  reports any backup that could not be restored.
- Persisted and public ordering is deterministic.
- Stock pytest is a differential oracle, never an automatic fallback.

## Runtime boundaries

Monitoring events enter Rust directly. Configured third-party Python plug-ins
cross back into Python through the adapter.

<p class="diagram-scroll-hint">Scroll horizontally to view the full diagram.</p>

<div class="architecture-diagram" tabindex="0" role="region" aria-label="Runtime boundaries diagram" markdown="1">

![CPython events and batched Python API calls enter vfy._native, which uses vfy-core and calls configured Python plug-ins.](../assets/diagrams/runtime-boundaries-light.svg#only-light)
![CPython events and batched Python API calls enter vfy._native, which uses vfy-core and calls configured Python plug-ins.](../assets/diagrams/runtime-boundaries-dark.svg#only-dark)

</div>

[Diagram structure (Mermaid)](../assets/diagrams/runtime-boundaries.mmd).

Python reporting adapters call configured file reporters separately.

### Async pytest boundary

`vfy.asyncio_plugin` is the installed-pytest asyncio adapter. It classifies
callables per session, rechecks their code after instrumentation, and creates lazy
`asyncio.Runner` instances through pytest fixtures. In native runs, Rust plans
fixture dependencies and loop scopes. Python awaits coroutines, propagates context,
finalizes generators, and closes runners.

## Test execution

`vfy test --engine=native` owns collection, fixture resolution, execution,
and reporting without requiring pytest. Its Python facade exposes the pytest
interfaces used by test suites and adapts Python objects to Rust operations.
Compatibility names identify those public protocols; they do not delegate
execution to pytest. See [compatibility limits](compatibility.md).

### Invocation preparation and execution

The independent and selection controllers share a Rust plan for scope, workers,
scheduling, and replay. Python supplies interpreter-specific locations, paths,
and resolved pytest configuration. Each invocation owns its child processes,
temporary results, coverage, and any pending dependency fingerprint scan.

### Native lifecycle owners

Native capture owners retain redirected descriptors, stream objects, and phase
state. Rust reads captured output; Python stream and logging operations remain
observable through their public protocols. Logging handlers and their shared
formatter belong to the configured session. Each test owns separate phase records,
and phase guards unwind handler attachments on errors.

Each native fixture binding owns its callable, scoped outcome, and finalizers.
Reading `cached_result` exposes the same authoritative outcome as a public tuple;
later public replacements remain visible to fixture resolution and teardown.

Report history stores phase values under stable IDs. Public hooks and readers
observe the same Python report objects and their mutations. Serial execution
reads live reports at final publication so hook edits reach machine results;
workers and JUnit writers retain the projections required at their earlier
publication boundaries.

Hook dispatch uses the active conftest registrations; when none exist, it omits
item-path filtering. Registration changes update that decision. Calls into tests,
fixtures, plug-ins, descriptors, and streams cross the actual CPython boundary;
native ownership preserves the public objects and callbacks observed there.

### Native collection

`vfy test --engine=pytest-native` uses installed pytest to collect the test source. The
adapter compiles it into a validated Rust plan and Python bindings. Rust resolves
and runs fixtures. CPython calls the bound test and fixture functions.

The plan holds typed test and fixture IDs, scope and dependency chains, and validated
references. The adapter keeps pytest items, callables, and live parameter values in
aligned binding tables. Dynamic fixture discovery crosses this boundary, and Rust
updates the plan before execution. A parameter change invalidates the affected
dependency closure, and teardown runs in reverse setup order.

Each fixture has one live cached instance. A cache hit keeps the instance's original
owner. Owners unwind from inner to outer, and finalizers run in reverse registration
order within each owner. A test reads the live callable and its code after fixture
setup.

### Workers

One worker is the default. Native file-group workers (`-n`, `-j`, or
`--workers`) fork the collected session when eligible. Each worker owns fixture
lifetimes, capture files, and an empty coverage collector. Otherwise, fresh workers
collect independently; the controller checks collection agreement. It merges
results in collection order, combines collection coverage with worker shards, and
publishes final reports. Workers never publish them independently.

Pytest's `--numprocesses` selects pytest-xdist scheduling and transport. Each xdist
worker keeps a native session across dispatches and emits pytest phase reports.
An explicit fork request rejects an ineligible run. See
[worker start policy](../reference/configuration.md#native-worker-start)
for eligibility and collection overrides.

A passing full run on a clean checkout stores source and execution identities
with the baseline used by selection and mutation.

### Stock pytest oracle

Stock pytest execution is an internal differential oracle. It shares the Rust coverage
model, and pytest runs the tests. Rust keeps a bounded failure-confirmation history for
both routes. The override is hidden and is never an automatic fallback.

Native HTML reporting passes the execution result handle to the Rust renderer. Python
supplies metadata and supported customization-hook values. Attachments travel with
validated worker result records, and the controller publishes the merged report.

## Watch execution ownership

The Rust watch controller handles file events, debounce, run identity, recovery, and
publication. One run is active per session. Before planning, the controller captures a
content-based identity, and the caller cannot substitute another at publication. Python
adapts pytest options, hooks, signals, and output, and starts a fresh test process for
each run.

Rust checks file contents and delivered events before it accepts a result. An
invalidated run stays obsolete, even if the edit is reverted. Accepted coverage stores
its input identity, so later selection rejects coverage from different source. Only a
current, passing full run clears recovery state.

Publication validates every staged artifact and destination first. Each file is replaced
atomically, with rollback copies prepared on disk. If publication fails, or the run goes
obsolete during finalization, Rust restores every replaced file. A failed restore does not
stop the others, and the error names any backup it kept. Publication and completion share
one commit point. Edits after acceptance belong to the next run.

The adapter releases the GIL while Rust waits for changes or publishes.

## The Rust handle boundary

Readers and collectors return a shared Rust handle. Reports use it directly. A mutable API
call or a configured Python file reporter materializes `FileCoverage` views (dashed path).
Ordinary reporting never does.

<p class="diagram-scroll-hint">Scroll horizontally to view the full diagram.</p>

<div class="architecture-diagram" tabindex="0" role="region" aria-label="Rust handle boundary diagram" markdown="1">

![Collector snapshots and SQLite data feed a shared CoverageData handle, report model, renderers, and output. Mutable APIs and file reporters use Python views.](../assets/diagrams/rust-handle-boundary-light.svg#only-light)
![Collector snapshots and SQLite data feed a shared CoverageData handle, report model, renderers, and output. Mutable APIs and file reporters use Python views.](../assets/diagrams/rust-handle-boundary-dark.svg#only-dark)

</div>

[Diagram structure (Mermaid)](../assets/diagrams/rust-handle-boundary.mmd).

Rust resolves configuration and path aliases, combines shards, and commits report files.
Python writes stdout or caller-owned file objects, and calls third-party reporters. Mixed
line and branch data fails validation before any output changes. Every renderer uses the
same Rust totals.

## Collection hot path

Monitoring callbacks record observations without parsing source or walking branch
trails. `PY_START` resolves and caches file identity; `LINE` records a hit in the
current context. Line locations disable after their first hit and reactivate when
collection starts or context changes. Code compiled before and after collection starts
is collected.

Branch strategies preserve frame state for suspension and exception transitions.
Direct branch collection deduplicates bytecode edges and resolves logical statement
destinations at snapshot time. Event retirement is separate from frame lifecycle.

Interned contexts, sparse 64-line chunks, compact arc pairs, and copy-on-write
partitions avoid dense allocation and duplicate unchanged data. Schema-7 line
reads share equal line sets among the contexts of each file; later updates copy
on write. Public API and schema-7 records materialize deterministic strings and
sorted observations.

The extension holds coverage data behind an immutable shared handle. Reports
acquire that handle before releasing the GIL. Successful mutations detach their
copy; fallible mutations replace the shared value only after validation.

## Source analysis reuse

Immutable source analysis is cached by source bytes, format version, exclusion and
partial-branch expressions, branch mode, and interpreter annotation semantics.
The cache is bounded by entries and estimated bytes. Compiled regular expressions
bypass it because their text does not capture every matching option. New coverage
observations rebuild aggregation without reparsing source.

After `fork()`, a child avoids the inherited cache mutex and analyses without
caching until exec. File selection precedes context matching; both filtering and
analysis run outside the GIL. Context validation precedes source analysis, with
errors ordered by context and path.

## Semantic coverage collection

Decision coverage shares the collector and report model. It does nothing unless enabled.
Source analysis defines stable decision and condition IDs. Monitoring events contribute
vectors of true, false, or not-evaluated values. The report evaluator uses these
vectors to check decision, condition, and MC/DC coverage without re-running user
expressions.

The collector never calls `bool()`, keeps condition objects, or rewrites bytecode. If
condition evaluation raises, the incomplete invocation is discarded. If a short-circuit
operand is skipped, the vector records `not evaluated`, so masking analysis cannot claim
unobserved evidence.

## Mutation-testing path

`vfy mutate` checks whether the suite detects changes to source expressions. It applies
each mutation in a child process and never edits source files.

<p class="diagram-scroll-hint">Scroll horizontally to view the full diagram.</p>

<div class="architecture-diagram" tabindex="0" role="region" aria-label="Mutation testing path diagram" markdown="1">

![A mutation uses a safe collected-session fork or a patched import in a child, then runs tests and saves the result.](../assets/diagrams/mutation-testing-path-light.svg#only-light)
![A mutation uses a safe collected-session fork or a patched import in a child, then runs tests and saves the result.](../assets/diagrams/mutation-testing-path-dark.svg#only-dark)

</div>

[Diagram structure (Mermaid)](../assets/diagrams/mutation-testing-path.mmd).

Rust builds the catalog, ranks tests from coverage contexts, and keys the cache. The
Python adapters handle CPython and pytest lifecycle hooks and print results. A collected
session is reused only when the mutation owner supports a code-object swap,
POSIX fork is available, no other threads are active, and portable execution
was not requested. The source hash and exact UTF-8 byte range must match before
launch and during import, or the mutation is rejected.
A test failure counts as protection only if the unchanged source passes the baseline tests.
Import-time mutations run the full configured test scope. Body mutations can select tests
from recorded coverage contexts.

## Test selection

Rust compares Git changes with a passing full-run baseline. It joins edited
source ranges, per-test coverage, and reverse import dependencies to produce a
selection plan. With scoped collection, the plan names the test files the
independent engine collects. The other files keep their baseline test IDs.
Passing selected runs combine fresh coverage with saved data for
skipped tests; failed runs keep the passing baseline. The
[selection guide](../guides/test-selection.md#how-selection-decides) owns the
rules, fallback conditions, and limits.

## Ruff parser and import graph

Coverage analysis, mutation discovery, and import-based selection use Ruff's
`ruff_python_parser` and `ruff_python_ast` at a pinned version. Their Rust APIs are
unstable, so the crates are exact-pinned. Updating them requires the full differential
analysis suite. The parser stays out of monitoring callbacks.

The import graph parses ordinary imports with the Ruff AST, resolves files within
configured source roots, computes reverse dependents, caches fingerprints, and joins file
identities to observed pytest node IDs. Dynamic imports such as `importlib` and
`__import__` are not seen, so selection can miss them. Reflection, environment-based
resolution, and type-aware import semantics are outside this contract.
