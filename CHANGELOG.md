# Changelog

All notable changes to this fork. Upstream releases are listed at
https://github.com/maxried/isdttool.

## 1.1.1 - 2026-11-04

### Changed
- The bridge logs a client only the first time it connects, and when it replaces another
  client, instead of every connection. A client that polls, like the Home Assistant
  integration, filled the journal with about 5,800 lines a day. `--debug` logs every
  connection.

## 1.1.0 - 2026-11-04

### Added
- `serve` exposes a charger on a local USB port to the network. A new client replaces the
  current one. For chargers that don't answer the `serial` command, the bridge answers it with
  the charger's USB serial number. systemd template in `contrib/isdttool@.service`.
- `-H/--host HOST[:PORT]` and `-p/--port` use a charger through a bridge.
- `-s/--serial` picks a local charger by USB serial number.
- `list` shows USB serial numbers.
- ISDT N24 support, including the reason why charging stopped. `serial` works through a bridge.
- Transport abstraction in the library: `get_device(host=..., port=...)`. hidapi is only needed
  for USB.

### Fixed
- Project URL, and trove classifiers in `setup.py`.
- `hidapi` no longer pinned to 0.10.x.
