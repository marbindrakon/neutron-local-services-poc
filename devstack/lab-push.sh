#!/usr/bin/env bash
#
# Push the local PoC repo to the lab DevStack instance and run the
# bootstrap. Run from the dev host (where this repo lives).
#
# Usage:
#   ./devstack/lab-push.sh [user@]<host>
#
# Defaults to almalinux@172.18.0.128 (the Alma 10 lab instance — see
# the published architecture docs).

set -euo pipefail

TARGET="${1:-almalinux@172.18.0.128}"
SSH_KEY="${SSH_KEY:-${HOME}/.ssh/neutron-localsvc-poc}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh_opts=(-i "${SSH_KEY}" -o StrictHostKeyChecking=no)

# Ensure /opt/stack exists with sane perms before rsync (the bootstrap
# script also does this, but we need the dir before we can rsync into it).
# After stack.sh has run once, /opt/stack/neutron-local-services is owned
# by `stack` (DevStack chowns its dirs), so we have to flip ownership to
# our SSH user for the rsync, then flip it back so the systemd
# devstack@neutron-api unit (which runs as stack) can keep reading the
# tree. Idempotent on first push too — chown of an absent path no-ops.
SSH_USER="${TARGET%@*}"
ssh "${ssh_opts[@]}" "${TARGET}" \
    "sudo mkdir -p /opt/stack && sudo chown ${SSH_USER}:${SSH_USER} /opt/stack && \
     ([ -d /opt/stack/neutron-local-services ] && \
        sudo chown -R ${SSH_USER}:${SSH_USER} /opt/stack/neutron-local-services || true)"

rsync -az --delete \
    --exclude='.venv/' --exclude='.tox/' --exclude='__pycache__/' \
    --exclude='*.pyc' --exclude='.pytest_cache/' \
    -e "ssh ${ssh_opts[*]}" \
    "${REPO_DIR}/" "${TARGET}:/opt/stack/neutron-local-services/"

# Flip ownership back to `stack` so devstack@neutron-api can still read
# the tree (and so subsequent unstack.sh / stack.sh runs see the right
# ownership). If the `stack` user doesn't exist yet (first-time bootstrap),
# skip silently — the bootstrap will create it and chown later.
ssh "${ssh_opts[@]}" "${TARGET}" \
    'id stack >/dev/null 2>&1 && sudo chown -R stack:stack /opt/stack/neutron-local-services || true'

# Hand off to the bootstrap script. Re-running is safe.
ssh "${ssh_opts[@]}" "${TARGET}" \
    'sudo NEUTRON_LOCAL_SERVICES_REPO=/opt/stack/neutron-local-services bash /opt/stack/neutron-local-services/devstack/lab-bootstrap.sh'

cat <<EOF

Bootstrap complete on ${TARGET}.

To run stack.sh:
  ssh -i ${SSH_KEY} ${TARGET}
  sudo -iu stack
  cd /opt/stack/devstack && ./stack.sh

EOF
