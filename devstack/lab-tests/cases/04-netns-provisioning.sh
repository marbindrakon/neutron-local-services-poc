#!/usr/bin/env bash
# tags: smoke
#
# netns + tap plumbing: agent provisions localsvc-<net_id> with a veth
# pair, the ns-side has the localport's IPv4, the root-side is in br-int
# with iface-id matching the LSP, and ns-side MAC matches OVN's
# Port_Binding. Teardown removes them (or, with an opt-out service in
# play, leaves them up — the test asserts the right outcome based on
# whether implicit attachments exist).

CASE_ID="04-netns-provisioning"
CASE_TITLE="netns + tap plumbing"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

SVC_ID=""
BIND_ID=""

case_teardown() {
    teardown_binding "$BIND_ID"
    teardown_service "$SVC_ID"
}

SVC_ID=$(setup_service)
BIND_ID=$(setup_binding "$SVC_ID" "$NET_ID")
sleep 2

NS_NAME="localsvc-$NET_ID"
VETH_ROOT="tls$(echo "$NET_ID" | head -c10)0"
VETH_NS="tls$(echo "$NET_ID" | head -c10)1"

if sudo ip netns list | awk '{print $1}' | grep -qx "$NS_NAME"; then
    pass "netns $NS_NAME exists"
else
    fail "netns $NS_NAME missing" "sudo ip netns list | grep localsvc"
fi

# IPv4 fixed_ip from the localport's subnet should be on the ns side.
ADDRS=$(sudo ip -n "$NS_NAME" -4 addr show "$VETH_NS" 2>/dev/null \
        | awk '/inet /{print $2}' || true)
if [[ -n "$ADDRS" ]]; then
    pass "veth $VETH_NS has IPv4: $ADDRS"
else
    fail "no IPv4 on $VETH_NS inside $NS_NAME" \
         "sudo ip -n $NS_NAME addr"
fi

# Root-side veth should be in br-int with iface-id matching the LSP.
IFACE_ID=$(sudo ovs-vsctl get Interface "$VETH_ROOT" external_ids:iface-id 2>/dev/null \
            | tr -d '"' || true)
OVN_LP=$($OVN_NBCTL --bare --columns=name find Logical_Switch_Port \
            external_ids:neutron\\:device_id="ovn-lb-hm-localsvc-$NET_ID" 2>/dev/null)
if [[ -n "$IFACE_ID" && "$IFACE_ID" == "$OVN_LP" ]]; then
    pass "$VETH_ROOT in br-int with iface-id == LSP $OVN_LP"
else
    fail "iface-id mismatch (got '$IFACE_ID', expected '$OVN_LP')"
fi

# MAC on the ns side should match the OVN port_binding row.
NS_MAC=$(sudo ip -n "$NS_NAME" link show "$VETH_NS" 2>/dev/null \
          | awk '/link\/ether/{print $2}' || true)
SB_MAC=$(sudo ovn-sbctl --bare --columns=mac find Port_Binding \
          logical_port="$OVN_LP" 2>/dev/null \
          | awk '{print $1}' || true)
if [[ -n "$NS_MAC" && "$NS_MAC" == "$SB_MAC" ]]; then
    pass "veth ns-side MAC matches LSP MAC ($NS_MAC)"
else
    fail "MAC mismatch (ns=$NS_MAC sb=$SB_MAC)"
fi

# Cleanup → if the network has no opt-out fan-out keeping it alive,
# netns and veth should disappear. If a cloud-wide opt-out service is
# implicitly attached, the localport (and therefore the netns and root
# veth) stays up; that's the expected post-opt-out behavior, so assert
# the appropriate outcome based on whether implicit attachments exist.
teardown_binding "$BIND_ID"
BIND_ID=""
sleep 2
if _network_has_implicit_attachment "$NET_ID"; then
    HAS_IMPLICIT=yes
else
    HAS_IMPLICIT=no
fi
if [[ "$HAS_IMPLICIT" == yes ]]; then
    if sudo ip netns list | awk '{print $1}' | grep -qx "$NS_NAME"; then
        pass "netns $NS_NAME kept alive by opt-out service (expected)"
    else
        fail "netns $NS_NAME disappeared despite opt-out attachment"
    fi
    if sudo ovs-vsctl list-ports br-int | grep -qx "$VETH_ROOT"; then
        pass "root veth $VETH_ROOT kept in br-int by opt-out service"
    else
        fail "root veth $VETH_ROOT disappeared despite opt-out attachment"
    fi
else
    if sudo ip netns list | awk '{print $1}' | grep -qx "$NS_NAME"; then
        fail "netns $NS_NAME still present after unbind"
    else
        pass "netns $NS_NAME removed on unbind"
    fi
    if sudo ovs-vsctl list-ports br-int | grep -qx "$VETH_ROOT"; then
        fail "root veth $VETH_ROOT still in br-int after unbind"
    else
        pass "root veth $VETH_ROOT removed from br-int on unbind"
    fi
fi
