#!/usr/bin/env bash
# tags: smoke
#
# host_routes injection: binding writes the service VIP into the
# subnet's host_routes (and the matching OVN DHCP_Options
# classless_static_route); a tenant strip-out is re-injected; teardown
# cleans the route up on unbind.

CASE_ID="02-host-routes-injection"
CASE_TITLE="host_routes injection"
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
sleep 1

# Find the IPv4 subnet that has DHCP enabled. `subnet list` doesn't
# expose enable_dhcp through `-f value`, so list IPv4 subnets and pick
# the first one whose `subnet show` reports enable_dhcp=True.
SUBNET_ID=""
for sn in $("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet list \
              --network "$NET_ID" --ip-version 4 -f value -c ID); do
    if [[ $("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$sn" \
              -f value -c enable_dhcp) == "True" ]]; then
        SUBNET_ID="$sn"; break
    fi
done
if [[ -z "$SUBNET_ID" ]]; then
    fail "no IPv4 DHCP subnet on $NET_NAME"
    exit 0
fi

# host_routes contains our VIP.
ROUTES=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$SUBNET_ID" -f json \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['host_routes'])")
if [[ "$ROUTES" == *"$SVC_VIP/32"* ]]; then
    pass "subnet host_routes contains $SVC_VIP/32"
else
    fail "subnet host_routes does not contain $SVC_VIP/32: $ROUTES"
fi

# OVN DHCP_Options classless_static_route contains our VIP.
DHCP_OPTS=$($OVN_NBCTL --bare --columns=options find DHCP_Options \
    external_ids:subnet_id="$SUBNET_ID" 2>/dev/null \
    | tr -d '\n' || true)
if [[ "$DHCP_OPTS" == *"$SVC_VIP/32"* ]]; then
    pass "OVN DHCP_Options.classless_static_route contains $SVC_VIP/32"
else
    fail "OVN DHCP_Options does not contain $SVC_VIP/32" \
         "options: $DHCP_OPTS"
fi

# Re-injection: PUT empty host_routes, expect ours to come back.
_curl PUT "/v2.0/subnets/$SUBNET_ID" \
    '{"subnet": {"host_routes": []}}' >/dev/null
sleep 1
ROUTES=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$SUBNET_ID" -f json \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['host_routes'])")
if [[ "$ROUTES" == *"$SVC_VIP/32"* ]]; then
    pass "host_routes re-injected after tenant strip"
else
    fail "host_routes not re-injected: $ROUTES"
fi

# Stale-route cleanup on unbind.
teardown_binding "$BIND_ID"
BIND_ID=""
sleep 1
ROUTES=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$SUBNET_ID" -f json \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['host_routes'])")
if [[ "$ROUTES" != *"$SVC_VIP/32"* ]]; then
    pass "host_routes cleaned up on unbind"
else
    fail "stale host_route remains after unbind: $ROUTES"
fi
