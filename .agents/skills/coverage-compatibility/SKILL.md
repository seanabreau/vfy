---
name: coverage-compatibility
description: >-
  Use when adding, changing, reviewing, or diagnosing Coverage.py or SlipCover
  compatibility across collection, data files, commands, configuration,
  reports, the Python API, subprocesses, or plug-ins.
---

# Coverage compatibility

Treat compatibility as observable behavior backed by evidence. A matching name,
signature, accepted option, or file shape is not sufficient by itself.

## Establish the contract

Read the relevant reference page under `docs/reference/` and the status matrix
in `docs/about/compatibility.md`. Inspect the pinned Coverage.py version in
`pyproject.toml` and `uv.lock`; do not silently compare against a different
release.

Reduce the request to an observable contract: inputs, outputs, persisted data,
exit status, error type, side effects, and applicable Python or platform range.
For SlipCover behavior, keep its contract separate from Coverage.py's.

## Prove the behavior

Native pytest fixture finalizers must preserve the installed pytest version's
registration position relative to each dependency resolution, cache comparison,
and parameter switch. Registering callbacks in a batch can change teardown order
and exception-group topology when another dependency or an old fixture generation
adds a finalizer. Prove those interleavings with the same public fixture project
against the supported pytest versions.

Prefer one of these oracles, in descending order:

1. run the same focused case against vfy and the pinned upstream package;
2. consume a data file produced by upstream, then verify upstream can consume
   vfy's output;
3. use an upstream test or documented example with attribution;
4. use a vfy-only expectation only when no upstream contract exists.

Compare semantic values before presentation. Normalize nondeterministic paths,
timestamps, hostnames, process IDs, and ordering only when they are not part of
the contract. Add a negative or edge case when invalid input, mixed collection
modes, missing source, corruption, or partial output is plausible.

When replacing textual condition checks with AST evaluation, compare both true
and false constants, alternate suites, and nested branch obligations. Exclusion
changes need replacement, empty, and additive cases so defaults cannot remain
hardcoded below the configured public boundary.

Source-decoding evidence must compare decoded text, including control-byte and
character-mapping boundaries. Matching statement counts alone cannot prove that
an encoding preserves the source Python executes.

Source-package configuration must not execute unimported parent packages.
Compare parents that extend or replace their search paths during import, and
keep unresolved package names restrictive when no static source roots exist.
Check unexecuted-file discovery against the oracle for regular and namespace
packages, rather than inferring it from `__path__` alone.
Preserve source origin during discovery: explicit file roots and module names
resolved to files have different inventory behavior. Run the complete
source-discovery differential suite after changing either.
Compare repeated code filenames with changing frame metadata, including static
and dynamic file tracers. Preserve the first filename disposition while calling
dynamic source tracers for each event and caching mapped-source decisions.
Dynamic filename changes within an active frame must not carry arc endpoints
across source paths. Prove that enabling branch collection preserves measured
lines and that return events credit the last measured source path, alongside
mapping changes between separate calls.

## Preserve the engine boundary

Coverage semantics belong in Rust: event and frame state, paths and filters,
schema IO, merging, source analysis, exclusions, branches, aggregation, totals,
and rendering. Python may expose public objects, register interpreter hooks,
bootstrap pytest or subprocesses, write completed outputs, and call third-party
Python plug-ins.

Do not add an ignored compatibility parameter or option. Implement it with a
test, or reject it explicitly. Persisted replacement must remain atomic, mixed
line and branch data must fail before mutation, and every report must consume
the shared Rust model.

## Finish the change

Run the narrowest differential and Rust tests while iterating, then the full
relevant Rust and Python suites. Update all affected public references, the
compatibility matrix, and the active series file under `changelogs/`. Make a new compatibility
claim only as broad as the tested versions, platforms, inputs, and formats.

## Related skills

- `$testing` for differential, round-trip, and process-boundary test design.
- `$python` for public adapters and third-party plug-in protocols.
- `$rust` for Rust semantics, schema behavior, analysis, and rendering.
- `$coverage-performance` when a compatibility-preserving change is measured.
