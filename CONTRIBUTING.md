# Contributing to vfy

Set up a checkout and run the required checks before opening a pull request.

<a id="setup"></a>

## Set up the repository

You need CPython 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [rustup](https://rustup.rs/). Source builds also need a C compiler: Xcode Command Line Tools on macOS or your Linux distribution's build tools.

From the checkout root, run:

```console
uv sync --locked --group dev
uv run maturin develop --release --skip-install
```

Rust is pinned in `rust-toolchain.toml`. Python dependencies are locked in `uv.lock`. Install the tools used by the unused-dependency, unlinked-source, and dependency-policy checks:

```console
cargo install --locked cargo-shear --version 1.13.4
cargo install --locked cargo-deny --version 0.20.2
```

The workspace has three internal crates, none published:

- `crates/vfy-core`: library crate for coverage data, analysis, configuration, selection, mutation, and reports. It does not depend on PyO3.
- `crates/vfy-pytest`: library crate for test collection and execution, built into the extension.
- `crates/vfy`: the `vfy._native` Python extension, which holds CPython monitoring and object conversion.

Give every new lint suppression a local reason. For real-process integration tests, use `tests.support.vfy_project.VfyProject` to isolate pytest, xdist, subprocess, and Coverage.py comparisons from the developer's environment.

<a id="before-a-pull-request"></a>

## Run the checks

From the checkout root:

```console
uv sync --locked --group dev --group benchmark
uv run ruff format --check .
uv run ruff check .
uv run ty check --python-version 3.12
uv run ty check --python-version 3.13
uv run ty check --python-version 3.14
uv run ty check --python-version 3.15
uv run validate-pyproject pyproject.toml
uv run pytest
cargo fmt --check
cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
cargo shear --deny-warnings --check-test-targets
cargo deny --locked --workspace --config .github/deny.toml check
scripts/cargo-test.sh --locked --workspace --no-default-features
uv sync --locked --only-group docs --no-install-project
uv run --no-sync zensical build --clean --strict
```

The Rust tests import the `vfy._native` extension built during setup; rerun `uv run maturin develop --release --skip-install` after changing Rust code.

See [CI change classification](docs/about/testing.md#ci-change-classification) for required jobs and platform selection.

Use [Testing](docs/about/testing.md) to choose an owning suite and independent oracle. Read the relevant instructions in [`.agents/skills/`](.agents/skills/) before changing compatibility, performance, tests, or writing. For tested command examples, see [Executable documentation](docs/about/testing.md#executable-documentation).

## Preview the documentation

Install the documentation group and start the local site:

```console
uv sync --locked --only-group docs --no-install-project
uv run --no-sync zensical serve
```

Before you submit documentation, run the strict build above. Check links in repository Markdown outside `docs/`. Inspect changed pages at desktop and mobile widths in both color schemes. Put commands and expected results in executable documentation tests when they protect a real user workflow.

## Use the Codex worktree hook

The repository includes a `SessionStart` hook in `.codex/hooks.json`. Review and trust it in Codex's Hooks panel (or `/hooks` in the CLI). Then start a task with **Worktree** selected. Bash, Git, jq, uv, and the Rust toolchain must be on the session's `PATH`.

On startup or resume, the hook runs the setup commands above when the task is in a linked Git worktree without a `.venv`. It keeps the virtual environment inside that worktree, even when `UV_PROJECT_ENVIRONMENT` is inherited. It skips the main checkout and existing virtual environments. If setup fails after creating `.venv`, rerun the setup commands manually in that worktree.

The hook prepares an existing worktree. It does not create worktrees or change the task's environment selection. Before you enable the repository hook, disable any personal copy of this setup hook. Codex runs matching hooks from every active source.

Before running setup, builds, or tests manually in a linked worktree, set these
variables from that worktree's root:

```console
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
export CARGO_TARGET_DIR="$PWD/target"
```

Use a distinct environment and Cargo target for each checkout. Shared targets
can reuse another checkout's compiled dependencies, even when commands run
serially. The hook sets only `UV_PROJECT_ENVIRONMENT` for its child commands;
set `CARGO_TARGET_DIR` in the session before the hook runs if it is inherited.
Downloaded crates can share `CARGO_HOME`.

## Run benchmarks

Validate correctness before timing a benchmark:

```console
uv run python -m benchmarks run report --report-formats terminal json --output target/benchmarks/report.json
```

Record the workload, warmup count, sample count, versions, and machine details with any published result. Compare the same output semantics. If one tool does less work, state that next to the result. Never publish the best single run, and never extend a report result to tracing overhead.

Performance changes should include a profile or a reasoned explanation of the hot path. Use end-to-end benchmarks for user-facing claims, and focused Rust benchmarks for local algorithm choices.

For commands, see [Rust benchmarks and profiling](benchmarks/README.md#rust-engine-diagnostics).

<a id="release-checklist"></a>

## Prepare a release

1. Update the active series file under `changelogs/` and the compatibility matrix.
   Check the installation commands and absolute links in `README.pypi.md`, the
   package description used by PyPI.
2. Run the full Python/Rust suite and wheel smoke tests on all CI targets. Use [release validation](docs/about/testing.md#release-validation) for the exact platform matrix and native-engine criteria.
3. Build the sdist and ABI3 wheels from a clean checkout.
4. Install each artifact into a clean environment. Run `vfy --version` and a Coverage.py database and report smoke test.
5. Review benchmark claims against the raw samples.
6. Read the version from `Cargo.toml`'s `[workspace.package].version`. Check the intended tag with `python scripts/check_release_version.py --tag vVERSION`, replacing `VERSION` with that value. Tag the verified commit on `main`. The release workflow publishes verified artifacts to PyPI through trusted publishing, then creates the GitHub release.
