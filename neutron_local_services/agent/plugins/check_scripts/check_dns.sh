#!/bin/sh
#
# MISC_CHECK probe for DNS health (LVS/Keepalived plugin).
# Args: $1 = backend address  $2 = backend port (default 53)
# Exit: 0 = up, 1 = down (Keepalived's MISC_CHECK convention).
#
# Strategy: ask the backend to resolve a well-known root nameserver
# name (a.root-servers.net). We don't care about the answer — only
# whether the server responded with NOERROR within the timeout.
# Using `dig +short +time=2 +tries=1` keeps the probe cheap; if
# `dig` isn't on the path we fall back to `nslookup` (BIND tools or
# busybox both ship one).
set -eu

ADDR="${1:?usage: check_dns.sh <addr> [port]}"
PORT="${2:-53}"
QNAME="${DNS_PROBE_QNAME:-a.root-servers.net}"

if command -v dig >/dev/null 2>&1; then
    dig +time=2 +tries=1 +short "@${ADDR}" -p "${PORT}" "${QNAME}" \
        >/dev/null 2>&1
    exit $?
fi
if command -v nslookup >/dev/null 2>&1; then
    nslookup -timeout=2 "${QNAME}" "${ADDR}" >/dev/null 2>&1
    exit $?
fi
# Last-resort: TCP connect on the port. UDP probe without dig is
# unreliable; a TCP connect at least proves the host is up.
exec 3<>"/dev/tcp/${ADDR}/${PORT}"
exit $?
