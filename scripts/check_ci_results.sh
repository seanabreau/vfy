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
        require_result CHECK_DOCS true
        require_result LINUX_WHEEL skipped
        require_result RUST skipped
        ;;
    code)
        require_result LINUX_WHEEL success
        require_result RUST skipped
        ;;
    rust|unknown)
        if [[ "$SCOPE" == unknown ]]; then
            require_result CHECK_DOCS true
        fi
        require_result LINUX_WHEEL success
        require_result RUST success
        ;;
    *)
        echo "unexpected CI scope: ${SCOPE:-missing}" >&2
        exit 1
        ;;
esac

case "${CHECK_DOCS:-}" in
    true) require_result DOCUMENTATION success ;;
    false) require_result DOCUMENTATION skipped ;;
    *)
        echo "unexpected documentation check selection: ${CHECK_DOCS:-missing}" >&2
        exit 1
        ;;
esac
