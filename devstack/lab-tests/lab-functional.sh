#!/usr/bin/env bash
#
# Run the lab functional tests against a remote DevStack instance.
# Designed to be run from this dev host after `lab-push.sh` succeeds.
#
# Usage:
#   ./devstack/lab-tests/lab-functional.sh [SELECTOR] [user@host] [-- runner-flags...]
#
# Examples:
#   ./devstack/lab-tests/lab-functional.sh                  # 'all', default host
#   ./devstack/lab-tests/lab-functional.sh smoke
#   ./devstack/lab-tests/lab-functional.sh 06-nat-plugin almalinux@172.18.0.128
#   ./devstack/lab-tests/lab-functional.sh all_full almalinux@172.18.0.128 -- --junit out.xml
#
# SELECTOR is forwarded to runner.sh — see runner.sh --help for the
# full list (all, all_full, smoke, plugin, multitenant, multichassis,
# underlay, or a case-id like "06-nat-plugin").
#
# What it does: rsyncs the lab-tests dir onto the target, then exec's
# runner.sh as the `stack` user. Cases are idempotent and self-cleaning
# so re-running is safe.

set -euo pipefail

SELECTOR="${1:-all}"
TARGET="${2:-almalinux@172.18.0.128}"
SSH_KEY="${SSH_KEY:-${HOME}/.ssh/neutron-localsvc-poc}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Anything after `--` is forwarded to runner.sh on the lab.
shift $(( $# > 2 ? 2 : $# ))
RUNNER_ARGS=()
if [[ "${1:-}" == "--" ]]; then
    shift
    RUNNER_ARGS=("$@")
fi

ssh_opts=(-i "${SSH_KEY}" -o StrictHostKeyChecking=no)

# rsync the whole lab-tests tree (cases/, lib/, runner.sh, fixtures).
# /tmp/lab-tests is a stable scratch path. The first ssh chowns it
# back to the SSH user in case a prior run left it owned by stack
# (otherwise rsync fails with EPERM); rsync runs; the final ssh chowns
# to stack so the runner can mkdir CASE_TMP under the tree, then exec's
# runner.sh under stack via sudo -iu.
ssh "${ssh_opts[@]}" "${TARGET}" \
    "sudo chown -R \$USER:\$USER /tmp/lab-tests 2>/dev/null || true"

rsync -az --delete -e "ssh ${ssh_opts[*]}" \
    --exclude '__pycache__' \
    "${HERE}/" "${TARGET}:/tmp/lab-tests/"

ssh "${ssh_opts[@]}" "${TARGET}" \
    "sudo chown -R stack:stack /tmp/lab-tests && \
     sudo -iu stack bash /tmp/lab-tests/runner.sh '${SELECTOR}' ${RUNNER_ARGS[*]:-}"
