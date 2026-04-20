"""Sensor platform for SnoPUD.

Exposes one sensor per configured meter. The sensor state is the most recent
interval reading's kWh delta, with ``state_class=total_increasing`` so that
Home Assistant's recorder builds short-term statistics from it automatically.

This complements the long-term statistics path (``statistics.py``): the
external statistics feed the Energy Dashboard directly (hourly only), while
this sensor gives users a live entity for dashboards, automations, and the
Statistics / Statistics-Graph helpers. A history-first-hour pass is *not*
attempted here — the Energy Dashboard reads LTS, not sensor state, so we only
need this entity to reflect "what's new" since the last refresh.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    entities: list[SnoPUDLastIntervalSensor] = []
    meters = (coordinator.data or {}).get("meters", {}) if coordinator.data else {}
    accounts = list(meters.keys()) or list(coordinator.requested_accounts)
    for account in accounts:
        entities.append(SnoPUDLastIntervalSensor(coordinator, account))
    async_add_entities(entities)


class SnoPUDLastIntervalSensor(CoordinatorEntity[SnoPUDCoordinator], SensorEntity):
    """Exposes the most-recent interval's kWh delta for a single meter."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = STATISTIC_UNIT_KWH

    def __init__(self, coordinator: SnoPUDCoordinator, account_number: str) -> None:
        super().__init__(coordinator)
        self._account = account_number
        self._attr_unique_id = f"{DOMAIN}_{account_number}_last_interval_kwh"
        self._attr_name = f"SnoPUD Meter {account_number} last interval"

    def _meter_block(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        meters = data.get("meters", {})
        return meters.get(self._account)

    @property
    def native_value(self) -> float | None:
        block = self._meter_block()
        if not block:
            return None
        # Report the monotonic cumulative kWh counter so HA's recorder can
        # compute short-term statistics directly from sensor state (matches
        # the ``total_increasing`` state class).
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
        if "reading_count" in block:
            attrs["reading_count"] = block["reading_count"]
        if "latest_reading" in block:
            attrs["latest_reading_at"] = block["latest_reading"]
        if "latest_reading_kwh" in block:
            attrs["latest_reading_kwh"] = block["latest_reading_kwh"]
        if "latest_reading_cost" in block:
            attrs["latest_reading_cost_usd"] = block["latest_reading_cost"]
        return attrs

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success and self._meter_block())
