# shellcheck shell=bash
#
# Per-case bootstrap: sources every other lib/ file, sets up strict
# bash, declares precondition helpers, registers the EXIT trap that
# runs the case's teardown function, and decides the case's exit code
# from $FAIL.
#
# Usage from a case file:
#
#     #!/usr/bin/env bash
#     CASE_ID="06-nat-plugin"
#     CASE_TITLE="plugin abstraction + nat (Keepalived/LVS)"
#     LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#     # shellcheck source=../lib/case.sh
#     . "${LAB_TESTS_DIR}/lib/case.sh"
#
#     case_teardown() {
#         # Best-effort cleanup. Runs under EXIT trap.
#         ...
#     }
#
#     # ... assertions via pass / fail ...
#
# The bootstrap below auto-fetches $TOKEN and $NET_ID, emits the case
# header, and exits non-zero if any `fail` was called. `requires_*`
# helpers short-circuit to skip-emit-and-exit-0 when a precondition
# fails — the runner treats SKIP separately from FAIL.

set -Eeuo pipefail

# Source siblings. LAB_TESTS_DIR is set by the case before sourcing us.
# shellcheck source=config.sh
. "${LAB_TESTS_DIR}/lib/config.sh"
# shellcheck source=assert.sh
. "${LAB_TESTS_DIR}/lib/assert.sh"
# shellcheck source=rest.sh
. "${LAB_TESTS_DIR}/lib/rest.sh"
# shellcheck source=netns.sh
. "${LAB_TESTS_DIR}/lib/netns.sh"
# shellcheck source=multichassis.sh
. "${LAB_TESTS_DIR}/lib/multichassis.sh"
# shellcheck source=underlay.sh
. "${LAB_TESTS_DIR}/lib/underlay.sh"

# Per-case scratch dir. Cases that need transient files (rendered
# fixtures, captured logs) put them here. The trap nukes it on exit.
CASE_TMP="/tmp/lab-tests/${CASE_ID:-unknown}"
mkdir -p "$CASE_TMP"

# Default no-op teardown — cases override this.
case_teardown() { :; }

_case_run_teardown() {
    # Run regardless of how we got here, in a subshell so a teardown
    # failure can't change the case's pass/fail outcome. Keep stdout
    # streaming so logs include the cleanup.
    local rc=$?
    set +e
    case_teardown 2>&1 | sed 's/^/# teardown: /'
    rm -rf "$CASE_TMP" 2>/dev/null || true
    if [[ "$rc" -ne 0 ]]; then
        # Script aborted (set -e or explicit exit). Surface as a TAP
        # bail-out so the runner records this case as FAIL even though
        # we never reached the explicit `fail` calls in the body.
        echo "Bail out! case $CASE_ID aborted with rc=$rc"
        exit "$rc"
    fi
    tap_summary
    [[ "${FAIL:-0}" -eq 0 ]] || exit 1
    exit 0
}
trap _case_run_teardown EXIT

# --- Preconditions ------------------------------------------------------
# Each `requires_*` either passes silently or calls `skip` (which TAP-
# emits and exits 0). A case at the top simply lists its preconditions
# in order; the runner treats skipped cases as neither pass nor fail.

requires_devstack() {
    # The baseline: neutron-api up, OS_CLOUD points at it, network
    # exists. If any of these are off, every case will fail; we want a
    # clean SKIP instead.
    if ! "$OS_BIN" --os-cloud "$OS_CLOUD_NAME" token issue >/dev/null 2>&1; then
        skip "OS_CLOUD_NAME=$OS_CLOUD_NAME cannot issue a token (devstack down?)"
    fi
    if ! "$OS_BIN" --os-cloud "$OS_CLOUD_NAME" network show "$NET_NAME" >/dev/null 2>&1; then
        skip "network '$NET_NAME' not present"
    fi
}

requires_second_chassis() {
    # The multi-chassis suite needs ssh-by-key access to two compute
    # nodes. Without the key, the run is meaningless — skip cleanly.
    if [[ ! -r "$MULTICHASSIS_SSH_KEY" ]]; then
        skip "MULTICHASSIS_SSH_KEY=$MULTICHASSIS_SSH_KEY not readable — second chassis unavailable"
    fi
    if ! m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "true" >/dev/null 2>&1 \
       || ! m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "true" >/dev/null 2>&1; then
        skip "ssh to compute A=$MULTICHASSIS_COMPUTE_A_IP or B=$MULTICHASSIS_COMPUTE_B_IP failed"
    fi
}

requires_underlay_backends() {
    # The underlay test is a no-op if the lab's HTTP / DNS underlay
    # services aren't actually reachable from this chassis. Probe them
    # before running the test body.
    #
    # Both probes retry: a single-shot probe is too fragile, since
    # "lab DNS dropped one UDP packet" or "lab HTTP momentarily under
    # load" silently flips the whole test to SKIP even though the
    # backends are otherwise healthy. dig's `+tries=3` retries the
    # query, curl's `--retry 2 --retry-all-errors` retries the request
    # — both keep the same timeout-per-try semantics.
    local tcp_probe udp_probe
    tcp_probe=$(curl -sS --max-time 4 --retry 2 --retry-all-errors --retry-delay 1 \
        -o /dev/null -w "%{http_code}" \
        "http://${UNDERLAY_TCP_BACKEND_ADDR}:${UNDERLAY_TCP_BACKEND_PORT}/" 2>&1 || true)
    if ! [[ "$tcp_probe" =~ ^[1-5][0-9][0-9]$ ]]; then
        skip "underlay TCP backend ${UNDERLAY_TCP_BACKEND_ADDR}:${UNDERLAY_TCP_BACKEND_PORT} unreachable (got '$tcp_probe')"
    fi
    udp_probe=$(dig +time=2 +tries=3 "@${UNDERLAY_UDP_BACKEND_ADDR}" -p "${UNDERLAY_UDP_BACKEND_PORT}" example.com a +short 2>&1 | head -1 || true)
    if ! [[ "$udp_probe" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        skip "underlay UDP DNS ${UNDERLAY_UDP_BACKEND_ADDR}:${UNDERLAY_UDP_BACKEND_PORT} unreachable (got '$udp_probe')"
    fi
}

# --- Bootstrap ---------------------------------------------------------
requires_devstack
TOKEN=$(_token)
NET_ID=$(_get_net_id)
tap_setup
echo "# network=$NET_NAME ($NET_ID) neutron=$NEUTRON_URL agent=$AGENT_UNIT"
echo "# === ${CASE_TITLE:-$CASE_ID} ==="
