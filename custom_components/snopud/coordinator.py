"""Data update coordinator for SnoPUD.

Runs on a scheduled interval (configurable via options; default 1 hour),
authenticates if needed, then fetches Green Button XML for each configured
meter on **two parallel grains**:

* **Hourly** — written into Home Assistant's long-term statistics so the
  Energy Dashboard can consume it directly. Required because
  ``async_add_external_statistics`` only accepts hour-aligned timestamps.
* **15-minute** — published as the sensor entity's state, so users can wire
  it into ordinary Lovelace cards and automations at the granularity the
  meter actually reports.

On first run for each meter we additionally perform a chunked hourly backfill
covering up to ``CONF_BACKFILL_DAYS`` (default 730). Independently, if
``CONF_ENABLE_BILLING_BACKFILL`` is enabled — either at initial setup or
switched on later via the options flow — we perform a one-shot chunked
billing-interval backfill for retired non-smart meters that predate the
smart-meter upgrade. Billing-backfill state is tracked per-meter in
``entry.options[CONF_BILLING_BACKFILLED_METERS]`` separately from the hourly
backfill so enabling the option post-setup actually triggers the import.

To survive Home Assistant restarts without producing a sawtooth in the
sensor's monotonic ``cumulative_kwh`` value, we seed both the in-process
cumulative counters **and** the per-meter "last-seen reading" cursor from
the persisted long-term-statistics feed on the first successful update after
construction. Seeding the cursor is critical: without it, the restart path
would reseed the cumulative from hourly stats and then re-add the most
recent 15-minute readings on top — producing a post-restart counter that
over-counts the last SENSOR_LOOKBACK_DAYS of consumption.

The cursor is set to ``last_hourly_start + 1h - 1µs`` — i.e. the last
microsecond of the last sealed hour — so that every 15-minute reading
falling inside that hour is correctly skipped on first advance (those
readings are already summed into the hourly stats row), while the first
reading of the next hour (``start = last_hourly_start + 1h``) is accepted.

Meters that have already been backfilled (hourly and/or billing-interval)
are tracked in ``entry.options`` so repeat backfills don't happen on every
restart.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

# One-microsecond epsilon used when seeding ``_last_seen_cumulative`` from the
# last persisted *hourly* stat. An hourly row starting at T covers [T, T+1h),
# so the latest 15-minute slice already accounted for by that row starts at
# T+45min (ends at T+1h). We set the cursor to T+1h-1µs so every 15-min slice
# inside the hour is skipped by the ``<=`` check in ``_advance_cumulative``,
# and the next hour's first slice (start=T+1h) is still accepted.
_HOURLY_END_EPSILON = timedelta(hours=1) - timedelta(microseconds=1)
from typing import Any

import aiohttp
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.components.recorder.util import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BACKFILL_DAYS,
    CONF_BACKFILLED_METERS,
    CONF_BILLING_BACKFILLED_METERS,
    CONF_ENABLE_BILLING_BACKFILL,
    CONF_LAST_APPLIED_BACKFILL_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_SENSOR_INTERVAL,
    DEFAULT_STATISTICS_INTERVAL,
    DOMAIN,
    INTERVAL_BILLING,
    MAX_BACKFILL_DAYS,
    MAX_DOWNLOAD_WINDOW_DAYS,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_BACKFILL_DAYS,
    MIN_SCAN_INTERVAL_MINUTES,
    SENSOR_LOOKBACK_DAYS,
)
from .green_button import GreenButtonFeed, IntervalReading, parse_green_button
from .snopud_client import (
    MeterInfo,
    SnoPUDAuthError,
    SnoPUDClient,
    SnoPUDDownloadError,
    SnoPUDError,
)
from .statistics import (
    async_import_billing_supplement,
    async_import_readings,
    energy_statistic_id,
    cost_statistic_id,
)

_LOGGER = logging.getLogger(__name__)


def _resolve_scan_interval(entry: ConfigEntry) -> timedelta:
    """Resolve the configured scan interval from entry.options, clamped."""
    raw = entry.options.get(
        CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
    )
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = DEFAULT_SCAN_INTERVAL_MINUTES
    minutes = max(MIN_SCAN_INTERVAL_MINUTES, min(MAX_SCAN_INTERVAL_MINUTES, minutes))
    return timedelta(minutes=minutes)


def _resolve_backfill_days(entry: ConfigEntry) -> int:
    """Resolve the configured initial-backfill window length, clamped."""
    raw = entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = DEFAULT_BACKFILL_DAYS
    return max(MIN_BACKFILL_DAYS, min(MAX_BACKFILL_DAYS, days))


class SnoPUDCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates periodic fetch + statistics import."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry: ConfigEntry,
        email: str,
        password: str,
        meter_account_numbers: list[str],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id}",
            update_interval=_resolve_scan_interval(entry),
        )
        self._entry = entry
        self._email = email
        self._password = password
        self._requested_accounts = list(meter_account_numbers)
        # In-memory cache of backfilled accounts. On first construction, seed
        # from entry.options so a restart doesn't repeat the full backfill.
        persisted = entry.options.get(CONF_BACKFILLED_METERS, [])
        self._backfilled: set[str] = set(persisted)
        # Independent cache of meters that have had a one-shot billing-interval
        # supplement run. Tracked separately so enabling the billing-backfill
        # option *after* setup still triggers the retroactive import for any
        # meter that hasn't had it yet.
        persisted_billing = entry.options.get(CONF_BILLING_BACKFILLED_METERS, [])
        self._billing_backfilled: set[str] = set(persisted_billing)
        # Cumulative kWh/USD per meter. We *seed* these from
        # get_last_statistics on the first successful update per meter so that
        # the sensor's TOTAL_INCREASING value continues monotonically across
        # restarts (otherwise it would reset to 0, producing a sawtooth and
        # confusing every consumer downstream — Utility Meter especially).
        self._cumulative_kwh: dict[str, float] = {}
        self._cumulative_cost_usd: dict[str, float] = {}
        self._cumulative_seeded: set[str] = set()
        # Per-meter "latest reading timestamp already reflected in the
        # cumulative counter". Used by ``_advance_cumulative`` to skip slices
        # we've already added. Seeded from persisted hourly stats so restarts
        # don't double-count the recent 15-minute rolling window — see
        # ``_seed_cumulative_from_stats``.
        self._last_seen_cumulative: dict[str, datetime] = {}
        # Circuit breaker state. After repeated auth failures we stop trying
        # until the config entry is reloaded, so a rotated password doesn't
        # cause us to repeatedly submit wrong credentials.
        self._consecutive_auth_failures = 0
        self._auth_failure_threshold = 3

    @property
    def requested_accounts(self) -> list[str]:
        return list(self._requested_accounts)

    def apply_options(self) -> None:
        """Re-read options from the config entry and apply them live.

        Called from the integration's options-update listener so users can
        change settings without a full Home Assistant restart.

        * ``scan_interval_minutes`` — applied to the coordinator's update
          interval immediately.
        * ``backfill_days`` — read fresh at the start of each hourly-backfill
          attempt; doesn't retroactively re-import history for a meter that's
          already been backfilled.
        * ``enable_billing_backfill`` — takes effect on the very next
          coordinator refresh: any configured meter that hasn't had a billing
          supplement run yet will get one. Flipping this off again does
          nothing to already-imported billing data.
        """
        new_interval = _resolve_scan_interval(self._entry)
        if new_interval != self.update_interval:
            _LOGGER.info(
                "SnoPUD scan interval changed to %s",
                new_interval,
            )
            self.update_interval = new_interval
        # The billing-backfill toggle and backfill-days knob are read fresh on
        # the next refresh — no state to mutate here.

    async def _persist_backfilled(self) -> None:
        """Persist the set of already-backfilled meters into entry.options."""
        current = self._entry.options.get(CONF_BACKFILLED_METERS, [])
        new = sorted(self._backfilled)
        if sorted(current) == new:
            return
        new_options = {**self._entry.options, CONF_BACKFILLED_METERS: new}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

    async def _persist_billing_backfilled(self) -> None:
        """Persist the set of meters that have had billing supplement run."""
        current = self._entry.options.get(CONF_BILLING_BACKFILLED_METERS, [])
        new = sorted(self._billing_backfilled)
        if sorted(current) == new:
            return
        new_options = {**self._entry.options, CONF_BILLING_BACKFILLED_METERS: new}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

    async def _maybe_reset_backfill_for_widened_window(self) -> None:
        """If ``backfill_days`` has been raised since we last honored it,
        clear the per-meter backfill flags so the next cycle re-imports the
        newly-uncovered range.

        Without this, raising ``backfill_days`` from (e.g.) 730 to 1359 in
        the options flow has no effect — both the hourly and billing flags
        are latched, so subsequent refreshes only fetch the rolling 3-day
        window. We compare the current value to the last persisted
        "honored" value; on increase, clear flags and persist; on lower or
        equal, just record the new value silently.

        ``async_add_external_statistics`` is idempotent on
        (statistic_id, start), so a re-import is non-destructive — at worst
        it re-writes existing rows with the same values.
        """
        new_days = _resolve_backfill_days(self._entry)
        last_raw = self._entry.options.get(CONF_LAST_APPLIED_BACKFILL_DAYS)
        if last_raw is None:
            # First-ever refresh on this entry (or first refresh on a v0.2.3
            # upgrade from an older release). Just record the current value
            # so future increases are detectable. We deliberately do NOT
            # clear flags here — that would cause every existing user to
            # re-import on the upgrade, which is surprising.
            await self._persist_last_applied_backfill_days(new_days)
            return
        try:
            last_days = int(last_raw)
        except (TypeError, ValueError):
            last_days = new_days
        if new_days > last_days:
            _LOGGER.info(
                "backfill_days raised from %d to %d — clearing backfill "
                "flags so the next refresh re-imports the additional %d "
                "days of history",
                last_days, new_days, new_days - last_days,
            )
            self._backfilled.clear()
            self._billing_backfilled.clear()
            # Persist cleared per-meter sets together with the new
            # last-applied value, so a HA restart between this point and
            # the next refresh doesn't lose the reset.
            new_options = {
                **self._entry.options,
                CONF_BACKFILLED_METERS: [],
                CONF_BILLING_BACKFILLED_METERS: [],
                CONF_LAST_APPLIED_BACKFILL_DAYS: new_days,
            }
            self.hass.config_entries.async_update_entry(
                self._entry, options=new_options
            )
        elif new_days != last_days:
            # Lowered. No need to re-import (data we already have stays);
            # just record the new value silently.
            await self._persist_last_applied_backfill_days(new_days)

    async def _persist_last_applied_backfill_days(self, days: int) -> None:
        """Persist the value of backfill_days the coordinator last honored."""
        if self._entry.options.get(CONF_LAST_APPLIED_BACKFILL_DAYS) == days:
            return
        new_options = {
            **self._entry.options,
            CONF_LAST_APPLIED_BACKFILL_DAYS: days,
        }
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options
        )

    async def _reseed_cumulative_from_stats(self, account_number: str) -> None:
        """Force-refresh the in-process cumulative counters from persisted
        stats, even if we've already seeded once. Used after a retroactive
        billing supplement, which changes the persisted ``sum`` for prior
        rows and would otherwise leave the in-process counter stale."""
        self._cumulative_seeded.discard(account_number)
        await self._seed_cumulative_from_stats(account_number)

    async def _seed_cumulative_from_stats(self, account_number: str) -> None:
        """Restart-sawtooth fix: load the most recent persisted statistics
        ``sum`` for each meter and use it as the starting cumulative value
        for the in-process counter. Also seeds ``_last_seen_cumulative`` so
        that 15-minute readings already summed into the persisted hourly
        stats aren't re-added on the first post-restart advance. Idempotent
        per meter unless explicitly cleared (see
        ``_reseed_cumulative_from_stats``)."""
        if account_number in self._cumulative_seeded:
            return
        recorder = get_instance(self.hass)

        async def _last_sum_and_start(stat_id: str) -> tuple[float, datetime | None]:
            last = await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, stat_id, True, {"sum", "start"}
            )
            rows = last.get(stat_id) or []
            if not rows:
                return 0.0, None
            row = rows[0]
            try:
                total = float(row.get("sum") or 0.0)
            except (TypeError, ValueError):
                total = 0.0
            start = row.get("start")
            if start is None:
                return total, None
            if not hasattr(start, "tzinfo"):
                # Some recorder versions hand back an epoch-seconds float.
                try:
                    start = datetime.fromtimestamp(float(start), tz=timezone.utc)
                except (TypeError, ValueError):
                    return total, None
            return total, start

        kwh_seed, kwh_last_start = await _last_sum_and_start(
            energy_statistic_id(account_number)
        )
        cost_seed, cost_last_start = await _last_sum_and_start(
            cost_statistic_id(account_number)
        )
        self._cumulative_kwh[account_number] = kwh_seed
        self._cumulative_cost_usd[account_number] = cost_seed

        # Seed the "last seen" cursor to the last microsecond of the last
        # sealed hour (T + 1h - 1µs). This ensures every 15-min slice *inside*
        # that hour is skipped on first advance (they're already accounted
        # for in the hourly sum we just seeded from), while the next hour's
        # first slice at T+1h passes through.
        if kwh_last_start is not None:
            self._last_seen_cumulative[account_number] = (
                kwh_last_start + _HOURLY_END_EPSILON
            )
        else:
            # No persisted stats → no cursor → first advance will add every
            # reading it's given. That's the correct behaviour for a
            # first-ever import (before any hourly row has been written).
            self._last_seen_cumulative.pop(account_number, None)

        self._cumulative_seeded.add(account_number)
        if kwh_seed or cost_seed or kwh_last_start is not None:
            _LOGGER.debug(
                "seeded cumulative counters for %s from prior stats: "
                "kWh=%.3f, USD=%.2f, last_hourly_start=%s",
                account_number, kwh_seed, cost_seed,
                kwh_last_start.isoformat() if kwh_last_start else None,
            )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest readings for each configured meter and import to statistics."""
        # Detect a widened backfill window (user raised CONF_BACKFILL_DAYS in
        # the options flow) and clear the per-meter backfill latches so the
        # next pass re-imports the newly-uncovered range. Must run *before*
        # the auth/fetch loop so the flag clears take effect this cycle.
        await self._maybe_reset_backfill_for_widened_window()

        # Circuit breaker: if we've had repeated auth failures, stop trying.
        # The user must reload the config entry (typically after updating their
        # password) to reset. This avoids repeatedly submitting known-bad
        # credentials, which would be indistinguishable from a credential-
        # stuffing attempt from the server's point of view.
        if self._consecutive_auth_failures >= self._auth_failure_threshold:
            raise UpdateFailed(
                f"authentication has failed {self._consecutive_auth_failures} "
                f"times in a row — refusing to retry until the config entry "
                f"is reloaded. If you changed your MySnoPUD password, remove "
                f"and re-add the integration."
            )

        # Use an isolated cookie jar so our long-lived auth cookies don't
        # pollute HA's shared aiohttp session.
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=False)
        ) as session:
            client = SnoPUDClient(session, self._email, self._password)
            try:
                await client.async_login()
            except SnoPUDAuthError as err:
                self._consecutive_auth_failures += 1
                raise UpdateFailed(
                    f"authentication failed ({self._consecutive_auth_failures}"
                    f"/{self._auth_failure_threshold} before circuit breaker "
                    f"trips): {err}"
                ) from err
            except SnoPUDError as err:
                raise UpdateFailed(f"login error: {err}") from err
            self._consecutive_auth_failures = 0

            try:
                all_meters = await client.async_get_meters()
            except SnoPUDError as err:
                raise UpdateFailed(f"could not enumerate meters: {err}") from err

            selected = [
                m for m in all_meters
                if m.account_number in set(self._requested_accounts)
            ]
            if not selected:
                raise UpdateFailed(
                    f"none of the configured meters {self._requested_accounts} "
                    f"are present on this account"
                )

            result: dict[str, Any] = {"meters": {}}
            today = datetime.now(timezone.utc).date()

            for meter in selected:
                # === Hourly path → Long-term statistics → Energy Dashboard ===
                try:
                    hourly_feed = await self._fetch_hourly_for_meter(
                        client, meter, all_meters, today
                    )
                except SnoPUDDownloadError as err:
                    _LOGGER.warning(
                        "hourly download failed for meter %s: %s",
                        meter.account_number, err,
                    )
                    hourly_feed = GreenButtonFeed(
                        reading_type=None, readings=[], usage_point_id=None
                    )

                if hourly_feed.readings:
                    await async_import_readings(
                        self.hass,
                        entry_id=self._entry.entry_id,
                        meter=meter,
                        readings=hourly_feed.readings,
                    )

                # === Billing-interval supplement (one-shot per meter) ===
                # Runs only when the option is enabled and this meter hasn't
                # had the supplement yet. Handles both the "enabled at initial
                # setup" case and the "enabled later from the options flow"
                # case, so a user can switch it on at any time.
                try:
                    billing_added = await self._run_billing_supplement_if_needed(
                        client, meter, all_meters, today, hourly_feed,
                    )
                except Exception as err:  # noqa: BLE001 — never fail whole refresh
                    _LOGGER.warning(
                        "billing-interval supplement raised for meter %s: %s",
                        meter.account_number, err,
                    )
                    billing_added = 0

                # Seed cumulative counters and last-seen cursor *after* the
                # hourly import + optional billing supplement so the seed
                # reflects the freshly-written state of the persisted stats.
                # This is what keeps first-ever runs (empty stats at start,
                # full backfill written during the refresh) and post-restart
                # runs (stats already populated) both produce a counter that
                # lines up with the persisted hourly ``sum``. Idempotent
                # per meter per session.
                await self._seed_cumulative_from_stats(meter.account_number)

                # === 15-min path → sensor state ===
                try:
                    sensor_feed = await self._fetch_sensor_for_meter(
                        client, meter, all_meters, today
                    )
                except SnoPUDDownloadError as err:
                    _LOGGER.debug(
                        "15-min download failed for meter %s: %s — "
                        "sensor will fall back to hourly cadence",
                        meter.account_number, err,
                    )
                    sensor_feed = hourly_feed

                # The sensor's cumulative counter advances by the fine-grain
                # readings if we got them, otherwise by the hourly readings.
                # Either way the same upstream meter delivered them, so the
                # arithmetic is consistent.
                advance_readings = (
                    sensor_feed.readings or hourly_feed.readings
                )
                self._advance_cumulative(meter.account_number, advance_readings)

                last = (
                    sensor_feed.readings[-1] if sensor_feed.readings
                    else (hourly_feed.readings[-1] if hourly_feed.readings else None)
                )
                result["meters"][meter.account_number] = {
                    "internal_id": meter.internal_id,
                    "rate_schedule": meter.rate_schedule,
                    "hourly_reading_count": len(hourly_feed.readings),
                    "sensor_reading_count": len(sensor_feed.readings),
                    "billing_supplement_added": billing_added,
                    "latest_reading": last.start.isoformat() if last else None,
                    "latest_reading_kwh": last.value_kwh if last else None,
                    "latest_reading_cost": (
                        last.value_dollars if last and last.cost_cents is not None else None
                    ),
                    "cumulative_kwh": self._cumulative_kwh[meter.account_number],
                    "cumulative_cost_usd": self._cumulative_cost_usd[meter.account_number],
                }

            try:
                await client.async_logout()
            except Exception:  # noqa: BLE001 — best-effort
                pass

        # Persist backfill state after a successful update cycle.
        await self._persist_backfilled()
        await self._persist_billing_backfilled()
        return result

    def _advance_cumulative(
        self, account_number: str, readings: list[IntervalReading]
    ) -> None:
        """Advance the in-process cumulative counters using only readings
        strictly later than the previously-seen latest one for this meter,
        so we don't double-count when hourly + 15-min windows overlap.

        ``_last_seen_cumulative`` is either (a) seeded from persisted hourly
        stats on the first post-restart refresh (via
        ``_seed_cumulative_from_stats``), or (b) maintained across refreshes
        within a session by this method. Either way, it's the authoritative
        "we've already added everything up to and including this timestamp"
        cursor, and a reading at ``r.start <= cursor`` is always a duplicate.
        """
        if not readings:
            return
        last_seen = self._last_seen_cumulative.get(account_number)

        kwh = 0.0
        cost = 0.0
        latest = last_seen
        for r in readings:
            if last_seen is not None and r.start <= last_seen:
                continue
            kwh += r.value_kwh
            if r.cost_cents is not None:
                cost += r.cost_cents / 100.0
            if latest is None or r.start > latest:
                latest = r.start

        if kwh or cost:
            self._cumulative_kwh[account_number] = (
                self._cumulative_kwh.get(account_number, 0.0) + kwh
            )
            self._cumulative_cost_usd[account_number] = (
                self._cumulative_cost_usd.get(account_number, 0.0) + cost
            )
        if latest is not None:
            self._last_seen_cumulative[account_number] = latest

    async def _fetch_hourly_for_meter(
        self,
        client: SnoPUDClient,
        meter: MeterInfo,
        all_meters: list[MeterInfo],
        today: date,
    ) -> GreenButtonFeed:
        """Hourly fetch — used for long-term statistics + initial backfill.

        Billing-interval backfill is handled separately in
        :meth:`_run_billing_supplement_if_needed`, gated on the
        ``enable_billing_backfill`` option and a per-meter
        ``_billing_backfilled`` flag. This split is what makes "enable
        billing-backfill after the initial setup" actually work.
        """
        if meter.account_number not in self._backfilled:
            backfill_days = _resolve_backfill_days(self._entry)
            _LOGGER.info(
                "performing initial %d-day hourly backfill for meter %s",
                backfill_days,
                meter.account_number,
            )
            merged, completed = await self._chunked_backfill(
                client, meter, all_meters, today,
                interval=DEFAULT_STATISTICS_INTERVAL,
                total_days=backfill_days,
            )
            if completed:
                self._backfilled.add(meter.account_number)
            else:
                # Partial backfill: import what we got, but don't latch the
                # "done" flag — the next refresh will retry the full range.
                # The import layer is idempotent on (statistic_id, start) so
                # the chunks that did succeed won't be written twice.
                _LOGGER.info(
                    "hourly backfill for meter %s partially completed "
                    "(%d chunks imported); will resume on next refresh",
                    meter.account_number, len(merged.readings),
                )
            return merged

        # Normal incremental hourly update: last 3 days covers the portal's
        # typical 5–8h lag plus a safety margin.
        start = today - timedelta(days=3)
        xml = await client.async_download_green_button(
            meter=meter,
            start=start,
            end=today,
            all_meters=all_meters,
            interval=DEFAULT_STATISTICS_INTERVAL,
        )
        return parse_green_button(xml)

    async def _run_billing_supplement_if_needed(
        self,
        client: SnoPUDClient,
        meter: MeterInfo,
        all_meters: list[MeterInfo],
        today: date,
        hourly_feed: GreenButtonFeed,
    ) -> int:
        """One-shot retroactive billing-interval import for a meter.

        Runs at most once per meter per config entry, gated on:
        * ``CONF_ENABLE_BILLING_BACKFILL`` being true on the config entry, and
        * the meter not already being in ``self._billing_backfilled``.

        Returns the number of new billing-interval rows that were written
        (zero on no-op or empty response).
        """
        if not self._entry.options.get(CONF_ENABLE_BILLING_BACKFILL, False):
            return 0
        if meter.account_number in self._billing_backfilled:
            return 0

        backfill_days = _resolve_backfill_days(self._entry)
        # Ask only for the slice strictly older than the earliest hourly point
        # we have for this meter — billing-interval rows older than that fill
        # in pre-smart-meter history. Rows newer than that may still come
        # back from SnoPUD (billing periods overlap the hourly window), but
        # the import-supplement layer drops collisions so existing finer-grain
        # hourly rows always win on overlap. We deliberately do NOT skip when
        # billing_end falls outside the configured backfill window: walking
        # back further is exactly how billing-interval supplementation reaches
        # the pre-smart-meter history that's older than the hourly cap.
        earliest_hourly = (
            min(r.start for r in hourly_feed.readings).date()
            if hourly_feed.readings
            else today
        )
        billing_end = earliest_hourly - timedelta(days=1)

        _LOGGER.info(
            "running one-shot billing-interval supplement for meter %s "
            "through %s (walking back %d days)",
            meter.account_number, billing_end, backfill_days,
        )
        # ``_chunked_backfill`` absorbs SnoPUDDownloadError internally and
        # surfaces it via the ``completed`` flag, so we don't need a
        # try/except around the call.
        billing, completed = await self._chunked_backfill(
            client, meter, all_meters, billing_end,
            interval=INTERVAL_BILLING,
            total_days=backfill_days,
        )

        if not billing.readings:
            if completed:
                _LOGGER.info(
                    "billing supplement for meter %s returned no readings — "
                    "marking complete so we don't keep retrying",
                    meter.account_number,
                )
                self._billing_backfilled.add(meter.account_number)
            else:
                _LOGGER.info(
                    "billing supplement for meter %s got no readings before "
                    "a download error — will retry on next refresh",
                    meter.account_number,
                )
            return 0

        added = await async_import_billing_supplement(
            self.hass,
            meter=meter,
            readings=billing.readings,
        )
        if added > 0:
            # The persisted statistics ``sum`` for this meter has been
            # rebuilt from zero; refresh the in-process cumulative counter so
            # the sensor's TOTAL_INCREASING value reflects the new total.
            await self._reseed_cumulative_from_stats(meter.account_number)
        # Only latch the "billing backfill done" flag on a fully-walked
        # range. Partial completions retry on the next refresh; the
        # supplement's rebuild-with-supplement merge is idempotent (existing
        # values win on collision, new-to-us older values get added) so
        # retries are safe.
        if completed:
            self._billing_backfilled.add(meter.account_number)
        else:
            _LOGGER.info(
                "billing supplement for meter %s partially completed "
                "(%d rows added this pass); will resume on next refresh",
                meter.account_number, added,
            )
        return added

    async def _fetch_sensor_for_meter(
        self,
        client: SnoPUDClient,
        meter: MeterInfo,
        all_meters: list[MeterInfo],
        today: date,
    ) -> GreenButtonFeed:
        """15-min fetch — published as the sensor's state stream.

        Always pulls a recent rolling window (no full backfill at this grain;
        the hourly statistics path covers the historical view, and HA's
        recorder will keep this sensor's state going forward subject to the
        user's recorder retention settings).
        """
        start = today - timedelta(days=SENSOR_LOOKBACK_DAYS)
        xml = await client.async_download_green_button(
            meter=meter,
            start=start,
            end=today,
            all_meters=all_meters,
            interval=DEFAULT_SENSOR_INTERVAL,
        )
        return parse_green_button(xml)

    async def _chunked_backfill(
        self,
        client: SnoPUDClient,
        meter: MeterInfo,
        all_meters: list[MeterInfo],
        end: date,
        *,
        interval: str,
        total_days: int,
    ) -> tuple[GreenButtonFeed, bool]:
        """Walk backward in chunks of MAX_DOWNLOAD_WINDOW_DAYS until empty.

        Returns ``(merged_feed, completed)`` where ``completed`` is ``True``
        iff the walk terminated naturally — either we covered the full
        requested ``total_days`` or the portal returned an empty chunk
        (meaning there is no further history to fetch). ``completed`` is
        ``False`` if we broke out on a download error, i.e. there may still
        be unfetched data in the requested range and a caller marking its
        per-meter "backfilled" flag based on this call should NOT do so
        until a later refresh returns ``completed=True``.

        Even on partial completion, we still return whatever chunks we did
        manage to download merged together — importing partial history is
        better than none, and the import layer is idempotent.
        """
        all_readings: list[IntervalReading] = []
        merged_feed: GreenButtonFeed | None = None
        days_remaining = total_days
        cursor = end
        completed = True  # flips to False only on a download-error break
        while days_remaining > 0:
            window = min(MAX_DOWNLOAD_WINDOW_DAYS, days_remaining)
            start = cursor - timedelta(days=window)
            try:
                xml = await client.async_download_green_button(
                    meter=meter,
                    start=start,
                    end=cursor,
                    all_meters=all_meters,
                    interval=interval,
                )
            except SnoPUDDownloadError as err:
                _LOGGER.warning(
                    "backfill chunk %s..%s failed for meter %s (interval=%s): %s "
                    "(stopping backfill, will resume on next refresh)",
                    start, cursor, meter.account_number, interval, err,
                )
                completed = False
                break
            feed = parse_green_button(xml)
            if merged_feed is None:
                merged_feed = feed
            all_readings.extend(feed.readings)
            cursor = start - timedelta(days=1)
            days_remaining -= window
            if not feed.readings:
                # Portal returned nothing for this window → no older history
                # exists. That's a legitimate terminal condition, not a
                # failure, so leave ``completed=True``.
                break

        if merged_feed is None:
            return (
                GreenButtonFeed(
                    reading_type=None, readings=[], usage_point_id=None
                ),
                completed,
            )
        seen: set[float] = set()
        merged: list[IntervalReading] = []
        for r in sorted(all_readings, key=lambda x: x.start):
            ts = r.start.timestamp()
            if ts in seen:
                continue
            seen.add(ts)
            merged.append(r)
        return (
            GreenButtonFeed(
                reading_type=merged_feed.reading_type,
                readings=merged,
                usage_point_id=merged_feed.usage_point_id,
            ),
            completed,
        )
