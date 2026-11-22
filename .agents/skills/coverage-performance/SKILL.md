---
name: coverage-performance
description: >-
  Use when profiling, optimizing, benchmarking, reviewing, or publishing
  performance work for vfy collection, startup, path filtering, SQLite data,
  combining, analysis, aggregation, or report generation.
---

# Coverage performance

Optimize the stage that matters without weakening semantics. Read
`benchmarks/README.md` and `docs/about/benchmarks.md` before changing the harness
or publishing a result.

## Define the stage

Separate interpreter startup, uninstrumented application time, collection
overhead, shutdown and persistence, shard combination, source analysis,
aggregation, and rendering. Do not use a collection result to claim reporting
speed, or a report result to claim tracing speed.

Profile before choosing an implementation. Keep engine work in Rust; Python is
an integration boundary, not a convenient fallback for a difficult hot path.
Measure end to end for user-facing claims and use focused Rust benchmarks to
explain a local algorithmic change.

## Gate timing on parity

Never time a workload until vfy and the comparison tool agree on the semantic
output relevant to that stage. Depending on the workload, verify:

- measured file identities and collection mode;
- exact lines, arcs, contexts, and tracer attribution;
- statement, missing, branch, and total counts;
- merged shard contents; and
- required machine-readable fields.

Abort on a mismatch. If a format intentionally implements a smaller contract,
state that beside the result rather than presenting the work as equivalent.

## Collect publishable evidence

Use release-mode Python-extension artifacts. Keep an uninstrumented baseline for
collection, rotate tool order, warm both tools equally, and retain every sample
with environment metadata. Publication provenance must bind the installed
artifact to its build revision; the controller checkout HEAD alone is not build
evidence. Missing revision, artifact, or run-status evidence rejects
publication. A prepared environment must consume a resolved lock containing
transitive dependencies and upstream test requirements; a short direct-
dependency list or a post-run freeze is not a replayable lock. The committed
benchmark claim uses at least three warmups and 15 measured runs and reports
the median; do not publish a best run. Follow stricter workload requirements
where documented, including thirty measured runs for mutation comparisons.

Dependency reviews include active benchmark and adoption locks as well as vfy's
runtime metadata. Preserve historical measurement locks. Record blocked or
incomplete advisory lookups as unverified.

Use an isolated `CARGO_TARGET_DIR` for release candidates built across worktrees.
Shared targets can retain path dependencies from another checkout even after
package-scoped cleaning. Retain the target path and build log.

Run the committed harness rather than recreating its logic:

```console
mkdir -p target/benchmarks
uv run python -m benchmarks run report --report-formats terminal json \
    --files 500 --warmups 3 --runs 15 \
    --output target/benchmarks/report.json
```

For a regression, compare against a relevant retained result or the exact base
revision on the same machine. Do not compare debug and release builds or
extrapolate one machine, Python version, source size, or shard count universally.

## Publish narrowly

Archive raw results and validation artifacts outside the source tree; commit
a compact evidence index with archive links and checksums. Update
`benchmarks/README.md`, `docs/about/benchmarks.md`, and any headline number
together. Report absolute times, relative change, workload,
versions, machine, sampling method, and semantic caveats.

Describe measurements, not ambitions. Do not frame arbitrary multipliers,
percentage improvements, or benchmark thresholds as release targets,
acceptance criteria, promises, gaps to close, or a product roadmap. If a
committed harness enforces a regression threshold, call it a benchmark or CI
check, explain the workload it protects, and keep the observed result distinct
from the threshold. User-facing performance pages should lead with commands,
measured times, methodology, and limitations; publish slower and inconclusive
results instead of narrating whether the product has met a goal.

## Related skills

- `$testing` for the semantic parity gate behind every measurement.
- `$rust` for Rust profiling and hot-path changes.
- `$python` for controller, pytest, or process-boundary overhead.
- `$coverage-compatibility` when a comparison depends on upstream behavior.
