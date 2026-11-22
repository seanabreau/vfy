#!/bin/sh
set -eu

. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/cargo-test-env.sh"
# cargo-insta 1.48 does not expose Cargo's `--locked` flag, while its
# nextest passthrough treats arguments after `--` as test-binary arguments.
# Keep the wrapper's documented interface by checking the lockfile here and
# forwarding the remaining test-runner options to cargo-insta.
if [ "${1:-}" = "--locked" ]; then
    cargo metadata --locked --no-deps --format-version 1 >/dev/null
    shift
fi

# The Python-extension crate is a cdylib and has no runnable Rust doctest target.  Tell
# cargo-insta not to launch its extra `cargo test --doc` pass after nextest.
exec cargo insta test \
    --unreferenced reject \
    --test-runner nextest \
    --disable-nextest-doctest \
    "$@"
