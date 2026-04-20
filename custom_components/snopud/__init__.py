"""Snohomish County PUD (community) integration for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_METER_IDS, DOMAIN
from .coordinator import SnoPUDCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SnoPUD from a config entry."""
    coordinator = SnoPUDCoordinator(
        hass,
        entry=entry,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        meter_account_numbers=entry.data[CONF_METER_IDS],
    )

    # Initial refresh — failures surface a persistent notification and HA will
    # retry on its normal cadence.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # React to options changes (scan interval, billing backfill toggle)
    # without requiring a full HA restart or integration reload.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply updated options to a running coordinator."""
    coordinator: SnoPUDCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(entry.entry_id)
    )
    if coordinator is None:
        return
    coordinator.apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
