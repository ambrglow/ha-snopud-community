"""Data update coordinator for SnoPUD.

Runs on a scheduled interval (configurable via options; default 1 hour),
authenticates if needed, fetches recent Green Button XML for each configured
meter, and writes the readings into Home Assistant's long-term statistics so
they appear in the Energy Dashboard.

On first run it also performs a backfill covering up to INITIAL_BACKFILL_DAYS,
chunked into MAX_DOWNLOAD_WINDOW_DAYS-sized windows. Meters that have already
been backfilled are tracked in entry.options so repeat backfills don't happen
on every HA restart.

If a meter returns no data at the hourly interval (typical for a retired,
non-smart meter that only reports billing-interval totals) and the option
``enable_billing_backfill`` is on, the coordinator will retry at
INTERVAL_BILLING to capture pre-smart-meter history.
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_BACKFILLED_METERS,
    CONF_ENABLE_BILLING_BACKFILL,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_SELECTED_INTERVAL,
    DOMAIN,
    INITIAL_BACKFILL_DAYS,
    INTERVAL_BILLING,
    MAX_DOWNLOAD_WINDOW_DAYS,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .green_button import GreenButtonFeed, IntervalReading, parse_green_button
from .snopud_client import (
    MeterInfo,
    SnoPUDAuthError,
    SnoPUDClient,
    SnoPUDDownloadError,
    SnoPUDError,
)
from .statistics import async_import_readings

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
        # Cumulative kWh/USD per meter, reconstructed from prior runs via the
        # statistics API on first update. Populated inside _async_update_data.
        self._cumulative_kwh: dict[str, float] = {}
        self._cumulative_cost_usd: dict[str, float] = {}
        # Circuit breaker state. After repeated auth failures we stop trying
        # until the config entry is reloaded, so a rotated password doesn't
        # cause us to repeatedly submit wrong credentials.
        self._consecutive_auth_failures = 0
        self._auth_failure_threshold = 3

    @property
    def requested_accounts(self) -> list[str]:
        return list(self._requested_accounts)

    def apply_options(self) -> None:
        """Re-read options from the config entry and apply them live."""
        new_interval = _resolve_scan_interval(self._entry)
        if new_interval != self.update_interval:
            _LOGGER.info(
                "SnoPUD scan interval changed to %s",
                new_interval,
            )
            self.update_interval = new_interval

    async def _persist_backfilled(self) -> None:
        """Persist the set of already-backfilled meters into entry.options."""
        current = self._entry.options.get(CONF_BACKFILLED_METERS, [])
        new = sorted(self._backfilled)
        if sorted(current) == new:
            return
        new_options = {**self._entry.options, CONF_BACKFILLED_METERS: new}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest readings for each configured meter and import to statistics."""
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
                try:
                    feed = await self._fetch_for_meter(
                        client, meter, all_meters, today
                    )
                except SnoPUDDownloadError as err:
                    _LOGGER.warning(
                        "download failed for meter %s: %s", meter.account_number, err
                    )
                    continue

                await async_import_readings(
                    self.hass,
                    entry_id=self._entry.entry_id,
                    meter=meter,
                    readings=feed.readings,
                )

                # Track cumulative kWh/USD in-process so the sensor entity has
                # a monotonic value. Restart reconstruction (from stats) is
                # deferred to statistics.py which already queries get_last_statistics.
                total_kwh = sum(r.value_kwh for r in feed.readings)
                self._cumulative_kwh[meter.account_number] = (
                    self._cumulative_kwh.get(meter.account_number, 0.0) + total_kwh
                )
                total_cost_cents = sum(
                    r.cost_cents for r in feed.readings if r.cost_cents is not None
                )
                total_cost_usd = total_cost_cents / 100.0 if total_cost_cents else 0.0
                self._cumulative_cost_usd[meter.account_number] = (
                    self._cumulative_cost_usd.get(meter.account_number, 0.0)
                    + total_cost_usd
                )

                last = feed.readings[-1] if feed.readings else None
                result["meters"][meter.account_number] = {
                    "internal_id": meter.internal_id,
                    "rate_schedule": meter.rate_schedule,
                    "reading_count": len(feed.readings),
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
        return result

    async def _fetch_for_meter(
        self,
        client: SnoPUDClient,
        meter: MeterInfo,
        all_meters: list[MeterInfo],
        today: date,
    ) -> GreenButtonFeed:
        """Fetch readings for a meter. Does an initial backfill on first run."""
        if meter.account_number not in self._backfilled:
            # First run for this meter: pull up to INITIAL_BACKFILL_DAYS, chunked.
            _LOGGER.info(
                "performing initial %d-day backfill for meter %s",
                INITIAL_BACKFILL_DAYS,
                meter.account_number,
            )
            merged = await self._chunked_backfill(
                client, meter, all_meters, today,
                interval=DEFAULT_SELECTED_INTERVAL,
            )

            # Optional: if the account also has pre-smart-meter history that
            # the hourly interval can't reach, retry at billing interval.
            if self._entry.options.get(CONF_ENABLE_BILLING_BACKFILL, False):
                # Only look earlier than whatever the hourly backfill found.
                earliest_hourly = (
                    min(r.start for r in merged.readings).date()
                    if merged.readings
                    else today
                )
                billing_end = earliest_hourly - timedelta(days=1)
                if billing_end > today - timedelta(days=INITIAL_BACKFILL_DAYS):
                    _LOGGER.info(
                        "attempting billing-interval backfill for meter %s through %s",
                        meter.account_number,
                        billing_end,
                    )
                    billing = await self._chunked_backfill(
                        client, meter, all_meters, billing_end,
                        interval=INTERVAL_BILLING,
                    )
                    # Merge, dedupe on start timestamp.
                    combined = list(merged.readings) + list(billing.readings)
                    seen: set[float] = set()
                    deduped: list[IntervalReading] = []
                    for r in sorted(combined, key=lambda x: x.start):
                        ts = r.start.timestamp()
                        if ts in seen:
                            continue
                        seen.add(ts)
                        deduped.append(r)
                    merged = GreenButtonFeed(
                        reading_type=merged.reading_type or billing.reading_type,
                        readings=deduped,
                        usage_point_id=merged.usage_point_id or billing.usage_point_id,
                    )

            self._backfilled.add(meter.account_number)
            return merged

        # Normal incremental update: last 3 days covers the portal's typical
        # 5–8h lag plus a safety margin.
        start = today - timedelta(days=3)
        xml = await client.async_download_green_button(
            meter=meter,
            start=start,
            end=today,
            all_meters=all_meters,
            interval=DEFAULT_SELECTED_INTERVAL,
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
    ) -> GreenButtonFeed:
        """Walk backward in chunks of MAX_DOWNLOAD_WINDOW_DAYS until empty."""
        all_readings: list[IntervalReading] = []
        merged_feed: GreenButtonFeed | None = None
        days_remaining = INITIAL_BACKFILL_DAYS
        cursor = end
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
                break
            feed = parse_green_button(xml)
            if merged_feed is None:
                merged_feed = feed
            all_readings.extend(feed.readings)
            cursor = start - timedelta(days=1)
            days_remaining -= window
            if not feed.readings:
                break

        if merged_feed is None:
            return GreenButtonFeed(
                reading_type=None, readings=[], usage_point_id=None
            )
        seen: set[float] = set()
        merged: list[IntervalReading] = []
        for r in sorted(all_readings, key=lambda x: x.start):
            ts = r.start.timestamp()
            if ts in seen:
                continue
            seen.add(ts)
            merged.append(r)
        return GreenButtonFeed(
            reading_type=merged_feed.reading_type,
            readings=merged,
            usage_point_id=merged_feed.usage_point_id,
        )
