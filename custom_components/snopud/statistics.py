"""Import parsed Green Button readings into Home Assistant's long-term statistics.

This module handles the **hourly** leg of the coordinator's dual-path fetch:
the hourly Green Button feed is written into HA's long-term statistics so the
Energy Dashboard can consume it directly.

The ESPI readings from MySnoPUD are **delta** (Wh consumed during each
interval, plus optional currency cost). The Energy Dashboard wants a
``total_increasing`` metered value (cumulative kWh that grows monotonically
except on meter replacement), so we compute a running cumulative sum per
meter and persist that as the ``sum`` field of each statistic row.

We write via ``async_add_external_statistics`` rather than relying on a real
sensor's auto-generated LTS because the data is historical and irregularly
updated; HA's statistics API supports retroactive writes and idempotent
upserts keyed on (statistic_id, start).

The statistics API requires hour-aligned timestamps (``minute==second==0``),
which is why the coordinator requests the hourly interval on this path.
Billing-interval backfill readings (which align on month boundaries and are
also hour-aligned) work too. The parallel 15-minute sensor path is *not*
imported here — it is surfaced as sensor state by ``sensor.py`` and kept by
HA's recorder on whatever retention the user configures.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from datetime import datetime, timezone

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant

# ``StatisticMeanType`` was introduced alongside the recorder statistics
# metadata refresh. Older HA releases don't have it; emitting metadata
# without a ``mean_type`` key works fine on those, while newer releases
# (2026.11+) require the explicit value. Import defensively so the
# integration still loads on older HA cores.
try:
    from homeassistant.components.recorder.models.statistics import (
        StatisticMeanType,
    )
    _STATISTIC_MEAN_TYPE_NONE = StatisticMeanType.NONE
except ImportError:  # pragma: no cover — pre-mean_type HA cores
    _STATISTIC_MEAN_TYPE_NONE = None

from .const import DOMAIN, STATISTIC_UNIT_KWH, STATISTIC_UNIT_USD


def _stat_metadata(*, statistic_id: str, name: str, unit: str) -> dict:
    """Build a metadata dict for ``async_add_external_statistics``.

    Centralised so both the kWh and USD series carry identical fields and
    we set ``mean_type`` in exactly one place. We use
    ``StatisticMeanType.NONE`` because both series are sum-only
    (``has_mean=False``); no arithmetic average is computed for them.

    On HA cores that predate ``StatisticMeanType``, the ``mean_type`` key is
    omitted entirely — the recorder ignores unknown keys, and ``has_mean``
    alone was the source of truth on those releases.
    """
    metadata: dict = {
        "has_mean": False,
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    if _STATISTIC_MEAN_TYPE_NONE is not None:
        # Required on HA 2026.11+; harmless on the releases that introduced
        # the enum but didn't yet make it mandatory.
        metadata["mean_type"] = _STATISTIC_MEAN_TYPE_NONE
    return metadata

if TYPE_CHECKING:
    from .green_button import IntervalReading
    from .snopud_client import MeterInfo

_LOGGER = logging.getLogger(__name__)


def energy_statistic_id(meter_account_number: str) -> str:
    """External statistic ID for a meter's kWh series.

    External statistic IDs for custom integrations must be prefixed with the
    integration domain + ':'. The second half becomes the visible series name
    in the Energy Dashboard.
    """
    return f"{DOMAIN}:energy_consumption_{meter_account_number}"


def cost_statistic_id(meter_account_number: str) -> str:
    """External statistic ID for a meter's USD cost series."""
    return f"{DOMAIN}:energy_cost_{meter_account_number}"


# Backwards-compatible private aliases so older internal references keep working.
_energy_statistic_id = energy_statistic_id
_cost_statistic_id = cost_statistic_id


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

    Handles two cases:

    * **Pure append** — all incoming readings have timestamps strictly
      newer than the last persisted row for that series. Fast path: accumulate
      from the last persisted ``sum`` and upsert the new rows only.
    * **Overlap / correction** — one or more incoming readings fall on or
      before the last persisted row (e.g. SnoPUD revised a recently-imported
      interval). Slow path: read the existing series in-window, merge new
      values (new wins on timestamp collision), recompute cumulative ``sum``
      from just-before-window onward, and upsert the merged rows. This way
      late corrections actually land in the persisted stats instead of being
      silently dropped.
    """
    if not readings:
        return

    energy_id = energy_statistic_id(meter.account_number)
    cost_id = cost_statistic_id(meter.account_number)

    # Continue cumulative sums from the last known point for each series.
    energy_running, energy_last_dt = await _last_sum_and_time(hass, energy_id)
    cost_running, cost_last_dt = await _last_sum_and_time(hass, cost_id)

    # === Energy ===
    any_energy_overlap = (
        energy_last_dt is not None
        and any(r.start <= energy_last_dt for r in readings)
    )
    if any_energy_overlap:
        energy_points = {r.start: r.value_kwh for r in readings}
        await _rebuild_series_with_supplement(
            hass,
            statistic_id=energy_id,
            unit=STATISTIC_UNIT_KWH,
            name=f"SnoPUD Meter {meter.account_number} — Energy",
            new_points_by_start=energy_points,
            new_wins=True,
        )
    else:
        energy_new = [
            r for r in readings
            if energy_last_dt is None or r.start > energy_last_dt
        ]
        if energy_new:
            energy_payload = []
            for r in energy_new:
                energy_running += r.value_kwh
                energy_payload.append(
                    {
                        "start": r.start,
                        "state": r.value_kwh,   # kWh consumed during interval
                        "sum": energy_running,  # monotonic cumulative kWh
                    }
                )
            energy_metadata = _stat_metadata(
                statistic_id=energy_id,
                name=f"SnoPUD Meter {meter.account_number} — Energy",
                unit=STATISTIC_UNIT_KWH,
            )
            _LOGGER.info(
                "importing %d energy readings for %s (from %s)",
                len(energy_payload),
                energy_id,
                energy_new[0].start.isoformat(),
            )
            async_add_external_statistics(hass, energy_metadata, energy_payload)

    # === Cost ===
    any_cost = any(r.cost_cents is not None for r in readings)
    if not any_cost:
        return

    any_cost_overlap = (
        cost_last_dt is not None
        and any(
            r.start <= cost_last_dt for r in readings
            if r.cost_cents is not None
        )
    )
    if any_cost_overlap:
        cost_points = {
            r.start: (r.cost_cents or 0) / 100.0
            for r in readings
            if r.cost_cents is not None
        }
        await _rebuild_series_with_supplement(
            hass,
            statistic_id=cost_id,
            unit=STATISTIC_UNIT_USD,
            name=f"SnoPUD Meter {meter.account_number} — Cost",
            new_points_by_start=cost_points,
            new_wins=True,
        )
    else:
        cost_new = [
            r for r in readings
            if r.cost_cents is not None
            and (cost_last_dt is None or r.start > cost_last_dt)
        ]
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
            cost_metadata = _stat_metadata(
                statistic_id=cost_id,
                name=f"SnoPUD Meter {meter.account_number} — Cost",
                unit=STATISTIC_UNIT_USD,
            )
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
    if hasattr(last_start, "tzinfo"):
        return running, last_start
    return running, datetime.fromtimestamp(float(last_start), tz=timezone.utc)


async def async_import_billing_supplement(
    hass: HomeAssistant,
    *,
    meter: "MeterInfo",
    readings: list["IntervalReading"],
) -> int:
    """Retroactively merge billing-interval readings into existing LTS.

    Used when a user enables ``CONF_ENABLE_BILLING_BACKFILL`` *after* a meter
    has already had its hourly backfill run. The billing readings carry
    timestamps strictly older than the latest existing hourly point, so
    ``async_import_readings``'s "after-latest-only" filter would drop them.

    Strategy: read every existing point for the meter, merge with the new
    billing readings (existing points win on overlap), recompute the
    cumulative ``sum`` from zero across the full sorted series, and re-upsert.
    ``async_add_external_statistics`` is keyed on (statistic_id, start), so the
    re-upsert overwrites in place without producing duplicates.

    Returns the number of new billing readings written.
    """
    if not readings:
        return 0

    energy_id = energy_statistic_id(meter.account_number)
    cost_id = cost_statistic_id(meter.account_number)

    new_energy_by_start = {r.start: r.value_kwh for r in readings}
    new_cost_by_start = {
        r.start: (r.cost_cents or 0) / 100.0
        for r in readings
        if r.cost_cents is not None
    }

    # Billing supplement is retroactive-fill only — existing finer-grain
    # (hourly) data always wins on timestamp overlap, so ``new_wins=False``.
    written = await _rebuild_series_with_supplement(
        hass,
        statistic_id=energy_id,
        unit=STATISTIC_UNIT_KWH,
        name=f"SnoPUD Meter {meter.account_number} — Energy",
        new_points_by_start=new_energy_by_start,
        new_wins=False,
    )

    if new_cost_by_start:
        await _rebuild_series_with_supplement(
            hass,
            statistic_id=cost_id,
            unit=STATISTIC_UNIT_USD,
            name=f"SnoPUD Meter {meter.account_number} — Cost",
            new_points_by_start=new_cost_by_start,
            new_wins=False,
        )

    return written


async def _rebuild_series_with_supplement(
    hass: HomeAssistant,
    *,
    statistic_id: str,
    unit: str,
    name: str,
    new_points_by_start: dict,
    new_wins: bool = False,
) -> int:
    """Read existing stats, merge in new points, recompute cumulative ``sum``
    from zero, upsert. Returns count of newly added points.

    ``new_wins`` controls the collision policy when a new point and an
    existing point share a timestamp:

    * ``False`` — existing wins. Used by the billing-interval supplement,
      where the existing hourly-grain row is always more accurate than the
      coarser billing-interval row that happens to cover the same hour.
    * ``True``  — new wins. Used by the normal incremental import path when
      SnoPUD has revised a recently-imported interval, so the latest
      authoritative value replaces what we had.
    """
    if not new_points_by_start:
        return 0

    # Far-past start so we sweep the entire existing series. Naive datetimes
    # aren't allowed by HA's stats API; use a tz-aware sentinel.
    very_old = datetime(1970, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    recorder = get_instance(hass)
    existing = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        very_old,
        now,
        {statistic_id},
        "hour",
        None,
        {"start", "state", "sum"},
    )
    rows = existing.get(statistic_id, []) or []

    # Normalise existing rows into {tz-aware start: state} for merge.
    existing_by_start: dict = {}
    for row in rows:
        start = row.get("start")
        if start is None:
            continue
        if not hasattr(start, "tzinfo"):
            start = datetime.fromtimestamp(float(start), tz=timezone.utc)
        state = row.get("state")
        if state is None:
            continue
        try:
            existing_by_start[start] = float(state)
        except (TypeError, ValueError):
            continue

    # Merge policy: when a new point and an existing row share a timestamp,
    # ``new_wins`` decides which value to keep. Count rows as "added" when
    # either the timestamp wasn't there before, or we're overwriting an
    # existing row with a different value (revision).
    added = 0
    for start, value in new_points_by_start.items():
        new_value = float(value)
        if start in existing_by_start:
            if not new_wins:
                continue
            if existing_by_start[start] == new_value:
                continue
            existing_by_start[start] = new_value
            added += 1
        else:
            existing_by_start[start] = new_value
            added += 1

    if added == 0:
        _LOGGER.debug(
            "rebuild-with-supplement for %s: nothing to add or change "
            "(new_wins=%s; all timestamps already present with matching "
            "values)",
            statistic_id, new_wins,
        )
        return 0

    # Sort by start, recompute cumulative sum from zero.
    sorted_starts = sorted(existing_by_start.keys())
    payload = []
    running = 0.0
    for start in sorted_starts:
        state = existing_by_start[start]
        running += state
        payload.append({"start": start, "state": state, "sum": running})

    metadata = _stat_metadata(
        statistic_id=statistic_id, name=name, unit=unit,
    )
    _LOGGER.info(
        "rebuild-with-supplement: rewriting %s with %d total points "
        "(%d newly added or revised, new_wins=%s)",
        statistic_id, len(payload), added, new_wins,
    )
    async_add_external_statistics(hass, metadata, payload)
    return added
