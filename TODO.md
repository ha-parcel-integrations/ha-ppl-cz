# PPL CZ — still to do

Generated from ha-carrier-template (`--auth credentials --interval configurable`), then built out fully against the
mechanics in `carrier-research/ppl-cz/api/` (login.md, tracking.md). Tests pass at 98% coverage; ruff is clean. What's
left is blocked on a real account with a real shipment, not on code.

## Done

- [x] All `TODO(carrier)` markers filled (`api.py`, `config_flow.py`, `const.py`, `device.py`, `diagnostics.py`,
      `parcels.py`, `CLAUDE.md`, `README.md`)
- [x] `tests/payloads.py` — sample payloads shaped from the mechanics doc (still `payload: reconstructed` upstream —
      see below)
- [x] `custom_components/ppl_cz/brand/icon.png` — real PPL logo, resized to 256x256

## Still open — blocked on a real shipment, not code

- [ ] Replace `tests/payloads.py`'s reconstructed shapes with a real, redacted `GET /api/v2/me/shipments` body once
      one exists, and fold that correction back into `carrier-research/ppl-cz/api/tracking.md`
- [ ] Install it in a real Home Assistant and track one real parcel through at least two status changes
- [ ] Add `ppl_cz` to the aggregator's `KNOWN_CARRIERS` and `CARRIER_EVENT_PREFIXES` (`ha-parcel-aggregator`,
      out of scope for this repo)

Delete this file once the above closes out.
