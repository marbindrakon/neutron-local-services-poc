# Lab functional tests

End-to-end checks that run on a DevStack lab as the `stack` user. Each
case stands up its own service / binding / backend, asserts behavior
against real Neutron + OVN + ovn-agent, and tears down. Unit tests cover
logic; this covers integration with the wire format and the agent
extension. Re-run after any push that touches them.

## Running

```bash
# From the dev workstation: rsync the dir and exec runner.sh on the lab.
./devstack/lab-tests/lab-functional.sh                         # `all`
./devstack/lab-tests/lab-functional.sh smoke                   # one tag
./devstack/lab-tests/lab-functional.sh 06-nat-plugin           # one case
./devstack/lab-tests/lab-functional.sh all_full almalinux@host -- --junit out.xml
```

Selector grammar (forwarded to `runner.sh`):

| selector       | runs                                          |
| -------------- | --------------------------------------------- |
| `all`          | smoke + plugin + multitenant (the original `all`) |
| `all_full`     | every case (adds multichassis + underlay)     |
| `smoke`        | the five baseline cases                       |
| `plugin`       | nat + proxy plugin cases                      |
| `multitenant`  | multitenant-isolation only                    |
| `multichassis` | multichassis-isolation only                   |
| `underlay`     | underlay-egress only                          |
| `<case-id>`    | a single case (e.g. `06-nat-plugin`)          |
| `<glob>`       | shell glob over case ids (e.g. `0[12]-*`)     |

Per-case logs land in `~/lab-test-logs/<case>.log` on the lab. Output
on stdout is TAP-13: each `pass` becomes `ok N - msg`, each `fail`
becomes `not ok N - msg` followed by a YAML diagnostic block. The
runner prints a colored summary table and exits non-zero if any case
failed (skips don't fail).

## Layout

```
devstack/lab-tests/
  lab-functional.sh        Dev-side wrapper: rsync + ssh + exec runner.
  runner.sh                On-lab dispatcher: enumerates cases/, prints
                           summary, optional JUnit XML.
  lib/
    case.sh                Per-case bootstrap. Sources every other lib
                           file, sets `set -Eeuo pipefail`, registers
                           the EXIT trap that runs `case_teardown`,
                           emits the TAP header, decides exit code from
                           $FAIL.
    config.sh              Env-overridable connection details + every
                           per-test fixture name / VIP / port.
    rest.sh                _curl, _jget, setup_service, setup_binding,
                           teardown_*, lookup_service_id, opt-out
                           detection.
    netns.sh               probe_client + parametrized m10 client +
                           backend spawn/kill helpers.
    multichassis.sh        ssh-to-compute helpers + per-host plumbing
                           + multichassis_clean_leftovers.
    underlay.sh            underlay_clean_leftovers (idempotency for
                           the underlay-test re-runs).
    assert.sh              TAP-13 emitter + pass/fail/skip/note.
    fixtures/
      m11-tcp-backend.py   Real file extracted from a heredoc; copied
                           into /tmp by case 10 before exec.
  cases/
    01-localport-lifecycle.sh    smoke
    02-host-routes-injection.sh  smoke
    03-agent-extension-events.sh smoke
    04-netns-provisioning.sh     smoke
    05-vip-reconciliation.sh     smoke
    06-nat-plugin.sh             plugin
    07-multitenant-isolation.sh  multitenant (currently skipped — envoy → proxy port pending)
    08-multichassis-isolation.sh multichassis (requires_second_chassis)
    09-underlay-egress.sh        underlay (requires_underlay_backends)
    10-proxy-plugin.sh           plugin
```

## Adding a case

Each case is a self-contained executable. Template:

```bash
#!/usr/bin/env bash
# tags: smoke           # or: plugin, multitenant, multichassis, underlay
#
# What this case verifies, in 2-4 lines.

CASE_ID="11-my-new-case"
CASE_TITLE="my new case"
LAB_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "${LAB_TESTS_DIR}/lib/case.sh"

# requires_second_chassis     # uncomment if the case needs c1+c2
# requires_underlay_backends  # uncomment if the case needs lab underlay services

SVC_ID=""
BIND_ID=""

case_teardown() {
    teardown_binding "$BIND_ID"
    teardown_service "$SVC_ID"
}

SVC_ID=$(setup_service)
BIND_ID=$(setup_binding "$SVC_ID" "$NET_ID")
sleep 2

if some_check; then
    pass "what we verified"
else
    fail "what went wrong" "optional details"
fi
```

Contract `case.sh` enforces:

- `set -Eeuo pipefail` — uncaught errors abort and show as `Bail out!` in TAP.
- `trap _case_run_teardown EXIT` — your `case_teardown` runs even on failure.
- `$TOKEN` and `$NET_ID` are populated before your code runs.
- `requires_*` calls short-circuit to `skip "reason"` and exit 0 cleanly
  if a precondition fails. The runner reports this as `SKIP`, not `FAIL`.
- The case's exit code is 0 iff every `fail` count is 0; the runner uses
  this exit code to decide PASS/FAIL.

Rules:

- Don't reach into another case's fixtures. If two cases need the same
  helper, lift it into `lib/`.
- Don't rely on prior-case state. Each case should provision what it
  needs (some cases use a stub binding to drive netns provisioning —
  that's fine, but it must come from inside the case).
- Add `# tags: <list>` so `runner.sh` selectors find the case.
- Prefer `pass`/`fail` over raw `echo` so the assertion shows up in TAP
  and JUnit.

## Pre-existing rough edges

Carried over from the monolith (run-on-lab.sh). Worth fixing later but
not part of step-1 decomposition:

- `PROXY_TCP_VIP` and `MULTICHASSIS_SVC_VIP` both equal
  `169.254.169.160`. Latent today because the runner serializes cases;
  revisit if introducing parallelism.
- Backend pid/log scratch paths (`/tmp/m8-backend.*.pid`,
  `/tmp/m11-backend.*.pid`, `/tmp/m10-client.*.port_id`) live in shared
  `/tmp` rather than under `$CASE_TMP`. Consolidating those was out of
  scope to keep the helpers drop-in compatible.
- Case 07 (multitenant) is `skip`'d until the (nat + proxy) mixed-plugin
  port lands. Body preserved for future reference.
