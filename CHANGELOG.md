# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
