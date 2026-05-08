# shellcheck shell=bash
#
# TAP-13 emitter + per-case PASS/FAIL counters.
#
# Each case sources lib/case.sh which sources this file. Test bodies call
# `pass "msg"` / `fail "msg" [details]`; the runner consumes the case's
# exit code (0 = all passed, non-zero = at least one failure) and the TAP
# stream from stdout (for JUnit XML conversion if requested).

# Colored output for humans reading the raw stream. The runner also
# parses TAP, but the stream stays readable when run interactively.
RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CLR=$'\033[0m'

# Counters scoped to a single case. Reset by tap_setup.
PASS=0
FAIL=0
TAP_N=0

tap_setup() {
    PASS=0
    FAIL=0
    TAP_N=0
    # TAP plan: we don't know N up-front, so emit it at exit instead.
    # Header (a TAP comment, ignored by parsers).
    echo "# case: ${CASE_ID:-unknown}"
}

pass() {
    TAP_N=$((TAP_N+1))
    PASS=$((PASS+1))
    echo "${GRN}ok${CLR} ${TAP_N} - $1"
}

fail() {
    TAP_N=$((TAP_N+1))
    FAIL=$((FAIL+1))
    echo "${RED}not ok${CLR} ${TAP_N} - $1"
    if [[ -n "${2:-}" ]]; then
        # YAML-ish diagnostic block per TAP-13 spec.
        echo "  ---"
        echo "  details: |"
        # Indent every line of the details by 4 spaces. Use printf to
        # preserve whitespace; iterate so we get the indent on each line
        # without echo eating backslashes.
        printf '%s\n' "$2" | sed 's/^/    /'
        echo "  ..."
    fi
}

note() {
    # TAP comment — runner displays but doesn't count.
    echo "${YEL}# ${CLR}$1"
}

skip() {
    # Emit a TAP "skip directive" and exit 0. Runner treats this case as
    # SKIP, not FAIL. Sets TAP_SKIPPED so the EXIT-trap-driven summary
    # doesn't append a second plan line.
    TAP_SKIPPED=1
    echo "1..0 # SKIP $1"
    exit 0
}

tap_summary() {
    # Skip path already emitted its plan line — don't double-up.
    [[ "${TAP_SKIPPED:-0}" -eq 1 ]] && return
    # Emit the TAP plan line (1..N) at the END so the runner has it even
    # though we don't know N up front.
    echo "1..${TAP_N}"
    if [[ "$FAIL" -gt 0 ]]; then
        echo "# ${RED}FAIL${CLR}: $FAIL of $TAP_N assertions failed"
    else
        echo "# ${GRN}PASS${CLR}: $TAP_N assertions"
    fi
}
