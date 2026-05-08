# shellcheck shell=bash
#
# Neutron REST + OpenStack-CLI helpers. Sourced by lib/case.sh.
#
# Expects $NEUTRON_URL, $NET_NAME, $OS_BIN, $OS_CLOUD_NAME from config.sh
# and $TOKEN, $NET_ID populated by case.sh during bootstrap.

_token() {
    "$OS_BIN" --os-cloud "$OS_CLOUD_NAME" token issue -f value -c id
}

_get_net_id() {
    "$OS_BIN" --os-cloud "$OS_CLOUD_NAME" network show "$NET_NAME" -f value -c id
}

_curl() {
    # _curl <method> <path> [json-body]
    # Retries: the lab's neutron-api is single-worker (API_WORKERS=1
    # per the controller's local.conf) and the multi-chassis lab has
    # 5 agents polling every 10s, which empirically causes occasional
    # broken-pipe / SIGPIPE on uwsgi. Curl --retry-all-errors retries
    # transparently on those, with a 1s delay.
    local method="$1" path="$2" body="${3:-}"
    local args=(-sS -X "$method" "${NEUTRON_URL}${path}"
                -H "X-Auth-Token: ${TOKEN}"
                -H "Content-Type: application/json"
                --retry 3 --retry-delay 1 --retry-all-errors
                --max-time 15)
    if [[ -n "$body" ]]; then
        curl "${args[@]}" -d "$body"
    else
        curl "${args[@]}"
    fi
}

_jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

# --- Setup / teardown ----------------------------------------------------
setup_service() {
    # Idempotent: returns existing svc id if a same-named svc exists.
    local existing
    existing=$(_curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
data=json.load(sys.stdin)
for s in data['local_services']:
    if s['name']=='$SVC_NAME':
        print(s['id']); break")
    if [[ -n "$existing" ]]; then
        echo "$existing"
        return
    fi
    _curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$SVC_NAME\",\"local_ipv4\":\"$SVC_VIP\",\"port\":$SVC_PORT,\"protocol\":\"$SVC_PROTO\"}}" \
        | _jget "['local_service']['id']"
}

setup_binding() {
    # setup_binding <svc_id> <net_id> → binding_id
    local existing
    existing=$(_curl GET "/v2.0/local_service_bindings?service_id=$1&network_id=$2" \
        | python3 -c "import sys,json
data=json.load(sys.stdin)
b=data.get('local_service_bindings',[])
print(b[0]['id'] if b else '')")
    if [[ -n "$existing" ]]; then
        echo "$existing"
        return
    fi
    _curl POST "/v2.0/local_service_bindings" \
        "{\"local_service_binding\": {\"service_id\":\"$1\",\"network_id\":\"$2\"}}" \
        | _jget "['local_service_binding']['id']"
}

teardown_binding() {
    [[ -n "${1:-}" ]] || return 0
    _curl DELETE "/v2.0/local_service_bindings/$1" >/dev/null || true
}

teardown_service() {
    [[ -n "${1:-}" ]] || return 0
    _curl DELETE "/v2.0/local_services/$1" >/dev/null || true
}

# Look up an existing service id by name; print empty string if absent.
# Idempotency helper for re-runs.
lookup_service_id() {
    local name="$1"
    _curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$name':
        print(s['id']); break"
}

# Number of localports already on $1 (network) — used as a baseline by
# tests that want to verify "binding lifecycle restored chassis state"
# rather than "localport count is exactly zero." The latter assumption
# breaks once any cloud-wide opt-out service is enabled, because such a
# service implicitly attaches to every network and keeps the localport
# alive even with no explicit binding.
_baseline_localport_count() {
    "$OS_BIN" --os-cloud "$OS_CLOUD_NAME" port list \
        --network "$1" --device-owner ovn-lb-hm:distributed -f value -c ID \
        | wc -l
}

# True (exit 0) if at least one enabled opt-out service exists in the
# cloud whose implicit attachment to ``$1`` is not explicitly disabled
# by an ``enabled=False`` binding row. When this returns true, an
# explicit binding teardown does NOT remove the localport / netns —
# the opt-out service keeps the chassis state alive.
_network_has_implicit_attachment() {
    local net="$1"
    _curl GET "/v2.0/local_services?attachment_policy=opt-out&enabled=True" 2>/dev/null \
        | NET="$net" python3 -c "
import json, os, sys, urllib.parse, urllib.request
out = json.load(sys.stdin).get('local_services', [])
if not out:
    sys.exit(1)
# Filter out any opt-out service explicitly excluded for this network
# via an enabled=False binding row.
net = os.environ['NET']
url = '${NEUTRON_URL}/v2.0/local_service_bindings?network_id=' + net
req = urllib.request.Request(url, headers={'X-Auth-Token': '${TOKEN}'})
try:
    excl_resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
except Exception:
    sys.exit(1)
excl = {b['service_id'] for b in excl_resp.get('local_service_bindings', [])
        if not b.get('enabled', True)}
sys.exit(0 if any(s['id'] not in excl for s in out) else 1)
" >/dev/null 2>&1
}
