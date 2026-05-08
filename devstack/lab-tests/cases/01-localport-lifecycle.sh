#!/usr/bin/env bash
# tags: smoke
#
# Localport piggyback on LB-HM: binding creates a Neutron port with
# device_owner=ovn-lb-hm:distributed; OVN materializes it as
# type=localport; unbinding removes it (or restores baseline if a
# cloud-wide opt-out service keeps the port alive).

CASE_ID="01-localport-lifecycle"
CASE_TITLE="localport via LB-HM piggyback"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/case.sh
. "${LAB_TESTS_DIR}/lib/case.sh"

# State tracked across body for case_teardown.
SVC_ID=""
BIND_ID=""

case_teardown() {
    teardown_binding "$BIND_ID"
    teardown_service "$SVC_ID"
}

# Capture baseline BEFORE we add our binding. Cloud-wide opt-out services
# (attachment_policy=opt-out, enabled=True) implicitly attach to every
# network and keep the localport alive; the test is "did our binding
# lifecycle restore chassis state?" not "is the count exactly zero," so
# we compare against this baseline.
BASELINE=$(_baseline_localport_count "$NET_ID")
SVC_ID=$(setup_service)
BIND_ID=$(setup_binding "$SVC_ID" "$NET_ID")
sleep 1

# Neutron port exists with our marker.
PORT_ID=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" port list \
    --network "$NET_ID" --device-owner ovn-lb-hm:distributed -f value -c ID \
    | head -1)
if [[ -n "$PORT_ID" ]]; then
    pass "Neutron localport created ($PORT_ID)"
else
    fail "no Neutron port with device_owner=ovn-lb-hm:distributed on network"
fi

# OVN LSP exists with type=localport.
LSP_TYPE=$($OVN_NBCTL --bare --columns=type list Logical_Switch_Port "$PORT_ID" 2>/dev/null || true)
if [[ "$LSP_TYPE" == "localport" ]]; then
    pass "OVN LSP type == localport"
else
    fail "OVN LSP type is '$LSP_TYPE' (expected 'localport')" \
         "$OVN_NBCTL find Logical_Switch_Port name=$PORT_ID"
fi

# Marker substring is present in device_id.
DEV_ID=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" port show "$PORT_ID" -f value -c device_id 2>/dev/null || true)
if [[ "$DEV_ID" == *"localsvc-"* ]]; then
    pass "device_id carries 'localsvc-' marker"
else
    fail "device_id missing localsvc- marker: '$DEV_ID'"
fi

# Cleanup: unbind, expect port count to return to baseline. baseline > 0
# means a cloud-wide opt-out service is keeping the localport alive;
# that's expected and the assertion is "we didn't leave a dangling extra
# localport." baseline == 0 is the original "removed on unbind" case.
teardown_binding "$BIND_ID"
BIND_ID=""
sleep 1
PORT_COUNT=$(_baseline_localport_count "$NET_ID")
if [[ "$PORT_COUNT" -eq "$BASELINE" ]]; then
    if [[ "$BASELINE" -eq 0 ]]; then
        pass "localport removed on last unbind"
    else
        pass "localport count returned to baseline ($BASELINE; opt-out service keeps it alive)"
    fi
else
    fail "$PORT_COUNT localport(s) on $NET_ID after unbind (baseline was $BASELINE)"
fi
