# Validate coverage with a source build

This example is a contributor fixture for testing the vfy checkout under
review. To install vfy from PyPI in your own pytest project, run:

```console
uv add --dev vfy
```

Then follow a user tutorial:

- [Run affected tests](https://github.com/seanabreau/vfy/blob/main/docs/getting-started/selection.md)
  on Linux or macOS. Edit a function, run one of two tests, and inspect which
  test reaches the edited line.
- [Measure coverage with vfy](https://github.com/seanabreau/vfy/blob/main/docs/getting-started/quickstart.md)
  on Linux or macOS. Find the missing branch and add its test.

See [Installation](https://github.com/seanabreau/vfy/blob/main/docs/getting-started/installation.md)
for requirements and other installation methods.

## Run the contributor fixture

Two tests check `clamp_low(5) == 5` and `format_count(42) == "42"`. The
`value < 0` branch in `clamp_low` is untested, so the combined statement and
branch coverage starts at 80%.

Run this fixture from `examples/coverage/` in a vfy checkout. You need
CPython 3.12+, uv, Rust 1.95+, and a C compiler. The lockfile fixes the Python
dependencies; vfy builds from the checkout two directories above. The first sync
builds its extension and can take several minutes. Keep the fixture inside the
checkout so that source path remains valid.

```console
uv sync --locked
```

For a full native run on Linux or macOS:

```console
uv run vfy test --all -j 1 --cov=src --cov-branch
```

Both tests pass; `.coverage` is written and the report shows 80%.
