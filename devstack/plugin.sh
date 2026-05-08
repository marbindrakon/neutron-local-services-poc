#!/usr/bin/env bash
#
# DevStack plugin for neutron-local-services.
#
# Phases:
#   install        — pip-install our package editable
#   post-config    — register the service plugin and the ovn-agent extension
#   extra          — create the agent state directory and run db migrations
#

NEUTRON_LOCAL_SERVICES_DIR=${NEUTRON_LOCAL_SERVICES_DIR:-${DEST}/neutron-local-services}
NEUTRON_LOCAL_SERVICES_STATE_DIR=${NEUTRON_LOCAL_SERVICES_STATE_DIR:-/var/lib/neutron-local-services}

function install_neutron_local_services {
    pip_install -e "${NEUTRON_LOCAL_SERVICES_DIR}"

    # dependencies: keepalived drives ip_vs in each per-network
    # netns, ipvsadm is the human-debuggable view of the kernel state,
    # iptables-services / iptables-nft / xt_conntrack are needed for
    # the LVS-NAT POSTROUTING MASQUERADE the plugin installs.
    if is_fedora; then
        install_package keepalived ipvsadm iptables-nft conntrack-tools \
            bind-utils chrony curl
    else
        install_package keepalived ipvsadm iptables conntrack \
            dnsutils sntp curl
    fi

    # — build and install the Rust proxy binaries that replace
    # the envoy plugin. Two binaries: a small privileged helper
    # and the unprivileged worker. Both ship as systemd units; they
    # come up after install_proxy_binaries / configure_proxy_systemd
    # have run.
    install_proxy_binaries
}

function install_proxy_binaries {
    # Need a Rust toolchain to compile proxy/. Install via the
    # distro's rustup if missing — rustup gives us a moving stable
    # toolchain that matches what proxy/rust-toolchain.toml asks for
    # without trying to track the package-manager version.
    if ! command -v cargo >/dev/null 2>&1; then
        if is_fedora; then
            sudo dnf install -y rustup gcc pkgconfig openssl-devel || true
            command -v rustup-init >/dev/null 2>&1 && rustup-init -y --default-toolchain stable --profile minimal --no-modify-path || true
        else
            sudo apt-get install -y rustup build-essential pkg-config libssl-dev || true
            command -v rustup-init >/dev/null 2>&1 && rustup-init -y --default-toolchain stable --profile minimal --no-modify-path || true
        fi
    fi
    # Make `cargo` reachable for this script if rustup just installed it.
    if ! command -v cargo >/dev/null 2>&1 && [[ -x "${HOME}/.cargo/bin/cargo" ]]; then
        export PATH="${HOME}/.cargo/bin:${PATH}"
    fi
    if ! command -v cargo >/dev/null 2>&1; then
        echo "ERROR: cargo not on \$PATH; cannot build the proxy" >&2
        return 1
    fi

    pushd "${NEUTRON_LOCAL_SERVICES_DIR}/proxy" >/dev/null
    cargo build --release --workspace
    sudo install -m 0755 target/release/nls-proxy /usr/local/bin/nls-proxy
    sudo install -m 0755 target/release/nls-proxy-priv /usr/local/bin/nls-proxy-priv
    popd >/dev/null

    install_proxy_systemd_units
    reserve_udp_ephemeral_range
}

function install_proxy_systemd_units {
    # Group used to mediate access to the worker's admin and control
    # sockets. The agent's STACK_USER is added to it so the agent can
    # connect over both. Idempotent: getent first, groupadd if absent.
    if ! getent group nls-admin >/dev/null; then
        sudo groupadd --system nls-admin
    fi
    sudo usermod -aG nls-admin "${STACK_USER}" || true

    # Privileged helper. Has only the two caps it strictly needs.
    sudo tee /etc/systemd/system/nls-proxy-priv.service >/dev/null <<EOF
[Unit]
Description=neutron-local-services privileged netns/bind helper
After=network.target

[Service]
ExecStart=/usr/local/bin/nls-proxy-priv
Type=simple
Restart=on-failure
RestartSec=2

# euid=root for setns + low-port bind, but egid=nls-admin so that
# (a) systemd creates RuntimeDirectory below as ``root:nls-admin``,
# closing a startup race against the worker (which depends on the
# dir being group-writable by nls-admin); (b) sockets the worker
# creates inside the dir inherit nls-admin via the setgid bit,
# keeping them reachable by the agent (also nls-admin).
User=root
Group=nls-admin

# Caps. setns + low-port bind cover the priv helper's spawn path.
# CAP_CHOWN lets ExecStartPost chgrp the runtime dir below to
# nls-admin so the worker (running as ${STACK_USER}, member of
# nls-admin) can bind sockets there. CAP_DAC_READ_SEARCH lets the
# helper read the agent-owned 0400 nonce file at bind time
# without needing a wider DAC override.
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_NET_BIND_SERVICE CAP_CHOWN CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_SYS_ADMIN CAP_NET_BIND_SERVICE CAP_CHOWN CAP_DAC_READ_SEARCH
NoNewPrivileges=yes

# Filesystem hardening. The helper only needs /run for the unix
# socket and /proc for /proc/self/fd; the rest is read-only.
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RuntimeDirectory=neutron-local-services/_proxy
RuntimeDirectoryMode=2770
RuntimeDirectoryPreserve=yes
# Belt-and-suspenders chgrp. Group= above already makes systemd
# create the dir as nls-admin; this ExecStartPost is a no-op on
# steady-state but recovers if the unit was re-applied without
# Group= set (older revisions of this script).
ExecStartPost=/bin/chgrp nls-admin /run/neutron-local-services/_proxy

# Resource caps. The priv helper spawns one short-lived bind-helper
# thread per BindListener and otherwise idles; these are generous
# enough to never trip on a real chassis but bound the worst-case
# blast radius from a runaway loop or a buggy peer.
TasksMax=256
LimitNOFILE=8192
MemoryMax=256M

[Install]
WantedBy=multi-user.target
EOF

    # Worker. Zero capabilities. All worker hardening lives here.
    sudo tee /etc/systemd/system/nls-proxy.service >/dev/null <<EOF
[Unit]
Description=neutron-local-services L4 proxy worker
Requires=nls-proxy-priv.service
After=nls-proxy-priv.service

[Service]
ExecStart=/usr/local/bin/nls-proxy
Type=simple
Restart=on-failure
RestartSec=2

# Run as the agent's user so the worker can read the agent-owned
# HMAC key and admin token (both 0400 stack:stack). nls-admin
# membership is what lets the worker bind its admin/control
# sockets inside the priv helper's RuntimeDirectory.
User=${STACK_USER}
Group=${STACK_USER}
SupplementaryGroups=nls-admin

# Drop everything; the worker holds no caps and never calls setns.
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
RestrictNamespaces=net
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources

# Filesystem
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/neutron-local-services/_proxy /var/run/neutron-local-services/_proxy

# Resource caps. The worker holds one tokio thread per tenant
# netns plus a small static set (admin, hc, watchdog, control
# accept). Real chassis deployments are O(100) tenants; these
# caps sit ~10x above that to bound runaway behavior without
# tripping on legitimate growth. Each TCP/UDP listener can hold
# up to max_concurrent (default 1000) sockets, so LimitNOFILE
# has to scale with listener count.
TasksMax=4096
LimitNOFILE=1048576
MemoryMax=2G

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable nls-proxy-priv.service nls-proxy.service
}

function reserve_udp_ephemeral_range {
    # Reserve a fixed range so the worker's per-session UDP backend
    # sockets don't collide with anything else on the chassis.
    # Range is configurable; default 50000-50999.
    local range="${NEUTRON_LOCAL_SERVICES_UDP_RESERVED:-50000-50999}"
    sudo sysctl -w "net.ipv4.ip_local_reserved_ports=${range}" || true
    # Persist across reboots.
    sudo tee /etc/sysctl.d/90-nls-proxy.conf >/dev/null <<EOF
net.ipv4.ip_local_reserved_ports = ${range}
EOF
}

function _append_unique_csv {
    # Append a value to a comma-separated list inside an INI file,
    # preserving existing entries. Args: file section opt newval.
    local file=$1 section=$2 opt=$3 newval=$4
    local existing
    existing=$(iniget "${file}" "${section}" "${opt}" || true)
    if [[ -z "${existing}" ]]; then
        iniset "${file}" "${section}" "${opt}" "${newval}"
    elif [[ ",${existing}," != *",${newval},"* ]]; then
        iniset "${file}" "${section}" "${opt}" "${existing},${newval}"
    fi
}

function configure_neutron_local_services {
    # Register the service plugin alongside whatever else is enabled.
    neutron_service_plugin_class_add "local_services"

    # Append our extension to the ovn-agent's extensions list. The
    # ovn-agent reads /etc/neutron/plugins/ml2/ovn_agent.ini in addition
    # to /etc/neutron/neutron.conf. Putting [agent] extensions in the
    # agent-specific ini matches the metadata extension's pattern.
    local agent_conf=/etc/neutron/plugins/ml2/ovn_agent.ini
    sudo mkdir -p "$(dirname "${agent_conf}")"
    sudo touch "${agent_conf}"
    sudo chown "${STACK_USER}:${STACK_USER}" "${agent_conf}"
    _append_unique_csv "${agent_conf}" agent extensions local_services

    # Register an oslo.config opt section that our plugin reads. Goes in
    # the API-server config (neutron.conf) since the plugin runs in
    # neutron-server. Reconciler interval goes into the agent ini too —
    # the agent reads it for its periodic VIP poll.
    iniset /etc/neutron/neutron.conf local_services \
        reconciler_interval "${NEUTRON_LOCAL_SERVICES_RECONCILER_INTERVAL}"
    iniset "${agent_conf}" local_services \
        reconciler_interval "${NEUTRON_LOCAL_SERVICES_RECONCILER_INTERVAL}"

    # Keystone auth for the agent's REST poll back to the local-services
    # API. Reuse the admin creds DevStack already exports — same
    # pattern the metadata agent uses for its nova client. Production
    # would scope this to a service account with read-only policy on
    # local_service_bindings / local_services.
    iniset "${agent_conf}" local_services_agent \
        auth_type "${NEUTRON_LOCAL_SERVICES_AGENT_AUTH_TYPE:-password}"
    iniset "${agent_conf}" local_services_agent \
        auth_url "${KEYSTONE_AUTH_URI}"
    iniset "${agent_conf}" local_services_agent \
        username "${OS_USERNAME:-admin}"
    iniset "${agent_conf}" local_services_agent \
        password "${OS_PASSWORD:-$ADMIN_PASSWORD}"
    iniset "${agent_conf}" local_services_agent \
        project_name "${OS_PROJECT_NAME:-admin}"
    iniset "${agent_conf}" local_services_agent \
        user_domain_id "${OS_USER_DOMAIN_ID:-default}"
    iniset "${agent_conf}" local_services_agent \
        project_domain_id "${OS_PROJECT_DOMAIN_ID:-default}"
    iniset "${agent_conf}" local_services_agent \
        region_name "${REGION_NAME:-RegionOne}"
}

function init_neutron_local_services {
    sudo mkdir -p "${NEUTRON_LOCAL_SERVICES_STATE_DIR}"
    sudo chown "${STACK_USER}:${STACK_USER}" "${NEUTRON_LOCAL_SERVICES_STATE_DIR}"

    # Per-plugin scratch dirs under the same prefix as the state dir
    # so all on-host artifacts cluster nicely. The proxy plugin
    # needs ${state}/_proxy for the catalog, HMAC key, and admin
    # token; /var/run/.../_proxy holds unix sockets and is owned by
    # the priv helper's RuntimeDirectory directive.
    sudo mkdir -p /var/log/neutron-local-services /var/run/neutron-local-services \
                  "${NEUTRON_LOCAL_SERVICES_STATE_DIR}/_proxy"
    sudo chown "${STACK_USER}:${STACK_USER}" \
        /var/log/neutron-local-services /var/run/neutron-local-services

    # The agent (running as STACK_USER) writes catalog.json, hmac.key,
    # admin.token, and nonces/* here; the worker (also STACK_USER)
    # reads them. Mode 0750 keeps them off other accounts.
    sudo chown "${STACK_USER}:${STACK_USER}" \
        "${NEUTRON_LOCAL_SERVICES_STATE_DIR}/_proxy"
    sudo chmod 0750 "${NEUTRON_LOCAL_SERVICES_STATE_DIR}/_proxy"

    # Pre-seed the HMAC key and admin token so the worker doesn't
    # exit on first start when neither file exists yet (the agent
    # creates them lazily on first apply, but the worker's startup
    # read fails if it races ahead). Idempotent: skip if present.
    if [[ ! -f "${NEUTRON_LOCAL_SERVICES_STATE_DIR}/_proxy/hmac.key" ]]; then
        sudo -u "${STACK_USER}" python3 -c "
import os, secrets
fd = os.open('${NEUTRON_LOCAL_SERVICES_STATE_DIR}/_proxy/hmac.key',
             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
os.write(fd, secrets.token_bytes(32)); os.close(fd)
"
    fi
    if [[ ! -f "${NEUTRON_LOCAL_SERVICES_STATE_DIR}/_proxy/admin.token" ]]; then
        sudo -u "${STACK_USER}" python3 -c "
import os, secrets
fd = os.open('${NEUTRON_LOCAL_SERVICES_STATE_DIR}/_proxy/admin.token',
             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
os.write(fd, secrets.token_urlsafe(32).encode() + b'\n'); os.close(fd)
"
    fi

    # Bring the proxy services up. Order matters: nls-proxy depends
    # on nls-proxy-priv via Requires=. systemd starts both.
    sudo systemctl restart nls-proxy-priv.service nls-proxy.service || true

    # Apply our alembic branch. neutron-db-manage discovers subprojects
    # via the neutron.db.alembic_migrations entry point, which our
    # setup.cfg registers.
    $NEUTRON_BIN_DIR/neutron-db-manage --subproject neutron-local-services upgrade head
}

if [[ "$1" == "stack" ]]; then
    case "$2" in
        install)
            echo_summary "Installing neutron-local-services"
            install_neutron_local_services
            ;;
        post-config)
            echo_summary "Configuring neutron-local-services"
            configure_neutron_local_services
            ;;
        extra)
            echo_summary "Initializing neutron-local-services"
            init_neutron_local_services
            ;;
    esac
elif [[ "$1" == "unstack" ]]; then
    :
elif [[ "$1" == "clean" ]]; then
    sudo systemctl stop nls-proxy.service nls-proxy-priv.service 2>/dev/null || true
    sudo systemctl disable nls-proxy.service nls-proxy-priv.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/nls-proxy.service \
              /etc/systemd/system/nls-proxy-priv.service \
              /etc/sysctl.d/90-nls-proxy.conf
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo rm -f /usr/local/bin/nls-proxy /usr/local/bin/nls-proxy-priv
    sudo rm -rf "${NEUTRON_LOCAL_SERVICES_STATE_DIR}" \
                /var/run/neutron-local-services
fi
