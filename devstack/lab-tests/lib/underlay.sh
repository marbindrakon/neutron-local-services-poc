# shellcheck shell=bash
#
# Underlay-test cleanup. Sourced by case 09-underlay-egress.

underlay_clean_leftovers() {
    # Delete any service named UNDERLAY_TCP_NAME or UNDERLAY_UDP_NAME plus
    # their backends and bindings. Without this the unique constraint on
    # (service_id, name) for backends causes neutron-api's
    # @retry_db_errors decorator to spin in a tight loop on the
    # subsequent POST, hanging the single API worker (API_WORKERS=1).
    local svc_name svc_id body
    for svc_name in "$UNDERLAY_TCP_NAME" "$UNDERLAY_UDP_NAME"; do
        svc_id=$(lookup_service_id "$svc_name")
        [[ -z "$svc_id" ]] && continue
        body=$(_curl GET "/v2.0/local_service_backends?service_id=$svc_id")
        for be_id in $(echo "$body" | python3 -c "import sys,json
for b in json.load(sys.stdin)['local_service_backends']: print(b['id'])" 2>/dev/null); do
            _curl DELETE "/v2.0/local_service_backends/$be_id" >/dev/null || true
        done
        body=$(_curl GET "/v2.0/local_service_bindings?service_id=$svc_id")
        for b_id in $(echo "$body" | python3 -c "import sys,json
for b in json.load(sys.stdin)['local_service_bindings']: print(b['id'])" 2>/dev/null); do
            _curl DELETE "/v2.0/local_service_bindings/$b_id" >/dev/null || true
        done
        teardown_service "$svc_id"
    done
}
