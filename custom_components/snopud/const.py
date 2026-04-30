"""Constants for the SnoPUD integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "snopud"
VERSION = "0.2.9"

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
# Tracks the value of CONF_BACKFILL_DAYS the coordinator last honored, so that
# raising the option later can trigger a re-import of the newly-uncovered
# range. Lowering it does nothing destructive — it just records the new value.
CONF_LAST_APPLIED_BACKFILL_DAYS = "last_applied_backfill_days"

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
# Steady-state 15-min fetch window per refresh. The portal lags 5–8 h, so a
# 1-day look-back is more than enough to cover the right edge plus a small
# cushion for short outages. The longer rolling window users actually see in
# the chart is provided by the persisted archive (see ``ARCHIVE_INTERVAL_LIMIT``
# below), not by re-fetching the same data on every refresh — keeping the
# steady-state payload small. On a fresh install (or any refresh where the
# persisted archive is missing for a meter) the integration runs a one-shot
# ``SENSOR_INITIAL_BACKFILL_DAYS`` chunked backfill instead, so the chart is
# populated immediately rather than ramping up over the next week.
SENSOR_LOOKBACK_DAYS = 1
# One-shot 15-min backfill window. Runs only when no persisted archive exists
# for a meter (fresh install, archive file deleted, etc.). Sized to match
# ``ARCHIVE_INTERVAL_LIMIT`` so first-setup immediately fills the persisted
# archive, after which the steady-state 1-day lookback path takes over.
# Chunked through ``_chunked_backfill`` so a single ~1300-bucket request
# doesn't time out on the SnoPUD portal.
SENSOR_INITIAL_BACKFILL_DAYS = 14
# How many 15-minute interval buckets to expose in the sensor's
# ``recent_intervals`` extra-state attribute. 4 intervals/hour × 24 h × 7 days
# = 672 — a 7-day ApexCharts bar chart. Bounded modestly so the recorder's
# per-state-change attribute payload stays small (HA writes the FULL
# attributes payload on every state update; an unbounded ``recent_intervals``
# would inflate recorder storage proportionally to the polling cadence).
SENSOR_RECENT_INTERVAL_LIMIT = 672
# How many 15-minute buckets to retain in the persisted on-disk archive,
# independent of the entity attribute exposure. 4 × 24 × 14 = 1344, i.e. a
# 14-day on-disk window. Must be ≥ ``SENSOR_RECENT_INTERVAL_LIMIT``. The
# archive lives outside HA's recorder (single JSON file via the integration's
# own ``Store``) so growing it doesn't bloat the recorder. The extra ~7 days
# beyond the chart window exists to keep the chart populated across HA
# downtime, integration reloads, or HACS upgrades — the archive seeds the
# in-memory rolling window so a refresh after an outage doesn't need to
# re-fetch a week of data from SnoPUD to repopulate the chart.
ARCHIVE_INTERVAL_LIMIT = 1344
# Storage layout version for the persisted archive JSON. Bump if the on-disk
# schema ever changes incompatibly so older data gets rebuilt cleanly via the
# one-shot backfill path on next setup.
ARCHIVE_STORAGE_VERSION = 1

# Sanity check: the archive must hold at least as many buckets as the sensor
# attribute exposes. ``_merge_recent_intervals`` trims the backing dict to
# ``ARCHIVE_INTERVAL_LIMIT`` and *then* slices the last
# ``SENSOR_RECENT_INTERVAL_LIMIT`` entries for the entity attribute — if a
# user edits these knobs and inverts the relationship, the slice silently
# truncates the chart with no error. Failing fast at import time with a
# clear message is much friendlier than a quietly-broken dashboard.
if ARCHIVE_INTERVAL_LIMIT < SENSOR_RECENT_INTERVAL_LIMIT:
    raise ValueError(
        f"ARCHIVE_INTERVAL_LIMIT ({ARCHIVE_INTERVAL_LIMIT}) must be >= "
        f"SENSOR_RECENT_INTERVAL_LIMIT ({SENSOR_RECENT_INTERVAL_LIMIT}); "
        f"otherwise the entity attribute slice would truncate the chart "
        f"window. Adjust the constants in const.py so the archive is at "
        f"least as large as the chart window."
    )
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
