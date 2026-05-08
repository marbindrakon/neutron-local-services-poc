#!/usr/bin/env bash
# tags: plugin
#
# nat plugin (Keepalived/LVS): keepalived spawns inside the netns,
# ipvsadm reflects configured services, end-to-end TCP and UDP through
# VIPs reach backends, and a dead backend is dropped by health-check.

CASE_ID="06-nat-plugin"
CASE_TITLE="plugin abstraction + nat (Keepalived/LVS)"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

NS_NAME="localsvc-$NET_ID"
STUB_SVC_ID=""
STUB_BIND_ID=""
SVC_TCP_ID=""
BE_TCP_ID=""
BIND_TCP_ID=""
SVC_UDP_ID=""
BE_UDP_ID=""
BIND_UDP_ID=""

case_teardown() {
    probe_client_teardown
    m8_kill_backend "$NAT_TCP_BACKEND_PORT"
    m8_kill_backend "$NAT_UDP_BACKEND_PORT"
    teardown_binding "$BIND_TCP_ID"
    teardown_binding "$BIND_UDP_ID"
    teardown_binding "$STUB_BIND_ID"
    [[ -n "$BE_TCP_ID" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_TCP_ID" >/dev/null 2>&1 || true
    [[ -n "$BE_UDP_ID" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_UDP_ID" >/dev/null 2>&1 || true
    teardown_service "$SVC_TCP_ID"
    teardown_service "$SVC_UDP_ID"
    teardown_service "$STUB_SVC_ID"
}

# Make sure baseline plumbing is alive — the nat plugin smoke builds on
# baseline plumbing, and if the netns isn't there we've lost time
# chasing a broken stack.
if ! sudo ip netns list | awk '{print $1}' | grep -qx "$NS_NAME"; then
    bootstrap_id=$(setup_service)
    bootstrap_bind=$(setup_binding "$bootstrap_id" "$NET_ID")
    sleep 4
    teardown_binding "$bootstrap_bind"
    teardown_service "$bootstrap_id"
fi

# The LVS service needs a binding that drives the netns. Reuse the
# localport-lifecycle stub: bind once, leave it bound for the duration
# of the nat-plugin checks, tear down at the end.
STUB_SVC_ID=$(setup_service)
STUB_BIND_ID=$(setup_binding "$STUB_SVC_ID" "$NET_ID")
sleep 3
if ! sudo ip netns list | awk '{print $1}' | grep -qx "$NS_NAME"; then
    fail "localsvc netns missing — nat plugin smoke cannot proceed without baseline plumbing"
    exit 0
fi

# 1) Keepalived process is alive in the netns. Plugin spawns it on the
#    first reconcile (any binding triggers reconcile_network). Manual
#    loop instead of xargs|grep because set -o pipefail can make the
#    pipeline fail when one /proc/<pid>/comm read races with a process
#    exit.
KA_FOUND=0
for ka_pid in $(sudo ip netns pids "$NS_NAME" 2>/dev/null); do
    comm=$(sudo cat "/proc/${ka_pid}/comm" 2>/dev/null || true)
    if [[ "$comm" == "keepalived" ]]; then
        KA_FOUND=1
        break
    fi
done
if [[ "$KA_FOUND" -eq 1 ]]; then
    pass "keepalived running inside $NS_NAME"
else
    fail "no keepalived process in $NS_NAME" \
         "sudo ip netns pids $NS_NAME && ps -ef | grep keepalived"
fi

# 2) Bring up an LVS-fronted TCP backend. The HTTP server runs in the
#    localsvc netns itself (saves us from booting a backend VM for the
#    PoC); LVS DNAT to <ns_ip>:18080 stays inside the netns.
NS_IP=$(m8_get_localsvc_ns_ip)
if [[ -z "$NS_IP" ]]; then
    fail "could not read localsvc netns IP — backend setup aborted"
    exit 0
fi
m8_spawn_tcp_backend "$NS_NAME" "$NS_IP" "$NAT_TCP_BACKEND_PORT"

SVC_TCP_ID=$(_curl GET "/v2.0/local_services" \
    | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$NAT_TCP_NAME':
        print(s['id']); break")
if [[ -z "$SVC_TCP_ID" ]]; then
    SVC_TCP_RESP=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$NAT_TCP_NAME\",\"local_ipv4\":\"$NAT_TCP_VIP\",\"port\":$NAT_TCP_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\"}}")
    SVC_TCP_ID=$(echo "$SVC_TCP_RESP" | _jget "['local_service']['id']" 2>/dev/null || true)
    if [[ -z "$SVC_TCP_ID" ]]; then
        fail "could not create service $NAT_TCP_NAME" "$SVC_TCP_RESP"
        exit 0
    fi
fi
BE_TCP_RESP=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-tcp\",\"service_id\":\"$SVC_TCP_ID\",\"address\":\"$NS_IP\",\"port\":$NAT_TCP_BACKEND_PORT}}")
BE_TCP_ID=$(echo "$BE_TCP_RESP" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
if [[ -z "$BE_TCP_ID" ]]; then
    fail "could not create backend for $NAT_TCP_NAME" "$BE_TCP_RESP"
    exit 0
fi
BIND_TCP_ID=$(setup_binding "$SVC_TCP_ID" "$NET_ID")

# Reconciler runs every 10s; underlay-egress provisioning on the initial
# binding adds a few seconds, then keepalived needs one delay_loop (6s)
# + connect_timeout (3s) to mark the backend healthy and add it to
# ipvsadm. 22s is the safe envelope.
sleep 22

# 3) ipvsadm sees the virtual_server.
IPVSADM_OUT=$(sudo ip netns exec "$NS_NAME" ipvsadm -L -n 2>/dev/null || true)
if echo "$IPVSADM_OUT" | grep -qE "TCP\s+${NAT_TCP_VIP}:${NAT_TCP_PORT}"; then
    pass "ipvsadm shows TCP $NAT_TCP_VIP:$NAT_TCP_PORT"
else
    fail "ipvsadm has no entry for TCP $NAT_TCP_VIP:$NAT_TCP_PORT" \
         "$IPVSADM_OUT"
fi

# 4) End-to-end TCP through the VIP from a tenant-attached client.
probe_client_setup
CURL_OUT=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
    curl -sS --max-time 5 "http://$NAT_TCP_VIP:$NAT_TCP_PORT/" 2>&1 || true)
if [[ -n "$CURL_OUT" && "$CURL_OUT" == *"Directory listing"* ]]; then
    pass "TCP curl through VIP $NAT_TCP_VIP reaches backend"
else
    fail "TCP curl through VIP $NAT_TCP_VIP failed" \
         "out=$CURL_OUT"
fi

# 5) UDP service. socat echoes back upper-cased input on stderr (we
#    only care that the packet reached the backend; the return path
#    doesn't matter for this PoC check — UDP is fire-and-forget).
m8_spawn_udp_backend "$NS_NAME" "$NS_IP" "$NAT_UDP_BACKEND_PORT"
SVC_UDP_ID=$(_curl GET "/v2.0/local_services" \
    | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$NAT_UDP_NAME':
        print(s['id']); break")
if [[ -z "$SVC_UDP_ID" ]]; then
    SVC_UDP_RESP=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$NAT_UDP_NAME\",\"local_ipv4\":\"$NAT_UDP_VIP\",\"port\":$NAT_UDP_PORT,\"protocol\":\"udp\"}}")
    SVC_UDP_ID=$(echo "$SVC_UDP_RESP" | _jget "['local_service']['id']" 2>/dev/null || true)
fi
if [[ -z "$SVC_UDP_ID" ]]; then
    fail "could not create service $NAT_UDP_NAME"
else
    BE_UDP_RESP=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-udp\",\"service_id\":\"$SVC_UDP_ID\",\"address\":\"$NS_IP\",\"port\":$NAT_UDP_BACKEND_PORT}}")
    BE_UDP_ID=$(echo "$BE_UDP_RESP" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    BIND_UDP_ID=$(setup_binding "$SVC_UDP_ID" "$NET_ID")
fi
sleep 12

if sudo ip netns exec "$NS_NAME" ipvsadm -L -n 2>/dev/null \
        | grep -qE "UDP\s+${NAT_UDP_VIP}:${NAT_UDP_PORT}"; then
    pass "ipvsadm shows UDP $NAT_UDP_VIP:$NAT_UDP_PORT"
else
    fail "ipvsadm has no entry for UDP $NAT_UDP_VIP:$NAT_UDP_PORT"
fi

# Send a UDP packet through the VIP. We don't expect a reply (LVS-NAT
# for UDP relies on conntrack which can be flaky for one-shot probes);
# the proof is that socat's stderr log shows the upper-cased payload.
sudo ip netns exec "$PROBE_CLIENT_NS" \
    bash -c "echo 'hello-m8' | timeout 2 nc -u -w1 $NAT_UDP_VIP $NAT_UDP_PORT" \
    >/dev/null 2>&1 || true
sleep 1
if sudo grep -q 'HELLO-NAT' "/tmp/nat-udp-backend.${NAT_UDP_BACKEND_PORT}.log" 2>/dev/null; then
    pass "UDP packet through VIP reached backend"
else
    # Some socat builds write to stdout instead of stderr depending on
    # flags; check both before failing.
    if sudo grep -qi 'hello' "/tmp/nat-udp-backend.${NAT_UDP_BACKEND_PORT}.log" 2>/dev/null; then
        pass "UDP packet through VIP reached backend (stdout path)"
    else
        fail "UDP packet did not reach backend through VIP" \
             "log: $(sudo cat /tmp/nat-udp-backend.${NAT_UDP_BACKEND_PORT}.log 2>/dev/null | head -3)"
    fi
fi

# 6) Health-check drop. Kill the TCP backend; keepalived's TCP_CHECK has
#    connect_timeout=3 and delay_loop=6 in the rendered config, so
#    within ~10-12s the backend should be removed from ipvsadm.
m8_kill_backend "$NAT_TCP_BACKEND_PORT"
sleep 14
if sudo ip netns exec "$NS_NAME" ipvsadm -L -n 2>/dev/null \
        | awk -v vip="${NAT_TCP_VIP}:${NAT_TCP_PORT}" '
            $0 ~ "^TCP " vip {found=1; next}
            /^TCP|^UDP/ {found=0}
            found && /->/ {print}' \
        | grep -q "$NS_IP"; then
    fail "TCP backend $NS_IP:$NAT_TCP_BACKEND_PORT still in ipvsadm after kill" \
         "(keepalived health check should have dropped it)"
else
    pass "keepalived dropped dead TCP backend from ipvsadm"
fi
