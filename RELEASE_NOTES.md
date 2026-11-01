## 0.4.3

### Lifecycle and input fixes

- Closes the owned HTTP session on Home Assistant shutdown, as well as explicit unload and failed setup. Removes shutdown hooks on cleanup and stops coordinator polling.
- Serializes complete polls and control actions with teardown. An in-flight poll cannot reopen a session after shutdown closes it; queued/new controls are rejected once shutdown begins. Cleanup is idempotent.
- Bounds polling intervals to **1–86400 whole seconds** in both the UI and runtime. Oversized integers/strings cannot overflow setup. Invalid saved values use the 30-second default, including in the options form so settings remain recoverable.

### Final audit fixes

- Preserves exact integer counter values, including 64-bit values, without a float round-trip.
- Ignores commented-out variables and variable-like text in script strings. Comments inside values cannot break extraction; literal string content is preserved.
- Invalidates affected readings when a write response is lost or a write/readback is cancelled. Original errors/cancellation still propagate; uncertain writes are never retried automatically.
- Invalidates caches when a control action's capability preflight discovers feature loss, as well as during polling.
- Rejects malformed optional device metadata instead of passing invalid types to Home Assistant.

### Validation

- 296 tests cover the real Home Assistant 2026.10.4 runtime and a local simulated switch, including actual entity services, supported PoE priorities/limits/statuses, session handling, malformed data, permissions, migration, recovery, cancellation and shutdown races.
- GitHub releases require the test suite and at least 90% combined statement/branch coverage to pass. Local coverage is approximately 92%; compilation and targeted static checks also pass.
- See [the final audit record](docs/audit-0.4.3.md). No new physical-switch tests were performed; previously confirmed hardware/firmware results and contributor credits remain in the README.

Requires Home Assistant 2026.10.4 or newer.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.4.2...v0.4.3

## 0.4.2

- Rejects boolean and fractional polling intervals instead of silently converting them to one second. Positive integer values and integer strings remain supported; the configuration UI keeps a serializable numeric selector.
- Prevents duplicate setup under a different display name using normalized endpoints and the switch MAC address. Newly configured entries receive a MAC-based unique ID. Existing entries gain identity metadata on successful setup without changing their existing unique IDs or entity IDs.
- Builds IPv6 URLs correctly for raw and bracketed literals, including scoped addresses. IPv4 addresses and DNS hostnames remain supported. The host field rejects full URLs and host:port expressions before creating the client.
- Adds regression coverage for strict input validation, form serialization, duplicate endpoints/MACs, legacy identity preservation and real IPv6 loopback HTTP requests.

Validation: 182 tests pass locally under Home Assistant 2026.10.4, plus compilation and static/whitespace checks. No new physical-switch testing was performed. Minimum HA version remains 2026.10.4.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.4.1...v0.4.2

## 0.4.1

- Invalidates cached port statistics, global PoE readings and per-port PoE readings when capability re-detection reports the feature unavailable. Existing entities stop presenting the old readings as current; they recover when the feature returns.
- Propagates authentication failures from every port/statistics/PoE read helper to the coordinator, even when the initial device-info read succeeded. Rejected credentials now trigger Home Assistant reauthentication during that refresh; transient authentication failures remain retryable.
- Distinguishes successful global/per-port PoE commands from failed readbacks. Actions report the refresh failure, invalidate affected cached readings and notify entities; normal polling retries the reads. Empty responses and unavailable capabilities also count as failed action readbacks.
- Preserves original command errors when the write itself fails and avoids reporting those as successful changes.
- Adds 25 regression cases for capability loss/recovery, authentication failures at each read stage, and global/per-port action readback failures. All 143 tests pass locally under HA 2026.10.4.

Requires Home Assistant 2026.10.4 or newer. Validation uses the real HA runtime and a local simulated switch; no additional physical-switch tests were performed.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.4.0...v0.4.1

## 0.4.0

Requires **Home Assistant 2026.10.4 or newer** and its supported Python runtime.

### Fixes

- Migrates legacy version-1 config entries through Home Assistant's update API, preserves existing options, and rejects newer unsupported configuration versions.
- Replaces the version-dependent domain-wide service permission decorator with authorization for the selected switch/port. Restricted users cannot control other switches by substituting a MAC address.
- Parses boolean action inputs correctly (`"false"`, `"off"` and `"0"` no longer enable PoE). Validates manual-limit requirements, port numbers, finite power limits and positive polling intervals before accepting them.
- Makes service registration and cleanup idempotent across multiple switches and repeated unload attempts.
- Validates switch identity, port/PoE arrays, packet counters and power readings; malformed/partial responses cannot silently count as successful statistics reads.
- Parses multiple JavaScript variables on one line, script attributes, quoted commas and semicolons without executing JavaScript.
- Stops unnecessary login retries on non-authentication HTTP errors. Expired sessions still reauthenticate once; failed/empty login results are handled explicitly and request options are forwarded consistently.
- Serializes concurrent feature probes, retries transport failures after a cooldown, and rechecks negative results periodically. HTTP 404/405 capability responses are treated as unsupported rather than blocking otherwise usable switches.
- Marks disabled ports as known/off rather than unavailable. PoE short-circuit and overtemperature states no longer report power on.
- Corrects displayed PoE Class 1 and Class 2 labels.
- Cleans up API sessions on failed setup/partial forwarding; explicitly associates coordinators with their config entry.
- Supports credential reauthentication without deleting existing entities, with password fields masked in the configuration forms.

### Validation and release process

- Replaced isolated source-definition checks with tests importing real integration modules under Home Assistant 2026.10.4, plus a local simulated switch using aiohttp.
- 118 tests cover HTTP/session behavior, parsing and invalid data, migrations, configuration flows, action permissions, multiple-switch service cleanup, entity availability and recovery, and counter clearing. A lifecycle test loads the integration through Home Assistant, creates all 11 entities, performs actions, and unloads it.
- GitHub Actions must pass the test suite before publishing a release. ZIP packaging excludes bytecode caches.
- Existing user-reported hardware tests remain documented in the README, including TL-SG108PE V5.0 firmware 1.0.0 Build 20230218 Rel.51358. No new physical-switch tests were performed for 0.4.0.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.3.9...v0.4.0

## 0.3.9

- Failed initial port reads now fail setup and allow Home Assistant to retry, instead of completing setup with missing port entities.
- Empty JavaScript arrays are parsed correctly; invalid array syntax produces a controlled parsing error.
- Port-state and PoE responses are validated before indexing their arrays, preventing incomplete snapshots from causing indexing errors.
- Disconnected feature probes remain unknown and are retried after a 60-second cooldown on a subsequent poll, rather than being permanently cached as unsupported. Entities for recovered ports, PoE and statistics are added automatically without duplicates.
- Failed or cancelled first refreshes close the API session and remove the failed coordinator before setup retries. Disconnect also resets API initialization state.
- Service MAC-address lookup is case-insensitive and tolerates missing device information.
- Added TL-SG108PE V5.0 to the confirmed models table. @JCalvi confirmed operation and Clear port statistics on firmware 1.0.0 Build 20230218 Rel.51358.
- Added a dependency-free regression suite: `python -m unittest discover -s tests -v`.

Validation: 10 focused source-level regression tests, Python compilation and whitespace checks. Tests execute production definitions in isolated namespaces; a full Home Assistant runtime and additional physical-switch testing were not available.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.3.8...v0.3.9

## 0.3.8

- Fixed failed integration unloads decrementing the active service count and potentially removing services still in use. Service cleanup now runs only after all platforms unload successfully.
- Removed the duplicate asynchronous disconnect callback; the API connection is closed once in the awaited successful-unload path.
- The Clear port statistics button now reports when clearing succeeds but refreshing the counters fails, including empty responses. Cached statistics are invalidated and entities are notified, so old counters are removed. Normal polling retries the read.
- A failed clear request still reports its original failure and is not described as a successful reset.

Validation: focused regression checks for failed/successful unloads, service counts, clear failures, refresh failures, empty responses, and recovery; Python compilation. No additional physical-switch testing.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.3.7...v0.3.8

## 0.3.7

- Fixed the general PoE power-limit action reporting ServiceNotFound after successfully executing the command.
- Fixed port-control cache bounds checking to prevent an IndexError when a successful command completes after the cached port list becomes empty.
- Made sensor, binary sensor and switch availability respect coordinator update failures, so cached values are marked unavailable when communication fails.
- Invalidated cached PoE consumption after a failed PoE read, so stale power readings are no longer presented as current.
- Documented successful Clear port statistics testing on TL-SG105E, TL-SG108E and TL-SG108PE by @JCalvi, alongside the TL-SG116E V2.20 test by @bakernigel. Revisions and firmware for the additional models were not recorded.

Validation: focused regression checks and Python compilation. No additional physical-switch tests performed for these fixes.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.3.6...v0.3.7

## 0.3.6

- Added a device-level **Clear port statistics** configuration button to reset all port packet counters and refresh the displayed statistics. Thanks to [@bakernigel](https://github.com/bakernigel), who contributed the feature and tested it on a TL-SG116E V2.20.
- Fixed an off-by-one bounds check when parsing port statistics. Missing or truncated packet and port-state arrays no longer cause indexing errors; only complete port records are returned.
- Removed stale packet-count attributes when current statistics are unavailable, including after a failed refresh. Port connectivity remains independent of statistics availability.

Hardware testing of the clear button was reported by the contributor; these bug fixes were verified with focused regression checks, without additional physical-switch testing.

## Installation

Download `tplink_easy_smart.zip`, extract it into your Home Assistant configuration directory, and restart Home Assistant.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.3.5...v0.3.6

## What's Changed
- Fixed integration logo to display correctly in Home Assistant
- Updated logo.png to correct 256×256 dimensions (was 512×512)
- Added logo@2x.png (512×512) for high-DPI displays
- Bumped version to 0.3.5 in manifest.json

## Technical Details
Home Assistant requires integration logos to be exactly 256×256 pixels. The previous logo was 512×512 which caused it to be silently ignored by the Home Assistant UI.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.3.4...v0.3.5