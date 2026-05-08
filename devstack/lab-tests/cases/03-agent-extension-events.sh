#!/usr/bin/env bash
# tags: smoke
#
# Agent-extension Port_Binding watcher: extension is loaded into the
# OVN agent and the watcher fires for our network at least once during
# the agent's lifetime.

CASE_ID="03-agent-extension-events"
CASE_TITLE="agent extension Port_Binding watcher"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

SVC_ID=""
BIND_ID=""

case_teardown() {
    teardown_binding "$BIND_ID"
    teardown_service "$SVC_ID"
}

# Confirm the extension actually loaded. We use bash `case` for the
# substring check rather than `grep -q`, because journalctl can emit
# multi-megabyte output and `echo "$LOGS" | grep -q` SIGPIPEs when grep
# exits early — `set -o pipefail` then fails the whole pipe even though
# grep matched.
LOGS=$(sudo journalctl -u "$AGENT_UNIT" --no-pager 2>&1)
case "$LOGS" in
    *"Extension manager: local-services OVN agent extension started"*)
        pass "extension loaded into $AGENT_UNIT" ;;
    *)
        fail "extension not in $AGENT_UNIT log" \
             "sudo journalctl -u $AGENT_UNIT | grep -i extension"
        exit 0 ;;
esac

SVC_ID=$(setup_service)
BIND_ID=$(setup_binding "$SVC_ID" "$NET_ID")
sleep 2

# The PB watcher fires on Logical_Switch_Port (Port_Binding) row CREATE
# / UPDATE / DELETE. host_routes injection happens at the *subnet*
# level (DHCP_Options), so an explicit bind/unbind that doesn't change
# localport existence (opt-out kept it alive) won't trigger any PB row
# change at all — the steady-state reconcile is via the periodic timer,
# not events.
#
# So: assert the watcher has fired *at any point* since the agent
# started for this network. The startup sync provisions a netns for
# every existing localport, which produces a "provision netns for
# network $NET_ID" log line. That line is the canonical evidence the PB
# watcher is correctly wired (its match_fn passed, its run() executed).
LOGS=$(sudo journalctl -u "$AGENT_UNIT" --no-pager 2>&1)
case "$LOGS" in
    *"local-services: "*"netns for network $NET_ID"*)
        pass "PB watcher fired for $NET_ID at least once" ;;
    *)
        fail "no local-services PB-event log line for $NET_ID in agent history" \
             "expected one of: provision/reconcile/teardown netns for network $NET_ID" ;;
esac
