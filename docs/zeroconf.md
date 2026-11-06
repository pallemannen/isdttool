# Zeroconf / mDNS autodiscovery

The bridge (`isdttool serve`) advertises itself on the local network over mDNS, so a
client — in practice the Home Assistant integration `hass_isdt` — can find a charger
without being told its address and port by hand.

Advertising is on by default. `--no-advertise` turns it off.

## Service

- **Service type:** `_isdt._tcp.local.`
- **Instance name:** `ISDT <model> <serial>`, e.g. `ISDT N24 00000000011c`. The serial makes
  the instance name unique on the network, so several chargers (each its own `serve`
  process on its own port) never collide. `--instance-name` overrides it.
- **Port:** the TCP port `serve` listens on.
- **TXT records:**
  - `serial` — the *effective* serial (see below).
  - `model` — the charger model, e.g. `N24`.
  - `version` — the isdttool version, informational.

### The effective serial

A client identifies a charger by the serial it reads over the link. A charger that answers
the serial command reports its own; one that does not (e.g. the N24) is given the USB serial
number by the bridge instead. The TXT `serial` is whichever of these a client would see, so
that the integration's unique id matches whether it learns the charger by discovery or by
hand. The bridge already resolves this when it probes the charger at startup; it advertises
the same value.

## Bridge

At startup the bridge probes the charger once for its serial and its model, then registers
the service. It unregisters on shutdown. `zeroconf` is imported lazily: if it is not
installed, the bridge logs one line and serves without advertising, rather than failing.

## Integration

`hass_isdt` declares the service type in its manifest and adds a discovery step to its config
flow. Discovery carries the host, port, and the TXT `serial`, so the flow can set the unique
id and abort — or repoint an existing entry to a new address — before it connects. It still
probes the charger on confirmation to read the authoritative model and build the entry, so the
TXT `model` only labels the discovery card.

## Wire independence

Advertising is the bridge's job and says nothing about how the charger is attached to the
host, or about the TCP wire format (see `transport.py`). A client that already has an address
and port never needs mDNS.
