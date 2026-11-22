#!/usr/bin/env bash
set -euo pipefail

environment_python="${UV_PROJECT_ENVIRONMENT:-.venv}/bin/python"
uv pip uninstall --python "$environment_python" pytest-asyncio

# Exercise the declared lower bounds before the regular version matrix.
uv pip uninstall --python "$environment_python" pytest-xdist
uv pip install --python "$environment_python" 'pytest==9.1.0' 'pluggy==1.5.0'
"$environment_python" - <<'PY_MINIMUM'
from importlib import metadata, util

assert metadata.version("pytest") == "9.1.0"
assert metadata.version("pluggy") == "1.5.0"
assert util.find_spec("xdist") is None
PY_MINIMUM
"$environment_python" -m pytest -q tests/integration/test_native_pytest_versions.py
uv pip install --python "$environment_python" 'pytest-xdist==3.8.0'
"$environment_python" -m pytest -q \
  tests/integration/test_native_protocol_hooks.py \
  tests/integration/test_native_warnings.py \
  tests/integration/test_native_lifecycle.py::test_control_exceptions_retain_partial_warnings
# Restore the newer wrapper adapter for the regular version matrix.
uv pip install --python "$environment_python" 'pluggy==1.6.0'

for pytest_version in 9.1.0 9.1.1 9.2.0; do
  uv pip uninstall --python "$environment_python" pytest-xdist
  uv pip install --python "$environment_python" "pytest==$pytest_version"
  PYTEST_VERSION="$pytest_version" "$environment_python" - <<'PY'
import os
from importlib import metadata, util

assert metadata.version("pytest") == os.environ["PYTEST_VERSION"]
assert util.find_spec("xdist") is None
PY
  "$environment_python" -m pytest -q tests/integration/test_native_pytest_versions.py
  # The broader differential suite uses xdist for its stock-pytest reference runs.
  uv pip install --python "$environment_python" "pytest==$pytest_version" 'pytest-xdist==3.8.0'
  "$environment_python" -m pytest -q \
    tests/integration/test_native_*.py \
    tests/integration/test_explicit_plugin_loads.py \
    tests/integration/test_pytest_config_projection.py \
    tests/integration/test_watch_native_workers.py \
    tests/integration/test_native_pytest_support.py \
    tests/integration/test_cli_diagnostics.py
done

# Pluggy 1.7 passes a keyword mapping to its legacy wrapper adapter.
uv pip install --python "$environment_python" 'pluggy==1.7.0'
"$environment_python" - <<'PY_PLUGGY'
from importlib import metadata

assert metadata.version("pluggy") == "1.7.0"
PY_PLUGGY
"$environment_python" -m pytest -q \
  tests/integration/test_native_protocol_hooks.py \
  tests/integration/test_native_warnings.py \
  tests/integration/test_native_compatible_plugins.py
