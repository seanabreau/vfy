# vfy

A Python test runner and coverage tool, built in Rust.

vfy collects and runs pytest-style tests with its own engine by default, without
installing pytest. It measures line and branch coverage, reruns affected tests,
and provides mutation testing and decision coverage.

For optional execution with pytest 9.1 or newer, install `vfy[pytest]` and use `vfy test --engine=pytest`.

vfy is **Alpha**. The test runner requires Linux or macOS and CPython 3.12 or
newer. Check the [supported pytest features and plug-ins](https://vfy.neocline.com/about/compatibility/)
before adopting it.

Install vfy in your project and run your tests:

```console
uv add --dev vfy
uv run vfy test
```

To measure lines and branches, replace `src` with your source directory:

```console
uv run vfy test --cov=src --cov-branch --cov-report=term-missing
```

See the [coverage tutorial](https://vfy.neocline.com/getting-started/quickstart/),
[test selection tutorial](https://vfy.neocline.com/getting-started/selection/),
and [mutation guide](https://vfy.neocline.com/guides/mutation/) for complete examples.

[Documentation](https://vfy.neocline.com/) ·
[Source and issues](https://github.com/seanabreau/vfy)

Available under the Apache-2.0 or MIT license.
