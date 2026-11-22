#!/bin/sh
set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/cargo-test-env.sh"
exec cargo nextest run "$@"
