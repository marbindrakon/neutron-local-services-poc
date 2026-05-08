# shellcheck shell=bash
#
# Helpers for driving REMOTE compute chassis from the controller. Used
# by case 08-multichassis-isolation and (cleanup-only) any case that
# needs to wipe leftover multichassis state.

# Run a command on a remote compute as $MULTICHASSIS_SSH_USER. We're
# already stack on the controller; the m10mc-key was placed in
# ~stack/.ssh by the lab operator. Returns the command's stdout.
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
