#!/usr/bin/env bash
# tags: plugin
#
# proxy plugin: Rust L4 proxy plugin. Both daemons (priv + worker)
# alive; service+backend creation triggers catalog reload; HMAC-signed
# catalog file present and well-formed; end-to-end TCP and UDP through
# proxy VIPs; HMAC tamper kept last-good state; tenant netns has no
# underlay path.

CASE_ID="10-proxy-plugin"
CASE_TITLE="proxy plugin: Rust L4 proxy"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

NS_NAME="localsvc-$NET_ID"
SVC_TCP_ID=""
BIND_TCP_ID=""
BE_TCP_ID=""
SVC_UDP_ID=""
BIND_UDP_ID=""
BE_UDP_ID=""

case_teardown() {
    probe_client_teardown
    sudo pkill -f "m11-tcp-backend.py" 2>/dev/null || true
    sudo pkill -f "socat.*UDP4-RECVFROM:${PROXY_UDP_BACKEND_PORT}" 2>/dev/null || true
    teardown_binding "$BIND_TCP_ID"
    teardown_binding "$BIND_UDP_ID"
    [[ -n "$BE_TCP_ID" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_TCP_ID" >/dev/null 2>&1 || true
    [[ -n "$BE_UDP_ID" ]] && _curl DELETE "/v2.0/local_service_backends/$BE_UDP_ID" >/dev/null 2>&1 || true
    teardown_service "$SVC_TCP_ID"
    teardown_service "$SVC_UDP_ID"
    sudo rm -f "${PROXY_CATALOG}.bak" 2>/dev/null || true
}

# 1) Both proxy processes alive on chassis. systemd manages both.
if systemctl is-active --quiet nls-proxy-priv.service; then
    pass "nls-proxy-priv.service is active"
else
    fail "nls-proxy-priv.service not active" \
         "systemctl status nls-proxy-priv.service"
    exit 0
fi
if systemctl is-active --quiet nls-proxy.service; then
    pass "nls-proxy.service is active"
else
    fail "nls-proxy.service not active" \
         "systemctl status nls-proxy.service"
    exit 0
fi

# Create the proxy-plugin TCP service first (no plugin runs until
# there's a binding). Bind so the agent provisions the netns. The
# proxy plugin runs but emits no catalog entries yet (no backends).
SVC_TCP_RESP=$(_curl POST "/v2.0/local_services" \
    "{\"local_service\": {\"name\":\"$PROXY_TCP_NAME\",\"local_ipv4\":\"$PROXY_TCP_VIP\",\"port\":$PROXY_TCP_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}")
SVC_TCP_ID=$(echo "$SVC_TCP_RESP" | _jget "['local_service']['id']" 2>/dev/null || true)
if [[ -z "$SVC_TCP_ID" ]]; then
    fail "could not create proxy-plugin TCP service" "$SVC_TCP_RESP"
    exit 0
fi
BIND_TCP_ID=$(setup_binding "$SVC_TCP_ID" "$NET_ID")

# Wait for the netns to materialize — Port_Binding event drives the
# agent's provision() within a couple of seconds.
i=0
while ! sudo ip netns list | awk '{print $1}' | grep -qx "$NS_NAME"; do
    sleep 1
    i=$((i+1))
    if [[ $i -ge 15 ]]; then
        fail "localsvc netns did not appear within 15s"
        exit 0
    fi
done

# 2) TCP backend on host root netns. The proxy worker dials backends
#    from the host root netns, so the backend must be reachable from
#    there. 127.0.0.1 keeps the test self-contained on the chassis
#    without needing route changes.
BACKEND_ADDR=127.0.0.1
sudo pkill -f "socat.*TCP4-LISTEN:${PROXY_TCP_BACKEND_PORT}" 2>/dev/null || true
sudo pkill -f "m11-tcp-backend.py" 2>/dev/null || true
# Wait for the kernel to release the port (TIME_WAIT can hold it
# briefly even with reuseaddr if a previous run accepted a connection).
i=0
while sudo ss -tlnp | grep -q ":${PROXY_TCP_BACKEND_PORT} "; do
    sleep 1
    i=$((i+1))
    [[ $i -ge 5 ]] && break
done
# Tiny HTTP responder. socat's SYSTEM addresses use commas as option
# separators which mangles HTTP headers — so the responder lives in its
# own fixture file.
sudo install -m0755 "${LAB_TESTS_DIR}/lib/fixtures/m11-tcp-backend.py" /tmp/m11-tcp-backend.py
sudo /tmp/m11-tcp-backend.py "$PROXY_TCP_BACKEND_PORT" \
     >/tmp/m11-tcp-backend.${PROXY_TCP_BACKEND_PORT}.log 2>&1 &
echo "$!" | sudo tee /tmp/m11-backend.${PROXY_TCP_BACKEND_PORT}.pid >/dev/null
sleep 1

# Add the backend. This triggers another reconcile → proxy.py re-emits
# the catalog with a non-empty entries list → worker accepts and
# BindListener runs.
BE_TCP_RESP=$(_curl POST "/v2.0/local_service_backends" \
    "{\"local_service_backend\": {\"name\":\"be-m11-tcp\",\"service_id\":\"$SVC_TCP_ID\",\"address\":\"${BACKEND_ADDR}\",\"port\":$PROXY_TCP_BACKEND_PORT}}")
BE_TCP_ID=$(echo "$BE_TCP_RESP" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
sleep 12

# 3) Catalog file exists and is HMAC-signed (first line is 64 hex).
if sudo test -r "$PROXY_CATALOG"; then
    FIRST_LINE=$(sudo head -n1 "$PROXY_CATALOG")
    if [[ "$FIRST_LINE" =~ ^[0-9a-f]{64}$ ]]; then
        pass "catalog signed (HMAC line is 64-hex)"
    else
        fail "catalog HMAC line not 64-hex" \
             "head -1 $PROXY_CATALOG: $FIRST_LINE"
    fi
else
    fail "catalog file not present at $PROXY_CATALOG"
fi

# 4) End-to-end TCP through the proxy VIP from a tenant-attached client.
probe_client_setup
CURL_OUT=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
    curl -sS --max-time 5 "http://$PROXY_TCP_VIP:$PROXY_TCP_PORT/" 2>&1 || true)
if [[ "$CURL_OUT" == *"Directory listing"* ]]; then
    pass "TCP curl via proxy VIP $PROXY_TCP_VIP:$PROXY_TCP_PORT reaches backend"
else
    fail "TCP curl via proxy VIP failed" \
         "curl_out=${CURL_OUT:0:200}"
fi

# 5) UDP forward path. socat upper-case echo backend on host loopback;
#    send a known marker via netcat from the client netns and assert
#    the backend logged the upper-cased version.
sudo pkill -f "socat.*UDP4-RECVFROM:${PROXY_UDP_BACKEND_PORT}" 2>/dev/null || true
sudo socat -u UDP4-RECVFROM:${PROXY_UDP_BACKEND_PORT},bind=127.0.0.1,reuseaddr,fork \
     SYSTEM:'tr a-z A-Z >&2' \
     >/tmp/proxy-udp-backend.${PROXY_UDP_BACKEND_PORT}.log 2>&1 &
echo "$!" | sudo tee /tmp/m11-backend.${PROXY_UDP_BACKEND_PORT}.pid >/dev/null
sleep 1
SVC_UDP_RESP=$(_curl POST "/v2.0/local_services" \
    "{\"local_service\": {\"name\":\"$PROXY_UDP_NAME\",\"local_ipv4\":\"$PROXY_UDP_VIP\",\"port\":$PROXY_UDP_PORT,\"protocol\":\"udp\",\"exposure_plugin\":\"proxy\"}}")
SVC_UDP_ID=$(echo "$SVC_UDP_RESP" | _jget "['local_service']['id']" 2>/dev/null || true)
if [[ -n "$SVC_UDP_ID" ]]; then
    BIND_UDP_ID=$(setup_binding "$SVC_UDP_ID" "$NET_ID")
    BE_UDP_RESP=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-m11-udp\",\"service_id\":\"$SVC_UDP_ID\",\"address\":\"127.0.0.1\",\"port\":$PROXY_UDP_BACKEND_PORT}}")
    BE_UDP_ID=$(echo "$BE_UDP_RESP" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    sleep 12
    sudo ip netns exec "$PROBE_CLIENT_NS" \
        bash -c "echo hello-m11 | nc -u -w1 $PROXY_UDP_VIP $PROXY_UDP_PORT" || true
    sleep 2
    # The socat backend uppercases its input — sending "hello-m11"
    # produces "HELLO-M11" in the log.
    if sudo grep -q "HELLO-M11" "/tmp/proxy-udp-backend.${PROXY_UDP_BACKEND_PORT}.log" 2>/dev/null; then
        pass "UDP datagram via proxy VIP $PROXY_UDP_VIP:$PROXY_UDP_PORT reached backend"
    else
        fail "UDP datagram via proxy VIP did not reach backend" \
             "log: $(sudo cat /tmp/proxy-udp-backend.${PROXY_UDP_BACKEND_PORT}.log 2>/dev/null | head -3)"
    fi
    sudo pkill -f "socat.*UDP4-RECVFROM:${PROXY_UDP_BACKEND_PORT}" 2>/dev/null || true
else
    fail "could not create proxy-plugin UDP service" "$SVC_UDP_RESP"
fi

# 6) HMAC tamper resistance: flip a byte in the payload and confirm the
#    worker keeps last-good state (TCP curl still succeeds).
sudo cp "$PROXY_CATALOG" "$PROXY_CATALOG.bak"
sudo python3 -c "
import sys
p = sys.argv[1]
with open(p, 'rb') as fh:
    data = bytearray(fh.read())
nl = data.index(b'\n')
data[nl + 5] ^= 0x20
with open(p, 'wb') as fh:
    fh.write(bytes(data))
" "$PROXY_CATALOG"
sleep 4
CURL_OUT=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
    curl -sS --max-time 5 "http://$PROXY_TCP_VIP:$PROXY_TCP_PORT/" 2>&1 || true)
if [[ "$CURL_OUT" == *"Directory listing"* ]]; then
    pass "HMAC tamper kept last-good state (curl still reaches backend)"
else
    fail "HMAC tamper unexpectedly tore down listeners" \
         "curl_out=${CURL_OUT:0:200}"
fi
sudo mv "$PROXY_CATALOG.bak" "$PROXY_CATALOG"
sleep 2

# 7) Cross-tenant isolation: from inside the tenant netns, attempts to
#    reach the chassis underlay must fail. (The proxy worker bridges
#    from tenant netns sockets to host netns dial; tenant has no IP
#    path to underlay.)
UNDERLAY_PROBE=$(sudo ip netns exec "$NS_NAME" \
    timeout 2 ping -c1 -W1 172.18.0.128 2>&1 || true)
if echo "$UNDERLAY_PROBE" | grep -qE "Network is unreachable|100% packet loss"; then
    pass "tenant netns has no IP path to chassis underlay (172.18.0.128)"
else
    fail "tenant netns reached chassis IP — isolation breach" \
         "$UNDERLAY_PROBE"
fi
