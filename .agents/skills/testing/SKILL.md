---
name: testing
description: >-
  Use when adding, reviewing, fixing, or removing Rust #[cfg(test)] modules
  under crates/*/src/, Python tests under tests/integration/ or tests/, semantic
  fixtures, or benchmarks,
  including Coverage.py differentials, pytest/xdist processes, storage,
  reports, selection, scheduling, or performance regressions.
---

# Test vfy

Write the smallest durable proof of a user-visible contract. Test count and
coverage percentage are not quality goals; every test must protect a distinct
behavior or remove a demonstrated uncertainty.

Read `docs/about/testing.md` and the nearest existing test before editing. For a
Coverage.py or SlipCover contract, also use `$coverage-compatibility`. For a
performance claim or regression, also use `$coverage-performance`.

When running `uv sync` or `uv run` from a linked worktree, bind
`UV_PROJECT_ENVIRONMENT` to an environment inside that worktree first. Never
mutate another checkout's environment; see the worktree setup guidance in
`CONTRIBUTING.md`.

Use a separate Cargo target directory for each checkout. If
`CARGO_TARGET_DIR` is inherited, set it to that worktree's `target/` before
building or testing. Sharing compiled artifacts between worktrees can reuse
stale outputs when source timestamps differ, even when commands are serialized.
Downloaded crates can share `CARGO_HOME`.

Before counting subprocess tests as evidence for a source change, verify the
selected vfy installation. `VfyProject` uses the active installation; reading test
files from a checkout does not make children execute that checkout's package.
Release validation must match the installed Python payload and native extension
to the source-bound wheel manifest. After an artifact mismatch, rerun the owning
test with the intended interpreter. Keep frozen comparison environments unchanged.

Give paired subprocess oracles the same random inputs. When observing hash or
equality calls, set the same `PYTHONHASHSEED` in both engines and exercise collision
cases: randomized dictionary probing can legitimately change those calls.

Pin collection order in process fixtures that require particular tests to start
first, including when ordering plug-ins autoload. Put readiness signals inside
the `try` block whose `finally` records or performs cleanup.

Keep orchestration settings out of validation subprocesses. Use `python -B`
for helpers instead of exporting `PYTHONDONTWRITEBYTECODE`, which changes the
cache behavior under test. Give process tests a short, real `TMPDIR`; include
CPython's generated socket-name suffix when checking the Unix socket path limit.

## Organize tests by ownership

Keep product boundary tests under `tests/integration/`, retaining pytest's
`test_*.py` naming. Name suites after the owned behavior; a compatibility or
boundary suite may cover more than one implementation module. Keep architecture,
distribution metadata, CI tooling, and harness checks in `tests/repository/`.
Keep shared process helpers in `tests/support/` and reviewed semantic inputs
in `tests/fixtures/`.
Keep focused product regressions in discoverable pytest tests. A standalone
checker is appropriate for artifact or corpus orchestration; a small rendered
output fixture should use explicit expectations in its owning integration suite.
When moving a test, update its repository-root and fixture paths, CI workflow
paths, and documented focused commands.

Keep Rust unit tests in a sibling `tests.rs` beside the module they exercise.
For a source module such as `src/analysis.rs` or `src/selection.rs`, put
the tests at `src/analysis/tests.rs` or `src/selection/tests.rs` and
declare them with `#[cfg(test)] mod tests;`; crate-root tests belong in
`src/tests.rs`. Nested implementation modules should use the same directory
topology. Do not leave large inline suites in production files or collapse
unrelated Rust tests into a repository-wide test file.

Initialize Python explicitly in each test that attaches to it, or through its
setup helper; nextest runs tests in separate processes.

Declare native pytest execution with `requires_native_pytest` from
`tests.support.platforms`; Windows has no native pytest adapter. Mark only the
native parameters in mixed tests. Keep portable configuration checks, stock
pytest routes, and Windows rejection tests active.

## Define the proof first

Before writing test code, state the regression in one sentence:

> Given this public input and environment, vfy must produce this observable
> result, even when this relevant boundary condition occurs.

Then identify:

1. the layer that owns the behavior;
2. the independent oracle, if one exists;
3. the smallest fixture that distinguishes correct from incorrect behavior; and
4. the test that would have failed before the implementation changed.

If those answers are unclear, clarify the product contract before expanding the
suite. Do not expose a private API merely to make a test convenient.

## Put the test at the owning layer

| Behavior | Primary proof |
| --- | --- |
| Algorithms, state machines, parsing, ordering, merge rules | Focused Rust test beside the implementation |
| Python source to semantic definitions and vectors | Literate fixture in `tests/fixtures/semantic/` |
| Coverage.py-shaped behavior or schema-7 data | Differential or round-trip test against the pinned upstream version |
| Python API, configuration, or report model | Public Python call with semantic assertions |
| CLI diagnostics, streams, files, or exit status | Real command or `main` boundary; subprocess when process behavior matters |
| pytest, xdist, subprocess, signals, or fork behavior | Temporary project executed through the real public entry point |
| Scheduler or selection policy | Rust invariant test plus a real pytest/xdist boundary test |
| Installed package behavior | Clean wheel installation on the supported interpreter/platform matrix |
| Speed or memory | Parity-gated release benchmark, separate from correctness tests |

Rust owns engine semantics. Python tests prove the ecosystem boundary; they
must not carry a second implementation of collection, analysis, MC/DC, merge,
or scheduling logic. A repository architecture test is appropriate only when
it prevents a deliberate boundary regression, such as reintroducing a Python
fallback. It never replaces behavioral proof.

## Use Astral-style fixtures deliberately

Follow the useful testing discipline from Ruff, ty, and uv:

- Make fixtures readable specifications. Put short prose beside the exact
  source and expected behavior it explains.
- Minimize real-world reproducers until every remaining line is necessary to
  trigger or distinguish the regression.
- Keep independent scenarios separate. Reuse setup only when it makes the
  behavior easier to understand.
- Prefer direct semantic assertions for most cases. Use a snapshot only when
  the complete diagnostic or rendered output is itself the contract.
- Regenerate snapshots through the test harness and review every changed line.
  Never accept a snapshot because the implementation produced it.
- Add ecosystem-scale evidence after the focused proof, not instead of it.

For semantic coverage, the expected decision kind, source range, logic tree,
tri-state vector, outcome, and MC/DC evidence are review artifacts. Derive them
from the Python expression or an independent truth-table oracle. Never compute
expected evidence with the production evaluator.

## Prefer real boundaries and independent oracles

Use the public command, package API, pytest hook, SQLite file, or CPython event
path that a user exercises. Do not replace pytest or xdist with fake workers,
mock monitoring callbacks to prove collection, or assert private call order.
Rust unit tests may isolate deterministic state transitions; integration
tests must cross the real boundary they claim to protect.

For compatibility, run each new fixture against the pinned reference before
fixing expected values, including cleanup timing and retained object lifetimes.
Run the same minimal case against vfy. Compare normalized semantic values
before presentation. Normalize
temporary paths, process IDs, timestamps, and nondeterministic ordering only
when they are outside the contract. Keep unexpected differences visible.
For terminal differentials, observe the pinned reference before asserting
label counts or spacing; live progress and summary output can repeat a label.
Keep the affected output block intact so filtering cannot hide the regression.

For data mutation, include the failure path that proves the old file remains
valid and unchanged. Make the rejected run's rendered content deterministically
different from the accepted baseline so byte equality cannot hide accidental
republication. For distributed collection, prove context attribution,
path remapping, shard conflict behavior, and final atomicity only when the
change can affect them; do not add a boilerplate cross-product to every test.

Name pytest hook implementations with a `pytest_` prefix, including those using
`specname`: pytest ignores other Python names before pluggy sees that option.
Assert observer activation so a differential cannot pass by omitting its hooks.

Native execution proofs must stay within the supported hook policy. Collection
hooks, including `pytest_generate_tests` in `conftest.py`, run through pytest.
Optimization proofs must assert candidate activation; setting a flag is insufficient.
Deferred collection proofs need at least two native workers; the serial collector
does not install that adapter. Use plain conftest phase hooks: the native hook
policy rejects their wrappers.
Tests proving fresh worker collection or worker-only adapter activation must set
`VFY_WORKER_START=exec` explicitly; the default worker route may collect once
in the controller before forking.

## Make failures diagnostic

A missing or unreadable optional procfs file, including a task's `children`
file, is not evidence that a process exited. Use pidfd exit readiness or read
`stat` and its start identity independently; preserve each diagnostic read error.
Pipe or FIFO EOF proves that its writers closed, not that a process exited or
released other resources. After asynchronous process-group cancellation, wait
for the resource being asserted, with a bounded deadline and restored signal
handlers, or use actual process-exit readiness. Do not infer lock release from
an unrelated descriptor closing or add sleeps to make the ordering pass.
Managed project cleanup must stop its owned process group even after the
launcher exits, then reap the launcher and drain its captured streams.
Process cleanup proofs must acquire descriptors for every observed owned process
before asserting its expected topology, so a topology failure still cleans up.
Sampled-liveness assertions must keep the required descendants alive until the
actual sampler acknowledges their snapshot; a fixed lifetime is insufficient.

A good test name describes behavior, not the method under test. Its assertions
should identify the first meaningful semantic difference: exact lines or arcs,
decision vectors, stored rows, exit status and stream, selected node IDs, or
rendered public output.

For configuration discovery, assert a setting or validation error from the
selected file as well as its reported source. A filename alone cannot prove
that the file's contents were loaded.

When a command emits a structured report, include that report in unexpected
exit-status and summary failures. Stderr can be empty while a mutation row's
`detail` explains the worker failure; an `error` result is not timeout evidence.

For an injected failure, prove that execution reached the intended boundary
with its specific diagnostic or a fixture marker. A nonzero exit and unchanged
output files can also result from an unrelated bootstrap failure; neither
alone proves rejection or atomicity at the boundary under test.

For structural output validators, check a real pinned-reference output before
accepting synthetic fixtures. Keep its framework sections and their order in
positive tests; omitted sections can make an invalid validator appear to pass.

Preserve pytester's default process mode when reusing upstream tests. A passing
subprocess run can omit the parent monkeypatch or import state that exercises
the regression. Prove that the intended branch runs, and limit process-mode
overrides to named cases with a reproduced lifecycle failure.

Prefer one decisive assertion surface over a long list of incidental details.
When several values form one public contract, compare one structured value with
a readable diff. Avoid tautologies, arbitrary test data variants, giant golden
files for one field, sleeps as synchronization, and assertions that merely
mirror implementation structure.

Keep both sides of an important boundary:

- success and the most consequential failure;
- enabled behavior and the disabled-mode non-regression;
- write and read, or producer and consumer;
- selected execution and safe fallback; or
- current source and stale or conflicting evidence.

Add only the side relevant to the risk of the change.

## Preserve vfy's specific contracts

- Line and branch behavior must remain unchanged when semantic decisions are
  disabled.
- Coverage.py compatibility claims require differential evidence against the
  pinned version, not matching names or signatures.
- Semantic masking evidence comes from the authoritative Rust logic-aware
  evaluator; Python may adapt Rust rows but must not recalculate them.
- pytest and xdist behavior is proven with real temporary projects, including
  standard exit codes and filesystem effects.
- Scheduler correctness is result parity first; elapsed time is a separate
  benchmark question.
- Pytest corpus accounting proves outer-session semantics: ordered case IDs,
  report phase, outcome and exception, case and session warnings, collected
  count, and exit status. Keep diagnostic and captured-output fields as literal
  evidence; never rewrite transcripts to make the corpus compare equal. See
  [`docs/about/native-pytest-testing.md`](../../../docs/about/native-pytest-testing.md)
  for the contract-to-test map.
- Bind the observer to the recorded harness source, and the native adapter and
  extension to the exact installed wheel origins and hashes. Keep the full
  stock baseline and controlled stock/native pair on their recorded routes;
  retain pytester's default process behavior
  except for individually named process-mode exceptions with a recorded reason.
- Make a broad compatibility claim only after the focused Rust proof, the real
  `VfyProject` boundary, and the pinned upstream comparison pass at their
  owning layers. A source change or a green lower layer does not establish the
  full corpus result.
- Performance samples are invalid until the relevant files, lines, arcs,
  contexts, report fields, or selections pass parity.
- A benchmark's breaking edit requires pytest test-failure evidence and exit
  code 1. Collection errors, missing outcome reports, and other nonzero exits
  cannot satisfy a failure-detection gate.
- CPython-versioned behavior must be exercised on each version in the supported
  CI matrix when opcode or monitoring-event mapping can differ. Do not add
  unsupported runtime branches for the sake of a broader matrix.

## Review existing tests with a deletion bias

Pytest configuration parser changes need pinned-reference differentials for
section whitespace, inline section comments, continuation indentation, delimiter
precedence, and shell quoting. Preserve these lexical distinctions before
projecting values; trimming or splitting alone does not prove parser parity.
Path projections also need home and environment expansion differentials,
including a named user, an unknown user, and an unset home environment variable.

Remove or consolidate a test when it duplicates an existing contract, asserts
private structure without protecting an architectural boundary, depends on
incidental formatting, or cannot explain what regression it catches. Preserve
tests that encode a discovered compatibility difference, process failure,
language edge case, or independently derived semantic rule.

When changing an expected fixture or snapshot, explain whether the old output
was wrong or the contract intentionally changed. An unexplained expectation
update is not validation.

## Validate in layers

When tightening a shared test helper's accepted inputs, inspect values passed
through local helpers and parameter loops as well as direct calls. Run all
affected caller suites; a focused helper test does not prove their compatibility.

Keep fixtures and helpers annotated. Run `uv run ty check` alongside Ruff;
semantic fixture source is test input, not a type-checking target. Type contract
tests use `assert_type` for consumers and reject deliberately invalid calls with
the expected diagnostic. Check native declarations against the installed wheel.
Unexpected warnings and unexpected xfail successes fail the repository suite;
child projects explicitly retain any warning or xfail behavior under test.
Close reference resources in teardown even when assertions fail. A SQLite
transaction context manages commit and rollback; it does not close the
connection. Coverage.py in-memory reference data also needs explicit cleanup.
Give every reference database an explicit owner and destination: use the
`reference_coverage` context manager for memory-only analysis, or a test's
temporary path for persisted data. Never let parallel tests share the checkout's
default `.coverage` file. Keep cache writes in temporary directories, including
configuration tests whose reference rootdir is a system directory.
Coverage.py configurers can receive either its
`Coverage` object or `CoverageConfig`; test adapters must accept both.

For lifecycle changes, exercise failed acquisition followed by successful reuse
through `VfyProject`. For data changes, compare the state after rejection
against the pinned reference, not just the raised exception.

For dynamically requested package-scoped fixtures, compare two tests in the
defining package against the pinned pytest wheel and assert one setup and one
final teardown for that package.

Compact collection changes need a public run without `-k`, explicit node IDs,
or collection hooks that disable compaction. Compare per-case markers and
report metadata with the expanded pytest oracle, including teardown.
When admitting a collection hook, also exercise it alone: other observer hooks
or explicit selectors can expand items and hide compact-family differences.

Installed plug-in admission must inspect declarations inside module-level control
flow and leave dynamic hook decorators or hook-name options unadmitted. Prove
omission without importing the plug-in, and confirm the declaration with stock
pytest.

Native lifecycle changes must compare runtime marker evaluation, teardown
xfail matching, and early-stop finalizers against pytest. Preserve finalizer
output and control exceptions, cached fixture access during finalization, and
the public request methods through real `VfyProject` differentials.
Interleave test finalizers with dynamically requested fixtures, and verify
destructors can re-enter a saved request without hanging. Apply the installed
pytest version's rules to first-time fixture lookup during teardown.
Compare owner nesting when a broader fixture is requested after a narrower
fixture starts. Exercise both `request.addfinalizer()` and the real
`request.node.addfinalizer()` alongside fixture cleanup. Cache-hit tests must
cover scope changes and dependency invalidation before parameter equality.
Test callable replacement during fixture setup, including code and coroutine
marker changes that alter async classification; collection-time bindings do not
prove call-time behavior.
Synchronous adapters must not inherit coroutine markers from wrapped functions.
Apply the same proof to fixture callables after dependency setup. Compare public
request keyword arguments and finalizers registered through saved requests after
a fixture's cache generation changes. Resolve the current async loop scope
before acquiring its runner, preserving that runner's fixture invalidation edge.
Compare user repr callbacks during failure reporting; derive rendered and
canonical diagnostics from one pytest representation. Version-dependent
outcomes must follow the installed oracle, including its enabled plug-ins.
Probe parser capabilities in the child oracle when its plug-ins differ from
the parent test session; disabling cacheprovider also removes stepwise options.

Collection optimizations must preserve custom collector methods as well as
ordinary pytest items. Replay identities must distinguish execution engines;
prove both same-engine reuse and invalidation when the engine changes.
Configuration projection tests must cover grouped short options and targets
outside the invocation directory. Watch publication tests must include worker
bootstrap failures with report paths supplied by configuration or environment.
Watch recovery must also start from malformed pytest configuration and resume
after its correction without publishing failed-run artifacts. Native coverage
finalization must reject invalid child shards before replacing prior public data,
including serial runs that collect subprocess coverage.
Watch removal tests must cover a deleted directory whose kernel watch disappears
before its event is consumed, while preserving failures for other unwatch errors.
Cache read differentials must include numeric conversion failures as well as JSON
syntax errors, preserving pytest's default-value behavior and interpreter limits.
Last-failed selection differentials must check unselected file imports and
collection errors, not only the final selected node IDs.
Argument-file tests must cover option precedence and reuse of saved evidence
after an external argument file changes without a checkout change.
JUnit differentials must compare outcome attributes and diagnostic bodies as well
as element counts; matching tag shapes alone does not prove report compatibility.
Adoption coverage gates must compare ordered outcomes as well as identities;
coverage-only skips must fail even when source inventories and reports match.
Exercise adoption commands with file, node, and directory selectors to catch
incompatible selection options.

Iterate with the narrowest owning test, then run the complete affected layer.
Before handoff, run the applicable [pre-PR checks](../../../CONTRIBUTING.md#before-a-pull-request).

Build the docs when public behavior, examples, compatibility, or test policy
changes. Installed-wheel and CPython-matrix claims belong to CI or an equivalent
clean artifact test; do not imply they were proven by a source-tree run.

When validating an installed wheel, invoke the environment's interpreter and
tools directly. `uv run` can synchronize the source project and rebuild vfy,
invalidating the installed-artifact boundary.

Before expanding an installed-wheel matrix, check every selected suite and its
subprocess helpers for oracle imports and explicit plug-ins. Install their
locked dependencies and verify imports before running tests. For each explicit
`-p NAME` used by a reference invocation, run a minimal command under pinned
pytest to verify the intended loading or blocking behavior; a dotted-module
import alone does not prove an enabled entry-point alias resolves and loads.
A smaller suite's environment may lack requirements of the expanded selection.

Use a fresh, exclusively created output path for each validation attempt. Preserve
failed and superseded logs and receipts; a later attempt must not overwrite them.

When copying a project checkout or test environment, exclude generated `.pyc`
files and `__pycache__` directories from the copy. Pytest-rewritten bytecode can
retain the original environment's source paths, causing false coverage or
native hook-admission failures. Record the reset outside any benchmark timer and
allow bytecode to warm normally during preflight and warmups.

Install candidate wheels with `uv pip install --link-mode=copy`. Installed
package files can otherwise share hardlinks with the cache and other environments.
Never overwrite an installed file in place for an overlay test: use a separate
source tree or atomic replacement, and verify the original wheel's files before
and after the run. Record overlay evidence separately from installed-wheel proof.

Wheel inventory validators must accept a real clean installation before testing
tampering. Compare immutable wheel-owned files separately from generated
bytecode and installer metadata such as `RECORD`, `INSTALLER`, and `direct_url.json`.

In the handoff, name the contract protected, the layer and oracle chosen, and
the exact focused and broader checks that passed.

## Related skills

- `$python` for Python boundary and pytest integration changes.
- `$rust` for Rust invariants and engine ownership.
- `$coverage-compatibility` for differential and round-trip oracles.
- `$coverage-performance` for parity-gated measurements.
