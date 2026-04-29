"""Sensor platform for SnoPUD.

Exposes one sensor per configured meter — the **latest 15-minute Green Button
interval's kWh value** — and surfaces a ``recent_intervals`` array in the
entity's extra-state attributes for dashboard bar-chart cards (ApexCharts,
Plotly).

Entity model
------------
This sensor is **not** a cumulative energy counter. Its state is the kWh
*delivered during the most recent complete 15-minute interval*, and that
value naturally rises and falls with consumption. We therefore use:

    device_class = energy
    state_class  = measurement       (NOT total_increasing)
    unit         = kWh

A ``total_increasing`` state class would tell HA's statistics engine to
treat the value as a meter reading that only ever resets, which is the
opposite of what 15-minute interval consumption looks like.

The Energy Dashboard's canonical feed is the parallel
``snopud:energy_consumption_<account>`` external long-term statistic
written by ``statistics.py`` from the **hourly** Green Button export. That
path is unaffected by anything in this file. Users who want a monotonic
kWh counter at 15-minute grain should wrap this sensor in HA's built-in
**Utility Meter** helper — it integrates per-interval deltas into a proper
cumulative for them.

Why a fresh unique_id
---------------------
Older releases used unique_id ``snopud_<account>_energy`` for an entity
that, depending on the release, was either a cumulative ``total_increasing``
or an instantaneous ``measurement`` sensor — but **always** recorded its
state at HA polling time, not at the SnoPUD interval timestamp. Combined
with SnoPUD's 6–8h portal lag, that produced a contaminated history where
older cumulative-style points are mixed with latest-interval points and
none of them line up with their real interval boundaries. Reusing that
entity's history would defeat the redesign.

We therefore publish on a **new** unique_id
``snopud_<account>_latest_15min_usage`` so HA creates a fresh entity with
a clean state history. The old entity's history is left untouched in the
recorder; users who want to drop it can delete the old entity from
Settings → Devices & Services → Entities.

The ``recent_intervals`` attribute
----------------------------------
HA's standard sensor history uses the time HA *received* a state update,
not the timestamp of the underlying SnoPUD interval. Because SnoPUD lags
several hours and a single refresh may newly reveal multiple intervals at
once, that history graph would be misaligned and blocky. Instead, the
coordinator carries a rolling per-meter window of every 15-minute slice it
has seen (deduped by interval start, trimmed to
``SENSOR_RECENT_INTERVAL_LIMIT``) and we expose that window verbatim as
``attributes.recent_intervals``. An ApexCharts ``custom:apexcharts-card``
or Plotly ``data_generator`` reads this list and plots true 15-minute
buckets at their original SnoPUD timestamps, regardless of when HA polled.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STATISTIC_UNIT_KWH
from .coordinator import SnoPUDCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SnoPUD sensor entities from a config entry."""
    coordinator: SnoPUDCoordinator = hass.data[DOMAIN][entry.entry_id]
    # One entity per configured meter account.
    entities: list[SnoPUDLatest15MinSensor] = []
    meters = (coordinator.data or {}).get("meters", {}) if coordinator.data else {}
    accounts = list(meters.keys()) or list(coordinator.requested_accounts)
    for account in accounts:
        entities.append(SnoPUDLatest15MinSensor(coordinator, account))
    async_add_entities(entities)


class SnoPUDLatest15MinSensor(CoordinatorEntity[SnoPUDCoordinator], SensorEntity):
    """Latest 15-minute interval kWh sensor for a single SnoPUD meter.

    State semantics:
        * ``native_value`` — kWh consumed during the most recent complete
          15-minute Green Button interval. Naturally varies up and down.
        * ``state_class`` — ``measurement`` (per-interval reading, not a
          cumulative counter).
        * ``device_class`` — ``energy``.
        * Extra state attributes include the original SnoPUD interval
          timestamps and a rolling ``recent_intervals`` list intended for
          dashboard bar charts.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    # MEASUREMENT, not TOTAL_INCREASING — see module docstring for why.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = STATISTIC_UNIT_KWH

    def __init__(self, coordinator: SnoPUDCoordinator, account_number: str) -> None:
        super().__init__(coordinator)
        self._account = account_number
        # Fresh unique_id: do NOT reuse the legacy ``..._energy`` slot, whose
        # recorded history mixes cumulative-style and per-interval values
        # written at HA polling time. A new ID gives this entity a clean
        # state history starting from this release.
        self._attr_unique_id = f"{DOMAIN}_{account_number}_latest_15min_usage"
        self._attr_name = f"SnoPUD Meter {account_number} Latest 15-min Usage"

    def _meter_block(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        meters = data.get("meters", {})
        return meters.get(self._account)

    @property
    def native_value(self) -> float | None:
        """Latest complete 15-minute interval's kWh value.

        Sourced from the coordinator's ``latest_interval_kwh`` field, which
        is the last entry of the parsed 15-min Green Button feed. May be
        ``None`` briefly on first refresh or when the 15-min download
        failed and the coordinator only has the hourly fallback.
        """
        block = self._meter_block()
        if not block:
            return None
        latest = block.get("latest_interval_kwh")
        if latest is None:
            # Fallback for the very first refresh on an existing install
            # where the coordinator hasn't built a 15-min batch yet — keeps
            # the entity available rather than going Unknown.
            latest = block.get("latest_reading_kwh")
        if latest is None:
            return None
        return round(float(latest), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        block = self._meter_block() or {}
        attrs: dict[str, Any] = {
            "account_number": self._account,
        }
        if "internal_id" in block:
            attrs["internal_id"] = block["internal_id"]
        if "rate_schedule" in block:
            attrs["rate_schedule"] = block["rate_schedule"]
        # Diagnostics — counts of readings parsed in the most recent refresh.
        if "sensor_reading_count" in block:
            attrs["sensor_reading_count"] = block["sensor_reading_count"]
        if "hourly_reading_count" in block:
            attrs["hourly_reading_count"] = block["hourly_reading_count"]
        # Latest 15-min interval — the data the bar chart's rightmost bar
        # represents. Use the coordinator's pre-formatted ISO strings so
        # downstream JS can ``new Date(...)`` them directly.
        if block.get("latest_interval_start") is not None:
            attrs["latest_interval_start"] = block["latest_interval_start"]
        if block.get("latest_interval_end") is not None:
            attrs["latest_interval_end"] = block["latest_interval_end"]
        if block.get("latest_interval_kwh") is not None:
            attrs["latest_interval_kwh"] = block["latest_interval_kwh"]
        if block.get("latest_interval_cost_usd") is not None:
            attrs["latest_interval_cost_usd"] = block["latest_interval_cost_usd"]
        if block.get("data_lag_minutes") is not None:
            attrs["data_lag_minutes"] = block["data_lag_minutes"]
        # The headline attribute: a rolling window of 15-min interval
        # buckets, each ``{"start", "end", "kwh", "cost_usd"?}``. Sorted
        # chronologically, deduped by start, trimmed to
        # ``SENSOR_RECENT_INTERVAL_LIMIT``. Includes EVERY newly-discovered
        # bucket from the latest refresh (not just the newest one) so the
        # ApexCharts/Plotly bar chart fills in correctly even when SnoPUD's
        # 6–8h portal lag surfaces multiple new intervals at once.
        attrs["recent_intervals"] = block.get("recent_intervals", [])
        return attrs

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success and self._meter_block())
