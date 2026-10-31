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