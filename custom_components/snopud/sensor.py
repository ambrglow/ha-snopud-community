"""Sensor platform for SnoPUD.

Exposes one sensor per configured meter. The sensor's ``native_value`` is the
**cumulative kWh** counter for that meter, and the entity uses
``state_class=total_increasing`` so HA's recorder treats it as a metered
counter and produces hourly long-term aggregates from it automatically.

The cumulative counter is seeded from the integration's persisted long-term
statistics (see ``coordinator._seed_cumulative_from_stats``) on the first
update after a Home Assistant restart, so it continues monotonically rather
than restarting at zero (which would otherwise show up as a sawtooth in any
downstream consumer like the Utility Meter helper).

The sensor's update cadence is the coordinator's poll interval, but the
underlying readings come from the **15-minute** Green Button feed — so
``latest_reading_at`` and ``latest_reading_kwh`` reflect 15-minute slices,
suitable for dashboard cards and automations.

This complements ``statistics.py``, which writes a parallel
``snopud:energy_consumption_<account>`` external statistic on the hourly
grain. The Energy Dashboard should be pointed at that external statistic
(it's the canonical, idempotently-upserted feed); this sensor exists so users
can wire current kWh into ordinary entities and automations.
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
    """Cumulative-kWh sensor for a single SnoPUD meter, fed by 15-min readings."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
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
        # The monotonic cumulative kWh counter, seeded across HA restarts from
        # persisted long-term statistics so total_increasing stays monotonic.
        total = block.get("cumulative_kwh")
        if total is None:
            return None
        return round(float(total), 3)

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
        if "cumulative_cost_usd" in block:
            attrs["cumulative_cost_usd"] = round(
                float(block["cumulative_cost_usd"]), 2
            )
        return attrs

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success and self._meter_block())
