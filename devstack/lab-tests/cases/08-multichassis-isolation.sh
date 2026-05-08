#!/usr/bin/env bash
# tags: multichassis
#
# Multi-chassis: two REMOTE compute chassis. Plumb a tenant client port
# on each via ssh-to-the-compute (port_id realized in br-int there →
# OVN binds the port to that chassis → localport realized there →
# agent extension provisions localsvc-<net> netns + LVS state). Assert
# per-chassis netns + keepalived, the data path from each chassis's
# tenant client through the VIP, and per-chassis state independence.
#
# Backend lives in each chassis's localsvc netns (one per chassis), so
# each chassis's keepalived TCP_CHECK marks its LOCAL backend healthy
# and the REMOTE one dead, and LVS-NAT picks the local one. This is the
# multi-chassis exit criterion: "boot tenant VMs across both chassis,
# confirm reachability on each."

CASE_ID="08-multichassis-isolation"
CASE_TITLE="multi-chassis: 2 compute chassis"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

requires_second_chassis

NS_NAME="localsvc-$NET_ID"
SVC_ID=""
BIND_ID=""
BE_ID_A=""
BE_ID_B=""

case_teardown() {
    m10mc_remote_backend_kill "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_BACKEND_PORT_A"
    m10mc_remote_backend_kill "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_BACKEND_PORT_B"
    m10mc_remote_client_teardown "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_A"
    m10mc_remote_client_teardown "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_B"
    teardown_binding "$BIND_ID"
    [[ -n "$BE_ID_A" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_ID_A" >/dev/null 2>&1 || true
    [[ -n "$BE_ID_B" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_ID_B" >/dev/null 2>&1 || true
    teardown_service "$SVC_ID"
}

# Sanity: each compute reachable as the ssh user.
HN_A=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "uname -n" 2>/dev/null)
HN_B=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "uname -n" 2>/dev/null)
if [[ -z "$HN_A" || -z "$HN_B" ]]; then
    fail "ssh to one of the computes failed (a=$HN_A b=$HN_B) — skipping"
    exit 0
fi
pass "ssh-from-controller to both computes OK ($HN_A, $HN_B)"

# 0) Wipe any leftover state from a prior run so we start clean.
multichassis_clean_leftovers

# 1) Plumb a tenant client on each compute. This is what makes OVN bind
#    the port to that chassis, and that's what makes the agent
#    provision the localsvc netns there.
PORT_ID_A=$(m10mc_remote_client_setup "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_A")
PORT_ID_B=$(m10mc_remote_client_setup "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_B")
if [[ -z "$PORT_ID_A" || -z "$PORT_ID_B" ]]; then
    fail "could not create + plumb tenant client port on one of the computes"
    exit 0
fi
pass "tenant client ports plumbed on both chassis (A=$PORT_ID_A B=$PORT_ID_B)"

# 2) Service + binding. Service first (so it exists before the binding
#    triggers reconciles).
SVC_ID=$(lookup_service_id "$MULTICHASSIS_SVC_NAME")
if [[ -z "$SVC_ID" ]]; then
    SVC_ID=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$MULTICHASSIS_SVC_NAME\",\"local_ipv4\":\"$MULTICHASSIS_SVC_VIP\",\"port\":$MULTICHASSIS_SVC_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\"}}" \
        | _jget "['local_service']['id']" 2>/dev/null || true)
fi
if [[ -z "$SVC_ID" ]]; then
    fail "could not create service $MULTICHASSIS_SVC_NAME"
    exit 0
fi
BIND_ID=$(setup_binding "$SVC_ID" "$NET_ID")

# OVN binds tenant ports → realizes localports on both computes →
# agent extension fires CREATE → provisions netns. Allow time: remote
# agents poll/event on a 10s tick like the local one.
sleep 16

# 3) Per-chassis netns + keepalived presence.
NETNS_A=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "sudo ip netns list 2>/dev/null | awk '{print \$1}' | grep -x $NS_NAME")
NETNS_B=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns list 2>/dev/null | awk '{print \$1}' | grep -x $NS_NAME")
if [[ -n "$NETNS_A" ]]; then
    pass "chassis A has $NS_NAME"
else
    fail "chassis A missing $NS_NAME (agent extension on A didn't provision)"
fi
if [[ -n "$NETNS_B" ]]; then
    pass "chassis B has $NS_NAME"
else
    fail "chassis B missing $NS_NAME (agent extension on B didn't provision)"
fi

KA_A=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "
    for pid in \$(sudo ip netns pids $NS_NAME 2>/dev/null); do
        comm=\$(sudo cat /proc/\${pid}/comm 2>/dev/null)
        [[ \$comm == keepalived ]] && echo yes && break
    done
")
KA_B=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "
    for pid in \$(sudo ip netns pids $NS_NAME 2>/dev/null); do
        comm=\$(sudo cat /proc/\${pid}/comm 2>/dev/null)
        [[ \$comm == keepalived ]] && echo yes && break
    done
")
if [[ "$KA_A" == "yes" ]]; then
    pass "keepalived running in netns on chassis A"
else
    fail "no keepalived process in $NS_NAME on chassis A"
fi
if [[ "$KA_B" == "yes" ]]; then
    pass "keepalived running in netns on chassis B"
else
    fail "no keepalived process in $NS_NAME on chassis B"
fi

# 4) One backend per chassis. OVN `type=localport` blocks ingress
#    traffic via tunnels, so chassis B cannot reach chassis A's
#    localport IP — a single shared backend is unreachable from the
#    other chassis. Per-chassis backends sidestep this: each chassis's
#    keepalived TCP_CHECK marks its LOCAL backend healthy and the
#    REMOTE one dead, and LVS-NAT picks the local one. The registry
#    has no AZ awareness (see docs/limitations.md §2), so register both
#    addresses blindly; keepalived's HC does the per-chassis filtering.
NS_A_IP=$(m10mc_remote_backend_spawn "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_BACKEND_PORT_A")
NS_B_IP=$(m10mc_remote_backend_spawn "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_BACKEND_PORT_B")
if [[ -z "$NS_A_IP" || -z "$NS_B_IP" ]]; then
    fail "could not spawn per-chassis backends (a=$NS_A_IP b=$NS_B_IP)"
    exit 0
fi
BE_ID_A=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-m10mc-a\",\"service_id\":\"$SVC_ID\",\"address\":\"$NS_A_IP\",\"port\":$MULTICHASSIS_BACKEND_PORT_A}}" \
    | _jget "['local_service_backend']['id']" 2>/dev/null || true)
BE_ID_B=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-m10mc-b\",\"service_id\":\"$SVC_ID\",\"address\":\"$NS_B_IP\",\"port\":$MULTICHASSIS_BACKEND_PORT_B}}" \
    | _jget "['local_service_backend']['id']" 2>/dev/null || true)
if [[ -z "$BE_ID_A" || -z "$BE_ID_B" ]]; then
    fail "per-chassis backend POST returned empty id (a=$BE_ID_A b=$BE_ID_B)"
fi
# Allow two keepalived TCP_CHECK passes: first probes both backends
# (~6s delay_loop + 3s connect_timeout for the cross-chassis fail);
# second pass lands the LOCAL backend in ipvsadm. ~22s is empirically
# the safe budget on this lab.
sleep 22

# 5) Per-chassis ipvsadm: each chassis sees the VIP, and crucially ONLY
#    its local backend (the remote one fails HC and is pruned).
IPVS_A=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "sudo ip netns exec $NS_NAME ipvsadm -L -n 2>/dev/null")
IPVS_B=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns exec $NS_NAME ipvsadm -L -n 2>/dev/null")
# Both backend addresses are identical (same localport IP), so we
# distinguish by PORT — A's backend on PORT_A, B's on PORT_B.
if echo "$IPVS_A" | grep -qE "TCP\s+${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}" \
   && echo "$IPVS_A" | grep -q ":${MULTICHASSIS_BACKEND_PORT_A}\b"; then
    pass "chassis A ipvsadm has VIP + local backend (port ${MULTICHASSIS_BACKEND_PORT_A})"
else
    fail "chassis A ipvsadm missing local backend on port ${MULTICHASSIS_BACKEND_PORT_A}" "$IPVS_A"
fi
if echo "$IPVS_B" | grep -qE "TCP\s+${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}" \
   && echo "$IPVS_B" | grep -q ":${MULTICHASSIS_BACKEND_PORT_B}\b"; then
    pass "chassis B ipvsadm has VIP + local backend (port ${MULTICHASSIS_BACKEND_PORT_B})"
else
    fail "chassis B ipvsadm missing local backend on port ${MULTICHASSIS_BACKEND_PORT_B}" "$IPVS_B"
fi
# Cross-chassis backend should NOT be in the other's ipvsadm.
# keepalived TCP_CHECK from chassis A to "${NS_A_IP}:${PORT_B}"
# actually probes A's OWN netns kernel (since the netns IP is the same
# on both chassis). Port B has no listener on A → HC fails → backend
# pruned. Same logic in reverse for chassis B.
if echo "$IPVS_A" | grep -q ":${MULTICHASSIS_BACKEND_PORT_B}\b"; then
    fail "chassis A leaked remote (port ${MULTICHASSIS_BACKEND_PORT_B}) backend into ipvsadm"
else
    pass "chassis A does not list cross-chassis backend (HC correctly prunes port ${MULTICHASSIS_BACKEND_PORT_B})"
fi
if echo "$IPVS_B" | grep -q ":${MULTICHASSIS_BACKEND_PORT_A}\b"; then
    fail "chassis B leaked remote (port ${MULTICHASSIS_BACKEND_PORT_A}) backend into ipvsadm"
else
    pass "chassis B does not list cross-chassis backend (HC correctly prunes port ${MULTICHASSIS_BACKEND_PORT_A})"
fi

# 6) End-to-end data path from EACH chassis's tenant client. Both cases
#    route via the LOCAL chassis's LVS director to the LOCAL backend
#    (since the remote backend isn't healthy from here).
OUT_A=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "sudo ip netns exec $MULTICHASSIS_CLIENT_NS curl -sS --max-time 6 http://${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}/" 2>&1)
OUT_B=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns exec $MULTICHASSIS_CLIENT_NS curl -sS --max-time 6 http://${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}/" 2>&1)
if [[ "$OUT_A" == *"Directory listing"* ]]; then
    pass "tenant on chassis A → VIP reaches local backend"
else
    fail "tenant on chassis A → VIP failed" "out=$OUT_A"
fi
if [[ "$OUT_B" == *"Directory listing"* ]]; then
    pass "tenant on chassis B → VIP reaches local backend"
else
    fail "tenant on chassis B → VIP failed" "out=$OUT_B"
fi

# 7) Per-chassis state independence: kill the backend on chassis A.
#    Chassis A's data path should break (within ~12s of HC); chassis B's
#    data path should KEEP WORKING (uses its own local backend, which is
#    unrelated to A's). Proves the chassises don't share LB state.
KA_PID_B_BEFORE=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "
    for pid in \$(sudo ip netns pids $NS_NAME 2>/dev/null); do
        [[ \$(sudo cat /proc/\${pid}/comm 2>/dev/null) == keepalived ]] && echo \$pid && break
    done
")
m10mc_remote_backend_kill "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_BACKEND_PORT_A"
sleep 14
OUT_B=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns exec $MULTICHASSIS_CLIENT_NS curl -sS --max-time 6 http://${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}/" 2>&1)
if [[ "$OUT_B" == *"Directory listing"* ]]; then
    pass "chassis B data path still works after chassis A backend killed (per-chassis state)"
else
    fail "chassis B data path broke when chassis A backend died (cross-chassis dependency!)" "out=$OUT_B"
fi
KA_PID_B_AFTER=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "
    for pid in \$(sudo ip netns pids $NS_NAME 2>/dev/null); do
        [[ \$(sudo cat /proc/\${pid}/comm 2>/dev/null) == keepalived ]] && echo \$pid && break
    done
")
if [[ -n "$KA_PID_B_AFTER" && "$KA_PID_B_AFTER" == "$KA_PID_B_BEFORE" ]]; then
    pass "chassis B keepalived PID unchanged across chassis A churn (pid=$KA_PID_B_BEFORE)"
elif [[ -n "$KA_PID_B_AFTER" ]]; then
    # SIGHUP reload is allowed; new PID would mean a kill+respawn (also fine).
    pass "chassis B keepalived alive (before=$KA_PID_B_BEFORE after=$KA_PID_B_AFTER)"
else
    fail "chassis B keepalived disappeared (before=$KA_PID_B_BEFORE)"
fi
