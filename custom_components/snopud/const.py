"""Constants for the SnoPUD integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "snopud"
VERSION = "0.2.0"

# Integration config keys (entry.data)
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_METER_IDS = "meter_ids"  # list of *account* meter numbers (e.g. "1000000001")

# Integration option keys (entry.options — editable after initial setup)
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
CONF_ENABLE_BILLING_BACKFILL = "enable_billing_backfill"
CONF_BACKFILLED_METERS = "backfilled_meters"  # list[str] persisted across restarts

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
# detail-interval data; we pull all of it on first run so HA statistics are
# populated.
INITIAL_BACKFILL_DAYS = 730

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

# Default interval we request. Home Assistant's long-term statistics API only
# accepts hour-aligned timestamps, so hourly is the natural granularity.
DEFAULT_SELECTED_INTERVAL = INTERVAL_HOURLY
DEFAULT_INTERVAL_SECONDS = 3600

# ESPI ReadingType codes we expect for hourly electricity consumption.
# If any of these don't match, we warn but still try to parse.
ESPI_COMMODITY_ELECTRICITY = 1
ESPI_UOM_WATT_HOURS = 72
ESPI_FLOW_DIRECTION_DELIVERED = 1
ESPI_ACCUMULATION_DELTA = 4
ESPI_INTERVAL_LENGTH_SECONDS = 3600  # 1 hour

# Statistics identity
STATISTIC_UNIT_KWH = "kWh"
STATISTIC_UNIT_USD = "USD"
