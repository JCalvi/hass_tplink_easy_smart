## What's Changed
- Fixed integration logo to display correctly in Home Assistant
- Updated logo.png to correct 256×256 dimensions (was 512×512)
- Added logo@2x.png (512×512) for high-DPI displays
- Bumped version to 0.3.5 in manifest.json

## Technical Details
Home Assistant requires integration logos to be exactly 256×256 pixels. The previous logo was 512×512 which caused it to be silently ignored by the Home Assistant UI.

**Full Changelog**: https://github.com/JCalvi/hass_tplink_easy_smart/compare/v0.3.4...v0.3.5