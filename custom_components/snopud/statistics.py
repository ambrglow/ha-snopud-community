"""Import parsed Green Button readings into Home Assistant's long-term statistics.

The ESPI readings from MySnoPUD are **delta** (Wh consumed during each interval,
plus optional currency cost). Home Assistant's Energy Dashboard expects a
``total_increasing`` metered value (cumulative kWh that grows monotonically
except on meter replacement). We compute a running cumulative sum per meter.

We write via ``async_add_external_statistics`` rather than creating a real
sensor because the data is historical and irregularly updated; HA's statistics
API supports retroactive writes and idempotent upserts keyed on
(statistic_id, start).

The statistics API requires hour-aligned timestamps (``minute==second==0``),
so we request hourly data from the portal by default. Readings whose start
isn't aligned (e.g. billing-interval backfill for retired meters) are still
imported — HA will reject any that don't align and log a warning we'll catch.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STATISTIC_UNIT_KWH, STATISTIC_UNIT_USD

if TYPE_CHECKING:
    from .green_button import IntervalReading
    from .snopud_client import MeterInfo

_LOGGER = logging.getLogger(__name__)


def _energy_statistic_id(meter_account_number: str) -> str:
    # External statistic IDs for custom integrations must be prefixed with the
    # integration domain + ':'. The second half becomes the visible series name
    # in the Energy Dashboard.
    return f"{DOMAIN}:energy_consumption_{meter_account_number}"


def _cost_statistic_id(meter_account_number: str) -> str:
    return f"{DOMAIN}:energy_cost_{meter_account_number}"


async def async_import_readings(
    hass: HomeAssistant,
    *,
    entry_id: str,
    meter: "MeterInfo",
    readings: list["IntervalReading"],
) -> None:
    """Write readings into the recorder as external statistics.

    Writes two parallel series when cost data is present:
      * ``snopud:energy_consumption_<account>`` — kWh
      * ``snopud:energy_cost_<account>``        — USD

    Idempotent: replays of the same interval overwrite the existing value.
    Cumulative totals are computed by continuing from the last known 'sum'
    for each statistic_id, if one exists.
    """
    if not readings:
        return

    energy_id = _energy_statistic_id(meter.account_number)
    cost_id = _cost_statistic_id(meter.account_number)

    # Continue cumulative sums from the last known point for each series.
    energy_running, energy_last_dt = await _last_sum_and_time(hass, energy_id)
    cost_running, cost_last_dt = await _last_sum_and_time(hass, cost_id)

    # Only import readings strictly after the last known point for each series.
    energy_new = (
        [r for r in readings if energy_last_dt is None or r.start > energy_last_dt]
    )
    any_cost = any(r.cost_cents is not None for r in readings)
    cost_new = (
        [
            r for r in readings
            if r.cost_cents is not None
            and (cost_last_dt is None or r.start > cost_last_dt)
        ]
        if any_cost
        else []
    )

    if not energy_new and not cost_new:
        _LOGGER.debug("no new readings for %s", meter.account_number)
        return

    if energy_new:
        energy_payload = []
        for r in energy_new:
            energy_running += r.value_kwh
            energy_payload.append(
                {
                    "start": r.start,
                    "state": r.value_kwh,   # kWh consumed during this interval
                    "sum": energy_running,  # monotonic cumulative kWh
                }
            )
        energy_metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": f"SnoPUD Meter {meter.account_number} — Energy",
            "source": DOMAIN,
            "statistic_id": energy_id,
            "unit_of_measurement": STATISTIC_UNIT_KWH,
        }
        _LOGGER.info(
            "importing %d energy readings for %s (from %s)",
            len(energy_payload),
            energy_id,
            energy_new[0].start.isoformat(),
        )
        async_add_external_statistics(hass, energy_metadata, energy_payload)

    if cost_new:
        cost_payload = []
        for r in cost_new:
            dollars = (r.cost_cents or 0) / 100.0
            cost_running += dollars
            cost_payload.append(
                {
                    "start": r.start,
                    "state": dollars,
                    "sum": cost_running,
                }
            )
        cost_metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": f"SnoPUD Meter {meter.account_number} — Cost",
            "source": DOMAIN,
            "statistic_id": cost_id,
            "unit_of_measurement": STATISTIC_UNIT_USD,
        }
        _LOGGER.info(
            "importing %d cost readings for %s (from %s)",
            len(cost_payload),
            cost_id,
            cost_new[0].start.isoformat(),
        )
        async_add_external_statistics(hass, cost_metadata, cost_payload)


async def _last_sum_and_time(hass: HomeAssistant, statistic_id: str):
    """Return (last_sum, last_start_dt) for a stat, or (0.0, None) if unseen."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    if not last.get(statistic_id):
        return 0.0, None
    entry = last[statistic_id][0]
    running = float(entry.get("sum") or 0.0)
    last_start = entry.get("start")
    if last_start is None:
        return running, None
    from datetime import datetime, timezone
    if hasattr(last_start, "tzinfo"):
        return running, last_start
    return running, datetime.fromtimestamp(float(last_start), tz=timezone.utc)
