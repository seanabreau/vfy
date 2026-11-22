#!/usr/bin/env bash
set -euo pipefail

environment_python="${UV_PROJECT_ENVIRONMENT:-.venv}/bin/python"

for mock_version in 3.13.0 3.14.0 3.15.0 3.16.0 3.15.1; do
  uv pip install --python "$environment_python" --no-deps "pytest-mock==$mock_version"
  "$environment_python" -m pytest -q \
    tests/integration/test_native_mock.py \
    tests/integration/test_native_mock_autoload.py \
    tests/integration/test_native_collected_fork_mock.py \
    tests/integration/test_native_xdist.py \
    tests/integration/test_cli_diagnostics.py -k mock
done
