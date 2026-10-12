# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - 2026-10-12

First public, open-source release.

### Added
- `THIRD_PARTY_LICENSES.md` with the licenses of everything bundled into the exe; releases include it next to the exe.
- Releases include the SHA-256 hash of the exe (`Glowtray.exe.sha256` and in the release notes) with a command to
  check it.

### Changed
- Renamed the app from "Hue Kontrol" to **Glowtray**.
- Translated the whole UI to English.
- The city list for sunrise/sunset now covers major cities worldwide, and custom coordinates are still supported.
- No default location: sunrise/sunset triggers and the *dark outside* condition stay paused until a location is set.
- *Start with Windows* is now off by default; the first-run notification says where to turn it on.
- Energy cost is shown without a fixed currency symbol.
- On first start with no bulb selected, the panel opens on *Settings*.

### Fixed
- Sunrise/sunset rules with an offset that crosses midnight now fire at the right time, and the weekday filter
  applies to the day of the sun event.
- Usage stats are saved again on the next attempt if writing the file fails.

### Removed
- The developer-only debug socket.
- The hard-coded default bulb address.

### Migration
- Settings and stats are copied automatically from `%APPDATA%\HueKontrol` to `%APPDATA%\Glowtray` on first start.
- The old "HueKontrol" autostart entry is replaced with a "Glowtray" one.
