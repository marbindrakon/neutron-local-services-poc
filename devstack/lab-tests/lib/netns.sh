# shellcheck shell=bash
#
# Tenant-attached probe-client netns + backend-spawn helpers, used by the
# nat / proxy / underlay / multi-tenant cases. Kept as bash so cases can
# call `ip netns exec` directly without Python wrappers.

# A tenant-attached test client netns. Creates a Neutron port on
# $NET_ID, plumbs a veth into br-int with iface-id == port_id (the same
# binding shape OVN uses for VMs and localports), and puts the port's
# IP/MAC inside the netns. Result: a process running in the netns
# behaves exactly like a VM on the tenant network — needed to send
# traffic at the service VIP without SSH-ing into a real guest.
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
    # 121 (host_routes injection). Our ad-hoc netns doesn't speak DHCP,
    # so add a wide link-local-onlink route so the kernel ARPs for any
    # 169.254/16 VIP on this veth — the localsvc netns owns the /32s
    # and ARP-responds.
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
# bound to the netns's own subnet IP. The lab tests use this in lieu of
# running a real backend host: the localsvc netns already has tenant-
# network reachability so this satisfies LVS-NAT routing without extra
# plumbing. PID is written to /tmp/m8-backend.<port>.pid for teardown.
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
    # useful proof that the request actually reached the backend (the
    # upper-case is the marker), not just that the VIP is up.
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
    # The localsvc netns's IP is the localport's fixed_ip. We need it to
    # point backends at, since "localhost from the perspective of the
    # LVS director" is the netns's own subnet IP.
    local ns_name="localsvc-$NET_ID"
    local veth="tls$(echo "$NET_ID" | head -c10)1"
    sudo ip -n "$ns_name" -4 -o addr show "$veth" 2>/dev/null \
        | awk '/inet /{print $4}' | grep -v '/32' \
        | head -1 | cut -d/ -f1
}

# --- Multi-tenant client helpers ----------------------------------------
# Parametrized variant of probe_client_setup for the multi-tenant suite,
# which needs one client per tenant network.

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

m10_get_localsvc_ns_ip_for() {
    # Same trick as m8_get_localsvc_ns_ip but parametric on the network.
    local net_id="$1"
    local ns_name="localsvc-$net_id"
    local veth="tls$(echo "$net_id" | head -c10)1"
    sudo ip -n "$ns_name" -4 -o addr show "$veth" 2>/dev/null \
        | awk '/inet /{print $4}' | grep -v '/32' \
        | head -1 | cut -d/ -f1
}
