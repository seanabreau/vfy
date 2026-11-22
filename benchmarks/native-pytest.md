# Installed-wheel benchmarks

Run these recipes from a clean, committed repository checkout after
[dependency setup](README.md#getting-started). The harnesses verify source
revisions, installed wheel bytes, and build manifests. Keep artifacts outside
the checkout so it stays clean.

For the plain-pytest comparison, [Prepare a project](README.md#prepare-a-project)
builds and installs a release wheel. Its preparation directory contains
`vfy-build.json`, `wheels/`, and `environment/bin/python`. The manifest records
the wheel path, source revision, and artifact hashes. Use those paths below.
The coverage comparison needs the separate locked setup in its section.
Paths under `/path/to/` are placeholders for your artifacts and environments.

## Plain pytest and native execution

[The public command harness](../validation/pytest_runner/benchmark_public_pytest.py)
compares plain pytest with `vfy test --workers=1 --no-cov`:

```console
uv run --no-sync python validation/pytest_runner/benchmark_public_pytest.py \
  --python /path/to/installed/bin/python --wheel /path/to/vfy.whl \
  --build-manifest /path/to/build.json --warmups 3 --runs 15 \
  --output target/benchmarks/public-pytest
```

It checks ordered JUnit outcomes before measuring cold/warm bytecode, full
1,000-case suites, and one/ten-test selections from 10,000 cases. Both routes
disable pytest's cache provider; bytecode state does not describe filesystem caches.

## Coverage and revision comparisons

[The coverage harness](../validation/pytest_runner/benchmark_vfy_coverage.py)
compares stock pytest and native execution with vfy coverage, one worker,
and pytest 9.1.1. Both environments must have identical dependencies from the
repository's `uv.lock`; a prepared upstream project's dependency set is different.

The following setup uses one freshly built vfy wheel for both engines.
Choose a new, absolute artifact directory. Run this from the repository root
with Python 3.12 and the locked development dependencies installed:

```bash
export BENCH_DIR=/tmp/vfy-native-coverage
mkdir -p "$BENCH_DIR/wheels"
uv export --locked --group dev --group benchmark --no-emit-project \
  --output-file "$BENCH_DIR/requirements.txt"
for role in stock native; do
  uv venv "$BENCH_DIR/$role" --python python3.12
  uv pip install --python "$BENCH_DIR/$role/bin/python" --require-hashes \
    -r "$BENCH_DIR/requirements.txt"
done
uv run --no-sync maturin build --release --locked \
  --interpreter "$BENCH_DIR/stock/bin/python" \
  --manifest-path crates/vfy/Cargo.toml --out "$BENCH_DIR/wheels"
```

Retain the exact pytest wheel listed in the lock and verify its hash:

```bash
uv run --no-sync python - <<'PYTHON'
import hashlib
import os
from pathlib import Path
import tomllib
from urllib.request import urlopen

packages = tomllib.loads(Path("uv.lock").read_text())["package"]
pytest = next(package for package in packages if package["name"] == "pytest")
wheel = next(item for item in pytest["wheels"] if item["url"].endswith("py3-none-any.whl"))
payload = urlopen(wheel["url"]).read()
if "sha256:" + hashlib.sha256(payload).hexdigest() != wheel["hash"]:
    raise RuntimeError("pytest wheel hash differs from uv.lock")
path = Path(os.environ["BENCH_DIR"]) / "wheels" / wheel["url"].rsplit("/", 1)[1]
path.write_bytes(payload)
PYTHON
```

Install both exact wheels and record each environment's imported extension.
The manifest helper verifies that extension's bytes occur in the wheel:

```bash
for role in stock native; do
  uv pip install --python "$BENCH_DIR/$role/bin/python" --no-deps \
    --force-reinstall "$BENCH_DIR"/wheels/*.whl
  "$BENCH_DIR/$role/bin/python" - "$role" <<'PYTHON'
import os
import sys
from pathlib import Path
from benchmarks.benchmark_provenance import require_clean, write_build_manifest

root = Path.cwd()
artifacts = Path(os.environ["BENCH_DIR"])
write_build_manifest(
    artifacts / sys.argv[1] / "vfy-build.json",
    source_root=root,
    source_revision=require_clean(root).revision,
    wheel=next((artifacts / "wheels").glob("vfy-*.whl")),
    build_command=("maturin", "build", "--release", "--locked",
                   "--interpreter", str(artifacts / "stock/bin/python"),
                   "--manifest-path", "crates/vfy/Cargo.toml",
                   "--out", str(artifacts / "wheels")),
)
PYTHON
done
```

Validate ten synchronous tests before choosing a larger workload:

```bash
uv run --no-sync python validation/pytest_runner/benchmark_vfy_coverage.py \
  --stock-python "$BENCH_DIR/stock/bin/python" \
  --candidate-python "$BENCH_DIR/native/bin/python" \
  --stock-wheel "$BENCH_DIR"/wheels/vfy-*.whl \
  --candidate-wheel "$BENCH_DIR"/wheels/vfy-*.whl \
  --stock-pytest-wheel "$BENCH_DIR"/wheels/pytest-*.whl \
  --candidate-pytest-wheel "$BENCH_DIR"/wheels/pytest-*.whl \
  --stock-build-manifest "$BENCH_DIR/stock/vfy-build.json" \
  --candidate-build-manifest "$BENCH_DIR/native/vfy-build.json" \
  --workload sync --cases 10 --validate-only \
  --output "$BENCH_DIR/sync-validation.json"
```

Validation prints the retained `sync-validation.json.evidence` directory;
it collects no timings and does not write a summary JSON file. For measurement,
use the prepared paths with a new output path and explicit sample counts:

```console
uv run --no-sync python validation/pytest_runner/benchmark_vfy_coverage.py \
  --stock-python /path/to/stock/bin/python \
  --candidate-python /path/to/native/bin/python \
  --stock-wheel /path/to/vfy.whl --candidate-wheel /path/to/vfy.whl \
  --stock-pytest-wheel /path/to/pytest.whl \
  --candidate-pytest-wheel /path/to/pytest.whl \
  --stock-build-manifest /path/to/stock-build.json \
  --candidate-build-manifest /path/to/native-build.json \
  --workload fixtures --cases 10000 --warmups 3 --runs 15 \
  --output target/benchmarks/native-fixtures-10000.json
```

First add `--validate-only` and use a separate output path; validation retains
artifacts that the measured run must not overwrite. For a native before/after
comparison, add `--stock-engine=native` and supply baseline/candidate wheels and
manifests. `--workers=auto` measures automatic capacity on the generated
single-file suite; the setting applies to both roles.

Workloads are `sync`, `async`, `fixtures`, and `capture`, with 1, 10, 1,000,
10,000, or 100,000 cases. Every RSS, warmup, and timed child checks ordered
outcomes, fixture lifecycle, schema-7 lines/arcs/contexts, and complete JSON
reports. Child measurements include execution-order and lifecycle recording;
post-run hashing and compression are excluded. Tool order rotates, and raw
stdout/stderr, proofs, and resource counters are retained with checksums.

These are fresh-process command times with coverage and reports. Bytecode and
filesystem caches stay warm after validation. Capture writes 64 KiB per case:
100,000 cases write about 6.1 GiB per process, so timing includes substantial
capture and filesystem work. Results describe the generated workload.

## Small project selections

[The project candidate harness](../validation/pytest_runner/benchmark_real_candidates.py)
compares installed release wheels on pinned Rich and iniconfig checkouts by
default. Its artifacts JSON maps `baseline` and each candidate name to absolute
`wheel` and `build_manifest` paths:

```console
uv run python validation/pytest_runner/benchmark_real_candidates.py \
  --prepare-only --python-version 3.12 --artifacts /path/to/artifacts.json \
  --output target/benchmarks/real-prepared
uv run python validation/pytest_runner/benchmark_real_candidates.py \
  --prepared target/benchmarks/real-prepared \
  --output target/benchmarks/real-results --counts 1 10
uv run python validation/pytest_runner/audit_real_candidates.py \
  --results target/benchmarks/real-results --require-publication \
  --output target/benchmarks/real-audit.json
```

Use `--project packaging` on both preparation and measurement commands to select
Packaging. The controller and prepared runtimes must share a Python minor version.
Upstream configuration and deterministic preparation diffs are recorded in `upstream.json`.
A full-suite run establishes history; retained-history runs restore it outside
the timer. Timed commands include collection, execution, coverage persistence,
JSON, and JUnit reports.

Before timing and after every child, checks compare outcomes, warnings, JUnit,
per-context lines/arcs, tracers, full JSON, and history. Coverage.py independently
checks each database's report. Three warmups and fifteen measured runs rotate
role order; the auditor rechecks samples and wheel identities. Filesystem and
bytecode caches remain warm; RSS is not measured. Use `--history-state cold retained`
for both history states or `--counts 0 --warmups 0 --runs 0` for full-suite parity only.

For broader project suites, see the
[bounded/full-suite driver](../validation/pytest_runner/README.md#time-bounded-project-suites)
and [measurement limits](../docs/about/benchmarks.md#how-to-read-benchmark-evidence).
