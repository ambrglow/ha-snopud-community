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

Credentials are stored in Home Assistant's config entry on your own device,
encrypted at rest by HA. They are never transmitted anywhere except to
`my.snopud.com` during normal authentication. This repository contains no
account data, usage data, or credentials.

## Features

- Hourly interval data, written into HA's long-term statistics so the Energy
  Dashboard can display it directly.
- Optional 15-minute-grain sensor entity (one per meter) for real-time
  dashboards and automations.
- Optional cost series (USD), when SnoPUD includes a `cost` column in the
  export — no tariff configuration required on the HA side.
- Initial backfill of up to 2 years of hourly history on first setup.
- Optional billing-interval backfill for older retired / non-smart meters
  that predate your smart-meter upgrade.
- Configurable refresh interval (15 min – 12 h; default 1 h).
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

### Options (adjustable after setup)

Open **Settings → Devices & Services → SnoPUD → Configure** to change:

- **Refresh interval** (15 min – 12 h, default 1 h). SnoPUD's data lags the
  wall clock by ~5–8 hours, so more frequent polling gains nothing real.
- **Back-fill billing-interval history.** Enable this when your account has
  a pre-smart-meter period you want to import. The integration will request
  monthly billing-interval totals for any date range the hourly feed
  couldn't reach.

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
3. Calls `/Usage/Download` with the same form body the portal's download
   button submits: `SelectedFormat=1` (Green Button XML), `SelectedInterval`
   = hourly (or billing, for the retired-meter path), `SelectedUsageType=1`
   (kWh consumption), and a `Start`/`End` date range.
4. Parses the returned Atom feed's `IntervalReading` entries into kWh deltas
   (plus optional cost in USD).
5. Writes the cumulative sum as an external statistic via
   `async_add_external_statistics`.

## Known limits

- **~5–8 hour data lag.** MySnoPUD's data appears in the portal several hours
  behind wall clock. Afternoon readings typically show up the same evening.
  The Energy Dashboard will show "today" as partial until then.
- **Hourly resolution in HA's long-term statistics.** HA's external-statistics
  API requires hour-aligned timestamps, so sub-hourly resolution isn't kept
  in long-term storage. The optional 15-min sensor entity preserves the
  finer grain in short-term history (typically 10 days).
- **Max download window.** Empirical; the integration defaults to 90-day
  chunks. If you hit errors on backfill, lower `MAX_DOWNLOAD_WINDOW_DAYS` in
  `const.py`.
- **MFA / security questions not supported.** The integration assumes a plain
  email + password login. If MySnoPUD ever requires a second factor and you
  have it enabled, login will fail.
- **Credentials stored in the config entry.** Same as most HA cloud
  integrations. Use a strong password that isn't reused anywhere.
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

If you want to wipe and re-import history:

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
