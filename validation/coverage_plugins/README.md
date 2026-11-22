# Coverage.py plug-in checks

These small fixtures compare vfy with Coverage.py using installed third-party
plug-ins. They are separate from the synthetic protocol tests in
`tests/integration/test_plugin.py` and related suites.

Use an isolated environment and a candidate vfy wheel. Replace the wheel path
below with the artifact you want to check:

```console
uv venv /tmp/vfy-plugin-check --python 3.12
uv pip install --python /tmp/vfy-plugin-check/bin/python -r validation/coverage_plugins/requirements.txt /path/to/vfy.whl
/tmp/vfy-plugin-check/bin/python validation/coverage_plugins/package_smoke.py /tmp/vfy-plugin-results
```

Use Python 3.12 for this recipe. On Python 3.14, Coverage.py 7.15.4 defaults
to SysMonitor, which warns that file-tracer plug-ins are unsupported; the Django
comparison then fails because the reference engine collects no template data.
The harness clears `COVERAGE_*` variables, including `COVERAGE_CORE`.

The harness runs each fixture in separate Coverage.py and vfy processes. It
compares measured files, lines, tracer identities, per-line contexts, statements,
exclusions, missing lines, and JSON report totals. Both saved schema-7 data files
are read through Coverage.py's `CoverageData`. It exits nonzero if collection or
comparison fails and retains commands, stdout, stderr, reports, databases, and
canonical observations in the output directory.

| Package | Fixture |
| --- | --- |
| `coverage-conditional-plugin` | INI exclusions; TOML exclusions and conditional omission |
| `django-coverage-plugin` | A rendered template with an untaken branch, plus discovery of an unrendered template |
| `kodo-plugins-cover-test-context` | `unittest` class setup, test setup, test call, test teardown, and class teardown contexts |
