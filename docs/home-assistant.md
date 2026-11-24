# Home Assistant

The chances on the phone page are also in `site/predictions.json` on the `data` branch, refreshed every 20 minutes by the collect job. Home Assistant reads that file straight from GitHub. No token, no add-on, and nothing on the HA side is reachable from outside.

## Install

1. Copy [`homeassistant/house_ferry_chances.yaml`](../homeassistant/house_ferry_chances.yaml) to `/config/packages/house_ferry_chances.yaml` (File editor or Studio Code Server).
2. Packages must be switched on in `configuration.yaml`. They already are if `house_ferries.yaml` lives there:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
3. **Developer tools → YAML → Check configuration**, then restart Home Assistant (a new `rest` resource needs a restart, not just a reload).

## What you get

| Entity | State | Attributes |
|---|---|---|
| `sensor.house_ferry_chances` | When the chances were last worked out (timestamp) | `next`: the next six sailings each way on 003 and 301 (date, time, vessel, chance %, `show` percent or band, band, CalMac's word); `model_as_of`, `data_through`, `runs` |
| `sensor.house_ferry_chance_lowest` | Lowest chance (%) of any sailing in the next 24 hours that CalMac hasn't already cancelled | `sailing`, for example "Brodick to Ardrossan Wed 08:20: 61%" |
| `sensor.house_ferry_next_from_brodick` | Chance (%) of the next 003 sailing from Brodick; 0 if CalMac has cancelled it | `time` ("Wed 08:20"), `label` ("61%", "Likely", "at risk", "cancelled by CalMac") |
| `sensor.house_ferry_next_from_ardrossan` | Same, next 003 sailing from Ardrossan | same |
| `sensor.house_ferry_next_brodick_to_troon` | Same, next 301 sailing from Brodick | same |
| `sensor.house_ferry_next_from_troon` | Same, next 301 sailing from Troon | same |
| `binary_sensor.house_alert_ferry_chance` | On when a sailing in the next 24 hours is below 75% | `severity` (warning below 40%, otherwise info), `message`, in the same shape as `binary_sensor.house_alert_ferries` for the alert strip |

The chance sensors go **unavailable** when the file is more than three hours old, so a stalled pipeline shows as a gap, not as stale numbers. CalMac's own cancellations stay with `binary_sensor.house_alert_ferries`; the chance alert only covers sailings CalMac hasn't cancelled.

Today and the next two days carry a percentage. Further out the `label` is a band (Likely 75% or more, Uncertain, Unlikely under 40%): the number is still in the state, but it is rougher that far out.

## Dashboard card

A Markdown card listing the next four sailings each way:

```yaml
type: markdown
title: Ferries: chance it runs
content: |
  {%- set words = {'diverted': 'Diverted', 'at_risk_diversion': 'May divert', 'at_risk': 'At risk'} -%}
  {%- for d in state_attr('sensor.house_ferry_chances', 'next') or [] %}
  **{{ d['from'] }} → {{ d['to'] }}**
  {% for x in d.sailings[:4] -%}
  {{ as_timestamp(as_local(as_datetime(x.date ~ ' ' ~ x.time))) | timestamp_custom('%a %H:%M') }} · {{ 'Cancelled' if x.calmac == 'cancelled' else (x.band if x.show == 'band' else (x.chance ~ '%' if x.chance is number else 'no estimate')) }}{{ ' · ' ~ words[x.calmac] if x.calmac in words else '' }}
  {% endfor %}
  {%- endfor %}
  {% set t = states('sensor.house_ferry_chances') %}
  <small>{{ 'Updated ' ~ relative_time(t | as_datetime) ~ ' ago.' if t not in ['unknown', 'unavailable'] else 'No chances yet.' }} Unofficial: check CalMac before you travel.</small>
```

For tiles, use the `sensor.house_ferry_next_*` entities with a tile or gauge card (severity green from 75, amber from 40, red below).

## Notes

- `raw.githubusercontent.com` caches for up to five minutes, so HA can be up to 25 minutes behind the pipeline. That's fine for decisions made hours or days ahead.
- The page and these sensors read the same file, so they always agree.
