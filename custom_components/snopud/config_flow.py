"""Config flow for SnoPUD integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BACKFILL_DAYS,
    CONF_ENABLE_BILLING_BACKFILL,
    CONF_METER_IDS,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_BACKFILL_DAYS,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .snopud_client import (
    MeterInfo,
    SnoPUDAuthError,
    SnoPUDClient,
    SnoPUDError,
)

_LOGGER = logging.getLogger(__name__)


class SnoPUDConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """UI config flow: credentials → meter pick → create entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._meters: list[MeterInfo] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """First step: collect credentials, verify by logging in."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]

            try:
                self._meters = await self._validate(self._email, self._password)
            except SnoPUDAuthError:
                errors["base"] = "invalid_auth"
            except SnoPUDError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error validating SnoPUD credentials")
                errors["base"] = "unknown"

            if not errors:
                # Prevent duplicate configuration for the same account
                await self.async_set_unique_id(self._email.lower())
                self._abort_if_unique_id_configured()

                if len(self._meters) == 1:
                    return self._create_entry([self._meters[0].account_number])
                return await self.async_step_pick_meters()

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_pick_meters(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Second step: when multiple meters are on the account, let the user pick."""
        if user_input is not None:
            selected = user_input[CONF_METER_IDS]
            if not selected:
                return self.async_show_form(
                    step_id="pick_meters",
                    data_schema=self._meter_schema(),
                    errors={"base": "no_meters_selected"},
                )
            return self._create_entry(selected)

        return self.async_show_form(
            step_id="pick_meters",
            data_schema=self._meter_schema(),
            description_placeholders={
                "count": str(len(self._meters)),
            },
        )

    @callback
    def _meter_schema(self) -> vol.Schema:
        options = [
            SelectOptionDict(
                value=m.account_number,
                label=(
                    f"#{m.account_number} — {m.service_type}"
                    + (f" ({m.rate_schedule})" if m.rate_schedule else "")
                ),
            )
            for m in self._meters
        ]
        return vol.Schema(
            {
                vol.Required(
                    CONF_METER_IDS,
                    default=[m.account_number for m in self._meters],
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )

    @callback
    def _create_entry(self, meter_account_numbers: list[str]) -> FlowResult:
        assert self._email is not None and self._password is not None
        # Title intentionally does not embed the user's email: config-entry
        # titles appear in screenshots, notifications, and log lines, and the
        # email is already stored in entry.data for the integration's own use.
        return self.async_create_entry(
            title="SnoPUD (Community)",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_METER_IDS: meter_account_numbers,
            },
            options={
                CONF_SCAN_INTERVAL_MINUTES: DEFAULT_SCAN_INTERVAL_MINUTES,
                CONF_ENABLE_BILLING_BACKFILL: False,
                CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
            },
        )

    @staticmethod
    async def _validate(email: str, password: str) -> list[MeterInfo]:
        """Log in and enumerate meters. Raises SnoPUDAuthError or SnoPUDError."""
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=False)
        ) as session:
            client = SnoPUDClient(session, email, password)
            await client.async_login()
            try:
                meters = await client.async_get_meters()
            finally:
                try:
                    await client.async_logout()
                except Exception:  # noqa: BLE001
                    pass
            if not meters:
                raise SnoPUDError("no meters found on this account")
            return meters

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "SnoPUDOptionsFlow":
        return SnoPUDOptionsFlow(config_entry)


class SnoPUDOptionsFlow(config_entries.OptionsFlow):
    """Options flow for SnoPUD — tune refresh interval & enable billing backfill."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            # Preserve any options we don't surface in the form (e.g.
            # CONF_BACKFILLED_METERS, which tracks per-meter backfill state).
            merged = {**self._entry.options}
            merged[CONF_SCAN_INTERVAL_MINUTES] = int(
                user_input[CONF_SCAN_INTERVAL_MINUTES]
            )
            merged[CONF_ENABLE_BILLING_BACKFILL] = bool(
                user_input[CONF_ENABLE_BILLING_BACKFILL]
            )
            merged[CONF_BACKFILL_DAYS] = int(user_input[CONF_BACKFILL_DAYS])
            return self.async_create_entry(title="", data=merged)

        current_minutes = self._entry.options.get(
            CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        current_billing = self._entry.options.get(
            CONF_ENABLE_BILLING_BACKFILL, False
        )
        current_backfill_days = self._entry.options.get(
            CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL_MINUTES,
                    default=current_minutes,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES,
                        max=MAX_SCAN_INTERVAL_MINUTES,
                        step=15,
                        mode=NumberSelectorMode.SLIDER,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(
                    CONF_BACKFILL_DAYS,
                    default=current_backfill_days,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_BACKFILL_DAYS,
                        max=MAX_BACKFILL_DAYS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
                vol.Required(
                    CONF_ENABLE_BILLING_BACKFILL,
                    default=current_billing,
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
