---
name: rust
description: >-
  Use when editing crates/*/src/*.rs, Cargo.toml, or Python-extension PyO3 exports,
  including the
  collector, schema-7 data, analysis, semantic evidence, reports, test
  selection, scheduling, atomic writes, or Rust performance.
---

# Write vfy Rust

Rust is vfy's authoritative engine, not an accelerator attached to a Python
implementation. Read `docs/about/architecture.md`, `Cargo.toml`, and the nearest
module before editing. Use `$testing` whenever behavior or tests change; use
`$coverage-performance` before making or publishing a speed claim.

## Own the complete semantic operation

The Rust implementation owns collection state, path policy, configuration,
schema-7 data,
atomic combination, Ruff-backed source analysis, semantic evidence, report
models and renderers, test selection, and scheduling. Keep each operation
Rust-owned from input validation through its final structured result. Python may
register hooks and adapt that result, but must not finish the algorithm.

Before adding code, find the existing model or mechanism that should own the
behavior. Extend the underlying abstraction when the contract is shared; do
not add a local workaround, parallel representation, or format-specific copy of
the same rule.

## Encode invariants in types

- Use a domain enum or struct when values have a finite meaning or fields must
  remain consistent. Strings belong at SQLite, JSON, CLI, and PyO3 boundaries,
  not at the center of an algorithm.
- Make invalid states difficult to construct. Validate external data once,
  then pass a trusted type internally. Validation covers cross-record
  relationships and resource bounds, not only each field's type or range.
- Distinguish source identity, code-object identity, context identity, and
  persisted keys. Do not reuse one string or integer because their current
  values happen to match.
- Prefer borrowed inputs and owned outputs with clear lifetimes. Clone when it
  is the clearest ownership boundary, not to appease the borrow checker without
  understanding the data flow. Retain clones that protect transactional
  ownership until failure-state parity is proven. Review structural per-file or
  per-context clones with profiling and consider borrowed alternatives instead.
- Use deterministic collections for persisted data and public output. Use
  `FxHashMap` or `FxHashSet` only for ephemeral hot-path state whose iteration
  order cannot escape.
- Keep visibility as narrow as the real caller permits. Use `pub(crate)` when a
  module boundary requires it and public PyO3 exposure only for a deliberate
  Python contract.

Keep a struct beside its primary `impl`. Create a new module when it owns a
coherent concept, not merely to shorten a file. Avoid generic frameworks for a
hypothetical second implementation.

## Treat failure as part of the data model

External paths, configuration, SQLite rows, source text, shard files, Python
objects, and plug-in values are untrusted. Validate before mutation and return
an error that preserves the failed operation and relevant identity.

Treat relationships and workload size as part of validation: references must
resolve to the correct parent records, related identities and modes must agree,
and attacker-controlled collections, dimensions, and allocations must have
explicit bounds. Distinguish a cache miss from a corrupt cache entry or a
failed cache-producing operation; only the miss may follow the ordinary
recomputation path. Operational regex compilation or matching failures must
propagate rather than silently becoming no match.

Avoid `panic!`, `unreachable!`, `unwrap`, and `expect` in production paths.
Input-dependent assumptions require `Option`, `Result`, enums, or validated
constructors rather than a panic. Infallible formatting into an owned `String`
does not require a defensive error branch; do not extend that exception to
fallible I/O or user-provided formatter implementations. Test setup may use
`expect` only when its message makes a broken fixture immediately obvious and
no production path is being excused.

Translate errors to a precise Python exception once at the PyO3 boundary.
Internal code should not manufacture Python errors unless it is directly
interacting with Python. Never convert corruption, conflicting shards, mixed
line/branch modes, stale semantic data, or unsupported behavior into an empty
successful result.

`unsafe` is forbidden by the crate lint. Do not relax that policy. A dependency
or implementation that requires local unsafe code needs an architectural
decision, not a lint exception.

## Design the PyO3 boundary in batches

Update `python/vfy/_native.pyi` and the explicit export inventory with every
PyO3 interface change. Verify installed signatures and structured return values;
an importable extension alone does not prove its consumer typing contract.
Materialized Python records must use the same Rust validation and transactional
merge as native handles. Mutation cache policy, rendering, persistent
replacement, machine-output decoding, and completeness checks are engine
operations, including their failure paths. Python must not reconstruct partial
records or decide whether output is complete.

The best PyO3 method crosses the boundary once for a meaningful operation. It
accepts plain, validated inputs and returns a stable record or Rust data handle.
Do not expose a sequence of getters that forces Python to reconstruct the
engine's model or call Rust once per file, line, arc, event, or condition.

Touch Python objects only while holding the interpreter token. Release the GIL
around independent parsing, analysis, aggregation, SQLite, rendering, and
filesystem work when the implementation is demonstrably free of Python
objects. Do not retain a borrowed Python reference beyond its lifetime. When a
`Py<PyAny>` must be retained for code identity or a plug-in, document why the
reference is necessary and where it is released.

Dropping a Python reference can run user code through `__del__` or weak-reference
callbacks. Remove owned Python values from locked state, release the lock, then
drop them. The same rule applies to exception methods and other overridable
Python operations: none may run while holding a lock they can re-enter.

A PyO3 mutable borrow is also exclusive state. Monitoring callbacks must release
it before invoking plug-ins, audit hooks, or receiver descriptors: those calls
can release the GIL and let another thread enter the collector. Clone owned
callback inputs, drop the borrow, call Python, then reacquire it to commit the
result. Lazy `PyOnceLock` and `pyo3::intern!` initialization can release the GIL
too; initialize callback caches before monitoring starts. Exercise these
boundaries through `VfyProject` with another thread collecting while a selector
waits on an event.

The ordinary event path must not call Python, allocate Python objects, parse
source, or touch SQLite. Third-party plug-ins are the explicit slow path and
must not contaminate the Rust event path when disabled.

## Make hot paths simple and measured

Collection callbacks should perform cached identity resolution and compact
state updates. Move canonicalization, glob construction, opcode decoding,
source parsing, and allocation out of repeated callbacks whenever the contract
allows it.

Profile the relevant stage before optimizing. Separate startup, collection,
persistence, combination, analysis, rendering, and test execution. Optimize
the measured bottleneck without weakening exact lines, arcs, contexts, semantic
vectors, report totals, or atomicity.

Model aggregate totals as scalar counts. Retain source evidence on file rows so
drill-down reports and compatibility checks do not have to reverse-engineer it
from totals.

Do not add concurrency by instinct. Rayon is appropriate for independent,
sufficiently large work with deterministic reduction. Avoid nested parallelism,
fine-grained shared locks, and schedules whose output depends on completion
order. Preserve stable ordering at every public and persisted boundary.

For writes and shard combination, validate every input before replacing the
destination. Commit through the existing atomic path, and perform cleanup only
after the replacement succeeds. An interruption or conflict must leave the old
artifact valid. Cache evidence must match both the entry key and the embedded
result identity; a matching fingerprint alone cannot establish that association.

## Write readable Rust

- Keep imports at the top of the file.
- Prefer guard clauses, `let` chains, and match guards to nested `if let`
  pyramids.
- Match exhaustively when variants represent domain behavior. A wildcard arm
  must not hide a newly added semantic case.
- Comments explain invariants, ownership, algorithmic tradeoffs, CPython
  behavior, or a representative Python construct. They do not translate syntax
  into English.
- Delete dead code. If a lint suppression is genuinely clearer, use the
  narrowest `#[expect(..., reason = "...")]` supported by the toolchain. Its
  reason must identify the concrete boundary or invariant that requires the
  suppression and why the suppressed behavior is safe; labels such as
  `false positive` or `needed` are not sufficient.
- Keep any deliberate `allow` beside the boundary that requires it, as with a
  PyO3 extraction constraint; never weaken a crate-wide lint for one call site.

Prefer a straightforward specialized implementation over a clever abstraction
whose performance and invariants are harder to see. Reuse an existing utility
when it expresses the same contract; do not force reuse between concepts that
only look structurally similar.

Use shared source-location utilities for offsets, rows, columns, and ranges;
do not copy coordinate-conversion algorithms between analyzers or renderers.
Delete forwarding wrappers when callers can use the owning abstraction
directly rather than maintaining duplicate proofs for identical behavior.

## Preserve dependency and parser discipline

Add a crate only when it removes substantial risk or implements a maintained
capability better than local code. Disable unused default features, exact-pin
unstable internal APIs, and keep `cargo shear` and `cargo deny` clean.

vfy consumes Ruff's parser and AST as exact-pinned implementation
dependencies. A pin update requires the complete differential analysis and
semantic fixture suites; a successful compile is not evidence of equivalent
Python semantics.

## Review the finished change

Check for:

- a Python algorithm that should have moved with the Rust change;
- duplicated logic across report formats or line/branch/semantic modes;
- unnecessary allocation or cloning in a callback or per-file loop;
- nondeterministic iteration escaping into storage or output;
- validation after mutation rather than before it;
- a PyO3 API that exposes implementation pieces instead of one operation;
- a lock held across Python, IO, or expensive computation; and
- comments, wrappers, types, or configuration left unused after the refactor.

## Validate the change

Run the focused Rust test while iterating. Before handoff, run the checks
proportionate to the change. Use the canonical
[pre-pull-request commands](../../../CONTRIBUTING.md#before-a-pull-request) and
the [test ownership and focused-command guide](../../../docs/about/testing.md)
rather than copying a reduced validation list here.

Run `cargo shear --deny-warnings --check-test-targets` when dependencies or
module reachability change. A public or PyO3 change also requires its Python
boundary tests and documentation.

## Related skills

- `$python` when a Rust operation changes its public or PyO3 adapter.
- `$testing` for any changed invariant, fixture, or boundary.
- `$coverage-compatibility` for upstream parity or schema behavior.
- `$coverage-performance` for profiling, optimization, or benchmark claims.
