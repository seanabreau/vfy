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

require_selected() {
    local job=$1 selection=$2
    local selected=${!selection:-missing}
    case "$selected" in
        true) require_result "$job" success ;;
        false) require_result "$job" skipped ;;
        *)
            echo "$selection: expected true or false, got $selected" >&2
            exit 1
            ;;
    esac
}

for job in CHANGE_SCOPE REPOSITORY SUPPLY_CHAIN WORKFLOW_SECURITY; do
    require_result "$job" success
done

case "${SCOPE:-}" in
    documentation)
        require_result CHECK_DOCS true
        wheel=skipped
        rust=skipped
        ;;
    code)
        wheel=success
        rust=skipped
        ;;
    rust|unknown)
        if [[ "$SCOPE" == unknown ]]; then
            require_result CHECK_DOCS true
        fi
        wheel=success
        rust=success
        ;;
    *)
        echo "unexpected CI scope: ${SCOPE:-missing}" >&2
        exit 1
        ;;
esac

require_selected DOCUMENTATION CHECK_DOCS
require_selected RELEASE CHECK_RELEASE
require_selected NATIVE_CORPUS CHECK_CORPUS
if [[ "$CHECK_RELEASE" == true ]]; then
    wheel=skipped
    rust=skipped
fi
require_result LINUX_WHEEL "$wheel"
require_result RUST "$rust"
