"""Sensor platform for SnoPUD.

Exposes one sensor per configured meter. The sensor's ``native_value`` is
the **kWh consumed during the most recent 15-minute interval** reported by
SnoPUD's Green Button feed — i.e. the latest 15-min slice value, not a
cumulative counter. The entity uses ``state_class=measurement`` because it
represents an instantaneous per-interval reading.

This is deliberately a thin pass-through of the upstream feed. The original
design tried to maintain a synthetic monotonic ``cumulative_kwh`` counter
inside the integration, seeded from the integration's persisted long-term
statistics across HA restarts. That approach turned out to be fragile
(seeding races with the recorder, sawtooth on every restart) and wasn't
actually adding value — the Energy Dashboard reads from the parallel
``snopud:energy_consumption_<account>`` external statistic written by
``statistics.py``, which is the canonical cumulative feed. Users who need
a cumulative counter at this finer grain can wrap this sensor in HA's
built-in **Utility Meter** helper, which will integrate the per-interval
deltas into a properly-monotonic counter automatically.

``latest_reading_at`` and ``cumulative_cost_usd`` (the latter still computed
in the coordinator for cost-attribution) are exposed as extra state
attributes for users who want to surface them in cards/automations.
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
    entities: list[SnoPUDMeterSensor] = []
    meters = (coordinator.data or {}).get("meters", {}) if coordinator.data else {}
    accounts = list(meters.keys()) or list(coordinator.requested_accounts)
    for account in accounts:
        entities.append(SnoPUDMeterSensor(coordinator, account))
    async_add_entities(entities)


class SnoPUDMeterSensor(CoordinatorEntity[SnoPUDCoordinator], SensorEntity):
    """Per-interval kWh sensor for a single SnoPUD meter (15-min Green Button slice)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    # MEASUREMENT, not TOTAL_INCREASING — this sensor exposes the most
    # recent 15-min slice's kWh value, not a cumulative counter. Wrap it
    # in a Utility Meter helper if you want a monotonic total.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = STATISTIC_UNIT_KWH

    def __init__(self, coordinator: SnoPUDCoordinator, account_number: str) -> None:
        super().__init__(coordinator)
        self._account = account_number
        # Stable unique_id; the entity's friendly slug is derived from this.
        self._attr_unique_id = f"{DOMAIN}_{account_number}_energy"
        self._attr_name = f"SnoPUD Meter {account_number} Energy"

    def _meter_block(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        meters = data.get("meters", {})
        return meters.get(self._account)

    @property
    def native_value(self) -> float | None:
        block = self._meter_block()
        if not block:
            return None
        # kWh consumed during the most recent 15-min interval — a thin
        # pass-through of the upstream Green Button feed. No cumulative
        # synthesis, no seeding from LTS, no cross-restart state.
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
        if "sensor_reading_count" in block:
            attrs["sensor_reading_count"] = block["sensor_reading_count"]
        if "hourly_reading_count" in block:
            attrs["hourly_reading_count"] = block["hourly_reading_count"]
        # latest_reading_* reflect the most recent 15-min slice when the
        # 15-min path returned data, otherwise the most recent hourly slice.
        if "latest_reading" in block:
            attrs["latest_reading_at"] = block["latest_reading"]
        if "latest_reading_kwh" in block:
            attrs["latest_reading_kwh"] = block["latest_reading_kwh"]
        if "latest_reading_cost" in block:
            attrs["latest_reading_cost_usd"] = block["latest_reading_cost"]
        # NOTE: cumulative_cost_usd / cumulative_kwh are intentionally NOT
        # exposed. They were synthetic counters that drifted across
        # restarts; the canonical cumulative feed is the long-term
        # statistic written by ``statistics.py``, which the Energy
        # Dashboard reads directly.
        return attrs

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success and self._meter_block())
