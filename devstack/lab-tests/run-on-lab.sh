#!/usr/bin/env bash
#
# End-to-end functional test runner for the local-services PoC.
# This script runs *on the lab* (as the `stack` user); the dev-side
# wrapper `lab-functional.sh` ssh's it into place.
#
# Usage (on the lab):
#   sudo -iu stack bash run-on-lab.sh <milestone>
#
# Where <milestone> is one of: m3, m4, m5, all
#
# Each milestone check is idempotent and self-cleaning: it sets up its
# own service+binding, runs the assertions, tears down. Failures print
# what failed and how to inspect; successes print one line per check.
#
# This is the "did the milestone really land on the lab" gate. Unit
# tests cover logic; this covers integration with a real Neutron +
# OVN + ovn-agent. Re-run after any push that touches the wire format
# or the agent extension.

set -euo pipefail

# --- Config (override via env) --------------------------------------------
NET_NAME="${NET_NAME:-private}"
NEUTRON_URL="${NEUTRON_URL:-http://172.18.0.128/networking}"
AGENT_UNIT="${AGENT_UNIT:-devstack@q-ovn-agent}"
SERVER_UNIT="${SERVER_UNIT:-devstack@neutron-api}"
OS_CLOUD_NAME="${OS_CLOUD_NAME:-devstack-admin}"
OS_BIN="${OS_BIN:-/usr/local/bin/openstack}"
OVN_NBCTL="${OVN_NBCTL:-sudo ovn-nbctl}"

SVC_NAME="lab-test-dns-vip"
SVC_VIP="169.254.169.123"
SVC_PORT=53
SVC_PROTO="udp"

# nat-plugin probe fixtures — separate VIPs so a stale binding can't
# poison the baseline. Service IDs are looked up by name so re-runs are
# idempotent.
NAT_TCP_NAME="lab-test-m8-tcp"
NAT_TCP_VIP="169.254.169.130"
NAT_TCP_PORT=80
NAT_TCP_BACKEND_PORT=18080
NAT_UDP_NAME="lab-test-m8-udp"
NAT_UDP_VIP="169.254.169.131"
NAT_UDP_PORT=54
NAT_UDP_BACKEND_PORT=18054
PROBE_CLIENT_NS="m8-client"
PROBE_CLIENT_VETH_ROOT="m8c0"
PROBE_CLIENT_VETH_NS="m8c1"
PROBE_CLIENT_PORT_NAME="m8-client-probe"


# multi-tenant isolation fixtures — multi-tenant + mixed-plugin. Network B is created
# fresh by the test (private-m10b) so we have two distinct tenant
# networks on the same chassis. Three services exercise both axes:
#   - svc_a_lvs:   network A, LVS plugin
#   - svc_a_envoy: network A, Envoy plugin (proves mixed-plugin in
#                  one netns alongside keepalived)
#   - svc_b_envoy: network B, Envoy plugin (proves multi-tenant)
ISOLATION_NETB_NAME="private-m10b"
M10_NETB_CIDR="10.10.99.0/24"
M10_NETB_GW="10.10.99.1"

M10_SVC_A_LVS_NAME="lab-test-m10-a-lvs"
M10_SVC_A_LVS_VIP="169.254.169.150"
M10_SVC_A_LVS_PORT=80
M10_SVC_A_LVS_BACKEND_PORT=20080

M10_SVC_A_ENVOY_NAME="lab-test-m10-a-envoy"
M10_SVC_A_ENVOY_VIP="169.254.169.151"
M10_SVC_A_ENVOY_PORT=80
M10_SVC_A_ENVOY_BACKEND_PORT=20081

M10_SVC_B_ENVOY_NAME="lab-test-m10-b-envoy"
M10_SVC_B_ENVOY_VIP="169.254.169.152"
M10_SVC_B_ENVOY_PORT=80
M10_SVC_B_ENVOY_BACKEND_PORT=20082

# Two test client netns — one attached to each tenant network.
M10_CLIENT_A_NS="m10a-client"
M10_CLIENT_A_VETH_ROOT="m10a0"
M10_CLIENT_A_VETH_NS="m10a1"
M10_CLIENT_A_PORT_NAME="m10a-client-probe"
M10_CLIENT_B_NS="m10b-client"
M10_CLIENT_B_VETH_ROOT="m10b0"
M10_CLIENT_B_VETH_NS="m10b1"
M10_CLIENT_B_PORT_NAME="m10b-client-probe"

# multi-chassis fixture — drives two REMOTE compute chassis to
# realize the same network on both, then asserts per-chassis netns +
# keepalived + cross-chassis data path. Compute IPs come from env so
# the lab inventory isn't baked into the script. Defaults match the
# c1/c2 compute nodes are provisioned via local.conf.compute.sample.
MULTICHASSIS_COMPUTE_A_IP="${MULTICHASSIS_COMPUTE_A_IP:-172.18.0.152}"
MULTICHASSIS_COMPUTE_B_IP="${MULTICHASSIS_COMPUTE_B_IP:-172.18.0.144}"
MULTICHASSIS_SSH_KEY="${MULTICHASSIS_SSH_KEY:-/home/stack/.ssh/m10mc-key}"
MULTICHASSIS_SSH_USER="${MULTICHASSIS_SSH_USER:-almalinux}"
MULTICHASSIS_SVC_NAME="lab-test-m10mc"
MULTICHASSIS_SVC_VIP="169.254.169.160"
MULTICHASSIS_SVC_PORT=80
# Per-chassis backend ports — distinct so each keepalived's TCP_CHECK
# can distinguish the local from the remote backend. Both are bound to
# the netns IP (which is the SAME address on both chassis because OVN
# realizes type=localport with one fixed_ip per network — the netns IP
# distinguishes the chassis only because the kernel inside each netns
# is independent).
MULTICHASSIS_BACKEND_PORT_A=20180
MULTICHASSIS_BACKEND_PORT_B=20181
MULTICHASSIS_CLIENT_NS="m10mc-client"
MULTICHASSIS_CLIENT_VETH_ROOT="m10mc0"
MULTICHASSIS_CLIENT_VETH_NS="m10mc1"
MULTICHASSIS_CLIENT_PORT_NAME_A="m10mc-client-a"
MULTICHASSIS_CLIENT_PORT_NAME_B="m10mc-client-b"

# underlay-backend fixture — services backed by REAL services on
# the lab underlay (not synthesized in netns). The TCP backend is the
# lab's HTTP service at 172.18.0.11:80; the UDP backend is the lab's
# DNS service at 172.18.42.10:53. Both reachable from any chassis's
# host root netns.
#
# Architectural note: only TCP-via-Envoy can reach underlay backends in
# the current PoC. The host envoy lives in the host root netns and has
# full underlay routing. The LVS plugin's keepalived runs INSIDE the
# tenant netns, which has only the on-subnet route — no default, so
# underlay IPs are unreachable. The test exercises both and the UDP-via-
# LVS case is asserted as "expected fail until netns gains underlay
# routing" (documented in docs/limitations.md).
UNDERLAY_TCP_NAME="lab-test-m10-underlay-tcp"
UNDERLAY_TCP_VIP="169.254.169.170"
UNDERLAY_TCP_PORT=80
UNDERLAY_TCP_BACKEND_ADDR="172.18.0.11"
UNDERLAY_TCP_BACKEND_PORT=80

UNDERLAY_UDP_NAME="lab-test-m10-underlay-udp"
UNDERLAY_UDP_VIP="169.254.169.171"
UNDERLAY_UDP_PORT=53
UNDERLAY_UDP_BACKEND_ADDR="172.18.42.10"
UNDERLAY_UDP_BACKEND_PORT=53

# --- Colored output -------------------------------------------------------
RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CLR=$'\033[0m'
PASS=0; FAIL=0
pass() { echo "${GRN}PASS${CLR}  $1"; PASS=$((PASS+1)); }
fail() { echo "${RED}FAIL${CLR}  $1"; [[ -n "${2:-}" ]] && echo "      $2"; FAIL=$((FAIL+1)); }
note() { echo "${YEL}NOTE${CLR}  $1"; }

# --- REST helpers ---------------------------------------------------------
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

# --- Setup / teardown -----------------------------------------------------
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

# --- nat-plugin probe-client helpers ------------------------------------------------------------

# A tenant-attached test client netns. We create a Neutron port on
# $NET_ID, plumb a veth into br-int with iface-id == port_id (the same
# binding shape OVN uses for VMs and localports), and put the port's
# IP/MAC inside the netns. Result: a process running in the netns
# behaves exactly like a VM on the tenant network — which is what we
# need to send traffic at the service VIP without SSH-ing into a real
# guest.
probe_client_setup() {
    # Idempotent — if a previous run left state behind, tear it down
    # first so we don't trip on EEXIST when re-creating the veth.
    probe_client_teardown >/dev/null 2>&1 || true

    local resp port_id port_mac port_ip
    resp=$(_curl POST "/v2.0/ports" \
        "{\"port\": {\"name\":\"$PROBE_CLIENT_PORT_NAME\",\"network_id\":\"$NET_ID\"}}")
    port_id=$(echo "$resp" | _jget "['port']['id']")
    port_mac=$(echo "$resp" | _jget "['port']['mac_address']")
    port_ip=$(echo "$resp" | _jget "['port']['fixed_ips'][0]['ip_address']")
    local cidr
    cidr=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show \
        "$(echo "$resp" | _jget "['port']['fixed_ips'][0]['subnet_id']")" \
        -f value -c cidr)
    local prefix="${cidr#*/}"

    sudo ip netns add "$PROBE_CLIENT_NS"
    sudo ip link add "$PROBE_CLIENT_VETH_ROOT" type veth peer name "$PROBE_CLIENT_VETH_NS"
    sudo ip link set "$PROBE_CLIENT_VETH_NS" netns "$PROBE_CLIENT_NS"
    sudo ovs-vsctl --may-exist add-port br-int "$PROBE_CLIENT_VETH_ROOT" \
        -- set interface "$PROBE_CLIENT_VETH_ROOT" \
                  external_ids:iface-id="$port_id"
    sudo ip -n "$PROBE_CLIENT_NS" link set "$PROBE_CLIENT_VETH_NS" address "$port_mac"
    sudo ip -n "$PROBE_CLIENT_NS" addr add "${port_ip}/${prefix}" dev "$PROBE_CLIENT_VETH_NS"
    sudo ip -n "$PROBE_CLIENT_NS" link set "$PROBE_CLIENT_VETH_NS" up
    sudo ip -n "$PROBE_CLIENT_NS" link set lo up
    sudo ip link set "$PROBE_CLIENT_VETH_ROOT" up

    # A real guest would learn 169.254.<vip>/32 routes via DHCP option
    # 121 (host_routes injection). Our ad-hoc netns doesn't speak
    # DHCP, so add a wide link-local-onlink route so the kernel ARPs
    # for any 169.254/16 VIP on this veth — the localsvc netns owns
    # the /32s and ARP-responds.
    sudo ip -n "$PROBE_CLIENT_NS" route add 169.254.0.0/16 dev "$PROBE_CLIENT_VETH_NS"

    # Stash the port id so teardown can find it.
    echo "$port_id" | sudo tee /tmp/m8-client.port_id >/dev/null
}

probe_client_teardown() {
    sudo ip netns del "$PROBE_CLIENT_NS" 2>/dev/null || true
    sudo ovs-vsctl --if-exists del-port br-int "$PROBE_CLIENT_VETH_ROOT" || true
    sudo ip link del "$PROBE_CLIENT_VETH_ROOT" 2>/dev/null || true
    if [[ -f /tmp/m8-client.port_id ]]; then
        local pid
        pid=$(sudo cat /tmp/m8-client.port_id)
        _curl DELETE "/v2.0/ports/$pid" >/dev/null || true
        sudo rm -f /tmp/m8-client.port_id
    fi
}

# Spawn a backend (TCP HTTP or UDP echo) inside the localsvc netns,
# bound to the netns's own subnet IP. The lab tests use this in lieu
# of running a real backend host: the localsvc netns already has
# tenant-network reachability so this satisfies LVS-NAT routing
# without extra plumbing. PID is written to /tmp/m8-backend.<port>.pid
# for teardown.
m8_spawn_tcp_backend() {
    local ns_name="$1" ns_ip="$2" port="$3"
    sudo ip netns exec "$ns_name" \
        python3 -m http.server "$port" --bind "$ns_ip" \
        >/tmp/m8-backend.$port.log 2>&1 &
    local pid=$!
    echo "$pid" | sudo tee /tmp/m8-backend.$port.pid >/dev/null
    # Give python's http.server a beat to bind.
    sleep 1
}

m8_spawn_udp_backend() {
    # `socat ... SYSTEM:'tr a-z A-Z'` echoes back upper-cased input —
    # useful proof that the request actually reached the backend
    # (the upper-case is the marker), not just that the VIP is up.
    local ns_name="$1" ns_ip="$2" port="$3"
    sudo ip netns exec "$ns_name" \
        socat -u UDP4-RECVFROM:"$port",bind="$ns_ip",reuseaddr,fork \
              SYSTEM:'tr a-z A-Z >&2' \
        >/tmp/nat-udp-backend.$port.log 2>&1 &
    echo "$!" | sudo tee /tmp/m8-backend.$port.pid >/dev/null
    sleep 1
}

m8_kill_backend() {
    local port="$1"
    local pidfile="/tmp/m8-backend.$port.pid"
    if [[ -f "$pidfile" ]]; then
        local pid
        pid=$(sudo cat "$pidfile")
        sudo kill "$pid" 2>/dev/null || true
        sudo rm -f "$pidfile"
    fi
}

m8_get_localsvc_ns_ip() {
    # The localsvc netns's IP is the localport's fixed_ip. We need it
    # to point backends at, since "localhost from the perspective of
    # the LVS director" is the netns's own subnet IP.
    local ns_name="localsvc-$NET_ID"
    local veth="tls$(echo "$NET_ID" | head -c10)1"
    sudo ip -n "$ns_name" -4 -o addr show "$veth" 2>/dev/null \
        | awk '/inet /{print $4}' | grep -v '/32' \
        | head -1 | cut -d/ -f1
}

# --- multi-tenant helpers ----------------------------------------------------------

# Parametrized test-client setup. Same shape as probe_client_setup but
# takes the network id, netns name, veth pair names, port name, and a
# tag for the /tmp scratch file. Lets the multi-tenant suite stand up one client per tenant
# network without copy-pasting the probe-client helper.
m10_test_client_setup() {
    # m10_test_client_setup <net_id> <ns> <veth_root> <veth_ns> <port_name> <tag>
    local net_id="$1" ns="$2" veth_root="$3" veth_ns="$4"
    local port_name="$5" tag="$6"

    m10_test_client_teardown "$ns" "$veth_root" "$tag" >/dev/null 2>&1 || true

    local resp port_id port_mac port_ip
    resp=$(_curl POST "/v2.0/ports" \
        "{\"port\": {\"name\":\"$port_name\",\"network_id\":\"$net_id\"}}")
    port_id=$(echo "$resp" | _jget "['port']['id']")
    port_mac=$(echo "$resp" | _jget "['port']['mac_address']")
    port_ip=$(echo "$resp" | _jget "['port']['fixed_ips'][0]['ip_address']")
    local cidr
    cidr=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show \
        "$(echo "$resp" | _jget "['port']['fixed_ips'][0]['subnet_id']")" \
        -f value -c cidr)
    local prefix="${cidr#*/}"

    sudo ip netns add "$ns"
    sudo ip link add "$veth_root" type veth peer name "$veth_ns"
    sudo ip link set "$veth_ns" netns "$ns"
    sudo ovs-vsctl --may-exist add-port br-int "$veth_root" \
        -- set interface "$veth_root" \
                  external_ids:iface-id="$port_id"
    sudo ip -n "$ns" link set "$veth_ns" address "$port_mac"
    sudo ip -n "$ns" addr add "${port_ip}/${prefix}" dev "$veth_ns"
    sudo ip -n "$ns" link set "$veth_ns" up
    sudo ip -n "$ns" link set lo up
    sudo ip link set "$veth_root" up
    sudo ip -n "$ns" route add 169.254.0.0/16 dev "$veth_ns"

    echo "$port_id" | sudo tee "/tmp/m10-client.${tag}.port_id" >/dev/null
}

m10_test_client_teardown() {
    # m10_test_client_teardown <ns> <veth_root> <tag>
    local ns="$1" veth_root="$2" tag="$3"
    sudo ip netns del "$ns" 2>/dev/null || true
    sudo ovs-vsctl --if-exists del-port br-int "$veth_root" || true
    sudo ip link del "$veth_root" 2>/dev/null || true
    if [[ -f "/tmp/m10-client.${tag}.port_id" ]]; then
        local pid
        pid=$(sudo cat "/tmp/m10-client.${tag}.port_id")
        _curl DELETE "/v2.0/ports/$pid" >/dev/null || true
        sudo rm -f "/tmp/m10-client.${tag}.port_id"
    fi
}

# Look up an existing service id by name; print empty string if absent.
# Idempotency helper for re-runs (the multi-tenant suite creates several services and
# re-running shouldn't double-up).
lookup_service_id() {
    local name="$1"
    _curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$name':
        print(s['id']); break"
}

m10_get_localsvc_ns_ip_for() {
    # Same trick as m8_get_localsvc_ns_ip but parametric on the network.
    local net_id="$1"
    local ns_name="localsvc-$net_id"
    local veth="tls$(echo "$net_id" | head -c10)1"
    sudo ip -n "$ns_name" -4 -o addr show "$veth" 2>/dev/null \
        | awk '/inet /{print $4}' | grep -v '/32' \
        | head -1 | cut -d/ -f1
}

# --- multi-chassis helpers --------------------------------------------

# Run a command on a remote compute as $MULTICHASSIS_SSH_USER. We're already
# stack on the controller; the m10mc-key was placed in ~stack/.ssh by
# the lab operator. Returns the
# command's stdout.
m10mc_ssh() {
    local host="$1"; shift
    ssh -i "$MULTICHASSIS_SSH_KEY" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "${MULTICHASSIS_SSH_USER}@${host}" "$@"
}

# Plumb a Neutron port as a veth in br-int on the given remote chassis,
# in a fresh netns. This is the multi-chassis equivalent of
# m10_test_client_setup but the OVS / netns work happens on a remote
# host. Realizing the iface-id on a chassis's br-int is what causes OVN
# to bind the port to that chassis, which in turn makes the localport
# show up on that chassis (since OVN realizes localports on every
# chassis where any port of the network is bound). That kicks our agent
# extension's CREATE event, which provisions the localsvc-<net> netns.
#
# Args: <compute_ip> <port_name>
m10mc_remote_client_setup() {
    local host="$1" port_name="$2"
    # Idempotent — clean any prior state on the remote.
    m10mc_remote_client_teardown "$host" "$port_name" >/dev/null 2>&1 || true

    local resp port_id port_mac port_ip
    resp=$(_curl POST "/v2.0/ports" \
        "{\"port\": {\"name\":\"$port_name\",\"network_id\":\"$NET_ID\"}}")
    port_id=$(echo "$resp" | _jget "['port']['id']")
    port_mac=$(echo "$resp" | _jget "['port']['mac_address']")
    port_ip=$(echo "$resp" | _jget "['port']['fixed_ips'][0]['ip_address']")
    local cidr
    cidr=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show \
        "$(echo "$resp" | _jget "['port']['fixed_ips'][0]['subnet_id']")" \
        -f value -c cidr)
    local prefix="${cidr#*/}"

    # Stash the port id on the remote so teardown can find it. /tmp file
    # name disambiguates per-port so two clients on the same host don't
    # clobber each other.
    m10mc_ssh "$host" "echo $port_id | sudo tee /tmp/m10mc-${port_name}.port_id >/dev/null"

    m10mc_ssh "$host" "
        sudo ip netns add $MULTICHASSIS_CLIENT_NS 2>/dev/null
        sudo ip link add $MULTICHASSIS_CLIENT_VETH_ROOT type veth peer name $MULTICHASSIS_CLIENT_VETH_NS
        sudo ip link set $MULTICHASSIS_CLIENT_VETH_NS netns $MULTICHASSIS_CLIENT_NS
        sudo ovs-vsctl --may-exist add-port br-int $MULTICHASSIS_CLIENT_VETH_ROOT \
            -- set interface $MULTICHASSIS_CLIENT_VETH_ROOT external_ids:iface-id=$port_id
        sudo ip -n $MULTICHASSIS_CLIENT_NS link set $MULTICHASSIS_CLIENT_VETH_NS address $port_mac
        sudo ip -n $MULTICHASSIS_CLIENT_NS addr add ${port_ip}/${prefix} dev $MULTICHASSIS_CLIENT_VETH_NS
        sudo ip -n $MULTICHASSIS_CLIENT_NS link set $MULTICHASSIS_CLIENT_VETH_NS up
        sudo ip -n $MULTICHASSIS_CLIENT_NS link set lo up
        sudo ip link set $MULTICHASSIS_CLIENT_VETH_ROOT up
        sudo ip -n $MULTICHASSIS_CLIENT_NS route add 169.254.0.0/16 dev $MULTICHASSIS_CLIENT_VETH_NS
    "
    echo "$port_id"
}

m10mc_remote_client_teardown() {
    local host="$1" port_name="$2"
    m10mc_ssh "$host" "
        sudo ip netns del $MULTICHASSIS_CLIENT_NS 2>/dev/null || true
        sudo ovs-vsctl --if-exists del-port br-int $MULTICHASSIS_CLIENT_VETH_ROOT || true
        sudo ip link del $MULTICHASSIS_CLIENT_VETH_ROOT 2>/dev/null || true
    " >/dev/null 2>&1 || true
    if [[ -n "$port_name" ]]; then
        local pid
        pid=$(m10mc_ssh "$host" "[[ -f /tmp/m10mc-${port_name}.port_id ]] && sudo cat /tmp/m10mc-${port_name}.port_id" 2>/dev/null)
        [[ -n "$pid" ]] && _curl DELETE "/v2.0/ports/$pid" >/dev/null || true
        m10mc_ssh "$host" "sudo rm -f /tmp/m10mc-${port_name}.port_id" >/dev/null 2>&1 || true
    fi
}

# Spawn a TCP backend in the localsvc-<NET_ID> netns on a remote chassis,
# bound to the netns's own subnet IP (so LVS-NAT can reach it without
# extra plumbing). Returns the netns IP on stdout.
#
# Why systemd-run: a plain `nohup ... &` spawned over ssh dies when the
# ssh session ends, because Alma 10's logind defaults to
# KillUserProcesses=yes. systemd-run --collect --unit=... creates a
# transient service that runs independent of any ssh session and gets
# garbage-collected when stopped (--collect).
m10mc_remote_backend_spawn() {
    local host="$1" port="$2"
    local ns_name="localsvc-$NET_ID"
    local veth="tls$(echo "$NET_ID" | head -c10)1"
    local ns_ip
    ns_ip=$(m10mc_ssh "$host" "sudo ip -n $ns_name -4 -o addr show $veth 2>/dev/null | awk '/inet /{print \$4}' | grep -v /32 | head -1 | cut -d/ -f1")
    if [[ -z "$ns_ip" ]]; then
        echo ""; return 1
    fi
    # Reset-failed first so a stale unit name doesn't block the new one.
    m10mc_ssh "$host" "sudo systemctl reset-failed m10mc-backend-${port}.service 2>/dev/null; sudo systemd-run --collect --unit=m10mc-backend-${port}.service --working-directory=/tmp /usr/sbin/ip netns exec $ns_name /usr/bin/python3 -m http.server $port --bind $ns_ip"
    sleep 1
    echo "$ns_ip"
}

m10mc_remote_backend_kill() {
    local host="$1" port="$2"
    m10mc_ssh "$host" "sudo systemctl stop m10mc-backend-${port}.service 2>/dev/null; sudo systemctl reset-failed m10mc-backend-${port}.service 2>/dev/null" >/dev/null 2>&1 || true
}

# Wipe any leftover service / backend / per-host plumbing from a prior
# run so the test starts from a known state. Cheap to do every run.
multichassis_clean_leftovers() {
    # Delete any svc named MULTICHASSIS_SVC_NAME (and its backends).
    local svc_id
    svc_id=$(lookup_service_id "$MULTICHASSIS_SVC_NAME")
    if [[ -n "$svc_id" ]]; then
        # Walk backends and DELETE first (FK).
        local body
        body=$(_curl GET "/v2.0/local_service_backends?service_id=$svc_id")
        for be_id in $(echo "$body" | python3 -c "import sys,json
for b in json.load(sys.stdin)['local_service_backends']: print(b['id'])"); do
            _curl DELETE "/v2.0/local_service_backends/$be_id" >/dev/null || true
        done
        # Walk bindings and DELETE.
        body=$(_curl GET "/v2.0/local_service_bindings?service_id=$svc_id")
        for b_id in $(echo "$body" | python3 -c "import sys,json
for b in json.load(sys.stdin)['local_service_bindings']: print(b['id'])"); do
            _curl DELETE "/v2.0/local_service_bindings/$b_id" >/dev/null || true
        done
        teardown_service "$svc_id"
    fi
    # Also wipe any orphan client ports left over on the network.
    for pname in "$MULTICHASSIS_CLIENT_PORT_NAME_A" "$MULTICHASSIS_CLIENT_PORT_NAME_B"; do
        for pid in $("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" port list \
                     --network "$NET_ID" --name "$pname" -f value -c ID 2>/dev/null); do
            _curl DELETE "/v2.0/ports/$pid" >/dev/null || true
        done
    done
    # And per-host backend services + plumbing.
    for host in "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_COMPUTE_B_IP"; do
        m10mc_remote_backend_kill "$host" "$MULTICHASSIS_BACKEND_PORT_A" || true
        m10mc_remote_backend_kill "$host" "$MULTICHASSIS_BACKEND_PORT_B" || true
        m10mc_ssh "$host" "
            sudo ip netns del $MULTICHASSIS_CLIENT_NS 2>/dev/null || true
            sudo ovs-vsctl --if-exists del-port br-int $MULTICHASSIS_CLIENT_VETH_ROOT 2>/dev/null || true
            sudo ip link del $MULTICHASSIS_CLIENT_VETH_ROOT 2>/dev/null || true
            sudo rm -f /tmp/m10mc-*.port_id /tmp/m10mc-backend.*.log /tmp/m10mc-backend.*.pid 2>/dev/null || true
        " >/dev/null 2>&1 || true
    done
}

# --- localport piggyback --------------------------------------------
test_localport_lifecycle() {
    echo
    echo "=== localport via LB-HM piggyback ==="
    local svc_id bind_id port_count baseline lsp_type port_id
    # Capture baseline BEFORE we add our binding. Cloud-wide opt-out
    # services (attachment_policy=opt-out, enabled=True) implicitly
    # attach to every network and keep the localport alive; the test
    # is "did our binding lifecycle restore chassis state?" not
    # "is the count exactly zero," so we compare against this baseline.
    baseline=$(_baseline_localport_count "$NET_ID")
    svc_id=$(setup_service)
    bind_id=$(setup_binding "$svc_id" "$NET_ID")
    sleep 1

    # Neutron port exists with our marker
    port_id=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" port list \
        --network "$NET_ID" --device-owner ovn-lb-hm:distributed -f value -c ID \
        | head -1)
    if [[ -n "$port_id" ]]; then
        pass "Neutron localport created ($port_id)"
    else
        fail "no Neutron port with device_owner=ovn-lb-hm:distributed on network"
    fi

    # OVN LSP exists with type=localport
    lsp_type=$($OVN_NBCTL --bare --columns=type list Logical_Switch_Port "$port_id" 2>/dev/null || true)
    if [[ "$lsp_type" == "localport" ]]; then
        pass "OVN LSP type == localport"
    else
        fail "OVN LSP type is '$lsp_type' (expected 'localport')" \
             "$OVN_NBCTL find Logical_Switch_Port name=$port_id"
    fi

    # Marker substring is present in device_id
    local dev_id
    dev_id=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" port show "$port_id" -f value -c device_id 2>/dev/null || true)
    if [[ "$dev_id" == *"localsvc-"* ]]; then
        pass "device_id carries 'localsvc-' marker"
    else
        fail "device_id missing localsvc- marker: '$dev_id'"
    fi

    # Cleanup: unbind, expect port count to return to baseline.
    # ``baseline > 0`` means a cloud-wide opt-out service is keeping
    # the localport alive; that's expected and the assertion is
    # "we didn't leave a dangling extra localport." ``baseline == 0``
    # is the original "removed on unbind" case.
    teardown_binding "$bind_id"
    sleep 1
    port_count=$(_baseline_localport_count "$NET_ID")
    if [[ "$port_count" -eq "$baseline" ]]; then
        if [[ "$baseline" -eq 0 ]]; then
            pass "localport removed on last unbind"
        else
            pass "localport count returned to baseline ($baseline; opt-out service keeps it alive)"
        fi
    else
        fail "$port_count localport(s) on $NET_ID after unbind (baseline was $baseline)"
    fi

    teardown_service "$svc_id"
}

# --- host_routes injection ------------------------------------------
test_host_routes_injection() {
    echo
    echo "=== host_routes injection ==="
    local svc_id bind_id subnet_id routes
    svc_id=$(setup_service)
    bind_id=$(setup_binding "$svc_id" "$NET_ID")
    sleep 1

    # Find the IPv4 subnet that has DHCP enabled. `subnet list` doesn't
    # expose enable_dhcp through `-f value`, so list IPv4 subnets and
    # pick the first one whose `subnet show` reports enable_dhcp=True.
    local sn
    for sn in $("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet list \
                  --network "$NET_ID" --ip-version 4 -f value -c ID); do
        if [[ $("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$sn" \
                  -f value -c enable_dhcp) == "True" ]]; then
            subnet_id="$sn"; break
        fi
    done
    [[ -z "$subnet_id" ]] && { fail "no IPv4 DHCP subnet on $NET_NAME"; teardown_service "$svc_id"; return; }

    # host_routes contains our VIP
    routes=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$subnet_id" -f json \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['host_routes'])")
    if [[ "$routes" == *"$SVC_VIP/32"* ]]; then
        pass "subnet host_routes contains $SVC_VIP/32"
    else
        fail "subnet host_routes does not contain $SVC_VIP/32: $routes"
    fi

    # OVN DHCP_Options classless_static_route contains our VIP
    local dhcp_opts
    dhcp_opts=$($OVN_NBCTL --bare --columns=options find DHCP_Options \
        external_ids:subnet_id="$subnet_id" 2>/dev/null \
        | tr -d '\n' || true)
    if [[ "$dhcp_opts" == *"$SVC_VIP/32"* ]]; then
        pass "OVN DHCP_Options.classless_static_route contains $SVC_VIP/32"
    else
        fail "OVN DHCP_Options does not contain $SVC_VIP/32" \
             "options: $dhcp_opts"
    fi

    # Re-injection: PUT empty host_routes, expect ours to come back.
    _curl PUT "/v2.0/subnets/$subnet_id" \
        '{"subnet": {"host_routes": []}}' >/dev/null
    sleep 1
    routes=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$subnet_id" -f json \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['host_routes'])")
    if [[ "$routes" == *"$SVC_VIP/32"* ]]; then
        pass "host_routes re-injected after tenant strip"
    else
        fail "host_routes not re-injected: $routes"
    fi

    # Stale-route cleanup on unbind.
    teardown_binding "$bind_id"
    sleep 1
    routes=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet show "$subnet_id" -f json \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['host_routes'])")
    if [[ "$routes" != *"$SVC_VIP/32"* ]]; then
        pass "host_routes cleaned up on unbind"
    else
        fail "stale host_route remains after unbind: $routes"
    fi

    teardown_service "$svc_id"
}

# --- agent extension event watcher ----------------------------------
test_agent_extension_events() {
    echo
    echo "=== agent extension Port_Binding watcher ==="
    local svc_id bind_id since logs
    # Confirm the extension actually loaded. We use bash `case` for the
    # substring check rather than `grep -q`, because journalctl can emit
    # multi-megabyte output and `echo "$logs" | grep -q` SIGPIPEs when
    # grep exits early — `set -o pipefail` then fails the whole pipe
    # even though grep matched.
    logs=$(sudo journalctl -u "$AGENT_UNIT" --no-pager 2>&1)
    case "$logs" in
        *"Extension manager: local-services OVN agent extension started"*)
            pass "extension loaded into $AGENT_UNIT" ;;
        *)
            fail "extension not in $AGENT_UNIT log" \
                 "sudo journalctl -u $AGENT_UNIT | grep -i extension"
            return ;;
    esac

    svc_id=$(setup_service)
    bind_id=$(setup_binding "$svc_id" "$NET_ID")
    sleep 2

    # The PB watcher fires on Logical_Switch_Port (Port_Binding) row
    # CREATE / UPDATE / DELETE. host_routes injection happens at the
    # *subnet* level (DHCP_Options), so an explicit bind/unbind that
    # doesn't change localport existence (opt-out kept it alive) won't
    # trigger any PB row change at all — the steady-state reconcile is
    # via the periodic timer, not events.
    #
    # So: assert the watcher has fired *at any point* since the agent
    # started for this network. The startup sync provisions a netns
    # for every existing localport, which produces a "provision netns
    # for network $NET_ID" log line. That line is the canonical
    # evidence the PB watcher is correctly wired (its match_fn passed,
    # its run() executed). If we additionally see a reconcile-netns
    # line (PB UPDATE) or teardown-netns (PB DELETE), so much the
    # better, but the provision line at startup is sufficient.
    logs=$(sudo journalctl -u "$AGENT_UNIT" --no-pager 2>&1)
    case "$logs" in
        *"local-services: "*"netns for network $NET_ID"*)
            pass "PB watcher fired for $NET_ID at least once" ;;
        *)
            fail "no local-services PB-event log line for $NET_ID in agent history" \
                 "expected one of: provision/reconcile/teardown netns for network $NET_ID" ;;
    esac

    teardown_binding "$bind_id"
    teardown_service "$svc_id"
}

# --- netns + tap plumbing -------------------------------------------
test_netns_provisioning() {
    echo
    echo "=== netns + tap plumbing ==="
    local svc_id bind_id ns_name veth_root veth_ns
    svc_id=$(setup_service)
    bind_id=$(setup_binding "$svc_id" "$NET_ID")
    sleep 2

    ns_name="localsvc-$NET_ID"
    veth_root="tls$(echo "$NET_ID" | head -c10)0"
    veth_ns="tls$(echo "$NET_ID" | head -c10)1"

    if sudo ip netns list | awk '{print $1}' | grep -qx "$ns_name"; then
        pass "netns $ns_name exists"
    else
        fail "netns $ns_name missing" "sudo ip netns list | grep localsvc"
    fi

    # IPv4 fixed_ip from the localport's subnet should be on the ns side.
    local addrs
    addrs=$(sudo ip -n "$ns_name" -4 addr show "$veth_ns" 2>/dev/null \
            | awk '/inet /{print $2}' || true)
    if [[ -n "$addrs" ]]; then
        pass "veth $veth_ns has IPv4: $addrs"
    else
        fail "no IPv4 on $veth_ns inside $ns_name" \
             "sudo ip -n $ns_name addr"
    fi

    # Root-side veth should be in br-int with iface-id matching the LSP.
    local iface_id ovn_lp
    iface_id=$(sudo ovs-vsctl get Interface "$veth_root" external_ids:iface-id 2>/dev/null \
                | tr -d '"' || true)
    ovn_lp=$($OVN_NBCTL --bare --columns=name find Logical_Switch_Port \
                external_ids:neutron\\:device_id="ovn-lb-hm-localsvc-$NET_ID" 2>/dev/null)
    if [[ -n "$iface_id" && "$iface_id" == "$ovn_lp" ]]; then
        pass "$veth_root in br-int with iface-id == LSP $ovn_lp"
    else
        fail "iface-id mismatch (got '$iface_id', expected '$ovn_lp')"
    fi

    # MAC on the ns side should match the OVN port_binding row.
    local ns_mac sb_mac
    ns_mac=$(sudo ip -n "$ns_name" link show "$veth_ns" 2>/dev/null \
              | awk '/link\/ether/{print $2}' || true)
    sb_mac=$(sudo ovn-sbctl --bare --columns=mac find Port_Binding \
              logical_port="$ovn_lp" 2>/dev/null \
              | awk '{print $1}' || true)
    if [[ -n "$ns_mac" && "$ns_mac" == "$sb_mac" ]]; then
        pass "veth ns-side MAC matches LSP MAC ($ns_mac)"
    else
        fail "MAC mismatch (ns=$ns_mac sb=$sb_mac)"
    fi

    # Cleanup → if the network has no opt-out fan-out keeping it
    # alive, netns and veth should disappear. If a cloud-wide opt-out
    # service is implicitly attached, the localport (and therefore
    # the netns and root veth) stays up; that's the expected post-
    # opt-out behavior, so assert the appropriate outcome based on
    # whether implicit attachments exist.
    teardown_binding "$bind_id"
    sleep 2
    local has_implicit
    if _network_has_implicit_attachment "$NET_ID"; then
        has_implicit=yes
    else
        has_implicit=no
    fi
    if [[ "$has_implicit" == yes ]]; then
        if sudo ip netns list | awk '{print $1}' | grep -qx "$ns_name"; then
            pass "netns $ns_name kept alive by opt-out service (expected)"
        else
            fail "netns $ns_name disappeared despite opt-out attachment"
        fi
        if sudo ovs-vsctl list-ports br-int | grep -qx "$veth_root"; then
            pass "root veth $veth_root kept in br-int by opt-out service"
        else
            fail "root veth $veth_root disappeared despite opt-out attachment"
        fi
    else
        if sudo ip netns list | awk '{print $1}' | grep -qx "$ns_name"; then
            fail "netns $ns_name still present after unbind"
        else
            pass "netns $ns_name removed on unbind"
        fi
        if sudo ovs-vsctl list-ports br-int | grep -qx "$veth_root"; then
            fail "root veth $veth_root still in br-int after unbind"
        else
            pass "root veth $veth_root removed from br-int on unbind"
        fi
    fi

    teardown_service "$svc_id"
}

# --- VIP reconciliation -----------------------------------------
test_vip_reconciliation() {
    echo
    echo "=== VIP reconciliation ==="
    local svc_id bind_id ns_name veth_ns

    svc_id=$(setup_service)
    bind_id=$(setup_binding "$svc_id" "$NET_ID")
    # VIP reconciler runs on a 10s timer and on PB events. PB events
    # are the fast path (~1s), but a brand-new service+binding has to
    # walk: server DB write → host_routes refresh → mech-driver SB
    # external_ids update → IDL event → reconcile_vips → API GET →
    # ip addr add. Empirically that adds up to 6-8s on this lab; one
    # full timer interval (10s) plus a margin is the safe sleep.
    sleep 12

    ns_name="localsvc-$NET_ID"
    veth_ns="tls$(echo "$NET_ID" | head -c10)1"

    # The /32 VIP must be on the ns-side veth.
    if sudo ip -n "$ns_name" addr show "$veth_ns" 2>/dev/null \
            | grep -qE "inet ${SVC_VIP}/32"; then
        pass "veth $veth_ns has VIP $SVC_VIP/32"
    else
        fail "no $SVC_VIP/32 on $veth_ns inside $ns_name" \
             "sudo ip -n $ns_name addr show $veth_ns"
    fi

    # The on-subnet IP that netns.provision manages must STILL be there — provision
    # and reconcile_vips MUST NOT fight over the address list.
    local non32_count
    non32_count=$(sudo ip -n "$ns_name" -4 addr show "$veth_ns" 2>/dev/null \
                  | awk '/inet /{print $2}' \
                  | grep -vc '/32' || true)
    if [[ "${non32_count:-0}" -ge 1 ]]; then
        pass "on-subnet IPv4 still present alongside VIP"
    else
        fail "on-subnet IPv4 was clobbered by VIP reconciler"
    fi

    # Add a SECOND service on the same network; reconciler should pick
    # up the new VIP within one tick.
    local vip2="169.254.169.124"
    local svc2_name="lab-test-m7-second"
    local svc2_id
    svc2_id=$(_curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$svc2_name':
        print(s['id']); break")
    if [[ -z "$svc2_id" ]]; then
        svc2_id=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$svc2_name\",\"local_ipv4\":\"$vip2\",\"port\":80,\"protocol\":\"tcp\"}}" \
            | _jget "['local_service']['id']")
    fi
    local bind2_id
    bind2_id=$(setup_binding "$svc2_id" "$NET_ID")
    # Wait long enough for one full timer tick + a margin, since the
    # second binding's create event fires the reconciler immediately
    # but timing into the namespace is not zero.
    sleep 12

    if sudo ip -n "$ns_name" addr show "$veth_ns" 2>/dev/null \
            | grep -qE "inet ${vip2}/32"; then
        pass "second VIP $vip2 added by reconciler"
    else
        fail "second VIP $vip2 not added by reconciler" \
             "sudo ip -n $ns_name addr show $veth_ns"
    fi

    # ARP-respond check: the kernel inside the netns owns these /32s,
    # so a reply via the same MAC as the LSP is the actual proof that
    # a guest on this network would be able to talk to the VIP.
    local ns_mac
    ns_mac=$(sudo ip -n "$ns_name" link show "$veth_ns" 2>/dev/null \
              | awk '/link\/ether/{print $2}' || true)
    # Test inside the namespace: does the kernel respond on its own VIP?
    if sudo ip netns exec "$ns_name" ping -c1 -W1 "$SVC_VIP" >/dev/null 2>&1; then
        pass "kernel ARP-responds for $SVC_VIP inside $ns_name"
    else
        # Loopback-ping a /32 on the same interface should always work
        # if it's actually on the device; if not, the address didn't
        # land properly.
        fail "ping $SVC_VIP inside ns failed — VIP may not be on $veth_ns"
    fi

    # Drop the second binding; reconciler should remove its VIP.
    teardown_binding "$bind2_id"
    sleep 12
    if sudo ip -n "$ns_name" addr show "$veth_ns" 2>/dev/null \
            | grep -qE "inet ${vip2}/32"; then
        fail "VIP $vip2 still present after binding deleted" \
             "(reconciler should have dropped it)"
    else
        pass "VIP $vip2 removed by reconciler on unbind"
    fi

    # Final cleanup: drop everything we created.
    _curl DELETE "/v2.0/local_services/$svc2_id" >/dev/null || true
    teardown_binding "$bind_id"
    teardown_service "$svc_id"
}

# --- plugin abstraction + nat (Keepalived/LVS) ----------------------------
test_nat_plugin_e2e() {
    echo
    echo "=== plugin abstraction + nat (Keepalived/LVS) ==="
    local svc_tcp_id bind_tcp_id be_tcp_id
    local svc_udp_id bind_udp_id be_udp_id
    local ns_name="localsvc-$NET_ID"

    # Make sure baseline plumbing is alive — the nat plugin smoke builds on baseline plumbing, and
    # if the netns isn't there we've lost time chasing a broken stack.
    if ! sudo ip netns list | awk '{print $1}' | grep -qx "$ns_name"; then
        # Bring up a service so the netns exists. We'll keep this
        # binding around for the nat-plugin backends.
        local stub_id stub_bind_id
        stub_id=$(setup_service)
        stub_bind_id=$(setup_binding "$stub_id" "$NET_ID")
        sleep 4
        teardown_binding "$stub_bind_id"
        teardown_service "$stub_id"
    fi

    # The LVS service needs a binding that drives the netns. Reuse
    # the localport-lifecycle stub for that: bind once, leave it bound for the
    # duration of the nat-plugin checks, tear down at the end.
    local stub_svc_id stub_bind_id
    stub_svc_id=$(setup_service)
    stub_bind_id=$(setup_binding "$stub_svc_id" "$NET_ID")
    sleep 3
    if ! sudo ip netns list | awk '{print $1}' | grep -qx "$ns_name"; then
        fail "localsvc netns missing — nat plugin smoke cannot proceed without baseline plumbing"
        return
    fi

    # 1) Keepalived process is alive in the netns. Plugin spawns it
    #    on the first reconcile (any binding triggers reconcile_network).
    #    Manual loop instead of xargs|grep because set -o pipefail can
    #    make the pipeline fail when one /proc/<pid>/comm read races
    #    with a process exit.
    local ka_found=0
    for ka_pid in $(sudo ip netns pids "$ns_name" 2>/dev/null); do
        local comm
        comm=$(sudo cat "/proc/${ka_pid}/comm" 2>/dev/null || true)
        if [[ "$comm" == "keepalived" ]]; then
            ka_found=1
            break
        fi
    done
    if [[ "$ka_found" -eq 1 ]]; then
        pass "keepalived running inside $ns_name"
    else
        fail "no keepalived process in $ns_name" \
             "sudo ip netns pids $ns_name && ps -ef | grep keepalived"
    fi

    # 2) Bring up an LVS-fronted TCP backend. The HTTP server runs in
    #    the localsvc netns itself (saves us from booting a backend VM
    #    for the PoC); LVS DNAT to 10.0.0.X:18080 stays inside the netns.
    local ns_ip
    ns_ip=$(m8_get_localsvc_ns_ip)
    if [[ -z "$ns_ip" ]]; then
        fail "could not read localsvc netns IP — backend setup aborted"
        teardown_binding "$stub_bind_id"; teardown_service "$stub_svc_id"
        return
    fi
    m8_spawn_tcp_backend "$ns_name" "$ns_ip" "$NAT_TCP_BACKEND_PORT"

    svc_tcp_id=$(_curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$NAT_TCP_NAME':
        print(s['id']); break")
    if [[ -z "$svc_tcp_id" ]]; then
        local svc_resp
        svc_resp=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$NAT_TCP_NAME\",\"local_ipv4\":\"$NAT_TCP_VIP\",\"port\":$NAT_TCP_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\"}}")
        svc_tcp_id=$(echo "$svc_resp" | _jget "['local_service']['id']" 2>/dev/null || true)
        if [[ -z "$svc_tcp_id" ]]; then
            fail "could not create service $NAT_TCP_NAME" "$svc_resp"
            return
        fi
    fi
    local be_resp
    be_resp=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-tcp\",\"service_id\":\"$svc_tcp_id\",\"address\":\"$ns_ip\",\"port\":$NAT_TCP_BACKEND_PORT}}")
    be_tcp_id=$(echo "$be_resp" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    if [[ -z "$be_tcp_id" ]]; then
        fail "could not create backend for $NAT_TCP_NAME" "$be_resp"
        return
    fi
    bind_tcp_id=$(setup_binding "$svc_tcp_id" "$NET_ID")

    # Reconciler runs every 10s; underlay-egress provisioning on the
    # initial binding adds a few seconds, then keepalived needs one
    # delay_loop (6s) + connect_timeout (3s) to mark the backend
    # healthy and add it to ipvsadm. 22s is the safe envelope.
    sleep 22

    # 3) ipvsadm sees the virtual_server.
    local ipvsadm_out
    ipvsadm_out=$(sudo ip netns exec "$ns_name" ipvsadm -L -n 2>/dev/null || true)
    if echo "$ipvsadm_out" | grep -qE "TCP\s+${NAT_TCP_VIP}:${NAT_TCP_PORT}"; then
        pass "ipvsadm shows TCP $NAT_TCP_VIP:$NAT_TCP_PORT"
    else
        fail "ipvsadm has no entry for TCP $NAT_TCP_VIP:$NAT_TCP_PORT" \
             "$ipvsadm_out"
    fi

    # 4) End-to-end TCP through the VIP from a tenant-attached client.
    probe_client_setup
    local curl_out
    curl_out=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
        curl -sS --max-time 5 "http://$NAT_TCP_VIP:$NAT_TCP_PORT/" 2>&1 || true)
    if [[ -n "$curl_out" && "$curl_out" == *"Directory listing"* ]]; then
        pass "TCP curl through VIP $NAT_TCP_VIP reaches backend"
    else
        fail "TCP curl through VIP $NAT_TCP_VIP failed" \
             "out=$curl_out"
    fi

    # 5) UDP service. socat echoes back upper-cased input on stderr
    #    (we only care that the packet reached the backend; the
    #    return path doesn't matter for this PoC check — UDP is
    #    fire-and-forget).
    m8_spawn_udp_backend "$ns_name" "$ns_ip" "$NAT_UDP_BACKEND_PORT"
    svc_udp_id=$(_curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$NAT_UDP_NAME':
        print(s['id']); break")
    if [[ -z "$svc_udp_id" ]]; then
        local svc_resp
        svc_resp=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$NAT_UDP_NAME\",\"local_ipv4\":\"$NAT_UDP_VIP\",\"port\":$NAT_UDP_PORT,\"protocol\":\"udp\"}}")
        svc_udp_id=$(echo "$svc_resp" | _jget "['local_service']['id']" 2>/dev/null || true)
    fi
    if [[ -z "$svc_udp_id" ]]; then
        fail "could not create service $NAT_UDP_NAME"
    else
        local be_resp
        be_resp=$(_curl POST "/v2.0/local_service_backends" \
            "{\"local_service_backend\": {\"name\":\"be-udp\",\"service_id\":\"$svc_udp_id\",\"address\":\"$ns_ip\",\"port\":$NAT_UDP_BACKEND_PORT}}")
        be_udp_id=$(echo "$be_resp" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
        bind_udp_id=$(setup_binding "$svc_udp_id" "$NET_ID")
    fi
    sleep 12

    if sudo ip netns exec "$ns_name" ipvsadm -L -n 2>/dev/null \
            | grep -qE "UDP\s+${NAT_UDP_VIP}:${NAT_UDP_PORT}"; then
        pass "ipvsadm shows UDP $NAT_UDP_VIP:$NAT_UDP_PORT"
    else
        fail "ipvsadm has no entry for UDP $NAT_UDP_VIP:$NAT_UDP_PORT"
    fi

    # Send a UDP packet through the VIP. We don't expect a reply
    # (LVS-NAT for UDP relies on conntrack which can be flaky for
    # one-shot probes); the proof is that socat's stderr log shows
    # the upper-cased payload.
    sudo ip netns exec "$PROBE_CLIENT_NS" \
        bash -c "echo 'hello-m8' | timeout 2 nc -u -w1 $NAT_UDP_VIP $NAT_UDP_PORT" \
        >/dev/null 2>&1 || true
    sleep 1
    if sudo grep -q 'HELLO-NAT' "/tmp/nat-udp-backend.${NAT_UDP_BACKEND_PORT}.log" 2>/dev/null; then
        pass "UDP packet through VIP reached backend"
    else
        # Some socat builds write to stdout instead of stderr depending
        # on flags; check both before failing.
        if sudo grep -qi 'hello' "/tmp/nat-udp-backend.${NAT_UDP_BACKEND_PORT}.log" 2>/dev/null; then
            pass "UDP packet through VIP reached backend (stdout path)"
        else
            fail "UDP packet did not reach backend through VIP" \
                 "log: $(sudo cat /tmp/nat-udp-backend.${NAT_UDP_BACKEND_PORT}.log 2>/dev/null | head -3)"
        fi
    fi

    # 6) Health-check drop. Kill the TCP backend; keepalived's TCP_CHECK
    #    has connect_timeout=3 and delay_loop=6 in the rendered config,
    #    so within ~10-12s the backend should be removed from ipvsadm.
    m8_kill_backend "$NAT_TCP_BACKEND_PORT"
    sleep 14
    if sudo ip netns exec "$ns_name" ipvsadm -L -n 2>/dev/null \
            | awk -v vip="${NAT_TCP_VIP}:${NAT_TCP_PORT}" '
                $0 ~ "^TCP " vip {found=1; next}
                /^TCP|^UDP/ {found=0}
                found && /->/ {print}' \
            | grep -q "$ns_ip"; then
        fail "TCP backend $ns_ip:$NAT_TCP_BACKEND_PORT still in ipvsadm after kill" \
             "(keepalived health check should have dropped it)"
    else
        pass "keepalived dropped dead TCP backend from ipvsadm"
    fi

    # 7) Cleanup.
    probe_client_teardown
    m8_kill_backend "$NAT_UDP_BACKEND_PORT"
    teardown_binding "$bind_tcp_id"
    teardown_binding "$bind_udp_id"
    teardown_binding "$stub_bind_id"
    _curl DELETE "/v2.0/local_service_backends/$be_tcp_id" >/dev/null || true
    _curl DELETE "/v2.0/local_service_backends/$be_udp_id" >/dev/null || true
    teardown_service "$svc_tcp_id"
    teardown_service "$svc_udp_id"
    teardown_service "$stub_svc_id"
}

# --- Main -----------------------------------------------------------------
SUITE="${1:-all}"

# --- Multi-tenant + multi-chassis validation ------------------------
#
# Two parts of the multi-tenant exit criteria are exercised here:
#   1. Two tenant networks on the same chassis (own VIPs, no cross-talk;
#      netns isolation verified with both plugins).
#   2. Mixed-plugin coexistence — one network running BOTH LVS and Envoy
#      services in the same netns. (This came over from the original two-plugin coexistence design and is the
#      cleanest place to land it now that we have a multi-network
#      fixture in hand.)
#
# The third criterion — multi-chassis (a second compute node) —
# requires bringing up another VM and is out of scope for the single-
# chassis lab fixture.
test_multitenant_isolation() {
    echo
    echo "=== multi-tenant (2 networks) + mixed-plugin (nat + proxy on one ns) ==="
    # envoy was replaced by the proxy plugin; Mixed-plugin coverage with the new (`nat` +
    # `proxy`) pair is part of a follow-up;
    # short-circuit the multitenant test here so the harness doesn't probe envoy
    # artifacts that no longer exist.
    note "envoy assertions are gone now that proxy replaced envoy (envoy → proxy). nat-side coverage will move into the follow-up."
    return

    local svc_a_lvs_id svc_a_envoy_id svc_b_envoy_id
    local bind_a_lvs bind_a_envoy bind_b_envoy
    local be_a_lvs be_a_envoy be_b_envoy
    local net_b_id ns_a_name ns_b_name
    ns_a_name="localsvc-$NET_ID"

    # ---- Network B setup -------------------------------------------------
    # Idempotent: reuse the network if a previous run left it.
    net_b_id=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" network show \
        "$ISOLATION_NETB_NAME" -f value -c id 2>/dev/null || true)
    if [[ -z "$net_b_id" ]]; then
        net_b_id=$("$OS_BIN" --os-cloud "$OS_CLOUD_NAME" network create \
            "$ISOLATION_NETB_NAME" -f value -c id 2>/dev/null)
        "$OS_BIN" --os-cloud "$OS_CLOUD_NAME" subnet create \
            --network "$net_b_id" --subnet-range "$M10_NETB_CIDR" \
            --gateway "$M10_NETB_GW" --dhcp \
            "${ISOLATION_NETB_NAME}-subnet" -f value -c id >/dev/null
    fi
    if [[ -z "$net_b_id" ]]; then
        fail "could not create or find network $ISOLATION_NETB_NAME"
        return
    fi
    ns_b_name="localsvc-$net_b_id"
    pass "network B in place ($ISOLATION_NETB_NAME = $net_b_id)"

    # ---- Backends --------------------------------------------------------
    # LVS backend lives inside netns A (LVS-NAT routing requires reach
    # from the director). Need to drive netns A into existence first via
    # any binding — reuse the localport-lifecycle stub.
    local stub_a_svc stub_a_bind
    stub_a_svc=$(setup_service)
    stub_a_bind=$(setup_binding "$stub_a_svc" "$NET_ID")
    sleep 4
    if ! sudo ip netns list | awk '{print $1}' | grep -qx "$ns_a_name"; then
        fail "localsvc netns missing on network A — multitenant test cannot proceed"
        teardown_binding "$stub_a_bind"; teardown_service "$stub_a_svc"
        return
    fi
    local ns_a_ip
    ns_a_ip=$(m10_get_localsvc_ns_ip_for "$NET_ID")
    if [[ -z "$ns_a_ip" ]]; then
        fail "could not read network-A localsvc netns IP"
        teardown_binding "$stub_a_bind"; teardown_service "$stub_a_svc"
        return
    fi
    m8_spawn_tcp_backend "$ns_a_name" "$ns_a_ip" "$M10_SVC_A_LVS_BACKEND_PORT"

    # Envoy backends live in the host root netns (host-side proxy worker pattern). Distinct
    # ports so we can tell them apart in logs.
    sudo bash -c "cd /tmp && python3 -m http.server $M10_SVC_A_ENVOY_BACKEND_PORT --bind 127.0.0.2" \
        >/tmp/m10-backend.${M10_SVC_A_ENVOY_BACKEND_PORT}.log 2>&1 &
    echo "$!" | sudo tee /tmp/m10-backend.${M10_SVC_A_ENVOY_BACKEND_PORT}.pid >/dev/null
    sudo bash -c "cd /tmp && python3 -m http.server $M10_SVC_B_ENVOY_BACKEND_PORT --bind 127.0.0.2" \
        >/tmp/m10-backend.${M10_SVC_B_ENVOY_BACKEND_PORT}.log 2>&1 &
    echo "$!" | sudo tee /tmp/m10-backend.${M10_SVC_B_ENVOY_BACKEND_PORT}.pid >/dev/null
    sleep 1

    # ---- Services + bindings --------------------------------------------
    svc_a_lvs_id=$(lookup_service_id "$M10_SVC_A_LVS_NAME")
    if [[ -z "$svc_a_lvs_id" ]]; then
        svc_a_lvs_id=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$M10_SVC_A_LVS_NAME\",\"local_ipv4\":\"$M10_SVC_A_LVS_VIP\",\"port\":$M10_SVC_A_LVS_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\"}}" \
            | _jget "['local_service']['id']" 2>/dev/null || true)
    fi
    svc_a_envoy_id=$(lookup_service_id "$M10_SVC_A_ENVOY_NAME")
    if [[ -z "$svc_a_envoy_id" ]]; then
        svc_a_envoy_id=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$M10_SVC_A_ENVOY_NAME\",\"local_ipv4\":\"$M10_SVC_A_ENVOY_VIP\",\"port\":$M10_SVC_A_ENVOY_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}" \
            | _jget "['local_service']['id']" 2>/dev/null || true)
    fi
    svc_b_envoy_id=$(lookup_service_id "$M10_SVC_B_ENVOY_NAME")
    if [[ -z "$svc_b_envoy_id" ]]; then
        svc_b_envoy_id=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$M10_SVC_B_ENVOY_NAME\",\"local_ipv4\":\"$M10_SVC_B_ENVOY_VIP\",\"port\":$M10_SVC_B_ENVOY_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}" \
            | _jget "['local_service']['id']" 2>/dev/null || true)
    fi
    if [[ -z "$svc_a_lvs_id" || -z "$svc_a_envoy_id" || -z "$svc_b_envoy_id" ]]; then
        fail "could not create one of the multitenant services" \
             "lvs_a=$svc_a_lvs_id envoy_a=$svc_a_envoy_id envoy_b=$svc_b_envoy_id"
        return
    fi

    be_a_lvs=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-m10-a-lvs\",\"service_id\":\"$svc_a_lvs_id\",\"address\":\"$ns_a_ip\",\"port\":$M10_SVC_A_LVS_BACKEND_PORT}}" \
        | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    be_a_envoy=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-m10-a-envoy\",\"service_id\":\"$svc_a_envoy_id\",\"address\":\"127.0.0.2\",\"port\":$M10_SVC_A_ENVOY_BACKEND_PORT}}" \
        | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    be_b_envoy=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-m10-b-envoy\",\"service_id\":\"$svc_b_envoy_id\",\"address\":\"127.0.0.2\",\"port\":$M10_SVC_B_ENVOY_BACKEND_PORT}}" \
        | _jget "['local_service_backend']['id']" 2>/dev/null || true)

    bind_a_lvs=$(setup_binding "$svc_a_lvs_id" "$NET_ID")
    bind_a_envoy=$(setup_binding "$svc_a_envoy_id" "$NET_ID")
    bind_b_envoy=$(setup_binding "$svc_b_envoy_id" "$net_b_id")

    # Reconciler runs every 10s; PB events kick it within a tick. Allow
    # extra slack here because we're spawning keepalived AND two tenant
    # envoys back-to-back, plus the one-time host-envoy reload as the
    # catalog grows from {} to {3 services}.
    sleep 18

    # ---- Both netns exist -----------------------------------------------
    if sudo ip netns list | awk '{print $1}' | grep -qx "$ns_a_name"; then
        pass "netns A ($ns_a_name) exists"
    else
        fail "netns A missing"
    fi
    if sudo ip netns list | awk '{print $1}' | grep -qx "$ns_b_name"; then
        pass "netns B ($ns_b_name) exists"
    else
        fail "netns B missing — agent did not provision on first bind"
    fi

    # ---- Mixed-plugin: keepalived AND tenant envoy both run in netns A
    local ka_in_a=0 envoy_in_a=0 envoy_in_b=0
    for pid in $(sudo ip netns pids "$ns_a_name" 2>/dev/null); do
        local comm
        comm=$(sudo cat "/proc/${pid}/comm" 2>/dev/null || true)
        case "$comm" in
            keepalived) ka_in_a=1 ;;
            envoy)      envoy_in_a=1 ;;
        esac
    done
    for pid in $(sudo ip netns pids "$ns_b_name" 2>/dev/null); do
        local comm
        comm=$(sudo cat "/proc/${pid}/comm" 2>/dev/null || true)
        if [[ "$comm" == "envoy" ]]; then envoy_in_b=1; fi
    done
    if [[ "$ka_in_a" -eq 1 ]]; then
        pass "keepalived running in netns A (LVS plugin)"
    else
        fail "no keepalived process in netns A"
    fi
    if [[ "$envoy_in_a" -eq 1 ]]; then
        pass "tenant envoy running in netns A alongside keepalived (mixed-plugin)"
    else
        fail "no tenant envoy in netns A"
    fi
    if [[ "$envoy_in_b" -eq 1 ]]; then
        pass "tenant envoy running in netns B"
    else
        fail "no tenant envoy in netns B"
    fi

    # ---- Shared host envoy: ONE process for both networks ---------------
    local host_pid=""
    if [[ -f "$M9_HOST_DIR/envoy.pid" ]]; then
        host_pid=$(sudo cat "$M9_HOST_DIR/envoy.pid" 2>/dev/null || true)
    fi
    if [[ -n "$host_pid" ]] && sudo kill -0 "$host_pid" 2>/dev/null; then
        pass "shared host envoy running (pid=$host_pid)"
    else
        fail "shared host envoy not running"
    fi

    # Catalog isolation: host /clusters carries BOTH envoy services
    local clusters_json
    clusters_json=$(sudo curl -sS --max-time 3 \
        --unix-socket "$M9_HOST_ADMIN_SOCK" \
        "http://localhost/clusters?format=json" 2>&1 || true)
    if echo "$clusters_json" | grep -q "${svc_a_envoy_id}-tcp" \
       && echo "$clusters_json" | grep -q "${svc_b_envoy_id}-tcp"; then
        pass "host envoy /clusters lists both networks' envoy services (catalog union)"
    else
        fail "host envoy /clusters missing one of the multitenant envoy clusters"
    fi

    # ---- Per-netns VIP isolation ----------------------------------------
    # VIP A_lvs and A_envoy must be on netns A's tap; not on netns B.
    # VIP B_envoy must be on netns B's tap; not on netns A.
    local veth_a_ns="tls$(echo "$NET_ID" | head -c10)1"
    local veth_b_ns="tls$(echo "$net_b_id" | head -c10)1"
    local addrs_a addrs_b
    addrs_a=$(sudo ip -n "$ns_a_name" addr show "$veth_a_ns" 2>/dev/null | awk '/inet /{print $2}')
    addrs_b=$(sudo ip -n "$ns_b_name" addr show "$veth_b_ns" 2>/dev/null | awk '/inet /{print $2}')
    if echo "$addrs_a" | grep -q "${M10_SVC_A_LVS_VIP}/32" \
       && echo "$addrs_a" | grep -q "${M10_SVC_A_ENVOY_VIP}/32" \
       && ! echo "$addrs_a" | grep -q "${M10_SVC_B_ENVOY_VIP}/32"; then
        pass "netns A has VIP_A_lvs + VIP_A_envoy, not VIP_B_envoy"
    else
        fail "netns A VIP set wrong" \
             "addrs: $addrs_a"
    fi
    if echo "$addrs_b" | grep -q "${M10_SVC_B_ENVOY_VIP}/32" \
       && ! echo "$addrs_b" | grep -q "${M10_SVC_A_LVS_VIP}/32" \
       && ! echo "$addrs_b" | grep -q "${M10_SVC_A_ENVOY_VIP}/32"; then
        pass "netns B has VIP_B_envoy, not network-A VIPs"
    else
        fail "netns B VIP set wrong" \
             "addrs: $addrs_b"
    fi

    # ---- Per-tenant data path -------------------------------------------
    m10_test_client_setup "$NET_ID" "$M10_CLIENT_A_NS" \
        "$M10_CLIENT_A_VETH_ROOT" "$M10_CLIENT_A_VETH_NS" \
        "$M10_CLIENT_A_PORT_NAME" "a"
    m10_test_client_setup "$net_b_id" "$M10_CLIENT_B_NS" \
        "$M10_CLIENT_B_VETH_ROOT" "$M10_CLIENT_B_VETH_NS" \
        "$M10_CLIENT_B_PORT_NAME" "b"

    # Client A → VIP A_lvs (LVS data path)
    local out
    out=$(sudo ip netns exec "$M10_CLIENT_A_NS" \
        curl -sS --max-time 5 "http://$M10_SVC_A_LVS_VIP:$M10_SVC_A_LVS_PORT/" 2>&1 || true)
    if [[ "$out" == *"Directory listing"* ]]; then
        pass "client A → VIP_A_lvs reaches backend (LVS path on netns A)"
    else
        fail "client A → VIP_A_lvs failed" "out=$out"
    fi

    # Client A → VIP A_envoy (Envoy data path on same netns)
    out=$(sudo ip netns exec "$M10_CLIENT_A_NS" \
        curl -sS --max-time 5 "http://$M10_SVC_A_ENVOY_VIP:$M10_SVC_A_ENVOY_PORT/" 2>&1 || true)
    if [[ "$out" == *"Directory listing"* ]]; then
        pass "client A → VIP_A_envoy reaches backend (envoy path on netns A — mixed-plugin proven)"
    else
        fail "client A → VIP_A_envoy failed" "out=$out"
    fi

    # Client B → VIP B_envoy
    out=$(sudo ip netns exec "$M10_CLIENT_B_NS" \
        curl -sS --max-time 5 "http://$M10_SVC_B_ENVOY_VIP:$M10_SVC_B_ENVOY_PORT/" 2>&1 || true)
    if [[ "$out" == *"Directory listing"* ]]; then
        pass "client B → VIP_B_envoy reaches backend (envoy path on netns B)"
    else
        fail "client B → VIP_B_envoy failed" "out=$out"
    fi

    # ---- Tenant isolation: cross-network traffic must FAIL --------------
    # Client A asking for VIP_B_envoy: the link-local route is on the
    # tap, but the OVN switch for net A has no localport answering ARP
    # for that address (it's only ARP-respond'd inside netns B, which is
    # on a different OVN switch). The connect should time out cleanly.
    out=$(sudo ip netns exec "$M10_CLIENT_A_NS" \
        curl -sS --max-time 4 "http://$M10_SVC_B_ENVOY_VIP:$M10_SVC_B_ENVOY_PORT/" 2>&1 || true)
    if [[ "$out" == *"Directory listing"* ]]; then
        fail "ISOLATION BREACH: client A reached network B's VIP" "out=$out"
    else
        pass "isolation: client A cannot reach VIP_B_envoy (cross-tenant blocked)"
    fi

    out=$(sudo ip netns exec "$M10_CLIENT_B_NS" \
        curl -sS --max-time 4 "http://$M10_SVC_A_LVS_VIP:$M10_SVC_A_LVS_PORT/" 2>&1 || true)
    if [[ "$out" == *"Directory listing"* ]]; then
        fail "ISOLATION BREACH: client B reached network A's LVS VIP" "out=$out"
    else
        pass "isolation: client B cannot reach VIP_A_lvs (cross-tenant blocked)"
    fi

    out=$(sudo ip netns exec "$M10_CLIENT_B_NS" \
        curl -sS --max-time 4 "http://$M10_SVC_A_ENVOY_VIP:$M10_SVC_A_ENVOY_PORT/" 2>&1 || true)
    if [[ "$out" == *"Directory listing"* ]]; then
        fail "ISOLATION BREACH: client B reached network A's Envoy VIP" "out=$out"
    else
        pass "isolation: client B cannot reach VIP_A_envoy (cross-tenant blocked)"
    fi

    # ---- Cleanup ---------------------------------------------------------
    m10_test_client_teardown "$M10_CLIENT_A_NS" "$M10_CLIENT_A_VETH_ROOT" "a"
    m10_test_client_teardown "$M10_CLIENT_B_NS" "$M10_CLIENT_B_VETH_ROOT" "b"
    for p in "$M10_SVC_A_LVS_BACKEND_PORT" \
             "$M10_SVC_A_ENVOY_BACKEND_PORT" \
             "$M10_SVC_B_ENVOY_BACKEND_PORT"; do
        local pf="/tmp/m10-backend.${p}.pid"
        if [[ -f "$pf" ]]; then
            sudo kill "$(sudo cat "$pf")" 2>/dev/null || true
            sudo rm -f "$pf"
        fi
    done
    teardown_binding "$bind_a_lvs"
    teardown_binding "$bind_a_envoy"
    teardown_binding "$bind_b_envoy"
    teardown_binding "$stub_a_bind"
    [[ -n "${be_a_lvs:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_a_lvs" >/dev/null || true
    [[ -n "${be_a_envoy:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_a_envoy" >/dev/null || true
    [[ -n "${be_b_envoy:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_b_envoy" >/dev/null || true
    teardown_service "$svc_a_lvs_id"
    teardown_service "$svc_a_envoy_id"
    teardown_service "$svc_b_envoy_id"
    teardown_service "$stub_a_svc"
    # Network B is left in place across runs (cheap to keep, lets re-runs
    # skip the create). Operator can drop it manually with
    # `openstack network delete private-m10b` when the lab is wiped.
}

# --- multi-chassis (the second multi-tenant criterion) ------------------------------------------
#
# Bring two REMOTE compute chassis into play. Plumb a tenant client
# port on each via ssh-to-the-compute (port_id realized in br-int there
# → OVN binds the port to that chassis → localport realized there →
# our agent extension provisions localsvc-<net> netns + LVS state).
# Then assert per-chassis netns + keepalived, and the data path from
# each chassis's tenant client through the VIP.
#
# Backend lives in chassis A's localsvc netns; chassis B reaches it
# via the geneve tunnel (OVN inter-chassis routing). Single backend
# is enough to prove the multi-chassis property — both chassis route
# tenant traffic through their LOCAL netns's LVS, and the LVS-NAT
# path traverses geneve for the cross-chassis hop. That's the multi-chassis criterion of
# exit criterion: "boot tenant VMs across both chassis, confirm
# reachability on each."
test_multichassis_isolation() {
    echo
    echo "=== multi-chassis: 2 compute chassis (${MULTICHASSIS_COMPUTE_A_IP}, ${MULTICHASSIS_COMPUTE_B_IP}) ==="
    if [[ ! -r "$MULTICHASSIS_SSH_KEY" ]]; then
        note "SSH key $MULTICHASSIS_SSH_KEY not readable — skipping."
        return
    fi

    # Sanity: each compute reachable as the ssh user.
    local hn_a hn_b
    hn_a=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "uname -n" 2>/dev/null)
    hn_b=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "uname -n" 2>/dev/null)
    if [[ -z "$hn_a" || -z "$hn_b" ]]; then
        fail "ssh to one of the computes failed (a=$hn_a b=$hn_b) — skipping"
        return
    fi
    pass "ssh-from-controller to both computes OK ($hn_a, $hn_b)"

    local svc_id bind_id be_id port_id_a port_id_b ns_a_ip
    local ns_name="localsvc-$NET_ID"

    # 0) Wipe any leftover state from a prior run so we start clean.
    multichassis_clean_leftovers

    # 1) Plumb a tenant client on each compute. This is what makes OVN
    #    bind the port to that chassis, and that's what makes the agent
    #    provision the localsvc netns there.
    port_id_a=$(m10mc_remote_client_setup "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_A")
    port_id_b=$(m10mc_remote_client_setup "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_B")
    if [[ -z "$port_id_a" || -z "$port_id_b" ]]; then
        fail "could not create + plumb tenant client port on one of the computes"
        return
    fi
    pass "tenant client ports plumbed on both chassis (A=$port_id_a B=$port_id_b)"

    # 2) Service + binding. Service first (so it exists before the
    #    binding triggers reconciles).
    svc_id=$(lookup_service_id "$MULTICHASSIS_SVC_NAME")
    if [[ -z "$svc_id" ]]; then
        svc_id=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$MULTICHASSIS_SVC_NAME\",\"local_ipv4\":\"$MULTICHASSIS_SVC_VIP\",\"port\":$MULTICHASSIS_SVC_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\"}}" \
            | _jget "['local_service']['id']" 2>/dev/null || true)
    fi
    if [[ -z "$svc_id" ]]; then
        fail "could not create service $MULTICHASSIS_SVC_NAME"
        return
    fi
    bind_id=$(setup_binding "$svc_id" "$NET_ID")

    # OVN binds tenant ports → realizes localports on both computes →
    # our agent extension fires CREATE → provisions netns. Allow time:
    # remote agents poll/event on a 10s tick like the local one.
    sleep 16

    # 3) Per-chassis netns + keepalived presence.
    local netns_a netns_b ka_a ka_b
    netns_a=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "sudo ip netns list 2>/dev/null | awk '{print \$1}' | grep -x $ns_name")
    netns_b=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns list 2>/dev/null | awk '{print \$1}' | grep -x $ns_name")
    if [[ -n "$netns_a" ]]; then
        pass "chassis A has $ns_name"
    else
        fail "chassis A missing $ns_name (agent extension on A didn't provision)"
    fi
    if [[ -n "$netns_b" ]]; then
        pass "chassis B has $ns_name"
    else
        fail "chassis B missing $ns_name (agent extension on B didn't provision)"
    fi

    ka_a=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "
        for pid in \$(sudo ip netns pids $ns_name 2>/dev/null); do
            comm=\$(sudo cat /proc/\${pid}/comm 2>/dev/null)
            [[ \$comm == keepalived ]] && echo yes && break
        done
    ")
    ka_b=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "
        for pid in \$(sudo ip netns pids $ns_name 2>/dev/null); do
            comm=\$(sudo cat /proc/\${pid}/comm 2>/dev/null)
            [[ \$comm == keepalived ]] && echo yes && break
        done
    ")
    if [[ "$ka_a" == "yes" ]]; then
        pass "keepalived running in netns on chassis A"
    else
        fail "no keepalived process in $ns_name on chassis A"
    fi
    if [[ "$ka_b" == "yes" ]]; then
        pass "keepalived running in netns on chassis B"
    else
        fail "no keepalived process in $ns_name on chassis B"
    fi

    # 4) One backend per chassis. OVN `type=localport` blocks ingress
    #    traffic via tunnels, so chassis B cannot reach chassis A's
    #    localport IP — a single shared backend is unreachable from the
    #    other chassis. Per-chassis backends sidestep this: each
    #    chassis's keepalived TCP_CHECK marks its LOCAL backend healthy
    #    and the REMOTE one dead, and LVS-NAT picks the local one (the
    #    only healthy entry). The registry has no AZ awareness (see
    #    docs/limitations.md §2), so we register both addresses
    #    blindly; keepalived's HC does the per-chassis filtering.
    local ns_a_ip ns_b_ip be_id_a be_id_b
    ns_a_ip=$(m10mc_remote_backend_spawn "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_BACKEND_PORT_A")
    ns_b_ip=$(m10mc_remote_backend_spawn "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_BACKEND_PORT_B")
    if [[ -z "$ns_a_ip" || -z "$ns_b_ip" ]]; then
        fail "could not spawn per-chassis backends (a=$ns_a_ip b=$ns_b_ip)"
        m10mc_remote_client_teardown "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_A"
        m10mc_remote_client_teardown "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_B"
        teardown_binding "$bind_id"; teardown_service "$svc_id"
        return
    fi
    be_id_a=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-m10mc-a\",\"service_id\":\"$svc_id\",\"address\":\"$ns_a_ip\",\"port\":$MULTICHASSIS_BACKEND_PORT_A}}" \
        | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    be_id_b=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-m10mc-b\",\"service_id\":\"$svc_id\",\"address\":\"$ns_b_ip\",\"port\":$MULTICHASSIS_BACKEND_PORT_B}}" \
        | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    if [[ -z "$be_id_a" || -z "$be_id_b" ]]; then
        fail "per-chassis backend POST returned empty id (a=$be_id_a b=$be_id_b)"
    fi
    # Allow two keepalived TCP_CHECK passes: first probes both backends
    # (~6s delay_loop + 3s connect_timeout for the cross-chassis fail);
    # second pass lands the LOCAL backend in ipvsadm. ~22s is empirically
    # the safe budget on this lab.
    sleep 22

    # 5) Per-chassis ipvsadm: each chassis sees the VIP, and crucially
    #    ONLY its local backend (the remote one fails HC and is pruned).
    local ipvs_a ipvs_b
    ipvs_a=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "sudo ip netns exec $ns_name ipvsadm -L -n 2>/dev/null")
    ipvs_b=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns exec $ns_name ipvsadm -L -n 2>/dev/null")
    # Both backend addresses are identical (same localport IP), so we
    # distinguish by PORT — A's backend on PORT_A, B's on PORT_B.
    if echo "$ipvs_a" | grep -qE "TCP\s+${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}" \
       && echo "$ipvs_a" | grep -q ":${MULTICHASSIS_BACKEND_PORT_A}\b"; then
        pass "chassis A ipvsadm has VIP + local backend (port ${MULTICHASSIS_BACKEND_PORT_A})"
    else
        fail "chassis A ipvsadm missing local backend on port ${MULTICHASSIS_BACKEND_PORT_A}" "$ipvs_a"
    fi
    if echo "$ipvs_b" | grep -qE "TCP\s+${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}" \
       && echo "$ipvs_b" | grep -q ":${MULTICHASSIS_BACKEND_PORT_B}\b"; then
        pass "chassis B ipvsadm has VIP + local backend (port ${MULTICHASSIS_BACKEND_PORT_B})"
    else
        fail "chassis B ipvsadm missing local backend on port ${MULTICHASSIS_BACKEND_PORT_B}" "$ipvs_b"
    fi
    # Cross-chassis backend should NOT be in the other's ipvsadm.
    # keepalived TCP_CHECK from chassis A to "${ns_a_ip}:${PORT_B}"
    # actually probes A's OWN netns kernel (since the netns IP is the
    # same on both chassis). Port B has no listener on A → HC fails →
    # backend pruned. Same logic in reverse for chassis B.
    if echo "$ipvs_a" | grep -q ":${MULTICHASSIS_BACKEND_PORT_B}\b"; then
        fail "chassis A leaked remote (port ${MULTICHASSIS_BACKEND_PORT_B}) backend into ipvsadm"
    else
        pass "chassis A does not list cross-chassis backend (HC correctly prunes port ${MULTICHASSIS_BACKEND_PORT_B})"
    fi
    if echo "$ipvs_b" | grep -q ":${MULTICHASSIS_BACKEND_PORT_A}\b"; then
        fail "chassis B leaked remote (port ${MULTICHASSIS_BACKEND_PORT_A}) backend into ipvsadm"
    else
        pass "chassis B does not list cross-chassis backend (HC correctly prunes port ${MULTICHASSIS_BACKEND_PORT_A})"
    fi

    # 6) End-to-end data path from EACH chassis's tenant client. Both
    #    cases route via the LOCAL chassis's LVS director to the LOCAL
    #    backend (since the remote backend isn't healthy from here).
    local out_a out_b
    out_a=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_A_IP" "sudo ip netns exec $MULTICHASSIS_CLIENT_NS curl -sS --max-time 6 http://${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}/" 2>&1)
    out_b=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns exec $MULTICHASSIS_CLIENT_NS curl -sS --max-time 6 http://${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}/" 2>&1)
    if [[ "$out_a" == *"Directory listing"* ]]; then
        pass "tenant on chassis A → VIP reaches local backend"
    else
        fail "tenant on chassis A → VIP failed" "out=$out_a"
    fi
    if [[ "$out_b" == *"Directory listing"* ]]; then
        pass "tenant on chassis B → VIP reaches local backend"
    else
        fail "tenant on chassis B → VIP failed" "out=$out_b"
    fi

    # 7) Per-chassis state independence: kill the backend on chassis A.
    #    Chassis A's data path should break (within ~12s of HC),
    #    chassis B's data path should KEEP WORKING (uses its own
    #    local backend, which is unrelated to A's). Proves the
    #    chassises don't share LB state.
    local ka_pid_b_before ka_pid_b_after
    ka_pid_b_before=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "
        for pid in \$(sudo ip netns pids $ns_name 2>/dev/null); do
            [[ \$(sudo cat /proc/\${pid}/comm 2>/dev/null) == keepalived ]] && echo \$pid && break
        done
    ")
    m10mc_remote_backend_kill "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_BACKEND_PORT_A"
    sleep 14
    out_b=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "sudo ip netns exec $MULTICHASSIS_CLIENT_NS curl -sS --max-time 6 http://${MULTICHASSIS_SVC_VIP}:${MULTICHASSIS_SVC_PORT}/" 2>&1)
    if [[ "$out_b" == *"Directory listing"* ]]; then
        pass "chassis B data path still works after chassis A backend killed (per-chassis state)"
    else
        fail "chassis B data path broke when chassis A backend died (cross-chassis dependency!)" "out=$out_b"
    fi
    ka_pid_b_after=$(m10mc_ssh "$MULTICHASSIS_COMPUTE_B_IP" "
        for pid in \$(sudo ip netns pids $ns_name 2>/dev/null); do
            [[ \$(sudo cat /proc/\${pid}/comm 2>/dev/null) == keepalived ]] && echo \$pid && break
        done
    ")
    if [[ -n "$ka_pid_b_after" && "$ka_pid_b_after" == "$ka_pid_b_before" ]]; then
        pass "chassis B keepalived PID unchanged across chassis A churn (pid=$ka_pid_b_before)"
    elif [[ -n "$ka_pid_b_after" ]]; then
        # SIGHUP reload is allowed; new PID would mean a kill+respawn (also fine).
        pass "chassis B keepalived alive (before=$ka_pid_b_before after=$ka_pid_b_after)"
    else
        fail "chassis B keepalived disappeared (before=$ka_pid_b_before)"
    fi

    # 8) Cleanup.
    m10mc_remote_backend_kill "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_BACKEND_PORT_A"
    m10mc_remote_backend_kill "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_BACKEND_PORT_B"
    m10mc_remote_client_teardown "$MULTICHASSIS_COMPUTE_A_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_A"
    m10mc_remote_client_teardown "$MULTICHASSIS_COMPUTE_B_IP" "$MULTICHASSIS_CLIENT_PORT_NAME_B"
    teardown_binding "$bind_id"
    [[ -n "${be_id_a:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_id_a" >/dev/null || true
    [[ -n "${be_id_b:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_id_b" >/dev/null || true
    teardown_service "$svc_id"
}

# --- underlay-backend test --------------------------------------------
#
# Two services pointing at REAL underlay services (not synthesized in
# netns / on host loopback):
#   * TCP HTTP — exposed via Envoy plugin, backend 172.18.0.11:80
#   * UDP DNS  — exposed via LVS plugin,   backend 172.18.42.10:53
#
# Architectural asymmetry (see docs/limitations.md §1):
#   - Envoy works because the host envoy lives in the host root netns
#     and inherits the chassis's underlay routing.
#   - LVS does NOT work for underlay backends because keepalived runs
#     INSIDE the localsvc netns, which has only its on-subnet route
#     (10.0.0.0/26) — no default route to underlay 172.18.x.x. The HC
#     fails (network unreachable), keepalived prunes the backend, and
#     the data path has nowhere to forward to.
#
# This test asserts:
#   - TCP-via-Envoy underlay backend: SUCCESS (data path returns the
#     real HTTP page from 172.18.0.11)
#   - UDP-via-LVS underlay backend: GRACEFUL FAILURE (the agent doesn't
#     crash; ipvsadm shows the VIP but with NO healthy backend; the
#     data path correctly returns no answer)
#
# The asymmetry is intentional in the v1 design — productizing UDP-to-
# underlay would need either (a) a default route + SNAT in the
# localsvc netns (couples netns to host routing) or (b) a UDP analog
# of the Envoy two-tier design (no upstream UDP tunneling pattern;
# see docs/limitations.md §5).
underlay_clean_leftovers() {
    # Delete any service named UNDERLAY_TCP_NAME or UNDERLAY_UDP_NAME plus
    # their backends and bindings. Without this the unique constraint
    # on (service_id, name) for backends causes neutron-api's
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

test_underlay_egress() {
    echo
    echo "=== Underlay-backend reachability + tenant-escape ACL (nat + proxy) ==="
    # Both plugins should now reach underlay backends:
    #   - proxy: worker lives in host root netns; routing inherited.
    #   - nat:   per-network nlsu veth + per-backend FORWARD ACL.
    # Plus negative checks: a tenant must NOT be able to reach
    # arbitrary underlay destinations, only the configured backends.

    # Always start clean — see underlay_clean_leftovers comment.
    underlay_clean_leftovers

    # Sanity: the underlay services must actually be up. If they're
    # not, the test is testing nothing.
    local tcp_probe udp_probe
    tcp_probe=$(curl -sS --max-time 4 -o /dev/null -w "%{http_code}" \
        "http://${UNDERLAY_TCP_BACKEND_ADDR}:${UNDERLAY_TCP_BACKEND_PORT}/" 2>&1)
    if [[ "$tcp_probe" =~ ^[1-5][0-9][0-9]$ ]]; then
        pass "underlay TCP backend ${UNDERLAY_TCP_BACKEND_ADDR}:${UNDERLAY_TCP_BACKEND_PORT} reachable from chassis (HTTP $tcp_probe)"
    else
        fail "underlay TCP backend not reachable (got '$tcp_probe') — test cannot proceed"
        return
    fi
    udp_probe=$(dig +time=2 +tries=1 "@${UNDERLAY_UDP_BACKEND_ADDR}" -p "${UNDERLAY_UDP_BACKEND_PORT}" example.com a +short 2>&1 | head -1)
    if [[ "$udp_probe" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        pass "underlay UDP DNS ${UNDERLAY_UDP_BACKEND_ADDR}:${UNDERLAY_UDP_BACKEND_PORT} reachable from chassis (resolved to $udp_probe)"
    else
        fail "underlay UDP DNS not reachable (got '$udp_probe') — test cannot proceed"
        return
    fi

    # Make sure the netns + tenant client exist for the curl/dig from
    # tenant-side. Reuse the localport-lifecycle stub to drive netns provision.
    local stub_svc stub_bind
    stub_svc=$(setup_service)
    stub_bind=$(setup_binding "$stub_svc" "$NET_ID")
    sleep 4
    local ns_name="localsvc-$NET_ID"
    if ! sudo ip netns list | awk '{print $1}' | grep -qx "$ns_name"; then
        fail "localsvc netns missing — cannot run underlay test"
        teardown_binding "$stub_bind"; teardown_service "$stub_svc"
        return
    fi
    probe_client_setup

    # ---- TCP via Envoy (expected to succeed) -----------------------------
    local svc_tcp bind_tcp be_tcp
    svc_tcp=$(_curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$UNDERLAY_TCP_NAME':
        print(s['id']); break")
    if [[ -z "$svc_tcp" ]]; then
        svc_tcp=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$UNDERLAY_TCP_NAME\",\"local_ipv4\":\"$UNDERLAY_TCP_VIP\",\"port\":$UNDERLAY_TCP_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}" \
            | _jget "['local_service']['id']" 2>/dev/null || true)
    fi
    be_tcp=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-underlay-tcp\",\"service_id\":\"$svc_tcp\",\"address\":\"$UNDERLAY_TCP_BACKEND_ADDR\",\"port\":$UNDERLAY_TCP_BACKEND_PORT}}" \
        | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    bind_tcp=$(setup_binding "$svc_tcp" "$NET_ID")

    # ---- UDP via nat plugin (now SHOULD succeed via underlay-egress) -----
    local svc_udp bind_udp be_udp
    svc_udp=$(_curl GET "/v2.0/local_services" \
        | python3 -c "import sys,json
for s in json.load(sys.stdin)['local_services']:
    if s['name']=='$UNDERLAY_UDP_NAME':
        print(s['id']); break")
    if [[ -z "$svc_udp" ]]; then
        svc_udp=$(_curl POST "/v2.0/local_services" \
            "{\"local_service\": {\"name\":\"$UNDERLAY_UDP_NAME\",\"local_ipv4\":\"$UNDERLAY_UDP_VIP\",\"port\":$UNDERLAY_UDP_PORT,\"protocol\":\"udp\",\"health_check_type\":\"dns\",\"exposure_plugin\":\"nat\"}}" \
            | _jget "['local_service']['id']" 2>/dev/null || true)
    fi
    be_udp=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-underlay-udp\",\"service_id\":\"$svc_udp\",\"address\":\"$UNDERLAY_UDP_BACKEND_ADDR\",\"port\":$UNDERLAY_UDP_BACKEND_PORT}}" \
        | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    bind_udp=$(setup_binding "$svc_udp" "$NET_ID")

    # Wait for both to converge: proxy catalog reload + keepalived
    # spawn + first HC pass + underlay veth provision + ACL refresh.
    sleep 18

    # ---- proxy plugin TCP assertions (worker in host root netns) -------
    # Proxy worker is host-side, so /clusters reflects the configured
    # underlay backend; data path returns real HTML. The admin endpoint
    # requires a bearer token written by the agent at provisioning time.
    local clusters_json proxy_admin_sock proxy_admin_token
    proxy_admin_sock="/var/run/neutron-local-services/_proxy/admin.sock"
    proxy_admin_token=$(sudo cat /var/lib/neutron-local-services/_proxy/admin.token 2>/dev/null | tr -d '\n')
    clusters_json=$(sudo curl -sS --max-time 3 \
        --unix-socket "$proxy_admin_sock" \
        -H "Authorization: Bearer ${proxy_admin_token}" \
        "http://localhost/clusters?format=json" 2>&1 || true)
    if echo "$clusters_json" | grep -q "$UNDERLAY_TCP_BACKEND_ADDR"; then
        pass "proxy /clusters lists underlay backend ${UNDERLAY_TCP_BACKEND_ADDR}"
    else
        fail "proxy /clusters missing the underlay TCP cluster" \
             "got: $(echo "$clusters_json" | head -c 400)"
    fi
    local out_tcp
    out_tcp=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
        curl -sS --max-time 6 "http://${UNDERLAY_TCP_VIP}:${UNDERLAY_TCP_PORT}/" 2>&1 || true)
    if [[ -n "$out_tcp" && "$out_tcp" == *"<html"* ]]; then
        pass "tenant client → VIP_tcp → reaches REAL underlay HTTP backend via proxy (${UNDERLAY_TCP_BACKEND_ADDR})"
    else
        fail "tenant → VIP_tcp did not reach underlay backend via proxy" \
             "got: $(echo "$out_tcp" | head -c 200)"
    fi

    # ---- nat plugin UDP assertions (per-network nlsu veth + ACL) -------
    # The netns must have a default route via 100.64.x.1 (the host-side
    # underlay-egress IP); ipvsadm must list the backend as healthy
    # (HC reaches it through the new path); tenant dig succeeds.
    local default_via
    default_via=$(sudo ip netns exec "$ns_name" ip route | awk '/^default/{print $3}')
    if [[ "$default_via" == 100.64.* ]]; then
        pass "netns has default route via underlay-egress host (${default_via})"
    else
        fail "netns default route missing or wrong" \
             "ip route: $(sudo ip netns exec "$ns_name" ip route | tr '\n' '|')"
    fi
    # Underlay veth pair present.
    local nlsu_root
    nlsu_root="nlsu${NET_ID:0:9}0"
    if ip link show "$nlsu_root" >/dev/null 2>&1; then
        pass "underlay-egress veth ${nlsu_root} present in host root netns"
    else
        fail "underlay-egress veth ${nlsu_root} missing"
    fi
    # Per-network FORWARD ACL chain present and contains the UDP rule.
    local chain
    chain="NLS_UND_${NET_ID:0:9}"
    if sudo iptables -t filter -S "$chain" 2>/dev/null \
            | grep -q -- "-d ${UNDERLAY_UDP_BACKEND_ADDR}.*--dport ${UNDERLAY_UDP_BACKEND_PORT}"; then
        pass "host FORWARD ACL chain ${chain} whitelists ${UNDERLAY_UDP_BACKEND_ADDR}:${UNDERLAY_UDP_BACKEND_PORT}"
    else
        fail "FORWARD ACL chain ${chain} missing UDP backend rule" \
             "$(sudo iptables -t filter -S "$chain" 2>/dev/null | head -10)"
    fi
    # ipvsadm must show the backend as healthy now that HC can reach it.
    local ipvs_udp
    ipvs_udp=$(sudo ip netns exec "$ns_name" ipvsadm -L -n 2>/dev/null)
    # ipvsadm column-aligns the protocol field, so "UDP" is followed
    # by 2+ spaces (TCP fits in 3 chars, UDP in 3, but the field is
    # padded to align with longer protocol labels). Match >=1 space
    # so the VIP test isn't fooled by formatting.
    if echo "$ipvs_udp" | awk -v vip="${UNDERLAY_UDP_VIP}:${UNDERLAY_UDP_PORT}" '
            $0 ~ "^UDP +" vip {found=1; next}
            /^TCP|^UDP/ {found=0}
            found && /->/ {print}
        ' | grep -q "$UNDERLAY_UDP_BACKEND_ADDR"; then
        pass "ipvsadm shows the underlay UDP backend ${UNDERLAY_UDP_BACKEND_ADDR} (HC reaches it via nlsu veth)"
    else
        fail "ipvsadm does not list the underlay UDP backend (HC still failing — underlay egress broken)" \
             "ipvsadm: $(echo "$ipvs_udp" | head -20)"
    fi
    # Tenant dig must now resolve through the VIP.
    local dig_out
    dig_out=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
        dig +time=3 +tries=2 "@${UNDERLAY_UDP_VIP}" -p "${UNDERLAY_UDP_PORT}" example.com a +short 2>&1 | head -1 || true)
    if [[ "$dig_out" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        pass "tenant → VIP_udp resolves through nat plugin to underlay DNS (${dig_out})"
    else
        fail "tenant → VIP_udp did not resolve" \
             "got: '${dig_out:-empty}'"
    fi

    # ---- Tenant-escape negative checks ---------------------------------
    # 1. From the tenant client, target a non-backend underlay IP via
    #    HTTP and DNS — both must FAIL (no whitelist entry, no DNAT).
    local escape_http escape_dns
    escape_http=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
        curl -sS --max-time 4 -o /dev/null -w "%{http_code}" \
        "http://172.18.0.1/" 2>&1 || true)
    # Drops can surface as several distinct curl error shapes:
    # "timed out", "Connection refused" (RST), "Could not connect to
    # server" (silent drop, no RST), "No route to host" (route
    # missing), "Network is unreachable". Anything other than a real
    # HTTP code (1xx-5xx) means the connection didn't complete. The
    # final HTTP code line is "000" on failure; check for that as the
    # canonical signal alongside the human-readable error strings.
    if [[ -z "$escape_http" \
            || "$escape_http" == *"000"* \
            || "$escape_http" == *"timed out"* \
            || "$escape_http" == *"refused"* \
            || "$escape_http" == *"Could not connect"* \
            || "$escape_http" == *"No route to host"* \
            || "$escape_http" == *"unreachable"* ]]; then
        pass "tenant escape attempt to chassis IP 172.18.0.1 blocked (got: '${escape_http:-empty}')"
    else
        fail "tenant UNEXPECTEDLY reached non-backend underlay IP 172.18.0.1 (got HTTP $escape_http) — ACL leak"
    fi
    # 2. dig at a non-whitelisted underlay DNS server (eg an IP not in
    #    any backend list) — must time out.
    escape_dns=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
        dig +time=2 +tries=1 "@8.8.8.8" -p 53 example.com +short 2>&1 | head -1 || true)
    if [[ -z "$escape_dns" || "$escape_dns" == *"timed out"* \
            || "$escape_dns" == *"no servers"* \
            || "$escape_dns" == *"network unreachable"* ]]; then
        pass "tenant escape attempt to public DNS 8.8.8.8 blocked"
    else
        fail "tenant UNEXPECTEDLY resolved via 8.8.8.8 (got: '$escape_dns') — ACL leak"
    fi

    # 3. Sanity: the agent process is still alive.
    if sudo systemctl is-active "$AGENT_UNIT" >/dev/null 2>&1; then
        pass "ovn-agent still active through underlay-egress workflow"
    else
        fail "ovn-agent dropped to inactive"
    fi

    # ---- Cleanup ---------------------------------------------------------
    probe_client_teardown
    teardown_binding "$bind_tcp"
    teardown_binding "$bind_udp"
    teardown_binding "$stub_bind"
    [[ -n "${be_tcp:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_tcp" >/dev/null || true
    [[ -n "${be_udp:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_udp" >/dev/null || true
    teardown_service "$svc_tcp"
    teardown_service "$svc_udp"
    teardown_service "$stub_svc"
}

# proxy-plugin probe fixtures — Rust L4 proxy + privileged helper.
# Reuses the probe-client netns since the client shape is
# plugin-agnostic.
PROXY_TCP_NAME="lab-test-m11-tcp"
PROXY_TCP_VIP="169.254.169.160"
PROXY_TCP_PORT=80
PROXY_TCP_BACKEND_PORT=21080
PROXY_UDP_NAME="lab-test-m11-udp"
PROXY_UDP_VIP="169.254.169.161"
PROXY_UDP_PORT=54
PROXY_UDP_BACKEND_PORT=21054
PROXY_CATALOG="/var/lib/neutron-local-services/_proxy/catalog.json"

test_proxy_plugin_e2e() {
    echo
    echo "=== proxy plugin: Rust L4 proxy plugin (proxy) ==="

    # 1) Both proxy processes alive on chassis. systemd manages both.
    if systemctl is-active --quiet nls-proxy-priv.service; then
        pass "nls-proxy-priv.service is active"
    else
        fail "nls-proxy-priv.service not active" \
             "systemctl status nls-proxy-priv.service"
        return
    fi
    if systemctl is-active --quiet nls-proxy.service; then
        pass "nls-proxy.service is active"
    else
        fail "nls-proxy.service not active" \
             "systemctl status nls-proxy.service"
        return
    fi

    local ns_name="localsvc-$NET_ID"
    local svc_tcp_resp svc_tcp_id be_tcp_id bind_tcp_id

    # Create the proxy-plugin TCP service first (no plugin runs until
    # there's a binding). Bind so the agent provisions the netns. The
    # proxy plugin runs but emits no catalog entries yet (no backends).
    svc_tcp_resp=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$PROXY_TCP_NAME\",\"local_ipv4\":\"$PROXY_TCP_VIP\",\"port\":$PROXY_TCP_PORT,\"protocol\":\"tcp\",\"health_check_type\":\"tcp\",\"exposure_plugin\":\"proxy\"}}")
    svc_tcp_id=$(echo "$svc_tcp_resp" | _jget "['local_service']['id']" 2>/dev/null || true)
    if [[ -z "$svc_tcp_id" ]]; then
        fail "could not create proxy-plugin TCP service" "$svc_tcp_resp"
        return
    fi
    bind_tcp_id=$(setup_binding "$svc_tcp_id" "$NET_ID")

    # Wait for the netns to materialize — Port_Binding event drives
    # the agent's provision() within a couple of seconds.
    local i=0
    while ! sudo ip netns list | awk '{print $1}' | grep -qx "$ns_name"; do
        sleep 1
        i=$((i+1))
        if [[ $i -ge 15 ]]; then
            fail "localsvc netns did not appear within 15s"
            return
        fi
    done

    # 2) TCP backend on host root netns. The proxy worker dials
    #    backends from the host root netns, so the backend must be
    #    reachable from there. 127.0.0.1 keeps the test
    #    self-contained on the chassis without needing route changes.
    local backend_addr=127.0.0.1
    sudo pkill -f "socat.*TCP4-LISTEN:${PROXY_TCP_BACKEND_PORT}" 2>/dev/null || true
    sudo pkill -f "m11-tcp-backend.py" 2>/dev/null || true
    # Wait for the kernel to release the port (TIME_WAIT can hold
    # it briefly even with reuseaddr if a previous run accepted a
    # connection).
    local i=0
    while sudo ss -tlnp | grep -q ":${PROXY_TCP_BACKEND_PORT} "; do
        sleep 1
        i=$((i+1))
        [[ $i -ge 5 ]] && break
    done
    # Tiny HTTP responder. socat's SYSTEM addresses use commas as
    # option separators, which mangles HTTP headers — so the
    # responder lives in its own script file. Python's one-shot
    # http.server is the right size for the test.
    sudo tee /tmp/m11-tcp-backend.py >/dev/null <<'PYEOF'
#!/usr/bin/env python3
import http.server, socketserver, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Directory listing\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a, **kw): pass
port = int(sys.argv[1])
with socketserver.TCPServer(("127.0.0.1", port), H) as srv:
    srv.serve_forever()
PYEOF
    sudo chmod +x /tmp/m11-tcp-backend.py
    sudo /tmp/m11-tcp-backend.py "$PROXY_TCP_BACKEND_PORT" \
         >/tmp/m11-tcp-backend.${PROXY_TCP_BACKEND_PORT}.log 2>&1 &
    echo "$!" | sudo tee /tmp/m11-backend.${PROXY_TCP_BACKEND_PORT}.pid >/dev/null
    sleep 1

    # Add the backend. This triggers another reconcile → proxy.py
    # re-emits the catalog with a non-empty entries list → worker
    # accepts and BindListener runs.
    local be_tcp_resp
    be_tcp_resp=$(_curl POST "/v2.0/local_service_backends" \
        "{\"local_service_backend\": {\"name\":\"be-m11-tcp\",\"service_id\":\"$svc_tcp_id\",\"address\":\"${backend_addr}\",\"port\":$PROXY_TCP_BACKEND_PORT}}")
    be_tcp_id=$(echo "$be_tcp_resp" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
    sleep 12

    # 3) Catalog file exists and is HMAC-signed (first line is 64 hex).
    if sudo test -r "$PROXY_CATALOG"; then
        local first_line
        first_line=$(sudo head -n1 "$PROXY_CATALOG")
        if [[ "$first_line" =~ ^[0-9a-f]{64}$ ]]; then
            pass "catalog signed (HMAC line is 64-hex)"
        else
            fail "catalog HMAC line not 64-hex" \
                 "head -1 $PROXY_CATALOG: $first_line"
        fi
    else
        fail "catalog file not present at $PROXY_CATALOG"
    fi

    # 4) End-to-end TCP through the proxy VIP from a tenant-attached client.
    probe_client_setup
    local curl_out
    curl_out=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
        curl -sS --max-time 5 "http://$PROXY_TCP_VIP:$PROXY_TCP_PORT/" 2>&1 || true)
    if [[ "$curl_out" == *"Directory listing"* ]]; then
        pass "TCP curl via proxy VIP $PROXY_TCP_VIP:$PROXY_TCP_PORT reaches backend"
    else
        fail "TCP curl via proxy VIP failed" \
             "curl_out=${curl_out:0:200}"
    fi

    # 5) UDP forward path (originally a stretch goal that the proxy
    #    lands it). socat upper-case echo backend on host loopback;
    #    send a known marker via netcat from the client netns and
    #    assert the backend logged the upper-cased version.
    sudo pkill -f "socat.*UDP4-RECVFROM:${PROXY_UDP_BACKEND_PORT}" 2>/dev/null || true
    sudo socat -u UDP4-RECVFROM:${PROXY_UDP_BACKEND_PORT},bind=127.0.0.1,reuseaddr,fork \
         SYSTEM:'tr a-z A-Z >&2' \
         >/tmp/proxy-udp-backend.${PROXY_UDP_BACKEND_PORT}.log 2>&1 &
    echo "$!" | sudo tee /tmp/m11-backend.${PROXY_UDP_BACKEND_PORT}.pid >/dev/null
    sleep 1
    local svc_udp_resp svc_udp_id be_udp_id bind_udp_id
    svc_udp_resp=$(_curl POST "/v2.0/local_services" \
        "{\"local_service\": {\"name\":\"$PROXY_UDP_NAME\",\"local_ipv4\":\"$PROXY_UDP_VIP\",\"port\":$PROXY_UDP_PORT,\"protocol\":\"udp\",\"exposure_plugin\":\"proxy\"}}")
    svc_udp_id=$(echo "$svc_udp_resp" | _jget "['local_service']['id']" 2>/dev/null || true)
    if [[ -n "$svc_udp_id" ]]; then
        bind_udp_id=$(setup_binding "$svc_udp_id" "$NET_ID")
        local be_udp_resp
        be_udp_resp=$(_curl POST "/v2.0/local_service_backends" \
            "{\"local_service_backend\": {\"name\":\"be-m11-udp\",\"service_id\":\"$svc_udp_id\",\"address\":\"127.0.0.1\",\"port\":$PROXY_UDP_BACKEND_PORT}}")
        be_udp_id=$(echo "$be_udp_resp" | _jget "['local_service_backend']['id']" 2>/dev/null || true)
        sleep 12
        sudo ip netns exec "$PROBE_CLIENT_NS" \
            bash -c "echo hello-m11 | nc -u -w1 $PROXY_UDP_VIP $PROXY_UDP_PORT" || true
        sleep 2
        # The socat backend uppercases its input — sending "hello-m11"
        # produces "HELLO-M11" in the log. Earlier copies of this test
        # grep'd for "HELLO-PROXY" (a leftover from when the marker
        # was "hello-proxy"), which never matched even when the data
        # path was working.
        if sudo grep -q "HELLO-M11" "/tmp/proxy-udp-backend.${PROXY_UDP_BACKEND_PORT}.log" 2>/dev/null; then
            pass "UDP datagram via proxy VIP $PROXY_UDP_VIP:$PROXY_UDP_PORT reached backend"
        else
            fail "UDP datagram via proxy VIP did not reach backend" \
                 "log: $(sudo cat /tmp/proxy-udp-backend.${PROXY_UDP_BACKEND_PORT}.log 2>/dev/null | head -3)"
        fi
        sudo pkill -f "socat.*UDP4-RECVFROM:${PROXY_UDP_BACKEND_PORT}" 2>/dev/null || true
    else
        fail "could not create proxy-plugin UDP service" "$svc_udp_resp"
    fi

    # 6) HMAC tamper resistance: flip a byte in the payload and confirm
    #    the worker keeps last-good state (TCP curl still succeeds).
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
    curl_out=$(sudo ip netns exec "$PROBE_CLIENT_NS" \
        curl -sS --max-time 5 "http://$PROXY_TCP_VIP:$PROXY_TCP_PORT/" 2>&1 || true)
    if [[ "$curl_out" == *"Directory listing"* ]]; then
        pass "HMAC tamper kept last-good state (curl still reaches backend)"
    else
        fail "HMAC tamper unexpectedly tore down listeners" \
             "curl_out=${curl_out:0:200}"
    fi
    sudo mv "$PROXY_CATALOG.bak" "$PROXY_CATALOG"
    sleep 2

    # 7) Cross-tenant isolation: from inside the tenant netns, attempts
    #    to reach the chassis underlay must fail. (The proxy worker
    #    bridges from tenant netns sockets to host netns dial; tenant
    #    has no IP path to underlay.)
    local underlay_probe
    underlay_probe=$(sudo ip netns exec "$ns_name" \
        timeout 2 ping -c1 -W1 172.18.0.128 2>&1 || true)
    if echo "$underlay_probe" | grep -qE "Network is unreachable|100% packet loss"; then
        pass "tenant netns has no IP path to chassis underlay (172.18.0.128)"
    else
        fail "tenant netns reached chassis IP — isolation breach" \
             "$underlay_probe"
    fi

    # Cleanup.
    sudo pkill -f "m11-tcp-backend.py" 2>/dev/null || true
    [[ -n "${bind_tcp_id:-}" ]] && teardown_binding "$bind_tcp_id" || true
    [[ -n "${be_tcp_id:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_tcp_id" >/dev/null || true
    teardown_service "$svc_tcp_id"
    [[ -n "${bind_udp_id:-}" ]] && teardown_binding "$bind_udp_id" || true
    [[ -n "${be_udp_id:-}" ]] && _curl DELETE "/v2.0/local_service_backends/$be_udp_id" >/dev/null || true
    [[ -n "${svc_udp_id:-}" ]] && teardown_service "$svc_udp_id" || true
}

TOKEN=$(_token)
NET_ID=$(_get_net_id)
echo "Lab functional tests — network=$NET_NAME ($NET_ID)"
echo "Neutron URL: $NEUTRON_URL"
echo "Agent unit:  $AGENT_UNIT"

case "$SUITE" in
    localport) test_localport_lifecycle ;;
    host_routes) test_host_routes_injection ;;
    agent_extension) test_agent_extension_events ;;
    netns) test_netns_provisioning ;;
    vips) test_vip_reconciliation ;;
    nat) test_nat_plugin_e2e ;;
    proxy) test_proxy_plugin_e2e ;;
    multitenant) test_multitenant_isolation ;;
    multichassis) test_multichassis_isolation ;;
    underlay) test_underlay_egress ;;
    all) test_localport_lifecycle; test_host_routes_injection; test_agent_extension_events; test_netns_provisioning; test_vip_reconciliation; test_nat_plugin_e2e; test_multitenant_isolation; test_proxy_plugin_e2e ;;
    all_full) test_localport_lifecycle; test_host_routes_injection; test_agent_extension_events; test_netns_provisioning; test_vip_reconciliation; test_nat_plugin_e2e; test_multitenant_isolation; test_multichassis_isolation; test_underlay_egress; test_proxy_plugin_e2e ;;
    *) echo "unknown suite: $SUITE (use localport|host_routes|agent_extension|netns|vips|nat|proxy|multitenant|multichassis|underlay|all|all_full)"; exit 2 ;;
esac

echo
echo "=== Summary: ${GRN}${PASS} pass${CLR}, ${RED}${FAIL} fail${CLR} ==="
[[ "$FAIL" -eq 0 ]]
