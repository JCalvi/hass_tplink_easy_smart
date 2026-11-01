# Final audit for 0.4.3

## Target and method

The audit targets Home Assistant 2026.10.4 and Python 3.14.8. It combines production-source review, tests importing the real HA modules, an aiohttp switch simulator, and a lifecycle test that loads the integration through HA, creates its entities, executes their actions and unloads it.

The suite contains 296 tests. Local combined statement/branch coverage is approximately 92%. Python compilation, targeted static checks and whitespace checks are also run. CI requires all tests and at least 90% combined coverage before publishing.

## Regression matrix

| Area | Checks |
| --- | --- |
| Setup and identity | Successful setup, failed/cancelled refresh, partial forwarding failure, duplicate endpoints/MACs, legacy ID preservation, safe metadata |
| Configuration | Version-1 migration, future-version rejection, masked credentials, reauthentication, form serialization, strict interval bounds, recovery from invalid saved intervals, IPv4/IPv6 hosts |
| Transport | Real HTTP requests, cookies, expired sessions, bounded 401/403 retry, non-auth HTTP errors, reconnect, concurrent requests, IPv6 loopback |
| Parsing | Empty arrays, nested objects and quoted delimiters, script attributes, comments/debug strings, malformed identity/arrays/counts, exact 64-bit counters |
| Polling and recovery | Mandatory-read failure, optional-read failure, authentication errors at every stage, capability retry/loss/recovery, no duplicate recovered entities |
| State reporting | Available/off disabled ports, stale-counter removal, unavailable cached data, documented PoE statuses and class labels |
| Controls | Actual HA on/off services for Ethernet and PoE, counter clearing, global/per-port PoE actions, supported priorities and limits, device limits, original write errors, failed/empty readbacks, uncertain/cancelled operations |
| Permissions and services | Restricted target switch/port access, other-switch denial, unknown users, administrators, internal calls, multiple owners and repeated cleanup |
| Shutdown | Stop without explicit unload, in-flight requests, queued controls, no reopening after close, idempotent cleanup, failed-setup hook removal |
| Release | Version/notes update, CI before publication, tag tied to the tested commit, ZIP excludes bytecode caches |

## Scope and limits

No direct connection to a physical switch was available for this audit. The simulator exercises the endpoint formats implemented by this integration. Firmware-specific response variants, hardware timing and actual PoE delivery still require physical-switch validation. Existing user-reported tests remain recorded in the README.

The JavaScript reader extracts literal variable data; it does not execute switch scripts or implement the entire JavaScript language. HTTP-level command acceptance is tested against the simulator; application-specific error formats from untested firmware need captured responses before their semantics can be verified.

No outstanding reproducible failure remained in this matrix at release time. Passing tests and coverage do not prove that every possible firmware response or runtime condition is bug-free.

## Reproduce

```sh
python -m pip install -r requirements_test.txt
python -m pytest -q --cov=custom_components.tplink_easy_smart --cov-branch --cov-report=term --cov-fail-under=90
python -m compileall -q custom_components tests
```
