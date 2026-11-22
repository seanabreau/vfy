#!/bin/sh
# Shared PyO3 environment for Rust test runners. The abi3-py312 floor must
# be checked against the project interpreter, not whatever `python3` is on
# PATH, and the test binary links libpython dynamically.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
venv=${VIRTUAL_ENV:-$root/.venv}
python=$venv/bin/python
[ -x "$python" ] || python=$venv/Scripts/python.exe
if [ ! -x "$python" ]; then
    echo "no interpreter in $venv; run 'uv sync --group dev' first" >&2
    exit 1
fi

libdir=$("$python" -c '
from pathlib import Path
import sys
import sysconfig

reported = Path(sysconfig.get_config_var("LIBDIR") or "")
library = sysconfig.get_config_var("LDLIBRARY") or ""
if library and (reported / library).is_file():
    print(reported)
else:
    # Relocatable/standalone Python builds can retain the build-time
    # ``LIBDIR`` (for example ``/install/lib``). The shared library is
    # normally next to the interpreter prefix in that case.
    candidate = Path(sys.executable).resolve().parent.parent / "lib"
    print(candidate if library and (candidate / library).is_file() else reported)
')
PYO3_PYTHON=$python
export PYO3_PYTHON
# Rust tests that import the vfy package need the built extension next to the
# Python sources; a cached or --no-install-project environment does not have it.
if ! "$python" -c '
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
import sys

package = Path(sys.argv[1]) / "python" / "vfy"
sys.exit(0 if any((package / f"_native{suffix}").is_file() for suffix in EXTENSION_SUFFIXES) else 1)
' "$root"; then
    echo "vfy._native is not built in $root/python/vfy; run 'uv run maturin develop --uv --locked' first" >&2
    exit 1
fi
# Embedded CPython does not activate the venv or its editable-install .pth file.
# Rust tests exercise the Python adapters belonging to this checkout.
PYTHONPATH=$root/python${PYTHONPATH:+:$PYTHONPATH}
export PYTHONPATH
# PyO3 test binaries embed the interpreter instead of launching the venv's
# wrapper. Relocatable Python distributions can otherwise leave the embedded
# prefix at their build-time `/install` path and fail before the first test.
if [ -z "${PYTHONHOME:-}" ]; then
    PYTHONHOME=$("$python" -c 'import sys; print(sys.base_prefix)')
    export PYTHONHOME
fi
if [ -n "$libdir" ]; then
    LIBRARY_PATH=$libdir${LIBRARY_PATH:+:$LIBRARY_PATH}
    LD_LIBRARY_PATH=$libdir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
    DYLD_LIBRARY_PATH=$libdir${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}
    DYLD_FALLBACK_LIBRARY_PATH=$libdir${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}
    export LIBRARY_PATH LD_LIBRARY_PATH DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH
fi
