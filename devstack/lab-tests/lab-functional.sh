#!/usr/bin/env bash
#
# Run the lab functional tests against a remote DevStack instance.
# Designed to be run from this dev host after `lab-push.sh` succeeds.
#
# Usage:
#   ./devstack/lab-tests/lab-functional.sh [milestone] [user@host]
#
# Examples:
#   ./devstack/lab-tests/lab-functional.sh           # 'all', default host
#   ./devstack/lab-tests/lab-functional.sh m5
#   ./devstack/lab-tests/lab-functional.sh all almalinux@172.18.0.128
#
# What it does: ssh's the runner script (run-on-lab.sh) onto the lab,
# executes it as the `stack` user, and streams the output back. The
# runner is idempotent and self-cleaning, so re-running is fine.

set -euo pipefail

MILESTONE="${1:-all}"
TARGET="${2:-almalinux@172.18.0.128}"
SSH_KEY="${SSH_KEY:-${HOME}/.ssh/neutron-localsvc-poc}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ssh_opts=(-i "${SSH_KEY}" -o StrictHostKeyChecking=no)

# Push the runner up. /tmp is fine since the script is harmless and self-
# cleaning; using the lab repo path would mean the script must already be
# rsynced (chicken-and-egg if run-on-lab.sh itself was just edited).
scp "${ssh_opts[@]}" "${HERE}/run-on-lab.sh" "${TARGET}:/tmp/run-on-lab.sh" >/dev/null
ssh "${ssh_opts[@]}" "${TARGET}" \
    "sudo -iu stack bash /tmp/run-on-lab.sh ${MILESTONE}"
