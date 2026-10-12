# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- *Start with Windows* is now off by default; the first-run notification says where to turn it on.
- No default location: sunrise/sunset triggers and the *dark outside* condition stay paused until a location is set.
- README: clearer scope (supported lights, no Hue Bridge), a working `git clone` command, and a note about the log.

### Fixed
- Sunrise/sunset rules with an offset that crosses midnight now fire at the right time, and the weekday filter
  applies to the day of the sun event.
- Usage stats are saved again on the next attempt if writing the file fails.

## [1.2.0] - 2026-10-12

First public, open-source release.

### Changed
- Renamed the app from "Hue Kontrol" to **Glowtray**.
- Translated the whole UI to English.
- The city list for sunrise/sunset now covers major cities worldwide, and custom coordinates are still supported.
- Energy cost is shown without a fixed currency symbol.
- On first start with no bulb selected, the panel opens on *Settings*.

### Removed
- The developer-only debug socket.
- The hard-coded default bulb address.

### Migration
- Settings and stats are copied automatically from `%APPDATA%\HueKontrol` to `%APPDATA%\Glowtray` on first start.
- The old "HueKontrol" autostart entry is replaced with a "Glowtray" one.
