#!/usr/bin/env bash
# tags: underlay
#
# Underlay-backend reachability + tenant-escape ACL. Two services
# pointing at REAL underlay services (not synthesized in netns / on
# host loopback):
#   * TCP HTTP — exposed via proxy plugin, backend ${UNDERLAY_TCP_BACKEND_ADDR}:80
#   * UDP DNS  — exposed via nat plugin,   backend ${UNDERLAY_UDP_BACKEND_ADDR}:53
#
# Both should reach underlay backends:
#   - proxy: worker lives in host root netns; routing inherited.
#   - nat:   per-network nls veth + per-backend FORWARD ACL.
# Plus negative checks: a tenant must NOT be able to reach arbitrary
# underlay destinations, only the configured backends.

CASE_ID="09-underlay-egress"
CASE_TITLE="Underlay-backend reachability + tenant-escape ACL (nat + proxy)"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

# Probes the underlay backends; skips the case if they aren't actually
# reachable from this chassis (test would be meaningless).
requires_underlay_backends

NS_NAME="localsvc-$NET_ID"
STUB_SVC=""
STUB_BIND=""
SVC_TCP=""
BIND_TCP=""
BE_TCP=""
SVC_UDP=""
BIND_UDP=""
BE_UDP=""

case_teardown() {
    probe_client_teardown
    teardown_binding "$BIND_TCP"
    teardown_binding "$BIND_UDP"
    teardown_binding "$STUB_BIND"
    [[ -n "$BE_TCP" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_TCP" >/dev/null 2>&1 || true
    [[ -n "$BE_UDP" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_UDP" >/dev/null 2>&1 || true
    teardown_service "$SVC_TCP"
    teardown_service "$SVC_UDP"
    teardown_service "$STUB_SVC"
}

# Always start clean — see underlay_clean_leftovers comment.
underlay_clean_leftovers

# Underlay sanity checks already happened in requires_underlay_backends;
# emit the corresponding pass lines so the report is self-contained.
pass "underlay TCP backend ${UNDERLAY_TCP_BACKEND_ADDR}:${UNDERLAY_TCP_BACKEND_PORT} reachable from chassis"
pass "underlay UDP DNS ${UNDERLAY_UDP_BACKEND_ADDR}:${UNDERLAY_UDP_BACKEND_PORT} reachable from chassis"

# Make sure the netns + tenant client exist for the curl/dig from
# tenant-side. Reuse the localport-lifecycle stub to drive netns
# provision.
STUB_SVC=$(setup_service)
STUB_BIND=$(setup_binding "$STUB_SVC" "$NET_ID")
sleep 4
if ! sudo ip netns list | awk '{print $1}' | grep -qx "$NS_NAME"; then
    fail "localsvc netns missing — cannot run underlay test"
    exit 0
fi
probe_client_setup

# ---- TCP via proxy plugin (expected to succeed) ------------------------
SVC_TCP=$(_curl GET "/v2.0/local_services" \
    | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$UNDERLAY_TCP_NAME':
        print(s['id']); break")
if [[ -z "$SVC_TCP" ]]; then
    SVC_TCP=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$UNDERLAY_TCP_NAME\",\"local_ipv4\":\"$UNDERLAY_TCP_VIP\",\"port\":$UNDERLAY_TCP_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}" \
        | _jget "['local_service']['id']" 2>/dev/null || true)
fi
BE_TCP=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-underlay-tcp\",\"service_id\":\"$SVC_TCP\",\"address\":\"$UNDERLAY_TCP_BACKEND_ADDR\",\"port\":$UNDERLAY_TCP_BACKEND_PORT}}" \
    | _jget "['local_service_backend']['id']" 2>/dev/null || true)
BIND_TCP=$(setup_binding "$SVC_TCP" "$NET_ID")

# ---- UDP via nat plugin (now SHOULD succeed via underlay-egress) -------
SVC_UDP=$(_curl GET "/v2.0/local_services" \
    | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$UNDERLAY_UDP_NAME':
        print(s['id']); break")
if [[ -z "$SVC_UDP" ]]; then
    SVC_UDP=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$UNDERLAY_UDP_NAME\",\"local_ipv4\":\"$UNDERLAY_UDP_VIP\",\"port\":$UNDERLAY_UDP_PORT,\"protocol\":\"udp\",\"health_check_type\":\"dns\",\"exposure_plugin\":\"nat\"}}" \
        | _jget "['local_service']['id']" 2>/dev/null || true)
fi
BE_UDP=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-underlay-udp\",\"service_id\":\"$SVC_UDP\",\"address\":\"$UNDERLAY_UDP_BACKEND_ADDR\",\"port\":$UNDERLAY_UDP_BACKEND_PORT}}" \
    | _jget "['local_service_backend']['id']" 2>/dev/null || true)
BIND_UDP=$(setup_binding "$SVC_UDP" "$NET_ID")

# Wait for both to converge: proxy catalog reload + keepalived spawn +
# first HC pass + underlay veth provision + ACL refresh.
sleep 18

# ---- proxy plugin TCP assertions (worker in host root netns) ----------
# Proxy worker is host-side, so /clusters reflects the configured
# underlay backend; data path returns real HTML. The admin endpoint
# requires a bearer token written by the agent at provisioning time.
#
# We don't sudo here: the runner already executes as stack, which owns
# the token file (mode 0400) and matches the worker's effective uid
# (the admin socket peer-uid gate rejects connections from anyone
# else, including root — see proxy/worker/src/admin.rs:97).
PROXY_ADMIN_SOCK="/var/run/neutron-local-services/_proxy/admin.sock"
PROXY_ADMIN_TOKEN=$(cat /var/lib/neutron-local-services/_proxy/admin.token 2>/dev/null | tr -d '\n')
CLUSTERS_JSON=$(curl -sS --max-time 3 \
    --unix-socket "$PROXY_ADMIN_SOCK" \
    -H "Authorization: Bearer ${PROXY_ADMIN_TOKEN}" \
    "http://localhost/clusters?format=json" 2>&1 || true)
if echo "$CLUSTERS_JSON" | grep -q "$UNDERLAY_TCP_BACKEND_ADDR"; then
    pass "proxy /clusters lists underlay backend ${UNDERLAY_TCP_BACKEND_ADDR}"
else
    fail "proxy /clusters missing the underlay TCP cluster" \
         "got: $(echo "$CLUSTERS_JSON" | head -c 400)"
fi
OUT_TCP=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
    curl -sS --max-time 6 "http://${UNDERLAY_TCP_VIP}:${UNDERLAY_TCP_PORT}/" 2>&1 || true)
if [[ -n "$OUT_TCP" && "$OUT_TCP" == *"<html"* ]]; then
    pass "tenant client → VIP_tcp → reaches REAL underlay HTTP backend via proxy (${UNDERLAY_TCP_BACKEND_ADDR})"
else
    fail "tenant → VIP_tcp did not reach underlay backend via proxy" \
         "got: $(echo "$OUT_TCP" | head -c 200)"
fi

# ---- nat plugin UDP assertions (per-network nls veth + ACL) -----------
# The netns must have a default route via 100.64.x.1 (the host-side
# underlay-egress IP); ipvsadm must list the backend as healthy (HC
# reaches it through the new path); tenant dig succeeds.
DEFAULT_VIA=$(sudo ip netns exec "$NS_NAME" ip route | awk '/^default/{print $3}')
if [[ "$DEFAULT_VIA" == 100.64.* ]]; then
    pass "netns has default route via underlay-egress host (${DEFAULT_VIA})"
else
    fail "netns default route missing or wrong" \
         "ip route: $(sudo ip netns exec "$NS_NAME" ip route | tr '\n' '|')"
fi
# Underlay veth pair present.
NLS_ROOT="nls${NET_ID:0:10}0"
if ip link show "$NLS_ROOT" >/dev/null 2>&1; then
    pass "underlay-egress veth ${NLS_ROOT} present in host root netns"
else
    fail "underlay-egress veth ${NLS_ROOT} missing"
fi
# Per-network FORWARD ACL chain present and contains the UDP rule.
CHAIN="NLS_UND_${NET_ID:0:10}"
if sudo iptables -t filter -S "$CHAIN" 2>/dev/null \
        | grep -q -- "-d ${UNDERLAY_UDP_BACKEND_ADDR}.*--dport ${UNDERLAY_UDP_BACKEND_PORT}"; then
    pass "host FORWARD ACL chain ${CHAIN} whitelists ${UNDERLAY_UDP_BACKEND_ADDR}:${UNDERLAY_UDP_BACKEND_PORT}"
else
    fail "FORWARD ACL chain ${CHAIN} missing UDP backend rule" \
         "$(sudo iptables -t filter -S "$CHAIN" 2>/dev/null | head -10)"
fi
# ipvsadm must show the backend as healthy now that HC can reach it.
IPVS_UDP=$(sudo ip netns exec "$NS_NAME" ipvsadm -L -n 2>/dev/null)
# ipvsadm column-aligns the protocol field, so "UDP" is followed by 2+
# spaces (TCP fits in 3 chars, UDP in 3, but the field is padded to
# align with longer protocol labels). Match >=1 space so the VIP test
# isn't fooled by formatting.
if echo "$IPVS_UDP" | awk -v vip="${UNDERLAY_UDP_VIP}:${UNDERLAY_UDP_PORT}" '
        $0 ~ "^UDP +" vip {found=1; next}
        /^TCP|^UDP/ {found=0}
        found && /->/ {print}
    ' | grep -q "$UNDERLAY_UDP_BACKEND_ADDR"; then
    pass "ipvsadm shows the underlay UDP backend ${UNDERLAY_UDP_BACKEND_ADDR} (HC reaches it via nls veth)"
else
    fail "ipvsadm does not list the underlay UDP backend (HC still failing — underlay egress broken)" \
         "ipvsadm: $(echo "$IPVS_UDP" | head -20)"
fi
# Tenant dig must now resolve through the VIP.
DIG_OUT=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
    dig +time=3 +tries=2 "@${UNDERLAY_UDP_VIP}" -p "${UNDERLAY_UDP_PORT}" example.com a +short 2>&1 | head -1 || true)
if [[ "$DIG_OUT" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    pass "tenant → VIP_udp resolves through nat plugin to underlay DNS (${DIG_OUT})"
else
    fail "tenant → VIP_udp did not resolve" \
         "got: '${DIG_OUT:-empty}'"
fi

# ---- Tenant-escape negative checks ------------------------------------
# 1. From the tenant client, target a non-backend underlay IP via HTTP
#    and DNS — both must FAIL (no whitelist entry, no DNAT).
ESCAPE_HTTP=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
    curl -sS --max-time 4 -o /dev/null -w "%{http_code}" \
    "http://172.18.0.1/" 2>&1 || true)
# Drops can surface as several distinct curl error shapes: "timed out",
# "Connection refused" (RST), "Could not connect to server" (silent
# drop, no RST), "No route to host" (route missing), "Network is
# unreachable". Anything other than a real HTTP code (1xx-5xx) means
# the connection didn't complete. The final HTTP code line is "000" on
# failure; check for that as the canonical signal alongside the
# human-readable error strings.
if [[ -z "$ESCAPE_HTTP" \
        || "$ESCAPE_HTTP" == *"000"* \
        || "$ESCAPE_HTTP" == *"timed out"* \
        || "$ESCAPE_HTTP" == *"refused"* \
        || "$ESCAPE_HTTP" == *"Could not connect"* \
        || "$ESCAPE_HTTP" == *"No route to host"* \
        || "$ESCAPE_HTTP" == *"unreachable"* ]]; then
    pass "tenant escape attempt to chassis IP 172.18.0.1 blocked (got: '${ESCAPE_HTTP:-empty}')"
else
    fail "tenant UNEXPECTEDLY reached non-backend underlay IP 172.18.0.1 (got HTTP $ESCAPE_HTTP) — ACL leak"
fi
# 2. dig at a non-whitelisted underlay DNS server (eg an IP not in any
#    backend list) — must time out.
ESCAPE_DNS=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
    dig +time=2 +tries=1 "@8.8.8.8" -p 53 example.com +short 2>&1 | head -1 || true)
if [[ -z "$ESCAPE_DNS" || "$ESCAPE_DNS" == *"timed out"* \
        || "$ESCAPE_DNS" == *"no servers"* \
        || "$ESCAPE_DNS" == *"network unreachable"* ]]; then
    pass "tenant escape attempt to public DNS 8.8.8.8 blocked"
else
    fail "tenant UNEXPECTEDLY resolved via 8.8.8.8 (got: '$ESCAPE_DNS') — ACL leak"
fi

# 3. Sanity: the agent process is still alive.
if sudo systemctl is-active "$AGENT_UNIT" >/dev/null 2>&1; then
    pass "ovn-agent still active through underlay-egress workflow"
else
    fail "ovn-agent dropped to inactive"
fi
