# ha-snopud-community — Snohomish County PUD → Home Assistant

An **unofficial community** Home Assistant integration that pulls your SnoPUD
electric-meter data into HA's long-term statistics, feeding the Energy
Dashboard.

The integration authenticates against `my.snopud.com`, uses the same
"Download my usage" feature the portal exposes to every customer, receives
standard **Green Button ESPI XML**, parses it, and writes the readings into
Home Assistant's recorder.

> **Status:** running on the maintainer's HA instance. Built against
> Snohomish County PUD's customer portal. Not tested on other utilities.

## Disclaimer

This is an independent, community-maintained project. It is **not affiliated
with, endorsed by, sponsored by, or supported by** Snohomish County PUD
(SnoPUD), Aclara Technologies, Hubbell Incorporated, or any other utility or
vendor mentioned in this repository.

This integration is for **personal, residential use by SnoPUD customers who
want to access _their own_ usage data** for home automation and energy
tracking. SnoPUD's Privacy Policy explicitly recognises a customer's *"right
to access and disclose your usage information,"* and describes how third
parties in the general marketplace may develop "consumer products that use
Advanced Meter Data." See [SnoPUD Privacy Policy][privacy] for the full text.

Users are responsible for their own compliance with the [MySnoPUD Terms &
Conditions][tc]. Relevant points include §2(b)(g), which permits using the
service to *"export history and send custom reports,"* §3(i), which prohibits
forging headers or otherwise disguising the origin of your traffic, and §7,
which places liability and indemnity on the user. This integration is built
to respect those terms:

- It identifies itself honestly in the `User-Agent` header.
- It polls no more often than the portal's own data-refresh cadence would
  justify (the configured default is hourly; the data itself lags several
  hours).
- It stores no data outside your own Home Assistant installation.
- It ships with a circuit breaker that stops retrying after repeated auth
  failures, so a rotated password doesn't cause us to repeatedly submit wrong
  credentials.

This code is provided as-is under the MIT License with no warranty. If SnoPUD
or Aclara requests changes or removal, please [open an issue][issues] — the
maintainer will act in good faith.

Credentials are stored in Home Assistant's config entry on your own device.
They are never transmitted anywhere except to `my.snopud.com` during normal
authentication. Home Assistant does not by default encrypt config entries at
rest — they live in `<config>/.storage/core.config_entries` as readable JSON —
so treat the whole HA config directory like any other secret store: keep it
on an encrypted disk, back it up privately, use a strong unique MySnoPUD
password, and enable MFA on any account that supports it. This repository
itself contains no account data, usage data, or credentials.

## Features

- **Dual-grain fetch** on every refresh:
  - Hourly Green Button data is written into HA's long-term statistics so the
    Energy Dashboard can display it directly.
  - 15-minute Green Button data drives a sensor entity (one per meter) for
    Lovelace cards and automations at the granularity the meter actually
    reports.
- Optional cost series (USD), when SnoPUD includes a `cost` column in the
  export — no tariff configuration required on the HA side.
- Initial hourly backfill on first setup (default 2 years; configurable from
  7 days up to 5 years via the options flow).
- Optional billing-interval backfill for older retired / non-smart meters
  that predate your smart-meter upgrade.
- Configurable refresh interval (15 min – 12 h; default 1 h).
- Live-applied options — refresh interval, backfill window, and
  billing-interval backfill toggle all take effect without restarting Home
  Assistant.
- Multiple meters per account supported.

## Install

### Via HACS (custom repository)

1. HACS → Integrations → ⋮ menu → Custom repositories.
2. Add `https://github.com/ambrglow/ha-snopud-community` with category
   **Integration**.
3. Click Install on the "Snohomish County PUD (Community)" entry.
4. Restart Home Assistant.
5. Settings → Devices & Services → Add Integration → search "SnoPUD".

### Manually

Copy `custom_components/snopud/` into your HA config directory:

```
<ha-config>/custom_components/snopud/
```

Restart HA and add the integration via the UI.

## Configuration

When you add the integration you'll be asked for your **MySnoPUD email** and
**password** — the same credentials you use at `my.snopud.com`. If your
account has multiple meters (common if your house had an older non-smart
meter that was later replaced — both stay on the account), you'll get a
picker. **Retired / non-smart meters only report billing-interval totals,**
so selecting one produces no hourly history until you flip the billing
backfill option on.

The integration creates one external statistic per meter:

```
snopud:energy_consumption_1000000001
```

(with your meter number substituted) and optionally a parallel cost series:

```
snopud:energy_cost_1000000001
```

Go to **Settings → Dashboards → Energy** and add the energy series as a grid
consumption source; add the cost series under "Costs" if you want the dollar
view.

It also creates one **sensor entity** per meter, named like:

```
sensor.snopud_meter_1000000001_latest_15_min_usage
```

Its state is the kWh consumed during the most recent complete 15-minute
Green Button interval — a per-interval reading, not a cumulative counter.
Use it for "latest interval" automations and displays, and as the source
for a true 15-minute bar chart via its `recent_intervals` attribute (see
[15-minute bar chart card](#15-minute-bar-chart-card-apexcharts) below).
Do **not** wire it into the Energy Dashboard or a Utility Meter helper —
those want the hourly long-term statistic. The full rationale and the
caveats around state-history-based charts live in [The two data
paths](#the-two-data-paths-and-which-one-to-use-for-what).

> **Upgrade note (v0.2.7).** The 15-minute sensor was redesigned in v0.2.7.
> The old entity (`sensor.snopud_meter_<account>_energy`,
> `unique_id=snopud_<account>_energy`) has been replaced with a new entity
> on a fresh `unique_id` (`snopud_<account>_latest_15min_usage`). HA does
> **not** automatically migrate state history — the old entity's recorded
> values mixed cumulative-style and per-interval semantics and shouldn't be
> reused. After upgrading, find the old entity in **Settings → Devices &
> Services → Entities**, filter by integration "SnoPUD", and either delete
> or hide it. The Energy Dashboard is unaffected because it reads from the
> long-term statistic, which still receives hourly data exactly as before.

### Options (adjustable after setup)

Open **Settings → Devices & Services → SnoPUD → Configure** to change:

- **Refresh interval** (15 min – 12 h, default 1 h). SnoPUD's data lags the
  wall clock by ~5–8 hours, so more frequent polling gains nothing real.
- **Initial hourly backfill window** (7 days – 5 years, default 730 days).
  On initial import for a meter, the integration fetches hourly Green
  Button data for this many days of history (subject to what SnoPUD's
  portal actually exposes — empirically the portal caps hourly detail at
  about 2 years regardless of what you request). If you raise this value
  later, the next refresh will re-run the import to cover the widened
  window — useful for pairing with billing-interval backfill, since
  raising the window also re-runs the billing supplement so it can reach
  farther back than it did the first time. Lowering the value is a no-op:
  already-imported history is left alone.
- **Back-fill billing-interval history.** Enable this when your account has
  a pre-smart-meter period you want to import. The integration will request
  monthly billing-interval totals for any date range the hourly feed
  couldn't reach. This works **at any point**, not only at initial setup —
  switching the option on later kicks off a one-shot retroactive billing
  import on the next refresh for any selected meter that hasn't had a
  billing supplement yet. Once a meter has had its billing supplement run,
  the integration won't repeat it on every refresh; switching the option
  off afterwards does not erase already-imported billing data.

All three options are applied live — no Home Assistant restart needed.

## Getting daily / weekly / monthly totals

For daily / weekly / monthly / yearly kWh totals, the right source is the
**hourly long-term-statistics feed**
(`snopud:energy_consumption_<account>`), which is what the Energy
Dashboard already uses. The Energy Dashboard provides daily/weekly/monthly
breakdowns directly. If you want explicit period-bucket sensors (e.g. for
templating or notifications), Home Assistant's built-in **Utility Meter**
helper can produce them.

A Utility Meter helper requires a `total_increasing` source sensor. The
15-minute sensor in this integration deliberately exposes no
`state_class` (see [the design rationale](#why-the-design-works-around-snopuds-data-lag)),
so it is **not** a valid Utility Meter source. A common workaround in
other integrations is to use the long-term statistic via the
*Statistic-based template* path, but the cleanest approach here is
just to read totals straight off the Energy Dashboard, which is fed by
the same statistic.

Do not configure a Utility Meter against the 15-minute sensor expecting
it to produce a source-timestamp-aligned 15-minute history. Utility
Meter integrates the sensor's state history, and this sensor's state
history is timestamped at integration refresh time (not at the original
SnoPUD interval time) because of the 6–8h portal lag. The result would
not line up with the actual usage windows.

## The two data paths, and which one to use for what

The integration deliberately maintains two parallel feeds from the same
Green Button source. They serve different purposes and should not be
substituted for each other.

| | **Hourly long-term statistics** | **15-minute sensor** |
|---|---|---|
| Statistic / entity | `snopud:energy_consumption_<account>` | `sensor.snopud_meter_<account>_latest_15_min_usage` |
| Granularity | 1 hour | 15 minutes |
| Semantics | cumulative `sum`, monotonic | latest available per-interval kWh, varies up and down |
| State class | n/a (external statistic) | (none — see rationale below) |
| Source-timestamp alignment | yes — each row is keyed on the original SnoPUD hour boundary | yes for the `recent_intervals` attribute (each item carries its original Green Button interval `start`); **no** for the entity's HA state history (HA records state at refresh time, not at the original interval time) |
| Retained | indefinitely (long-term statistics) | rolling 7-day window in `attributes.recent_intervals` (672 buckets, marked `_unrecorded_attributes` so it lives on the live entity state but is not stored in HA's recorder; rehydrated each refresh from a 14-day on-disk JSON archive that survives HA restarts and multi-day outages); the entity's HA state history is governed by HA's recorder retention but, as noted above, it is not aligned to the original SnoPUD interval times |
| Use it for | **Energy Dashboard**, daily/weekly/monthly/yearly totals, anything billing-shaped | "latest available 15-min interval" displays and automations; aligned 15-min bar charts via `recent_intervals` and a chart card such as ApexCharts |
| **Do not** use it for | as a substitute for the 15-minute sensor when you want fine-grain detail | as the Energy Dashboard source; as a Utility Meter source; as the source for any "aligned 15-minute history" view via state history alone |

The next section explains *why* the design splits this way — both why
the sensor exposes no `state_class` or `device_class` at all, and why
aligned 15-minute charts have to read `recent_intervals` rather than
the entity's state history.

### Why the design works around SnoPUD's data lag

Two design decisions follow from one fact: SnoPUD's portal data lags
wall clock by 6–8 hours, and a single refresh often surfaces several
new 15-minute intervals at once (e.g. an 8 PM poll reveals
10 AM–noon).

**Why the sensor exposes no `state_class` or `device_class`.** Neither
of HA's available choices fits a per-interval delta:

* `total_increasing` would tell HA's statistics engine to treat every
  dip in the per-interval kWh value as a meter reset — broken
  arithmetic for a series that legitimately rises and falls.
* `measurement` would tell HA to auto-compile a parallel
  long-term-statistics series (mean / min / max per hour) from the
  entity's state history. But the entity's state history is
  timestamped at *integration refresh time*, not at the original
  SnoPUD interval time, so that auto-LTS series would be misaligned
  in exactly the same way the History Graph card is — and it would
  compete with the integration's own properly-aligned external
  statistic. Worse, transient unit hiccups during HA bootstrap
  (entity briefly unavailable, unit briefly None) lock in a unit-
  incompatibility warning the user has to fix manually via Developer
  Tools → Statistics.
* `device_class = energy` is incompatible with `measurement` under
  HA's post-Jan-2024 sensor validation, and would auto-wire the
  entity into the Energy Dashboard, which expects a cumulative.

The integration writes its own external long-term statistic
(`snopud:energy_consumption_<account>`) from the *hourly* Green Button
feed — that's the canonical cumulative for the Energy Dashboard and
all daily/weekly/monthly totals. The 15-minute sensor stays out of
HA's auto-LTS pipeline so there's exactly one LTS series, properly
aligned, per meter.

**Why charts read `recent_intervals`, not state history.** HA's
recorder stamps each state update at the time HA *received* it. With
SnoPUD's lag, those timestamps are 6–8 hours after the real interval —
several new buckets are recorded with the same poll-time stamp. A
History Graph or Statistics Graph card therefore shows stair-step
jumps at refresh time, not bars aligned to the actual 10:45, 11:00,
11:15… usage windows. The integration sidesteps this by keeping a
rolling per-meter set of every 15-minute bucket it has seen, **keyed
by the original SnoPUD interval start**, and exposing it as
`attributes.recent_intervals`:

```yaml
attributes:
  recent_intervals:
    - start: "2026-04-28T10:45:00-07:00"
      end:   "2026-04-28T11:00:00-07:00"
      kwh:   0.333
    - start: "2026-04-28T11:00:00-07:00"
      end:   "2026-04-28T11:15:00-07:00"
      kwh:   0.310
```

A chart card that positions bars by `recent_intervals[i].start`
plots them at the actual usage time, regardless of when HA polled.
The most recent 672 entries (7 days) are exposed in the attribute;
up to 1344 entries (14 days) are kept in a persisted on-disk archive
that survives HA restarts — see [How the persistent archive
works](#how-the-persistent-archive-works) below.

The native HA History Graph, Statistics Graph, Utility Meter, and any
template sensor that integrates state changes will all show the
shifted, blocky pattern described above — they are not bugs, they are
state-history consumers being honest about when state arrived. They
can be useful for "is the meter alive" liveness checks, but the
aligned 15-minute view lives in `recent_intervals`.

### 15-minute bar chart card (ApexCharts)

[ApexCharts Card](https://github.com/RomRider/apexcharts-card) (HACS) reads
arbitrary entity attributes via its `data_generator`, so it can plot each
bar at the original SnoPUD interval timestamp instead of the time HA
recorded the state update. That is the property we need: each bar's
horizontal position is set by `recent_intervals[i].start`, and each bar's
height is set by `recent_intervals[i].kwh`. Drop the following onto a
Lovelace dashboard, substituting your meter's account number:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: SnoPUD 15-Min Usage
  show_states: true
graph_span: 7d
span:
  end: minute
apex_config:
  chart:
    type: bar
  plotOptions:
    bar:
      columnWidth: 90%
  xaxis:
    type: datetime
series:
  - entity: sensor.snopud_meter_1000000001_latest_15_min_usage
    name: kWh
    type: column
    # The data_generator runs in the browser. ``entity.attributes`` is the
    # extra-state attributes payload from the SnoPUD sensor; we read the
    # rolling ``recent_intervals`` array and project each item into a
    # ``[x, y]`` pair where:
    #   x = new Date(item.start).getTime()  — the ORIGINAL Green Button
    #         interval start, parsed as a UTC ISO string. The chart uses
    #         this as the bar's position on the time axis, so bars line
    #         up with the actual 15-minute usage windows regardless of
    #         when HA polled.
    #   y = Number(item.kwh)                — the kWh consumed during
    #         that interval; the bar's height.
    data_generator: |
      const intervals = entity.attributes.recent_intervals || [];
      return intervals
        .filter(i => i.start && i.kwh !== undefined && i.kwh !== null)
        .map(i => [new Date(i.start).getTime(), Number(i.kwh)]);
```

Why this works under SnoPUD's portal lag: every bar is positioned using
`recent_intervals[i].start`, which is the *original* Green Button
timestamp the meter reported. So when SnoPUD's portal catches up at
8:00 PM and the integration discovers 15-minute intervals from 10:00 AM
through noon, all of those bars land in the late-morning region of the
chart, not at 8:00 PM. The bar chart is therefore aligned to actual
usage time, not to integration refresh time.

### How the persistent archive works

The 15-minute interval data is held in **two tiers**:

| Tier | Where | Default size | Purpose |
|---|---|---|---|
| Chart window | Entity attribute `recent_intervals` | 672 buckets (7 days) | What dashboard cards (ApexCharts, Plotly) plot. The attribute is marked `_unrecorded_attributes` on the sensor class so the recorder skips it on every state change — the live state still carries the full payload for cards to read, but the recorder isn't asked to persist a ~64 KB blob on every refresh. |
| Archive | On-disk JSON file managed by HA's `Store` helper | 1344 buckets (14 days) | Survives HA restarts, integration reloads, and HACS upgrades so the chart is populated immediately on next startup instead of needing to re-fetch a week of data from SnoPUD. |

The archive is a single JSON file at
`<config>/.storage/snopud_<entry_id>_archive`, written once per refresh
and loaded once at startup. It lives outside HA's recorder database, so
growing it doesn't bloat the recorder.

**Lifecycle:**

1. **Fresh install (or any refresh where the archive file is missing for
   a meter):** the integration runs a one-shot 14-day chunked backfill
   from SnoPUD (`SENSOR_INITIAL_BACKFILL_DAYS = 14`). The chart fills
   immediately — you don't have to wait days for it to populate.
2. **Steady state:** each refresh (default once per hour) asks SnoPUD for
   only the most recent 1 day of 15-minute data
   (`SENSOR_LOOKBACK_DAYS = 1`). The archive carries the rest of the
   rolling window forward. The 1-day fetch is enough to cover SnoPUD's
   6–8h portal lag plus a small cushion for short outages.
3. **Restart resilience:** when HA restarts, the archive is loaded into
   memory before the first refresh fires, so the entity is created with
   the full 7-day chart window already populated. The first refresh then
   merges the latest 1 day of data on top.
4. **Extended outage recovery:** the archive's 14-day window means a
   restart after up to ~13 days of HA downtime still rehydrates a full
   chart, rather than truncating to whatever a single 1-day fetch can
   recover.

### Tuning the retention windows

The two tiers are controlled by two constants in `const.py`:

```python
SENSOR_RECENT_INTERVAL_LIMIT = 672    # 7 days  (chart window)
ARCHIVE_INTERVAL_LIMIT = 1344         # 14 days (on-disk archive)
```

To change the chart window, edit `SENSOR_RECENT_INTERVAL_LIMIT`. Each
bucket in the entity attribute is ~95 bytes. The attribute is marked
`_unrecorded_attributes` on the sensor class, so the recorder skips
it on every state change — growing the chart window therefore does
**not** inflate recorder storage. The remaining cost is the in-memory
payload that ships with each state update to subscribed clients
(Lovelace, websocket consumers); at the default 672 the live payload
is ~64 KB, which is well within HA's per-state-change handling
budget. Pushing much beyond ~30 days (2880 buckets, ~270 KB live
payload) is feasible but starts to feel sluggish in cards that parse
the array on every state change.

To change the archive window, edit `ARCHIVE_INTERVAL_LIMIT`. The archive
is one JSON file, not in the recorder, so this is essentially free in
storage terms — at 14 days it's ~200 KB on disk. `const.py` raises
`ValueError` at import time if the archive is set smaller than the
chart window, since slicing a window larger than the trimmed archive
would silently truncate the chart. Increasing
`SENSOR_INITIAL_BACKFILL_DAYS` to match a larger archive size makes
first-setup populate the chart with a longer window immediately
rather than over the next several days.

For 15-minute history beyond ~30 days, mirror `recent_intervals` to
an external timeseries store (InfluxDB, TimescaleDB) — mirroring the
entity's state history inherits the same alignment caveat described
in [the design rationale](#why-the-design-works-around-snopuds-data-lag).
Most users will get what they need from the indefinitely-retained
hourly long-term statistic instead.

## How it works

1. Logs into `my.snopud.com` using your credentials (ASP.NET form auth with
   a cookie-based session).
2. Calls `/Usage/InitializeDownloadSettings` to enumerate meters and harvest
   the CSRF token the portal's own download form uses.
3. On HA startup (before any entity is created), loads the persisted
   15-minute interval archive from
   `<config>/.storage/snopud_<entry_id>_archive` into memory. This is what
   lets the dashboard chart appear with full history immediately rather
   than waiting for SnoPUD to deliver enough buckets to repopulate.
4. For each configured meter, performs **two** Green Button downloads per
   refresh cycle:
   - `SelectedInterval=5` (hourly) for the long-term-statistics feed,
     covering either the configured backfill window on first run or the
     last few days incrementally thereafter.
   - `SelectedInterval=3` (15-minute) for the sensor entity. On first
     setup (or any refresh where the archive is missing for a meter)
     this is a one-shot chunked 14-day backfill so the chart fills
     immediately. In steady state it's just the last day — the
     persisted archive carries the rest of the rolling window forward.
   Both calls submit the same body shape as the portal's own download form:
   `SelectedFormat=1` (Green Button XML), `SelectedUsageType=1` (kWh
   consumption), plus the requested `Start`/`End` date range.
5. Parses each returned Atom feed's `IntervalReading` entries into kWh
   deltas (plus optional cost in USD).
6. Writes the hourly readings into HA's recorder via
   `async_add_external_statistics` (idempotent upsert keyed on
   `(statistic_id, start)`).
7. Merges the parsed 15-minute readings into the per-meter rolling
   archive (deduped by interval start). Publishes the most recent 7 days
   (672 buckets) as the sensor entity's `recent_intervals` extra
   attribute — the data source for dashboard bar-chart cards. The full
   14-day archive is then saved back to the on-disk JSON file for
   restart resilience.

If you enable the "Back-fill billing-interval history" option — either at
setup or later from the integration's Configure menu — the integration
performs a **one-shot** billing-interval fetch (`SelectedInterval=7`) for
each configured meter that hasn't had a billing supplement yet, covering
the range strictly older than that meter's earliest hourly reading. The
billing rows are merged into the existing long-term-statistics series, and
the cumulative `sum` is recomputed across the full timeline so the Energy
Dashboard's totals stay coherent. Each meter's billing-supplement state is
remembered in the config entry's options so the supplement isn't repeated
on every refresh.

## Known limits & retention notes

- **~5–8 hour data lag.** MySnoPUD's data appears in the portal several hours
  behind wall clock. Afternoon readings typically show up the same evening.
  The Energy Dashboard will show "today" as partial until then.
- **SnoPUD's hourly feed caps at ~2 years.** Empirically, SnoPUD's portal
  only returns hourly Green Button data for roughly the last 2 years
  regardless of the requested window. For data older than that, you need
  the billing-interval backfill toggle (under SnoPUD → Configure) — that
  pulls one row per billing month and reaches as far back as your account
  has billing records.
- **Retention, in plain English.** Hourly long-term statistics
  (`snopud:energy_consumption_<account>` and the optional cost series)
  are retained indefinitely — that's the canonical feed behind the
  Energy Dashboard. The 15-minute sensor's HA state history is bound
  by HA's recorder (default 10 days, controlled by `purge_keep_days`,
  not configurable from this integration); the *aligned* 15-minute
  data lives in the entity's `recent_intervals` attribute and the
  integration's own 14-day on-disk archive — see [How the persistent
  archive works](#how-the-persistent-archive-works). For 15-minute
  history beyond ~30 days, mirror `recent_intervals` to an external
  timeseries store (InfluxDB, TimescaleDB).
- **Max download window.** Empirical; the integration defaults to 90-day
  chunks. If you hit errors on backfill, lower `MAX_DOWNLOAD_WINDOW_DAYS` in
  `const.py`.
- **MFA / security questions not supported.** The integration assumes a plain
  email + password login. If MySnoPUD ever requires a second factor and you
  have it enabled, login will fail.
- **Credentials stored in the config entry.** Home Assistant does not
  encrypt config entries at rest — they live in plain JSON inside your HA
  config directory. Keep HA's config directory on an encrypted disk and use
  a strong, unique MySnoPUD password.
- **Retired meters.** If your account has an old non-smart meter that was
  replaced, both will appear in the meter picker. The retired one only
  reports billing-interval totals — enable the "back-fill billing-interval
  history" option if you want that history imported.

## Troubleshooting

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.snopud: debug
```

Common failure modes:

| Log line | Meaning |
|---|---|
| `authentication failed: login rejected: ...` | Credentials wrong. Verify login works in a browser first. |
| `authentication has failed N times in a row — refusing to retry` | Circuit breaker tripped after 3 consecutive auth failures. Reload the integration to reset (typically happens after a password change). |
| `session expired (login form returned)` | Session cookie dropped mid-session; the integration will re-login on the next refresh. |
| `server returned an error page instead of Green Button XML` | The `/Usage/Download` form validation failed — most often a bad date range or a meter that isn't selectable on your account. |
| `could not locate CSRF token in settings response` | The portal's UI changed. The regex in `snopud_client.py` needs updating. |

## Re-running the backfill

Most re-import scenarios don't require deleting anything:

- **Need older billing-interval history?** Open Settings → Devices &
  Services → SnoPUD → Configure and turn on "Back-fill billing-interval
  history." The next refresh will run a one-shot retroactive import for
  any meter that hasn't already had one.
- **Need a deeper import than you originally set up with?** Raise the
  "Initial hourly backfill window (days)" option. On the next refresh the
  integration will re-import both the hourly series (idempotently — no
  data destruction) and, if billing-backfill is on, the billing-interval
  series, now reaching the new, wider horizon. Note that SnoPUD's hourly
  feed caps at ~2 years regardless of how high you set this — for data
  older than that you need billing-interval backfill enabled.

Only if the existing series has drifted or is genuinely corrupt do you
need the nuclear option:

1. Remove the integration (Settings → Devices & Services → SnoPUD → ⋮ → Delete).
2. In **Developer Tools → Statistics**, find each `snopud:...` series and
   delete it.
3. Re-add the integration.

## Credits & prior art

- Home Assistant's built-in **Opower** integration is the reference pattern
  for "utility-portal to external statistics" integrations.
- **`ha-green-button`** parses ESPI feeds from files on disk. The parser
  here is narrower and in-process.
- The **Green Button Alliance** / NAESB ESPI specification for the XML
  format.

## Changelog

### v0.2.11 — drop sensor `state_class` to suppress conflicting auto-LTS

Follow-up to v0.2.10. After dropping `device_class=energy`, HA's
sensor recorder began auto-compiling a long-term-statistics series
from the 15-minute sensor's state history (because the entity still
had `state_class=measurement` and a unit). That auto-LTS competed
with the integration's own external statistic and produced a
"unit cannot be converted to the unit of previously compiled
statistics" warning whenever a transient bootstrap state had left
the auto-LTS rows tagged with a different unit (or `None`).

**Fix:** the 15-minute sensor now exposes neither `state_class` nor
`device_class`. The unit is still `kWh`. HA's recorder no longer
auto-compiles LTS for this entity, so there is exactly one LTS
series per meter — the integration's own
`snopud:energy_consumption_<account>` external statistic, written
from the hourly Green Button feed. See [Why the design works around
SnoPUD's data lag](#why-the-design-works-around-snopuds-data-lag)
for the full reasoning.

**One-time cleanup after upgrading** (only if you saw the
"unit cannot be converted…" warning before upgrading): open
**Developer Tools → Statistics** in Home Assistant. Find the row
for `sensor.snopud_meter_<account>_latest_15_min_usage` flagged
with a unit issue and click **Fix issue → Delete**. This removes
the orphaned auto-LTS rows; nothing important is lost because the
canonical feed lives at `snopud:energy_consumption_<account>`,
which is unaffected. Fresh installs see no orphaned rows and need
no cleanup.

**No impact on:**

* The Energy Dashboard — uses the external statistic, unchanged.
* The 15-minute bar chart card — reads `recent_intervals`, unchanged.
* Automations and templates that read the sensor's current state —
  the value, unit, and entity ID are all unchanged.

If a Utility Meter helper was wired against this sensor, it has not
been a valid source since v0.2.10 (the integration warned about
this) and will need to be replaced with a Statistic-based template
against the long-term statistic, or removed.

### v0.2.10 — sensor recorder fixes (post-v0.2.9 patch)

Two HA log warnings surfaced after v0.2.9 raised the chart window to 7
days; both are addressed here.

* **`recent_intervals` no longer trips the recorder's 16 KB attribute
  cap.** With 672 buckets the attribute is ~64 KB, well over HA's per-
  state-change cap. The attribute is now marked `_unrecorded_attributes`
  on the sensor class — it stays on the live entity state (so
  ApexCharts / Plotly cards still see it), but the recorder skips it.
  The other small attributes (`latest_interval_*`, `data_lag_minutes`)
  continue to be recorded normally.
* **Sensor no longer warns about device-class / state-class mismatch.**
  HA's validator rejects `device_class=energy` with
  `state_class=measurement`. Since the value is a per-interval delta
  (not a monotonic counter), `measurement` is the honest state class
  — so `device_class` has been dropped. No impact on the Energy
  Dashboard (which uses the long-term statistic feed). If an
  automation or template filtered on `device_class == 'energy'`,
  switch to `unit_of_measurement == 'kWh'` or filter by entity ID.
* **Defensive guard:** `const.py` raises `ValueError` at import time
  if `ARCHIVE_INTERVAL_LIMIT < SENSOR_RECENT_INTERVAL_LIMIT`, which
  would otherwise silently truncate the chart.

### v0.2.9 — persistent 15-minute archive, 7-day chart, smaller refresh footprint

**What changed**

* The 15-minute interval data is now held in two tiers (see [How the
  persistent archive works](#how-the-persistent-archive-works) for full
  details):
  - **Chart window** — the entity's `recent_intervals` attribute exposes
    the most recent **7 days** (672 buckets). The previous default was
    48 hours.
  - **On-disk archive** — a 14-day JSON archive
    (`<config>/.storage/snopud_<entry_id>_archive`) survives HA
    restarts, integration reloads, and HACS upgrades. The archive lives
    outside HA's recorder DB so it doesn't bloat recorder storage.
* **Steady-state SnoPUD fetches are smaller.** Per-refresh 15-minute
  download is now 1 day (`SENSOR_LOOKBACK_DAYS = 1`), down from 3 days.
  The persisted archive carries the rest of the rolling window forward
  across refreshes, so the smaller fetch is sufficient — it just needs
  to cover SnoPUD's 6–8h portal lag plus a small cushion. Net effect:
  ~67% less data downloaded per refresh, while users see a ~3.5×
  longer chart.
* **First-setup populates the chart immediately.** On a fresh install
  (or any refresh where the archive file is missing for a meter), the
  integration runs a one-shot chunked 14-day backfill instead of
  ramping up over the next two weeks as single-day fetches accumulate.
* **Restart resilience.** When HA restarts, the archive is loaded into
  memory before the first refresh, so the entity is created with the
  full 7-day chart already populated. No empty-chart period during
  startup.
* **Two new constants in `const.py`:** `SENSOR_INITIAL_BACKFILL_DAYS`
  (one-shot fill window, default 14) and `ARCHIVE_INTERVAL_LIMIT`
  (on-disk archive cap, default 1344 buckets ≈ 14 days). Tuning
  guidance is in the
  [Tuning the retention windows](#tuning-the-retention-windows) section.

**Why**

Users wanted a longer dashboard window (a week of 15-minute usage is
very useful for spotting weekly patterns), but cranking up
`SENSOR_LOOKBACK_DAYS` would have meant fetching a week of data from
SnoPUD on every hourly refresh — inefficient and pushing the portal's
single-request size envelope. Persisting the rolling window separates
"what we ask SnoPUD for" from "what users see in the chart": the fetch
stays small while the chart can be much larger. The archive also lets
the integration recover full chart history after HA outages, which the
old design couldn't do without re-fetching from SnoPUD.

**Action required after upgrading**

* No action required for the Energy Dashboard — it reads the hourly
  long-term statistic, unchanged.
* No action required to use the new 7-day chart — your existing
  ApexCharts card will pick up the longer window automatically. If you
  want the card's visible span to match, change `graph_span: 48h` to
  `graph_span: 7d` in your card YAML (the README example has been
  updated).
* Existing installs upgrading from v0.2.8 will run the one-shot 14-day
  backfill on the first refresh after the upgrade (since the archive
  file doesn't exist yet). Expect that first refresh to take a bit
  longer than usual; subsequent refreshes return to the small 1-day
  fetch.

### v0.2.8 — bound recorder block_till_done; ships v0.2.7 redesign

**What changed**

* **Bug fix.** The coordinator's `_seed_cumulative_from_stats` awaited the
  recorder's `async_block_till_done` without a timeout. During Home
  Assistant bootstrap the recorder worker can still be starting up, in
  which case the integration's first refresh would hang on that await
  forever, eventually tripping HA's stage-2 setup timeout (5 minutes) and
  leaving the integration stuck on "Initializing". The await is now
  bounded to 5 seconds; on timeout the seed read proceeds against
  whatever is currently persisted, and the unlatched re-seed on the next
  refresh self-heals against any transient stale read.
* Includes the previously-unreleased v0.2.7 work: redesigned 15-minute
  sensor (new `unique_id`, `state_class=measurement`, `recent_intervals`
  attribute) and the README documentation pass. The v0.2.7 tag was
  prepared but not pushed because the upgrade exposed the bootstrap
  hang above; v0.2.8 is the first public release of the redesigned
  sensor.

**Action required after upgrading** — same as v0.2.7's notes: delete or
hide the old `sensor.snopud_meter_<account>_energy` entity from
**Settings → Devices & Services → Entities**, and repoint any Lovelace
card that referenced it at the new
`sensor.snopud_meter_<account>_latest_15_min_usage`.

### v0.2.7 — redesigned 15-minute sensor with `recent_intervals` array

**What changed**

* The 15-minute sensor entity is now published on a fresh `unique_id`
  (`snopud_<account>_latest_15min_usage`) — HA will create a **new entity**
  on upgrade, e.g. `sensor.snopud_meter_<account>_latest_15_min_usage`.
* The sensor's state is the kWh value of the most recent complete
  15-minute interval, with `state_class=measurement` (no longer
  `total_increasing`, no longer cumulative).
* New `attributes.recent_intervals` array holds the last 192 buckets
  (48 hours) of 15-minute readings, each carrying its **original SnoPUD
  interval start/end timestamp** plus `kwh` (and `cost_usd` when present).
  Designed for ApexCharts / Plotly bar-chart cards — see the example in
  [15-minute bar chart card](#15-minute-bar-chart-card-apexcharts).
* New attribute fields: `latest_interval_start`, `latest_interval_end`,
  `latest_interval_kwh`, `latest_interval_cost_usd`, `data_lag_minutes`.

**Why**

The previous 15-minute sensor mixed cumulative-style and per-interval
semantics in the same entity history, and was a `total_increasing` sensor
fed values that naturally rise and fall — which broke HA's statistics
engine and produced misleading history graphs. SnoPUD's 6–8h portal lag
also meant that single refreshes could surface several new intervals at
once, but the old design only published the latest one. The new
`recent_intervals` attribute merges every newly-discovered bucket
(deduped by interval start) into a rolling window so the bar chart
catches up as the portal does.

**Action required after upgrading**

* No action required for the Energy Dashboard — it reads from the hourly
  long-term statistic, which is unchanged.
* The old `sensor.snopud_meter_<account>_energy` entity is left in place
  but no longer updated. Open **Settings → Devices & Services →
  Entities**, filter by integration "SnoPUD", and either delete or hide
  the old entity. HA does **not** auto-migrate its history to the new
  entity — the old history is contaminated and not worth carrying
  forward.
* If you had a Lovelace card pointing at the old entity, repoint it at
  `sensor.snopud_meter_<account>_latest_15_min_usage`.

### v0.2.6 — sensor publishes latest 15-min interval, not a cumulative

* Sensor entity stopped synthesising a monotonic cumulative kWh and
  started publishing the latest 15-min slice value as `measurement`. This
  release kept the original `unique_id` and entity slug, so its history
  still mixed pre- and post-redesign rows; v0.2.7 splits those by issuing
  a fresh `unique_id`.

## License

MIT.

[privacy]: https://www.snopud.com/privacy-policy/
[tc]: https://www.snopud.com/wp-content/uploads/2021/08/MySnoPUD_TC.pdf
[issues]: https://github.com/ambrglow/ha-snopud-community/issues
