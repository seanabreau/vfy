#!/usr/bin/env bash
# A green summary requires the selected tier to finish, including artifact tests.
set -euo pipefail

require_result() {
    local job=$1 expected=$2
    local actual=${!job:-missing}
    if [[ "$actual" != "$expected" ]]; then
        echo "$job: expected $expected, got $actual (scope: ${SCOPE:-missing})" >&2
        exit 1
    fi
}

for job in CHANGE_SCOPE REPOSITORY SUPPLY_CHAIN WORKFLOW_SECURITY; do
    require_result "$job" success
done

case "${SCOPE:-}" in
    documentation)
        required=(DOCUMENTATION)
        skipped=(QUALITY RUST_MATRIX FULL_BUILD FULL_WHEEL FAST_BUILD FAST_WHEEL NATIVE_ADOPTION)
        ;;
    code|tests)
        required=(QUALITY FAST_BUILD FAST_WHEEL NATIVE_ADOPTION)
        skipped=(DOCUMENTATION RUST_MATRIX FULL_BUILD FULL_WHEEL)
        ;;
    platform|unknown)
        required=(QUALITY RUST_MATRIX FULL_BUILD FULL_WHEEL NATIVE_ADOPTION)
        skipped=(DOCUMENTATION FAST_BUILD FAST_WHEEL)
        ;;
    *)
        echo "unexpected CI scope: ${SCOPE:-missing}" >&2
        exit 1
        ;;
esac

for job in "${required[@]}"; do
    require_result "$job" success
done
for job in "${skipped[@]}"; do
    require_result "$job" skipped
done
