# Data files

vfy reads and writes Coverage.py schema-7 SQLite files. The default file is
`.coverage`. `COVERAGE_FILE`, `--data-file`, or `run:data_file` selects another
basename.

## Stored information

The database stores measured paths, packed line numbers or branch arcs, static
and dynamic contexts, file-tracer plug-in names, collection mode, and schema
metadata. Writes retain the recorded collection mode.

## Semantic tables

A decision-enabled run (`--cov-decisions`, `vfy coverage run --decisions`, or
`[tool.vfy.run] decisions = true`) adds these tables to the same schema-7
database:

| Table | Contents |
| --- | --- |
| `vfy_decision` | Stable decision key, canonical path, source digest, kind, expression, Boolean logic, condition lines, and source range |
| `vfy_condition` | Atomic condition index and source range for each decision |
| `vfy_observation` | Context, tri-state runtime vector, and decision outcome, deduplicated by identity |
| `vfy_path_decision` | Named decision identity and source range for `match`, exception dispatch (including `except*`), cancellation, and value selection |
| `vfy_path_outcome` | Ordered typed outcomes, source ranges, and handler body ranges |
| `vfy_path_observation` | Context and observed named outcome, deduplicated by identity |

The Boolean and path tables share a source digest for each measured path; a file
can have either or both families. Reading or combining different digests for one
path fails even when the revisions define different families.
A table is written only when its decision definitions are present. A Coverage.py
reader ignores these tables and reads `file`, `line_bits`, `arc`, `context`, and
`tracer` as usual.

vfy commits ordinary coverage and semantic data in one replacement
transaction. It validates the full dataset before replacing the file, so a failed
validation leaves the previous file in place.

## Compatibility with Coverage.py

Schema 7 is Coverage.py's internal database format, not a public interchange
standard. vfy rejects other schema versions instead of guessing.

vfy reads and reports Coverage.py schema-7 data. Interchange is tested
against Coverage.py 7.15.4; a matching schema number alone does not establish
compatibility with another release. See the [compatibility matrix](../about/compatibility.md#coveragepy-data).

## Parallel shards

Parallel files use the configured basename plus a process-specific suffix, such
as `.coverage.hostname.pid.token`.

`vfy coverage combine` checks the schema, mode, and tracer of every shard
before it writes anything, then replaces the output atomically. Line and branch
data can't be merged.

Shards are removed only after the output is replaced. If cleanup fails, the output
is still valid and the command reports the leftover shard. `--keep` skips cleanup.

`[paths]` groups and the controller/worker root pair share one path remapper. It
tries groups in order and applies a mapping only when the mapped local file
exists. Local shards and remote pytest-xdist payloads follow the same rules.

## Private serialized bytes

`CoverageData.dumps()` writes a vfy-only format for moving data between
matching vfy builds. It is not Coverage.py's `dumps` format and doesn't replace
schema-7 interchange. A round trip preserves coverage and semantic data. An
invalid payload leaves the destination unchanged.

`CoverageData.update()` validates the complete merge before changing its
destination. Measurements for paths mapped to the same filename are merged when
compatible.
See [CoverageData](python-api.md#coveragedata) for in-memory mutation and query
methods.

## Corruption and schema errors

Malformed SQLite, missing schema metadata, unsupported schema versions,
conflicting tracers, and mixed line and branch shards raise explicit data errors.
A failed validation never partly replaces the output file.
