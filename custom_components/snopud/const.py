"""Constants for the SnoPUD integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "snopud"
VERSION = "0.2.2"

# Integration config keys (entry.data)
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_METER_IDS = "meter_ids"  # list of *account* meter numbers (e.g. "1000000001")

# Integration option keys (entry.options — editable after initial setup)
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
CONF_ENABLE_BILLING_BACKFILL = "enable_billing_backfill"
CONF_BACKFILL_DAYS = "backfill_days"
CONF_BACKFILLED_METERS = "backfilled_meters"  # list[str] persisted across restarts
# Meters that have had a successful one-shot billing-interval import. Tracked
# independently of CONF_BACKFILLED_METERS so a user can enable the
# billing-backfill option *after* a meter has already had its hourly backfill
# and still get a one-time retroactive billing import.
CONF_BILLING_BACKFILLED_METERS = "billing_backfilled_meters"

# URLs
BASE_URL = "https://my.snopud.com"
LOGIN_URL = f"{BASE_URL}/Home/Login"
LOGOUT_URL = f"{BASE_URL}/User/LogOut"
DOWNLOAD_SETTINGS_URL = f"{BASE_URL}/Usage/InitializeDownloadSettings"
DOWNLOAD_URL = f"{BASE_URL}/Usage/Download"

# SnoPUD's Green Button data typically lags wall clock by ~5–8 hours, so polling
# more often than hourly gains nothing. Default to a polite hourly refresh;
# user can tune this from the config-entry options (15 min to 12 hours).
DEFAULT_SCAN_INTERVAL_MINUTES = 60
MIN_SCAN_INTERVAL_MINUTES = 15
MAX_SCAN_INTERVAL_MINUTES = 720  # 12 hours
DEFAULT_SCAN_INTERVAL = timedelta(minutes=DEFAULT_SCAN_INTERVAL_MINUTES)

# Initial backfill window on first setup. The portal offers up to 2 years of
# detail-interval data; we pull all of it by default on first run so HA
# statistics are populated. This is configurable via the options flow so users
# can trade download size against history depth.
DEFAULT_BACKFILL_DAYS = 730  # 2 years
MIN_BACKFILL_DAYS = 7        # at least a week so the Energy Dashboard isn't empty
MAX_BACKFILL_DAYS = 1825     # 5 years — well beyond the portal's own limit
# Backwards-compatible alias: older module-level references may still use this.
INITIAL_BACKFILL_DAYS = DEFAULT_BACKFILL_DAYS

# Max days per download request — chosen conservatively so a single POST
# doesn't time out. Longer backfills are chunked into windows of this size.
MAX_DOWNLOAD_WINDOW_DAYS = 90

# Form field values (MyMeterQ UI constants — match the values the portal's own
# download form submits).
FORMAT_GREEN_BUTTON = "1"
FORMAT_CSV = "2"
SERVICE_TYPE_ELECTRIC = "1"
INTERVAL_15MIN = "3"
INTERVAL_30MIN = "4"
INTERVAL_HOURLY = "5"
INTERVAL_DAILY = "6"
INTERVAL_BILLING = "7"
INTERVAL_WEEKLY = "8"
USAGE_TYPE_KWH = "1"
USAGE_TYPE_DOLLARS = "3"

# Two-path grain strategy:
#   * Hourly → Home Assistant long-term statistics (Energy Dashboard canonical).
#     Required because async_add_external_statistics only accepts hour-aligned
#     timestamps.
#   * 15-minute → sensor-entity state (regular dashboards + automations).
#     Preserved in the recorder subject to the user's recorder retention
#     settings; hourly aggregates of it are also kept forever as auto-LTS.
DEFAULT_STATISTICS_INTERVAL = INTERVAL_HOURLY
DEFAULT_SENSOR_INTERVAL = INTERVAL_15MIN
# How far back to fetch the 15-min sensor window on each refresh. The portal
# lags 5–8 h, so a 3-day look-back is enough to fill in late-arriving data
# while keeping payloads small.
SENSOR_LOOKBACK_DAYS = 3
# Legacy single-knob default (kept for tests that exercise the old path).
DEFAULT_SELECTED_INTERVAL = DEFAULT_STATISTICS_INTERVAL
DEFAULT_INTERVAL_SECONDS = 3600

# ESPI ReadingType codes we expect for electricity consumption. Interval length
# is not constrained here — we accept hourly, 15-min, and billing-interval.
ESPI_COMMODITY_ELECTRICITY = 1
ESPI_UOM_WATT_HOURS = 72
ESPI_FLOW_DIRECTION_DELIVERED = 1
ESPI_ACCUMULATION_DELTA = 4
ESPI_INTERVAL_LENGTH_SECONDS = 3600  # 1 hour (reference value; parser accepts others)

# Statistics identity
STATISTIC_UNIT_KWH = "kWh"
STATISTIC_UNIT_USD = "USD"
