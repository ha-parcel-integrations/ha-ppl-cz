# Examples

Ready-to-paste Home Assistant snippets for the PPL CZ integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |

PPL CZ is account-based: once you log in, your parcels (received and sent) are
imported automatically — there is nothing to add by hand, and no services to
call. The automations here react to the parcel **events** below.

All examples assume a single PPL CZ account. Adjust entity IDs to match yours;
with more than one account configured, every entity ID carries the account's
e-mail address.

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `ppl_cz_parcel_registered` | A new parcel you're receiving appears | The full normalised parcel dict |
| `ppl_cz_parcel_status_changed` | A received parcel's status changes | Same, plus `old_status` / `new_status` |
| `ppl_cz_parcel_delivered` | A received parcel is delivered | Same (fires *instead of* `status_changed` on that final hop) |
| `ppl_cz_outgoing_parcel_status_changed` | A sent parcel's status changes | Same, plus `old_status` / `new_status` |
| `ppl_cz_outgoing_parcel_delivered` | A sent parcel is delivered | Same |

Every payload also carries the account's `device_id`, which is what device
triggers filter on. Events are suppressed on the first refresh after
start-up. There is no `*_delivery_time_changed` event — PPL CZ exposes no ETA.
