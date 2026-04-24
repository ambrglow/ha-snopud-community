"""Unit tests for the coordinator's cumulative-counter logic.

These are plain-Python tests that don't need Home Assistant or the recorder;
we instantiate the coordinator via ``__new__`` and exercise the pure bits
(``_advance_cumulative`` and the shape of ``_seed_cumulative_from_stats``'s
cursor seeding) directly.

The critical scenarios covered here:

* Fresh advance with no prior cursor adds every reading.
* Advance with a prior cursor skips overlapping readings and only adds new ones.
* **Restart scenario (regression test)**: after seeding ``_cumulative_kwh``
  from the persisted hourly stats *and* seeding ``_last_seen_cumulative`` to
  the end of that hour, advancing with a 15-min feed whose window overlaps
  the hourly stats does NOT re-add the in-hour 15-min slices. This is the
  bug ChatGPT flagged: without cursor-seeding, the post-restart advance
  would double-count the recent SENSOR_LOOKBACK_DAYS of consumption.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Register shell package so relative imports resolve without running the
# real custom_components.snopud.__init__ (which imports Home Assistant).
pkg_cc = types.ModuleType("custom_components")
pkg_cc.__path__ = [str(ROOT / "custom_components")]
sys.modules["custom_components"] = pkg_cc

pkg_snopud = types.ModuleType("custom_components.snopud")
pkg_snopud.__path__ = [str(ROOT / "custom_components" / "snopud")]
sys.modules["custom_components.snopud"] = pkg_snopud

_load("custom_components.snopud.const", ROOT / "custom_components/snopud/const.py")
gb_mod = _load(
    "custom_components.snopud.green_button",
    ROOT / "custom_components/snopud/green_button.py",
)
IntervalReading = gb_mod.IntervalReading


# The coordinator module transitively imports homeassistant.*; we stub just
# enough of HA for the module to load so we can exercise the class methods.
def _install_ha_stubs() -> None:
    homeassistant = types.ModuleType("homeassistant")
    sys.modules.setdefault("homeassistant", homeassistant)

    components = types.ModuleType("homeassistant.components")
    homeassistant.components = components
    sys.modules["homeassistant.components"] = components

    recorder = types.ModuleType("homeassistant.components.recorder")
    components.recorder = recorder
    sys.modules["homeassistant.components.recorder"] = recorder

    recorder_stats = types.ModuleType(
        "homeassistant.components.recorder.statistics"
    )

    def _fake_get_last_statistics(*args, **kwargs):
        # Overridden per-test via monkeypatching if needed.
        return {}

    recorder_stats.get_last_statistics = _fake_get_last_statistics
    sys.modules["homeassistant.components.recorder.statistics"] = recorder_stats

    recorder_util = types.ModuleType("homeassistant.components.recorder.util")

    def _fake_get_instance(hass):
        class _Recorder:
            async def async_add_executor_job(self, fn, *args, **kwargs):
                return fn(*args, **kwargs)
        return _Recorder()

    recorder_util.get_instance = _fake_get_instance
    sys.modules["homeassistant.components.recorder.util"] = recorder_util

    config_entries = types.ModuleType("homeassistant.config_entries")

    class _ConfigEntry:
        pass

    config_entries.ConfigEntry = _ConfigEntry
    sys.modules["homeassistant.config_entries"] = config_entries

    core = types.ModuleType("homeassistant.core")

    class _HomeAssistant:
        pass

    core.HomeAssistant = _HomeAssistant
    sys.modules["homeassistant.core"] = core

    helpers = types.ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = helpers

    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _DataUpdateCoordinator:
        """Stub of HA's generic coordinator base; accepts subscript syntax."""

        def __init__(self, *args, **kwargs):
            pass

        def __class_getitem__(cls, item):
            # Allow ``DataUpdateCoordinator[dict[str, Any]]`` at class-definition
            # time without pulling in HA's real generic machinery.
            return cls

    class _UpdateFailed(Exception):
        pass

    uc.DataUpdateCoordinator = _DataUpdateCoordinator
    uc.UpdateFailed = _UpdateFailed
    sys.modules["homeassistant.helpers.update_coordinator"] = uc


_install_ha_stubs()
# Also stub out the statistics module (which imports more HA bits we don't
# need for coordinator-level tests).
stats_stub = types.ModuleType("custom_components.snopud.statistics")


async def _noop_async_import(*args, **kwargs):
    return None


async def _noop_async_import_billing(*args, **kwargs):
    return 0


stats_stub.async_import_readings = _noop_async_import
stats_stub.async_import_billing_supplement = _noop_async_import_billing
stats_stub.energy_statistic_id = lambda acct: f"snopud:energy_consumption_{acct}"
stats_stub.cost_statistic_id = lambda acct: f"snopud:energy_cost_{acct}"
sys.modules["custom_components.snopud.statistics"] = stats_stub

# Same for snopud_client — not used by _advance_cumulative / seed logic.
client_stub = types.ModuleType("custom_components.snopud.snopud_client")


class _MeterInfo:
    def __init__(self, account_number, internal_id, service_type, rate_schedule):
        self.account_number = account_number
        self.internal_id = internal_id
        self.service_type = service_type
        self.rate_schedule = rate_schedule


client_stub.MeterInfo = _MeterInfo
client_stub.SnoPUDAuthError = type("SnoPUDAuthError", (Exception,), {})
client_stub.SnoPUDClient = type("SnoPUDClient", (), {})
client_stub.SnoPUDDownloadError = type("SnoPUDDownloadError", (Exception,), {})
client_stub.SnoPUDError = type("SnoPUDError", (Exception,), {})
sys.modules["custom_components.snopud.snopud_client"] = client_stub

coord_mod = _load(
    "custom_components.snopud.coordinator",
    ROOT / "custom_components/snopud/coordinator.py",
)
SnoPUDCoordinator = coord_mod.SnoPUDCoordinator
_HOURLY_END_EPSILON = coord_mod._HOURLY_END_EPSILON


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_coord() -> "SnoPUDCoordinator":
    """Construct a coordinator with just enough state for cumulative tests."""
    c = SnoPUDCoordinator.__new__(SnoPUDCoordinator)
    c._cumulative_kwh = {}
    c._cumulative_cost_usd = {}
    c._cumulative_seeded = set()
    c._last_seen_cumulative = {}
    return c


def _reading(hour: int, minute: int = 0, wh: int = 250, cost_cents: int | None = None):
    """Build a 15-minute IntervalReading in UTC on 2026-04-22."""
    return IntervalReading(
        start=datetime(2026, 4, 22, hour, minute, tzinfo=timezone.utc),
        duration_seconds=900,
        value_wh=wh,
        cost_cents=cost_cents,
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_advance_cumulative_no_cursor_adds_everything() -> None:
    """With no prior cursor, every reading contributes."""
    c = _make_coord()
    readings = [_reading(14, 0), _reading(14, 15), _reading(14, 30)]
    c._advance_cumulative("ACCT", readings)
    assert c._cumulative_kwh["ACCT"] == 0.75  # 3 × 0.25 kWh
    assert c._last_seen_cumulative["ACCT"] == readings[-1].start
    print("✓ no-cursor advance adds every reading")


def test_advance_cumulative_with_cursor_skips_duplicates() -> None:
    """A cursor set by a prior advance must skip already-counted readings."""
    c = _make_coord()
    # Prior advance: 14:00, 14:15.
    c._advance_cumulative("ACCT", [_reading(14, 0), _reading(14, 15)])
    assert c._cumulative_kwh["ACCT"] == 0.5
    # Next advance arrives with 14:15 (duplicate) + 14:30 + 14:45 (both new).
    c._advance_cumulative(
        "ACCT",
        [_reading(14, 15), _reading(14, 30), _reading(14, 45)],
    )
    assert c._cumulative_kwh["ACCT"] == 1.0  # 4 × 0.25, not 5 × 0.25
    print("✓ cursor-guarded advance skips duplicates")


def test_restart_scenario_does_not_double_count() -> None:
    """Regression test for the restart double-count bug.

    Simulates what happens after a Home Assistant restart:
    1. ``_cumulative_kwh`` has been seeded from persisted hourly stats.
    2. ``_last_seen_cumulative`` has been seeded to
       ``last_hourly_start + 1h - 1µs``.
    3. The 15-min sensor feed covers the last 3 days, which overlap the
       hourly window.

    Before the fix: no cursor was persisted, so the advance re-added all 3
    days of 15-min readings on top of the hourly seed — double-counting the
    recent window. After the fix: the cursor is seeded, so all 15-min
    readings inside the last sealed hour are skipped, and only 15-min
    readings in the *next* hour onward contribute.
    """
    c = _make_coord()

    # Pretend the persisted hourly stats has last row starting at 14:00,
    # with cumulative sum of 1000 kWh through the end of that hour.
    last_hourly_start = datetime(2026, 4, 22, 14, 0, tzinfo=timezone.utc)
    c._cumulative_kwh["ACCT"] = 1000.0
    c._cumulative_cost_usd["ACCT"] = 50.0
    c._last_seen_cumulative["ACCT"] = last_hourly_start + _HOURLY_END_EPSILON
    c._cumulative_seeded.add("ACCT")

    # Simulated 15-min feed: overlaps the 14:00-15:00 hour, then extends
    # into 15:00-15:45 (the genuinely-new slices that should be added).
    feed = [
        _reading(14, 0,  cost_cents=5),   # duplicate of 14:00 hourly
        _reading(14, 15, cost_cents=5),   # duplicate
        _reading(14, 30, cost_cents=5),   # duplicate
        _reading(14, 45, cost_cents=5),   # duplicate
        _reading(15, 0,  cost_cents=5),   # NEW
        _reading(15, 15, cost_cents=5),   # NEW
        _reading(15, 30, cost_cents=5),   # NEW
        _reading(15, 45, cost_cents=5),   # NEW
    ]
    c._advance_cumulative("ACCT", feed)

    # Only the four 15-min readings at 15:00+ contribute: 4 × 0.25 = 1.0 kWh.
    assert c._cumulative_kwh["ACCT"] == 1001.0, (
        f"restart double-count regression: expected 1001.0, got "
        f"{c._cumulative_kwh['ACCT']}"
    )
    # Same for cost: 4 × $0.05 = $0.20.
    assert round(c._cumulative_cost_usd["ACCT"], 4) == 50.20, (
        f"cost restart double-count regression: expected 50.20, got "
        f"{c._cumulative_cost_usd['ACCT']}"
    )
    # Cursor advanced to the latest reading's start.
    assert c._last_seen_cumulative["ACCT"] == feed[-1].start
    print("✓ restart scenario does NOT double-count (fix verified)")


def test_restart_scenario_without_cursor_seed_would_double_count() -> None:
    """Negative control: without the cursor seed (i.e. the pre-fix state),
    the same restart scenario DOES double-count. This documents what the
    fix prevents."""
    c = _make_coord()

    # Seed cumulative from "persisted stats" but DO NOT seed the cursor —
    # this is what the pre-fix code did (the cursor was an in-memory
    # dynamic attribute not restored from storage).
    c._cumulative_kwh["ACCT"] = 1000.0
    c._cumulative_cost_usd["ACCT"] = 50.0
    c._cumulative_seeded.add("ACCT")
    # (_last_seen_cumulative left empty for "ACCT")

    feed = [_reading(h, m) for h in (14, 15) for m in (0, 15, 30, 45)]
    c._advance_cumulative("ACCT", feed)

    # All 8 readings added: 8 × 0.25 = 2.0 kWh double-counted on top of the
    # 1000 seed. This is the bug.
    assert c._cumulative_kwh["ACCT"] == 1002.0, (
        f"pre-fix behavior baseline changed: expected 1002.0, got "
        f"{c._cumulative_kwh['ACCT']}"
    )
    print("✓ negative control: pre-fix behavior reproduces double-count")


def test_advance_cumulative_empty_readings_noop() -> None:
    """Advancing with an empty reading list is a no-op in every field."""
    c = _make_coord()
    c._cumulative_kwh["ACCT"] = 5.0
    c._last_seen_cumulative["ACCT"] = datetime(
        2026, 4, 22, 12, 0, tzinfo=timezone.utc
    )
    c._advance_cumulative("ACCT", [])
    assert c._cumulative_kwh["ACCT"] == 5.0
    assert c._last_seen_cumulative["ACCT"] == datetime(
        2026, 4, 22, 12, 0, tzinfo=timezone.utc
    )
    print("✓ empty-readings advance is a no-op")


def _monkeypatch_parse_green_button(coord_module, scripted_feeds):
    """Replace ``parse_green_button`` in the coordinator module with one
    that pops from a scripted list of ``GreenButtonFeed`` instances. Returns
    the original function so the caller can restore it."""
    gb = sys.modules["custom_components.snopud.green_button"]
    original = coord_module.parse_green_button

    remaining = list(scripted_feeds)

    def _fake(_xml):
        if remaining:
            return remaining.pop(0)
        # Fallback: empty feed (terminates walk).
        return gb.GreenButtonFeed(
            reading_type=None, readings=[], usage_point_id=None
        )

    coord_module.parse_green_button = _fake
    return original


def test_chunked_backfill_partial_failure_does_not_mark_complete() -> None:
    """Regression test: if ``_chunked_backfill`` hits a download error before
    walking the full requested range, it must return ``completed=False`` so
    the caller does NOT latch the per-meter "backfilled" flag. The next
    refresh will then retry from scratch and continue filling in the
    unfetched range (the import layer is idempotent, so already-written
    chunks won't be duplicated).
    """
    c = _make_coord()

    SnoPUDDownloadError = sys.modules[
        "custom_components.snopud.snopud_client"
    ].SnoPUDDownloadError
    gb = sys.modules["custom_components.snopud.green_button"]

    # Script: first chunk returns a non-empty feed (so the walk *doesn't*
    # terminate naturally on empty readings); second chunk raises.
    first_feed = gb.GreenButtonFeed(
        reading_type=None,
        readings=[
            gb.IntervalReading(
                start=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
                duration_seconds=3600,
                value_wh=1000,
            )
        ],
        usage_point_id="up1",
    )
    original_parse = _monkeypatch_parse_green_button(coord_mod, [first_feed])

    call_count = {"n": 0}

    class _StubClient:
        async def async_download_green_button(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return b"<ignored-by-monkeypatched-parser/>"
            raise SnoPUDDownloadError("simulated transient failure")

    meter = types.SimpleNamespace(account_number="ACCT9", internal_id="int")

    try:
        merged, completed = asyncio.run(
            c._chunked_backfill(
                _StubClient(),
                meter,
                [meter],
                datetime(2026, 4, 22, tzinfo=timezone.utc).date(),
                interval="5",       # INTERVAL_HOURLY
                total_days=180,     # 2 chunks × 90-day MAX_DOWNLOAD_WINDOW_DAYS
            )
        )
    finally:
        coord_mod.parse_green_button = original_parse

    assert completed is False, (
        "partial backfill must signal completed=False so the caller doesn't "
        "latch its 'done' flag"
    )
    # First chunk succeeded and contributed one reading; second chunk raised
    # → merged feed has the one reading from the successful chunk.
    assert len(merged.readings) == 1, (
        f"expected 1 reading from the successful first chunk, got "
        f"{len(merged.readings)}"
    )
    print("✓ partial chunked_backfill returns completed=False")


def test_chunked_backfill_full_success_marks_complete() -> None:
    """Counterpart: when the portal returns an empty chunk terminating the
    walk naturally, ``completed`` is True — empty = "no older history
    available", which is a legitimate finish, not a failure."""
    c = _make_coord()

    class _StubClient:
        async def async_download_green_button(self, *args, **kwargs):
            return b"<ignored-by-monkeypatched-parser/>"

    original_parse = _monkeypatch_parse_green_button(coord_mod, [])

    meter = types.SimpleNamespace(account_number="ACCT10", internal_id="int")
    try:
        merged, completed = asyncio.run(
            c._chunked_backfill(
                _StubClient(),
                meter,
                [meter],
                datetime(2026, 4, 22, tzinfo=timezone.utc).date(),
                interval="5",
                total_days=180,
            )
        )
    finally:
        coord_mod.parse_green_button = original_parse

    assert completed is True, (
        "empty-feed terminal condition must signal completed=True so the "
        "caller latches 'done' and doesn't keep retrying forever"
    )
    print("✓ empty-feed chunked_backfill returns completed=True")


def test_hourly_end_epsilon_math() -> None:
    """The cursor seed (``T + 1h - 1µs``) must skip in-hour 15-min slices
    while accepting the next hour's first slice — verify the boundary math
    directly."""
    T = datetime(2026, 4, 22, 14, 0, tzinfo=timezone.utc)
    cursor = T + _HOURLY_END_EPSILON

    # 15-min slices inside 14:00-15:00: all should be <= cursor (skipped).
    for minute in (0, 15, 30, 45):
        slice_start = datetime(
            2026, 4, 22, 14, minute, tzinfo=timezone.utc
        )
        assert slice_start <= cursor, (
            f"expected in-hour slice 14:{minute:02d} to fall inside cursor, "
            f"but {slice_start} > {cursor}"
        )

    # First slice of next hour: should be > cursor (accepted).
    next_hour_slice = datetime(2026, 4, 22, 15, 0, tzinfo=timezone.utc)
    assert next_hour_slice > cursor, (
        f"next hour's first slice {next_hour_slice} should be accepted, "
        f"but it's not > {cursor}"
    )
    print("✓ _HOURLY_END_EPSILON boundary math is correct")


if __name__ == "__main__":
    test_hourly_end_epsilon_math()
    test_advance_cumulative_no_cursor_adds_everything()
    test_advance_cumulative_with_cursor_skips_duplicates()
    test_advance_cumulative_empty_readings_noop()
    test_restart_scenario_without_cursor_seed_would_double_count()
    test_restart_scenario_does_not_double_count()
    test_chunked_backfill_partial_failure_does_not_mark_complete()
    test_chunked_backfill_full_success_marks_complete()
    print("\nall coordinator tests passed")
