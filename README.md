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
- Cumulative kWh counters seeded from persisted statistics on every HA
  restart, so the sensor's `total_increasing` value continues monotonically
  rather than producing a sawtooth.
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
sensor.snopud_meter_1000000001_energy
```

The sensor's state is a cumulative-kWh counter (`state_class=total_increasing`),
fed by 15-minute Green Button readings. Use it for Lovelace cards, the
Statistics or Statistics-Graph helpers, and automations. The `latest_reading_*`
attributes carry the most recent 15-minute slice for automations that want a
"what just happened" trigger.

You can point the Energy Dashboard at *either* the external statistic or the
sensor — the external statistic is the canonical, idempotently-upserted feed
and is what we recommend.

### Options (adjustable after setup)

Open **Settings → Devices & Services → SnoPUD → Configure** to change:

- **Refresh interval** (15 min – 12 h, default 1 h). SnoPUD's data lags the
  wall clock by ~5–8 hours, so more frequent polling gains nothing real.
- **Initial hourly backfill window** (7 days – 5 years, default 730 days).
  Only takes effect on the *first* import of a given meter — once a meter
  has been backfilled, raising this won't pull older history. To re-run a
  backfill, see "Re-running the backfill" below.
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

Home Assistant ships a built-in **Utility Meter** helper that's purpose-built
for this. Add it once (Settings → Devices & Services → Helpers → Utility
Meter) pointing at the integration's sensor or long-term statistic and it
will produce daily, weekly, monthly, and yearly totals automatically,
including tariff-aware variants. That's a better fit for "how many kWh this
week" questions than adding separate sensors here.

## How it works

1. Logs into `my.snopud.com` using your credentials (ASP.NET form auth with
   a cookie-based session).
2. Calls `/Usage/InitializeDownloadSettings` to enumerate meters and harvest
   the CSRF token the portal's own download form uses.
3. For each configured meter, performs **two** Green Button downloads per
   refresh cycle:
   - `SelectedInterval=5` (hourly) for the long-term-statistics feed,
     covering either the configured backfill window on first run or the
     last few days incrementally thereafter.
   - `SelectedInterval=3` (15-minute) for the sensor entity, covering a
     short rolling lookback so dashboards stay current.
   Both calls submit the same body shape as the portal's own download form:
   `SelectedFormat=1` (Green Button XML), `SelectedUsageType=1` (kWh
   consumption), plus the requested `Start`/`End` date range.
4. Parses each returned Atom feed's `IntervalReading` entries into kWh
   deltas (plus optional cost in USD).
5. Writes the hourly readings into HA's recorder via
   `async_add_external_statistics` (idempotent upsert keyed on
   `(statistic_id, start)`).
6. Updates the sensor's cumulative-kWh state from the 15-minute readings,
   seeding the cumulative counter from the persisted long-term statistics
   on the first update after each restart.

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
- **Retention, in plain English.** Home Assistant keeps two kinds of
  history, and this integration uses both:
  1. **Long-term statistics** are kept indefinitely (they're the series
     behind the Energy Dashboard). The integration writes the hourly feed
     straight into LTS as `snopud:energy_consumption_<account>` (and
     `snopud:energy_cost_<account>` when cost is available). Once imported,
     those points are retained forever.
  2. **Sensor state history** (the 15-minute readings published by
     `sensor.snopud_meter_<account>_energy`) is kept by HA's **recorder**,
     which by default purges state history older than `purge_keep_days`
     (10 days). HA's recorder also *auto-generates* hourly long-term
     aggregates from any sensor with `state_class=total_increasing`, so the
     hourly shape of the 15-minute series is retained forever even after
     the raw 15-minute state rows are purged. If you want raw 15-minute
     state kept forever like a temperature sensor, raise
     `recorder:` / `purge_keep_days` in `configuration.yaml`, or mirror the
     sensor out to an external timeseries store (InfluxDB, TimescaleDB, etc.)
     using HA's existing integrations. The SnoPUD integration can't change
     HA's recorder retention from its own config.
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

If you just want to pull older **billing-interval** history for a meter
that's already on your account, you don't need to delete anything — open
Settings → Devices & Services → SnoPUD → Configure and turn on "Back-fill
billing-interval history." The next refresh will run a one-shot retroactive
import for any meter that hasn't already had one.

If you want to wipe and re-import **all** history from scratch — e.g. to
apply a longer `backfill_days` window to a meter that's already been
backfilled, or because the existing series has drifted — do this:

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

## License

MIT.

[privacy]: https://www.snopud.com/privacy-policy/
[tc]: https://www.snopud.com/wp-content/uploads/2021/08/MySnoPUD_TC.pdf
[issues]: https://github.com/ambrglow/ha-snopud-community/issues
