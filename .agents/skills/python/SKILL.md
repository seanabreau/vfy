---
name: python
description: >-
  Use when editing python/vfy/, Python benchmark helpers, or Python tests,
  especially Coverage API adapters, CLI code, pytest/xdist hooks, process
  bootstrap, plug-in calls, or Python-side PyO3 integration.
---

# Write vfy Python

Write Python that feels unsurprising to an experienced Python maintainer while
preserving vfy's Rust-first architecture. Read `docs/about/architecture.md`
and the nearest module before editing. Use `$testing` whenever behavior
or tests change.

## First ask whether the code belongs in Python

Python is the right owner only when the work requires Python objects or a
Python ecosystem protocol:

- Coverage.py-shaped public objects and signatures;
- CPython monitoring, import, signal, fork, or subprocess registration;
- pytest and xdist hooks, fixtures, and process bootstrap;
- third-party Coverage.py plug-in calls;
- standard output or caller-owned Python file-like objects; or
- a small adaptation from Rust values into a documented Python return type.

Collection semantics, paths and filters, configuration resolution, schema IO,
merging, source analysis, semantic evidence, test selection, scheduling,
aggregation, totals, rendering, and atomic filesystem commits belong in Rust.
Do not add a Python fallback, duplicate algorithm, or second data model because
the Rust change is harder. Use `$rust` for that work.

## Make the boundary boring

Python modules should translate protocols, not reinterpret results. Prefer one
coarse Rust operation over loops of fine-grained PyO3 calls. Keep Rust handles
intact through ordinary report and data paths; materialize Python
collections only for a public compatibility consumer that requires mutation.

Do not place Python on the ordinary monitoring-event path. A configured
third-party plug-in is the only accepted callback into Python during
collection. If a new boundary creates objects per event, per line, or per arc,
redesign it before optimizing it.

Keep `python/vfy/native.py` a typed, explicit inventory of Python-extension
exports.
Do not hide extension imports behind dynamic lookup, broad `getattr`, or import
side effects.

Maintain `python/vfy/_native.pyi` with every extension change. Preserve lazy
runtime exports with explicit `TYPE_CHECKING` imports, and ship `py.typed`.
Use `TypedDict` for dictionary wire formats, precise tuple aliases for Rust
records, and small protocols for plug-ins and writable streams. Preserve
decorator signatures with generics and overloads. Do not hide missing types
behind `Any`, unchecked casts, or blanket import exclusions.

Treat typed records constructed by the paired Rust extension as internal values.
Adapt their containers when immutability requires it; do not duplicate Rust
validation in Python. Keep validation of files, user inputs, and plug-in values
at their owning boundary. Verify the pairing with native declarations, consumer
type assertions, and integration tests. Pass completed Rust JSON directly to
Rust persistence or rendering; decode it only for a Python consumer.

## Write an exact public contract

- Preserve Coverage.py and pytest-cov signatures, defaults, exceptions,
  streams, exit statuses, and side effects where compatibility is claimed.
- Implement an accepted option or reject it precisely. Never accept and ignore
  it.
- Keep vfy-only configuration under `[tool.vfy]`. Give CLI and Python API
  extensions explicit vfy documentation; do not silently change the meaning
  of an upstream option.
- Translate Rust failures once, at the public boundary, into the documented
  vfy or compatible exception. Preserve the original cause with `raise ...
  from ...` when it helps diagnosis.
- Fail explicitly when a contract cannot be honored. Do not silently retry via
  a slower implementation or return partial data.

Compatibility sometimes requires permissive parameters or unusual lifecycle
methods. Isolate those shapes at the public adapter. Internal helpers remain
fully typed and purpose-specific.

## Use precise Python

- Start modules with `from __future__ import annotations`.
- Annotate parameters, returns, state, and empty collections. Prefer `object`
  when a value is opaque and `Any` only when the protocol is genuinely dynamic.
- Explain retained dynamic typing, delayed imports, and lint suppressions at
  their boundary. Suppressions name the exact rule; blanket ignores and missing
  import substitutes do not satisfy the type gate.
- Use `collections.abc` protocols for inputs and concrete built-ins for owned
  return values. Do not narrow supported iterable inputs to concrete containers
  to satisfy typing; prove accepted input shapes against the pinned reference.
- Use frozen dataclasses for immutable boundary records. Avoid dictionaries
  whose keys form an implicit schema.
- Keep finite states in `Literal` aliases or existing enums. Use consumer type
  assertions when changing public exports, decorators, or structured returns.
- Use `Path` for filesystem work and convert to `str` only at a library or
  compatibility boundary that requires it.
- Keep imports at module scope and ordered by Ruff. A local import needs a real
  bootstrap, cycle, or optional-compatibility reason that is apparent nearby.
- Do not split a readable operation across wrappers that merely rename
  arguments. Introduce a class only for identity, lifecycle, or a public
  protocol.

Public docstrings explain the contract and non-obvious compatibility behavior.
Comments explain an invariant, ecosystem constraint, or surprising decision;
they do not narrate code.

Treat mutation child pipes as byte streams: writes and reads may be partial.
Drain available output before waiting for exit, and preserve the watchdog while
the child is still writing a result.

## Make lifecycle and failure visible

Runtime hooks must have one owner and a symmetric installation/removal path.
Use `try`/`finally` or a context manager for temporary global state. A failed or
interrupted collection must leave monitoring, signals, import hooks, worker
files, and the previous coverage database in a valid state.

Publish lifecycle state only after acquisition succeeds. Restore temporary
registration context in `finally`, including nested initialization. Stage data
mutations in Rust and publish only after the complete operation validates.
Materializing mutable compatibility records must not select a Python merge
algorithm. Mutation caches and reports use Rust persistence and rendering too.
Each mutation runs in an independent child of the collected pytest session.
Restoring a function's code does not restore fixtures, imported modules, or
nested mutable objects. Baselines and mutation children must load the same
pytest execution plug-ins, including vfy's asyncio support.

Cancel an isolated test file's process group before releasing its worker lease;
killing only the direct child leaves subprocesses able to modify shared files.
Register private file groups with their outer watchdog before tests can start,
so mutation-level timeouts also terminate those groups. Prove cancellation
while another file is still running, not only after the mutation command exits.

Materializing a mutable coverage adapter must preserve Rust transaction
semantics. A read/materialize/mutate/write round trip must not resurrect
removed measurements or commit part of a failed update.

Keep locks narrow. Do not call user code, plug-ins, subprocesses, or Rust
long-running work while holding a Python lock. Avoid mutable module globals;
when CPython or pytest requires stable process-wide identity, make ownership and
reset behavior explicit.

Catch only exceptions the boundary can handle. Broad exception suppression is
not resilience. Diagnostics name the invalid value or operation and the action
the user can take. Reserve stdout for requested report output; usage errors and
diagnostics go to stderr.

Construct subprocess commands as argument lists and never enable a shell.
Propagate the child's standard pytest exit status unless vfy's documented
contract assigns a different one.

## Keep dependencies and compatibility deliberate

Prefer the standard library and existing Rust capabilities. A new runtime
dependency needs a product-level reason, a maintained compatibility range, and
clear value that cannot be obtained from the standard library or an existing
dependency. Do not add a dependency to avoid writing a small adapter.

Support CPython 3.12 and newer as declared by the project. Use the
documented version capability, not string comparisons or speculative branches
for unsupported interpreters.

## Review for Python-shaped Rust leaks

Before finishing, remove:

- Python recomputation of Rust values;
- repeated Rust calls that could return one structured result;
- eager expansion of a Rust data handle;
- stringly dictionaries standing in for stable records;
- public methods added only to expose internals to tests;
- broad `try` blocks or fallback branches that hide unsupported behavior; and
- abstractions with no concrete second use.

## Validate the change

Run focused tests while iterating, then the affected Python suite. Before
handoff, run the applicable [pre-PR checks](../../../CONTRIBUTING.md#before-a-pull-request).

If Python changed the Rust contract, also run the Rust suite. If public
behavior changed, update the relevant reference, compatibility statement, and
changelog in the same change.

## Related skills

- `$rust` for engine semantics or Python-extension PyO3 changes.
- `$testing` for any changed behavior or fixture.
- `$coverage-compatibility` for Coverage.py, pytest-cov, or SlipCover parity.
- `$coverage-performance` for profiling or benchmark claims.
