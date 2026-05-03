#!/usr/bin/env bash
#
# Lab DevStack bootstrap.
#
# Run this on the lab instance (almalinux@neutron-localsvc-devstack)
# *as root* via sudo. It installs prerequisites, creates the stack
# user, clones DevStack and our PoC, drops a working local.conf, and
# kicks off ./stack.sh as the stack user.
#
# Idempotent: re-running is safe; existing checkouts are skipped.

set -euxo pipefail

REPO_URL="${NEUTRON_LOCAL_SERVICES_REPO:-https://opendev.org/openstack/neutron-local-services}"
REPO_REF="${NEUTRON_LOCAL_SERVICES_REF:-main}"
DEVSTACK_BRANCH="${DEVSTACK_BRANCH:-master}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "must run as root (or via sudo)" >&2
    exit 1
fi

# Prereqs. AlmaLinux 10 (py3.12). EPEL + CRB cover most build deps.
# RabbitMQ isn't in EPEL10, so pull RDO master deps repo, which
# includes [centos10-rabbitmq] for rabbitmq-server.
dnf install -y epel-release
dnf config-manager --set-enabled crb
dnf config-manager --add-repo \
    https://trunk.rdoproject.org/centos10-master/delorean-deps.repo
dnf install -y git python3-pip jq

# kernel-modules-extra ships xt_MASQUERADE, needed by DevStack's
# `iptables -t nat -A POSTROUTING ... -j MASQUERADE` calls. The base
# image's running kernel may not have a matching modules-extra; if a
# newer kernel was installed by `dnf upgrade`, a reboot is required
# before stack.sh can succeed.
dnf install -y "kernel-modules-extra-$(uname -r)" || \
    dnf install -y kernel-modules-extra
echo xt_MASQUERADE > /etc/modules-load.d/devstack-iptables.conf
modprobe xt_MASQUERADE 2>/dev/null || \
    echo "WARNING: xt_MASQUERADE not loadable on running kernel; reboot needed"

# Stack user.
if ! getent passwd stack >/dev/null; then
    useradd -s /bin/bash -d /opt/stack -m stack
fi
echo "stack ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/stack
chmod 0440 /etc/sudoers.d/stack

# /opt/stack already exists from the rsync — chown to stack so the
# user can clone DevStack alongside the rsynced PoC. The PoC dir
# itself is also reassigned so devstack's `git checkout` can run.
chown -R stack:stack /opt/stack

# Stack home + clones.
sudo -u stack -H bash -euxo pipefail <<EOSU
cd /opt/stack
[[ -d devstack ]] || git clone -b "${DEVSTACK_BRANCH}" https://opendev.org/openstack/devstack
[[ -d neutron-local-services ]] || git clone "${REPO_URL}" neutron-local-services
cd neutron-local-services && git checkout "${REPO_REF}"
EOSU

# Drop in a local.conf if absent (we ship a sample with the repo).
if [[ ! -f /opt/stack/devstack/local.conf ]]; then
    cp /opt/stack/neutron-local-services/devstack/local.conf.sample \
        /opt/stack/devstack/local.conf
    chown stack:stack /opt/stack/devstack/local.conf
fi

echo "bootstrap done. Run as stack:"
echo "  sudo -iu stack"
echo "  cd /opt/stack/devstack && ./stack.sh"
