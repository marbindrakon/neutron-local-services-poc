#!/bin/sh
#
# MISC_CHECK probe for NTP health (LVS/Keepalived plugin).
# Args: $1 = backend address  $2 = backend port (default 123)
# Exit: 0 = up, 1 = down (Keepalived's MISC_CHECK convention).
#
# Strategy: send a single SNTP query with a 2s deadline. Prefer
# `sntp` (chrony / ntpsec ship it); fall back to `ntpdate -q` if
# present. As a final fallback, just check UDP reachability with
# nc — a backend that ACKs NTP-port traffic is at least up.
set -eu

ADDR="${1:?usage: check_ntp.sh <addr> [port]}"
PORT="${2:-123}"

if command -v sntp >/dev/null 2>&1; then
    sntp -K /dev/null -t 2 "${ADDR}" >/dev/null 2>&1
    exit $?
fi
if command -v ntpdate >/dev/null 2>&1; then
    ntpdate -q -t 2 "${ADDR}" >/dev/null 2>&1
    exit $?
fi
if command -v nc >/dev/null 2>&1; then
    # `nc -zu -w2` sends a single UDP probe and reports whether ICMP
    # unreachable came back. Not a perfect NTP check but better than
    # always-up.
    nc -zu -w2 "${ADDR}" "${PORT}" >/dev/null 2>&1
    exit $?
fi
exit 1
