#!/usr/bin/env bash
#
# Lab functional-test dispatcher. Runs cases/*.sh as independent
# subprocesses, captures TAP output, prints a summary table, and exits
# non-zero if any case failed. Each case is responsible for its own
# setup/teardown via lib/case.sh; this runner just enumerates and
# dispatches.
#
# Usage (from the lab, as `stack`):
#   ./runner.sh [SELECTOR] [--junit FILE] [--logs DIR]
#
# SELECTOR — what to run (defaults to "all"):
#   all           tag matches: smoke + plugin + multitenant
#                 (the cases that ran in the original `all` suite)
#   all_full      every case (adds multichassis + underlay)
#   smoke         tag: smoke only
#   plugin        tag: plugin only
#   multitenant   tag: multitenant only
#   multichassis  tag: multichassis only
#   underlay      tag: underlay only
#   <case-id>     a single case by id (e.g. "06-nat-plugin")
#   <glob>        case-id glob (e.g. "0[12]-*", "*nat*")
#
# --junit FILE   Write JUnit XML at FILE in addition to TAP.
# --logs DIR     Where to put per-case stdout logs (default: ./lab-test-logs).

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CASES_DIR="${HERE}/cases"

SELECTOR="all"
JUNIT_FILE=""
LOGS_DIR="${HOME}/lab-test-logs"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --junit) JUNIT_FILE="$2"; shift 2 ;;
        --logs)  LOGS_DIR="$2"; shift 2 ;;
        --help|-h)
            sed -n '4,24p' "$0" | sed 's/^# *//'
            exit 0 ;;
        -*)
            echo "unknown flag: $1" >&2; exit 2 ;;
        *)
            SELECTOR="$1"; shift ;;
    esac
done

mkdir -p "$LOGS_DIR"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CLR=$'\033[0m'

# Read a case's `# tags:` header line. Format:
#     # tags: smoke plugin
# Returns space-separated tags on stdout, empty if the line is absent.
case_tags() {
    awk '/^# tags:/{sub(/^# tags:[[:space:]]*/, ""); print; exit}' "$1"
}

# Decide whether a case matches the SELECTOR.
case_matches() {
    local file="$1" id tags
    id=$(basename "$file" .sh)
    tags=$(case_tags "$file")
    case "$SELECTOR" in
        all)
            # Match the original `all` suite: smoke + plugin +
            # multitenant. (multichassis + underlay are all_full only.)
            [[ " $tags " == *" smoke "* \
            || " $tags " == *" plugin "* \
            || " $tags " == *" multitenant "* ]] ;;
        all_full)
            return 0 ;;
        smoke|plugin|multitenant|multichassis|underlay)
            [[ " $tags " == *" $SELECTOR "* ]] ;;
        *)
            # Single id or glob match against the case-id (filename
            # without .sh).
            # shellcheck disable=SC2053  # intentional glob match
            [[ "$id" == $SELECTOR ]] ;;
    esac
}

# Collect matching cases in lexical order (filenames are NN-name.sh so
# this preserves declared ordering).
mapfile -t ALL_CASES < <(find "$CASES_DIR" -maxdepth 1 -type f -name '*.sh' | sort)
SELECTED=()
for f in "${ALL_CASES[@]}"; do
    if case_matches "$f"; then
        SELECTED+=("$f")
    fi
done

if [[ ${#SELECTED[@]} -eq 0 ]]; then
    echo "no cases match selector '$SELECTOR'" >&2
    echo "available cases:" >&2
    for f in "${ALL_CASES[@]}"; do
        echo "  $(basename "$f" .sh)  [tags: $(case_tags "$f")]" >&2
    done
    exit 2
fi

# JUnit accumulator. We emit a <testsuite> with one <testcase> per case;
# inside each, the TAP "ok N" lines aren't broken out into individual
# JUnit testcases (would require parsing TAP; deliberately deferred —
# the per-case granularity is enough for CI gating).
junit_xml=""
junit_failures=0
junit_skipped=0
junit_total=0
junit_time_total=0

# Header for the human-readable summary.
echo
echo "Lab functional tests — selector='$SELECTOR' cases=${#SELECTED[@]}"
echo

declare -a SUMMARY_LINES
overall_rc=0

for f in "${SELECTED[@]}"; do
    case_id=$(basename "$f" .sh)
    log_file="${LOGS_DIR}/${case_id}.log"
    junit_total=$((junit_total + 1))

    printf '  %-32s ' "$case_id"
    start=$SECONDS
    # Run the case as a subprocess. stderr+stdout interleaved, captured
    # for log replay; the TAP stream goes to the log too.
    set +e
    bash "$f" >"$log_file" 2>&1
    rc=$?
    set -e
    elapsed=$((SECONDS - start))
    junit_time_total=$((junit_time_total + elapsed))

    # Decide outcome:
    #   - rc==0 + "1..0 # SKIP" line → SKIP
    #   - rc==0 + plan line "1..N" with N>0 → PASS
    #   - rc!=0 → FAIL (case body called `fail` somewhere or aborted)
    if grep -q '^1\.\.0 # SKIP' "$log_file"; then
        outcome=SKIP
        reason=$(awk -F'# SKIP ' '/^1\.\.0 # SKIP/{print $2; exit}' "$log_file")
        printf '%bSKIP%b  (%ss) %s\n' "$YEL" "$CLR" "$elapsed" "$reason"
        SUMMARY_LINES+=("$case_id  SKIP  ${elapsed}s  $reason")
        junit_skipped=$((junit_skipped + 1))
        junit_xml+="    <testcase name=\"$case_id\" time=\"$elapsed\"><skipped message=\"$(printf '%s' "$reason" | sed 's/[<>&\"]/_/g')\"/></testcase>"$'\n'
    elif [[ "$rc" -eq 0 ]]; then
        # awk's multi-char -F is a regex (where ".." matches "any 2
        # chars"), so use sed for the literal "1..N" extraction.
        n_assert=$(sed -nE 's/^1\.\.([0-9]+).*/\1/p' "$log_file" | head -1)
        printf '%bPASS%b  (%ss) %s assertions\n' "$GRN" "$CLR" "$elapsed" "${n_assert:-0}"
        SUMMARY_LINES+=("$case_id  PASS  ${elapsed}s  ${n_assert:-0} assertions")
        junit_xml+="    <testcase name=\"$case_id\" time=\"$elapsed\"/>"$'\n'
    else
        # Pull the first failed assertion (or bail-out line) for the
        # summary so the operator sees what broke without grepping.
        # The TAP lines start with an ANSI color sequence, so grep with
        # `^not ok` misses; strip ANSI before/after match.
        first_fail=$(sed -r 's/\x1b\[[0-9;]*m//g' "$log_file" \
            | grep -m1 -E '^(not ok|Bail out!)' || true)
        [[ -z "$first_fail" ]] && first_fail="(see log)"
        printf '%bFAIL%b  (%ss) %s\n' "$RED" "$CLR" "$elapsed" "$first_fail"
        SUMMARY_LINES+=("$case_id  FAIL  ${elapsed}s  $first_fail")
        overall_rc=1
        junit_failures=$((junit_failures + 1))
        # Embed the log as <failure> body (truncated to 4KB so a runaway
        # test doesn't blow up the XML).
        log_body=$(head -c 4096 "$log_file" | sed 's/]]>/]]]]><![CDATA[>/g')
        junit_xml+="    <testcase name=\"$case_id\" time=\"$elapsed\"><failure message=\"$(printf '%s' "$first_fail" | sed 's/[<>&\"]/_/g')\"><![CDATA[${log_body}]]></failure></testcase>"$'\n'
    fi
done

# Footer: punch list.
echo
echo "==================== Summary ===================="
for line in "${SUMMARY_LINES[@]}"; do
    echo "  $line"
done
echo "================================================="
echo "Logs: $LOGS_DIR"

if [[ -n "$JUNIT_FILE" ]]; then
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo "<testsuite name=\"neutron-local-services lab\" tests=\"$junit_total\" failures=\"$junit_failures\" skipped=\"$junit_skipped\" time=\"$junit_time_total\">"
        printf '%s' "$junit_xml"
        echo "</testsuite>"
    } > "$JUNIT_FILE"
    echo "JUnit XML: $JUNIT_FILE"
fi

exit "$overall_rc"
