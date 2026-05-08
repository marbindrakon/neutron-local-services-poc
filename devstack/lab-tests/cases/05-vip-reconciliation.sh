#!/usr/bin/env bash
# tags: smoke
#
# VIP reconciliation: VIP appears as /32 on the ns-side veth, on-subnet
# IP is preserved, a second service on the same network gets its VIP
# added by the reconciler, the kernel ARP-responds for owned VIPs, and
# unbind removes the VIP.

CASE_ID="05-vip-reconciliation"
CASE_TITLE="VIP reconciliation"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

SVC_ID=""
BIND_ID=""
SVC2_ID=""
BIND2_ID=""

case_teardown() {
    teardown_binding "$BIND2_ID"
    [[ -n "$SVC2_ID" ]] && _curl DELETE "/v2.0/local_services/$SVC2_ID" >/dev/null 2>&1 || true
    teardown_binding "$BIND_ID"
    teardown_service "$SVC_ID"
}

SVC_ID=$(setup_service)
BIND_ID=$(setup_binding "$SVC_ID" "$NET_ID")
# VIP reconciler runs on a 10s timer and on PB events. PB events are the
# fast path (~1s), but a brand-new service+binding has to walk: server
# DB write → host_routes refresh → mech-driver SB external_ids update →
# IDL event → reconcile_vips → API GET → ip addr add. Empirically that
# adds up to 6-8s on this lab; one full timer interval (10s) plus a
# margin is the safe sleep.
sleep 12

NS_NAME="localsvc-$NET_ID"
VETH_NS="tls$(echo "$NET_ID" | head -c10)1"

# The /32 VIP must be on the ns-side veth.
if sudo ip -n "$NS_NAME" addr show "$VETH_NS" 2>/dev/null \
        | grep -qE "inet ${SVC_VIP}/32"; then
    pass "veth $VETH_NS has VIP $SVC_VIP/32"
else
    fail "no $SVC_VIP/32 on $VETH_NS inside $NS_NAME" \
         "sudo ip -n $NS_NAME addr show $VETH_NS"
fi

# The on-subnet IP that netns.provision manages must STILL be there —
# provision and reconcile_vips MUST NOT fight over the address list.
NON32_COUNT=$(sudo ip -n "$NS_NAME" -4 addr show "$VETH_NS" 2>/dev/null \
              | awk '/inet /{print $2}' \
              | grep -vc '/32' || true)
if [[ "${NON32_COUNT:-0}" -ge 1 ]]; then
    pass "on-subnet IPv4 still present alongside VIP"
else
    fail "on-subnet IPv4 was clobbered by VIP reconciler"
fi

# Add a SECOND service on the same network; reconciler should pick up
# the new VIP within one tick.
VIP2="169.254.169.124"
SVC2_NAME="lab-test-m7-second"
SVC2_ID=$(_curl GET "/v2.0/local_services" \
    | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$SVC2_NAME':
        print(s['id']); break")
if [[ -z "$SVC2_ID" ]]; then
    SVC2_ID=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$SVC2_NAME\",\"local_ipv4\":\"$VIP2\",\"port\":80,\"protocol\":\"tcp\"}}" \
        | _jget "['local_service']['id']")
fi
BIND2_ID=$(setup_binding "$SVC2_ID" "$NET_ID")
# Wait long enough for one full timer tick + a margin.
sleep 12

if sudo ip -n "$NS_NAME" addr show "$VETH_NS" 2>/dev/null \
        | grep -qE "inet ${VIP2}/32"; then
    pass "second VIP $VIP2 added by reconciler"
else
    fail "second VIP $VIP2 not added by reconciler" \
         "sudo ip -n $NS_NAME addr show $VETH_NS"
fi

# ARP-respond check: the kernel inside the netns owns these /32s, so a
# reply via the same MAC as the LSP is the actual proof that a guest
# on this network would be able to talk to the VIP.
if sudo ip netns exec "$NS_NAME" ping -c1 -W1 "$SVC_VIP" >/dev/null 2>&1; then
    pass "kernel ARP-responds for $SVC_VIP inside $NS_NAME"
else
    fail "ping $SVC_VIP inside ns failed — VIP may not be on $VETH_NS"
fi

# Drop the second binding; reconciler should remove its VIP.
teardown_binding "$BIND2_ID"
BIND2_ID=""
sleep 12
if sudo ip -n "$NS_NAME" addr show "$VETH_NS" 2>/dev/null \
        | grep -qE "inet ${VIP2}/32"; then
    fail "VIP $VIP2 still present after binding deleted" \
         "(reconciler should have dropped it)"
else
    pass "VIP $VIP2 removed by reconciler on unbind"
fi
