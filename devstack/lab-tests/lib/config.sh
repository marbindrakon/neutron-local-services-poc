# shellcheck shell=bash
#
# Shared lab-test configuration: env-overridable connection details and
# per-test fixture names. Sourced by lib/case.sh before any test body.

# --- Connection ----------------------------------------------------------
NET_NAME="${NET_NAME:-private}"
NEUTRON_URL="${NEUTRON_URL:-http://172.18.0.128/networking}"
AGENT_UNIT="${AGENT_UNIT:-devstack@q-ovn-agent}"
SERVER_UNIT="${SERVER_UNIT:-devstack@neutron-api}"
OS_CLOUD_NAME="${OS_CLOUD_NAME:-devstack-admin}"
OS_BIN="${OS_BIN:-/usr/local/bin/openstack}"
OVN_NBCTL="${OVN_NBCTL:-sudo ovn-nbctl}"

# --- Baseline DNS-VIP service used by lifecycle / host_routes / vips ----
SVC_NAME="lab-test-dns-vip"
SVC_VIP="169.254.169.123"
SVC_PORT=53
SVC_PROTO="udp"

# --- nat-plugin probe fixtures -------------------------------------------
# Distinct VIPs so a stale binding can't poison the baseline. Service IDs
# are looked up by name so re-runs are idempotent.
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

# --- Multi-tenant isolation fixtures ------------------------------------
# Two tenant networks on the same chassis (private + private-m10b), three
# services exercising both axes:
#   - svc_a_lvs:   network A, LVS plugin
#   - svc_a_envoy: network A, Envoy plugin (mixed-plugin in one netns)
#   - svc_b_envoy: network B, Envoy plugin (multi-tenant)
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

M10_CLIENT_A_NS="m10a-client"
M10_CLIENT_A_VETH_ROOT="m10a0"
M10_CLIENT_A_VETH_NS="m10a1"
M10_CLIENT_A_PORT_NAME="m10a-client-probe"
M10_CLIENT_B_NS="m10b-client"
M10_CLIENT_B_VETH_ROOT="m10b0"
M10_CLIENT_B_VETH_NS="m10b1"
M10_CLIENT_B_PORT_NAME="m10b-client-probe"

# --- Multi-chassis fixture ----------------------------------------------
# Two REMOTE compute chassis. Compute IPs come from env so the lab
# inventory isn't baked into the script. Defaults match the c1/c2
# compute nodes provisioned via local.conf.compute.sample.
MULTICHASSIS_COMPUTE_A_IP="${MULTICHASSIS_COMPUTE_A_IP:-172.18.0.152}"
MULTICHASSIS_COMPUTE_B_IP="${MULTICHASSIS_COMPUTE_B_IP:-172.18.0.144}"
MULTICHASSIS_SSH_KEY="${MULTICHASSIS_SSH_KEY:-/home/stack/.ssh/m10mc-key}"
MULTICHASSIS_SSH_USER="${MULTICHASSIS_SSH_USER:-almalinux}"
MULTICHASSIS_SVC_NAME="lab-test-m10mc"
MULTICHASSIS_SVC_VIP="169.254.169.160"
MULTICHASSIS_SVC_PORT=80
# Per-chassis backend ports — distinct so each keepalived's TCP_CHECK
# can distinguish the local from the remote backend. Both backends bind
# to the netns IP (which is the SAME address on both chassis because OVN
# realizes type=localport with one fixed_ip per network — the netns IP
# distinguishes the chassis only because the kernel inside each netns is
# independent).
MULTICHASSIS_BACKEND_PORT_A=20180
MULTICHASSIS_BACKEND_PORT_B=20181
MULTICHASSIS_CLIENT_NS="m10mc-client"
MULTICHASSIS_CLIENT_VETH_ROOT="m10mc0"
MULTICHASSIS_CLIENT_VETH_NS="m10mc1"
MULTICHASSIS_CLIENT_PORT_NAME_A="m10mc-client-a"
MULTICHASSIS_CLIENT_PORT_NAME_B="m10mc-client-b"

# --- Underlay-backend fixture -------------------------------------------
# Services backed by REAL underlay services (not synthesized in netns).
# TCP backend is the lab's HTTP service at 172.18.0.11:80; UDP is the
# lab's DNS at 172.18.42.10:53. Both reachable from any chassis's host
# root netns.
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

# --- proxy-plugin fixtures ----------------------------------------------
# Rust L4 proxy + privileged helper. Reuses the probe-client netns since
# the client shape is plugin-agnostic.
#
# NOTE: PROXY_TCP_VIP overlaps MULTICHASSIS_SVC_VIP at 169.254.169.160 in
# the original monolith. The runner serializes cases so the collision is
# latent today; revisit when adding parallelism. Pre-existing collision
# preserved here for drop-in compatibility.
PROXY_TCP_NAME="lab-test-m11-tcp"
PROXY_TCP_VIP="169.254.169.160"
PROXY_TCP_PORT=80
PROXY_TCP_BACKEND_PORT=21080
PROXY_UDP_NAME="lab-test-m11-udp"
PROXY_UDP_VIP="169.254.169.161"
PROXY_UDP_PORT=54
PROXY_UDP_BACKEND_PORT=21054
PROXY_CATALOG="/var/lib/neutron-local-services/_proxy/catalog.json"
