#!/usr/bin/env bash
# tags: multitenant
#
# Multi-tenant + mixed-plugin isolation: two tenant networks on the same
# chassis, each with its own VIPs; one network runs both LVS and Envoy
# services in the same netns (mixed-plugin). Cross-tenant traffic must
# be blocked.
#
# Currently a no-op: envoy was replaced by the proxy plugin, and the
# (nat + proxy) mixed-plugin port hasn't landed yet. The test body is
# preserved below for when proxy coverage is wired in.

CASE_ID="07-multitenant-isolation"
CASE_TITLE="multi-tenant (2 networks) + mixed-plugin (nat + proxy on one ns)"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

# Mirrors the original early-return in run-on-lab.sh:1213. envoy
# assertions are gone now that proxy replaced envoy; nat-side coverage
# will move into the follow-up. Skip cleanly so the case is visible in
# summaries but doesn't pollute pass/fail counts.
skip "envoy → proxy follow-up: nat+proxy mixed-plugin coverage not yet ported"

# --- Below is the original test body, kept for reference. ---------------
# When the (nat + proxy) mixed-plugin replacement lands, remove the
# `skip` above and rewire this section to use proxy where it currently
# uses envoy.

SVC_A_LVS_ID=""
SVC_A_ENVOY_ID=""
SVC_B_ENVOY_ID=""
BIND_A_LVS=""
BIND_A_ENVOY=""
BIND_B_ENVOY=""
BE_A_LVS=""
BE_A_ENVOY=""
BE_B_ENVOY=""
NET_B_ID=""
STUB_A_SVC=""
STUB_A_BIND=""

case_teardown() {
    m10_test_client_teardown "$M10_CLIENT_A_NS" "$M10_CLIENT_A_VETH_ROOT" "a"
    m10_test_client_teardown "$M10_CLIENT_B_NS" "$M10_CLIENT_B_VETH_ROOT" "b"
    for p in "$M10_SVC_A_LVS_BACKEND_PORT" \
             "$M10_SVC_A_ENVOY_BACKEND_PORT" \
             "$M10_SVC_B_ENVOY_BACKEND_PORT"; do
        pf="/tmp/m10-backend.${p}.pid"
        if [[ -f "$pf" ]]; then
            sudo kill "$(sudo cat "$pf")" 2>/dev/null || true
            sudo rm -f "$pf"
        fi
    done
    teardown_binding "$BIND_A_LVS"
    teardown_binding "$BIND_A_ENVOY"
    teardown_binding "$BIND_B_ENVOY"
    teardown_binding "$STUB_A_BIND"
    [[ -n "$BE_A_LVS" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_A_LVS" >/dev/null 2>&1 || true
    [[ -n "$BE_A_ENVOY" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_A_ENVOY" >/dev/null 2>&1 || true
    [[ -n "$BE_B_ENVOY" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_B_ENVOY" >/dev/null 2>&1 || true
    teardown_service "$SVC_A_LVS_ID"
    teardown_service "$SVC_A_ENVOY_ID"
    teardown_service "$SVC_B_ENVOY_ID"
    teardown_service "$STUB_A_SVC"
}

NS_A_NAME="localsvc-$NET_ID"

# ---- Network B setup ---------------------------------------------------
# Idempotent: reuse the network if a previous run left it.
NET_B_ID=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" network show \
    "$ISOLATION_NETB_NAME" -f value -c id 2>/dev/null || true)
if [[ -z "$NET_B_ID" ]]; then
    NET_B_ID=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" network create \
        "$ISOLATION_NETB_NAME" -f value -c id 2>/dev/null)
    "$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet create \
        --network "$NET_B_ID" --subnet-range "$M10_NETB_CIDR" \
        --gateway "$M10_NETB_GW" --dhcp \
        "${ISOLATION_NETB_NAME}-subnet" -f value -c id >/dev/null
fi
if [[ -z "$NET_B_ID" ]]; then
    fail "could not create or find network $ISOLATION_NETB_NAME"
    exit 0
fi
NS_B_NAME="localsvc-$NET_B_ID"
pass "network B in place ($ISOLATION_NETB_NAME = $NET_B_ID)"

# ---- Backends ----------------------------------------------------------
# LVS backend lives inside netns A (LVS-NAT routing requires reach from
# the director). Drive netns A into existence first via any binding —
# reuse the localport-lifecycle stub.
STUB_A_SVC=$(setup_service)
STUB_A_BIND=$(setup_binding "$STUB_A_SVC" "$NET_ID")
sleep 4
if ! sudo ip netns list | awk '{print $1}' | grep -qx "$NS_A_NAME"; then
    fail "localsvc netns missing on network A — multitenant test cannot proceed"
    exit 0
fi
NS_A_IP=$(m10_get_localsvc_ns_ip_for "$NET_ID")
if [[ -z "$NS_A_IP" ]]; then
    fail "could not read network-A localsvc netns IP"
    exit 0
fi
m8_spawn_tcp_backend "$NS_A_NAME" "$NS_A_IP" "$M10_SVC_A_LVS_BACKEND_PORT"

# Envoy backends live in the host root netns (host-side proxy worker
# pattern). Distinct ports so we can tell them apart in logs.
sudo bash -c "cd /tmp && python3 -m http.server $M10_SVC_A_ENVOY_BACKEND_PORT --bind 127.0.0.2" \
    >/tmp/m10-backend.${M10_SVC_A_ENVOY_BACKEND_PORT}.log 2>&1 &
echo "$!" | sudo tee /tmp/m10-backend.${M10_SVC_A_ENVOY_BACKEND_PORT}.pid >/dev/null
sudo bash -c "cd /tmp && python3 -m http.server $M10_SVC_B_ENVOY_BACKEND_PORT --bind 127.0.0.2" \
    >/tmp/m10-backend.${M10_SVC_B_ENVOY_BACKEND_PORT}.log 2>&1 &
echo "$!" | sudo tee /tmp/m10-backend.${M10_SVC_B_ENVOY_BACKEND_PORT}.pid >/dev/null
sleep 1

# ---- Services + bindings ----------------------------------------------
SVC_A_LVS_ID=$(lookup_service_id "$M10_SVC_A_LVS_NAME")
if [[ -z "$SVC_A_LVS_ID" ]]; then
    SVC_A_LVS_ID=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$M10_SVC_A_LVS_NAME\",\"local_ipv4\":\"$M10_SVC_A_LVS_VIP\",\"port\":$M10_SVC_A_LVS_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\"}}" \
        | _jget "['local_service']['id']" 2>/dev/null || true)
fi
SVC_A_ENVOY_ID=$(lookup_service_id "$M10_SVC_A_ENVOY_NAME")
if [[ -z "$SVC_A_ENVOY_ID" ]]; then
    SVC_A_ENVOY_ID=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$M10_SVC_A_ENVOY_NAME\",\"local_ipv4\":\"$M10_SVC_A_ENVOY_VIP\",\"port\":$M10_SVC_A_ENVOY_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}" \
        | _jget "['local_service']['id']" 2>/dev/null || true)
fi
SVC_B_ENVOY_ID=$(lookup_service_id "$M10_SVC_B_ENVOY_NAME")
if [[ -z "$SVC_B_ENVOY_ID" ]]; then
    SVC_B_ENVOY_ID=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$M10_SVC_B_ENVOY_NAME\",\"local_ipv4\":\"$M10_SVC_B_ENVOY_VIP\",\"port\":$M10_SVC_B_ENVOY_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}" \
        | _jget "['local_service']['id']" 2>/dev/null || true)
fi
if [[ -z "$SVC_A_LVS_ID" || -z "$SVC_A_ENVOY_ID" || -z "$SVC_B_ENVOY_ID" ]]; then
    fail "could not create one of the multitenant services" \
         "lvs_a=$SVC_A_LVS_ID envoy_a=$SVC_A_ENVOY_ID envoy_b=$SVC_B_ENVOY_ID"
    exit 0
fi

BE_A_LVS=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-m10-a-lvs\",\"service_id\":\"$SVC_A_LVS_ID\",\"address\":\"$NS_A_IP\",\"port\":$M10_SVC_A_LVS_BACKEND_PORT}}" \
    | _jget "['local_service_backend']['id']" 2>/dev/null || true)
BE_A_ENVOY=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-m10-a-envoy\",\"service_id\":\"$SVC_A_ENVOY_ID\",\"address\":\"127.0.0.2\",\"port\":$M10_SVC_A_ENVOY_BACKEND_PORT}}" \
    | _jget "['local_service_backend']['id']" 2>/dev/null || true)
BE_B_ENVOY=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-m10-b-envoy\",\"service_id\":\"$SVC_B_ENVOY_ID\",\"address\":\"127.0.0.2\",\"port\":$M10_SVC_B_ENVOY_BACKEND_PORT}}" \
    | _jget "['local_service_backend']['id']" 2>/dev/null || true)

BIND_A_LVS=$(setup_binding "$SVC_A_LVS_ID" "$NET_ID")
BIND_A_ENVOY=$(setup_binding "$SVC_A_ENVOY_ID" "$NET_ID")
BIND_B_ENVOY=$(setup_binding "$SVC_B_ENVOY_ID" "$NET_B_ID")

# Reconciler runs every 10s; PB events kick it within a tick. Allow
# extra slack here because we're spawning keepalived AND two tenant
# envoys back-to-back, plus the one-time host-envoy reload as the
# catalog grows from {} to {3 services}.
sleep 18

# ---- Both netns exist --------------------------------------------------
if sudo ip netns list | awk '{print $1}' | grep -qx "$NS_A_NAME"; then
    pass "netns A ($NS_A_NAME) exists"
else
    fail "netns A missing"
fi
if sudo ip netns list | awk '{print $1}' | grep -qx "$NS_B_NAME"; then
    pass "netns B ($NS_B_NAME) exists"
else
    fail "netns B missing — agent did not provision on first bind"
fi

# ---- Mixed-plugin: keepalived AND tenant envoy both run in netns A
KA_IN_A=0
ENVOY_IN_A=0
ENVOY_IN_B=0
for pid in $(sudo ip netns pids "$NS_A_NAME" 2>/dev/null); do
    comm=$(sudo cat "/proc/${pid}/comm" 2>/dev/null || true)
    case "$comm" in
        keepalived) KA_IN_A=1 ;;
        envoy)      ENVOY_IN_A=1 ;;
    esac
done
for pid in $(sudo ip netns pids "$NS_B_NAME" 2>/dev/null); do
    comm=$(sudo cat "/proc/${pid}/comm" 2>/dev/null || true)
    if [[ "$comm" == "envoy" ]]; then ENVOY_IN_B=1; fi
done
if [[ "$KA_IN_A" -eq 1 ]]; then
    pass "keepalived running in netns A (LVS plugin)"
else
    fail "no keepalived process in netns A"
fi
if [[ "$ENVOY_IN_A" -eq 1 ]]; then
    pass "tenant envoy running in netns A alongside keepalived (mixed-plugin)"
else
    fail "no tenant envoy in netns A"
fi
if [[ "$ENVOY_IN_B" -eq 1 ]]; then
    pass "tenant envoy running in netns B"
else
    fail "no tenant envoy in netns B"
fi

# ---- Shared host envoy: ONE process for both networks ------------------
HOST_PID=""
if [[ -f "${M9_HOST_DIR:-}/envoy.pid" ]]; then
    HOST_PID=$(sudo cat "${M9_HOST_DIR}/envoy.pid" 2>/dev/null || true)
fi
if [[ -n "$HOST_PID" ]] && sudo kill -0 "$HOST_PID" 2>/dev/null; then
    pass "shared host envoy running (pid=$HOST_PID)"
else
    fail "shared host envoy not running"
fi

# Catalog isolation: host /clusters carries BOTH envoy services.
CLUSTERS_JSON=$(sudo curl -sS --max-time 3 \
    --unix-socket "${M9_HOST_ADMIN_SOCK:-}" \
    "http://localhost/clusters?format=json" 2>&1 || true)
if echo "$CLUSTERS_JSON" | grep -q "${SVC_A_ENVOY_ID}-tcp" \
   && echo "$CLUSTERS_JSON" | grep -q "${SVC_B_ENVOY_ID}-tcp"; then
    pass "host envoy /clusters lists both networks' envoy services (catalog union)"
else
    fail "host envoy /clusters missing one of the multitenant envoy clusters"
fi

# ---- Per-netns VIP isolation ------------------------------------------
VETH_A_NS="tls$(echo "$NET_ID" | head -c10)1"
VETH_B_NS="tls$(echo "$NET_B_ID" | head -c10)1"
ADDRS_A=$(sudo ip -n "$NS_A_NAME" addr show "$VETH_A_NS" 2>/dev/null | awk '/inet /{print $2}')
ADDRS_B=$(sudo ip -n "$NS_B_NAME" addr show "$VETH_B_NS" 2>/dev/null | awk '/inet /{print $2}')
if echo "$ADDRS_A" | grep -q "${M10_SVC_A_LVS_VIP}/32" \
   && echo "$ADDRS_A" | grep -q "${M10_SVC_A_ENVOY_VIP}/32" \
   && ! echo "$ADDRS_A" | grep -q "${M10_SVC_B_ENVOY_VIP}/32"; then
    pass "netns A has VIP_A_lvs + VIP_A_envoy, not VIP_B_envoy"
else
    fail "netns A VIP set wrong" \
         "addrs: $ADDRS_A"
fi
if echo "$ADDRS_B" | grep -q "${M10_SVC_B_ENVOY_VIP}/32" \
   && ! echo "$ADDRS_B" | grep -q "${M10_SVC_A_LVS_VIP}/32" \
   && ! echo "$ADDRS_B" | grep -q "${M10_SVC_A_ENVOY_VIP}/32"; then
    pass "netns B has VIP_B_envoy, not network-A VIPs"
else
    fail "netns B VIP set wrong" \
         "addrs: $ADDRS_B"
fi

# ---- Per-tenant data path ---------------------------------------------
m10_test_client_setup "$NET_ID" "$M10_CLIENT_A_NS" \
    "$M10_CLIENT_A_VETH_ROOT" "$M10_CLIENT_A_VETH_NS" \
    "$M10_CLIENT_A_PORT_NAME" "a"
m10_test_client_setup "$NET_B_ID" "$M10_CLIENT_B_NS" \
    "$M10_CLIENT_B_VETH_ROOT" "$M10_CLIENT_B_VETH_NS" \
    "$M10_CLIENT_B_PORT_NAME" "b"

# Client A → VIP A_lvs (LVS data path)
OUT=$(sudo ip netns exec "$M10_CLIENT_A_NS" \
    curl -sS --max-time 5 "http://$M10_SVC_A_LVS_VIP:$M10_SVC_A_LVS_PORT/" 2>&1 || true)
if [[ "$OUT" == *"Directory listing"* ]]; then
    pass "client A → VIP_A_lvs reaches backend (LVS path on netns A)"
else
    fail "client A → VIP_A_lvs failed" "out=$OUT"
fi

# Client A → VIP A_envoy (Envoy data path on same netns)
OUT=$(sudo ip netns exec "$M10_CLIENT_A_NS" \
    curl -sS --max-time 5 "http://$M10_SVC_A_ENVOY_VIP:$M10_SVC_A_ENVOY_PORT/" 2>&1 || true)
if [[ "$OUT" == *"Directory listing"* ]]; then
    pass "client A → VIP_A_envoy reaches backend (envoy path on netns A — mixed-plugin proven)"
else
    fail "client A → VIP_A_envoy failed" "out=$OUT"
fi

# Client B → VIP B_envoy
OUT=$(sudo ip netns exec "$M10_CLIENT_B_NS" \
    curl -sS --max-time 5 "http://$M10_SVC_B_ENVOY_VIP:$M10_SVC_B_ENVOY_PORT/" 2>&1 || true)
if [[ "$OUT" == *"Directory listing"* ]]; then
    pass "client B → VIP_B_envoy reaches backend (envoy path on netns B)"
else
    fail "client B → VIP_B_envoy failed" "out=$OUT"
fi

# ---- Tenant isolation: cross-network traffic must FAIL ----------------
OUT=$(sudo ip netns exec "$M10_CLIENT_A_NS" \
    curl -sS --max-time 4 "http://$M10_SVC_B_ENVOY_VIP:$M10_SVC_B_ENVOY_PORT/" 2>&1 || true)
if [[ "$OUT" == *"Directory listing"* ]]; then
    fail "ISOLATION BREACH: client A reached network B's VIP" "out=$OUT"
else
    pass "isolation: client A cannot reach VIP_B_envoy (cross-tenant blocked)"
fi

OUT=$(sudo ip netns exec "$M10_CLIENT_B_NS" \
    curl -sS --max-time 4 "http://$M10_SVC_A_LVS_VIP:$M10_SVC_A_LVS_PORT/" 2>&1 || true)
if [[ "$OUT" == *"Directory listing"* ]]; then
    fail "ISOLATION BREACH: client B reached network A's LVS VIP" "out=$OUT"
else
    pass "isolation: client B cannot reach VIP_A_lvs (cross-tenant blocked)"
fi

OUT=$(sudo ip netns exec "$M10_CLIENT_B_NS" \
    curl -sS --max-time 4 "http://$M10_SVC_A_ENVOY_VIP:$M10_SVC_A_ENVOY_PORT/" 2>&1 || true)
if [[ "$OUT" == *"Directory listing"* ]]; then
    fail "ISOLATION BREACH: client B reached network A's Envoy VIP" "out=$OUT"
else
    pass "isolation: client B cannot reach VIP_A_envoy (cross-tenant blocked)"
fi

# Network B is left in place across runs (cheap to keep, lets re-runs
# skip the create). Operator can drop it manually with
# `openstack network delete private-m10b` when the lab is wiped.
