#!/usr/bin/env bash
set -euo pipefail

environment_python="${UV_PROJECT_ENVIRONMENT:-.venv}/bin/python"

check_minimum_versions() {
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
}

check_pytest_version() {
  local pytest_version="$1"

  uv pip uninstall --python "$environment_python" pytest-xdist
  uv pip install --python "$environment_python" "pytest==$pytest_version" 'pluggy==1.6.0'
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
}

check_pluggy_1_7() {
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
}

case "$#:${1-}" in
  0:) pytest_versions=(9.1.0 9.1.1 9.2.0) ;;
  1:9.1.0|1:9.1.1|1:9.2.0) pytest_versions=("$1") ;;
  *)
    printf 'Usage: %s [9.1.0|9.1.1|9.2.0]\n' "$0" >&2
    exit 2
    ;;
esac

uv pip uninstall --python "$environment_python" pytest-asyncio
for pytest_version in "${pytest_versions[@]}"; do
  if [[ "$pytest_version" == 9.1.0 ]]; then
    check_minimum_versions
  fi
  check_pytest_version "$pytest_version"
  if [[ "$pytest_version" == 9.2.0 ]]; then
    check_pluggy_1_7
  fi
done
