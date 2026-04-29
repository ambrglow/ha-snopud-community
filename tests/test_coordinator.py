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
# Capture references at import time so later tests in the same pytest
# session can't shadow them by re-importing the real ``snopud_client``
# module. The chunked-backfill tests below catch this exception class to
# simulate a transient SnoPUD download failure.
_STUB_SnoPUDDownloadError = client_stub.SnoPUDDownloadError

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
    # State for ``_merge_recent_intervals`` (added in v0.2.7).
    c._recent_intervals_by_start = {}
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


def test_seed_cumulative_self_heals_from_stale_initial_read() -> None:
    """Regression test for the CSV-bug pattern.

    In production we observed sensor values like:
      pre-restart:   87.36
      post-restart:  718.973   ← seed read 0, advance added 7d of 15-min
      ...           830.326    ← still wrong, latch prevented re-seed
      eventually:   31423.973  ← only fixed via the billing-supplement path

    Root cause: the seed was latched once-per-process. If the very first
    post-restart seed read 0 (because ``async_add_external_statistics`` had
    not yet been flushed to the recorder, or because of a transient DB
    hiccup), the bad value would stick for the rest of the process
    lifetime.

    Fix: ``_seed_cumulative_from_stats`` is no longer latched. Subsequent
    refreshes re-read the LTS and pick up the real value, healing the
    counter automatically. This test simulates that exact scenario:
    first seed returns 0, second seed returns the real cumulative — and
    asserts the in-memory counter ends up at the real value, not stuck
    on whatever was accumulated against the stale 0.
    """
    c = _make_coord()

    # Scripted seed source: first call returns (0.0, None) — pretending
    # the recorder hasn't flushed; second call returns the correct value.
    seeds = iter([
        (0.0, None),
        (1000.0, datetime(2026, 4, 22, 14, 0, tzinfo=timezone.utc)),
    ])

    async def _scripted_seed(account_number: str) -> None:
        kwh_seed, kwh_last_start = next(seeds)
        c._cumulative_kwh[account_number] = kwh_seed
        c._cumulative_cost_usd[account_number] = 0.0
        if kwh_last_start is not None:
            c._last_seen_cumulative[account_number] = (
                kwh_last_start + _HOURLY_END_EPSILON
            )
        else:
            c._last_seen_cumulative.pop(account_number, None)

    # First refresh: stale seed (0) → advance with 4 readings → counter
    # ends up at 4 × 0.25 = 1.0 kWh. Wrong, but that's the transient state.
    asyncio.run(_scripted_seed("ACCT"))
    c._advance_cumulative(
        "ACCT",
        [_reading(14, 0), _reading(14, 15), _reading(14, 30), _reading(14, 45)],
    )
    assert c._cumulative_kwh["ACCT"] == 1.0, (
        f"first refresh should accumulate against stale seed; got "
        f"{c._cumulative_kwh['ACCT']}"
    )

    # Second refresh: seed re-runs (no latch), reads the *real* value
    # (1000.0) AND seeds the cursor to 14:00 + 1h - 1µs. The next advance
    # now skips the in-hour 15-min slices (already in the hourly sum) and
    # only adds genuinely new 15:00+ slices. Critically, the in-memory
    # counter resets to 1000.0 — *not* 1000.0 + (whatever stale value we
    # had). The LTS is the source of truth.
    asyncio.run(_scripted_seed("ACCT"))
    assert c._cumulative_kwh["ACCT"] == 1000.0, (
        f"second seed must overwrite the stale in-memory cumulative with "
        f"the real LTS sum; got {c._cumulative_kwh['ACCT']}"
    )
    c._advance_cumulative(
        "ACCT",
        [_reading(14, 0), _reading(14, 15), _reading(14, 30), _reading(14, 45),
         _reading(15, 0), _reading(15, 15)],
    )
    # Only the two 15:00+ slices contribute: 1000 + 0.5 = 1000.5.
    assert c._cumulative_kwh["ACCT"] == 1000.5, (
        f"after second seed + advance, expected 1000.5; got "
        f"{c._cumulative_kwh['ACCT']}"
    )
    print("✓ seed is non-latched: stale initial read self-heals on re-seed")


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

    # Use the captured stub class rather than re-reading from sys.modules at
    # call time — under pytest collection, an earlier test in the same
    # session may have imported the real ``snopud_client`` module and
    # replaced the stub, in which case sys.modules now points at a real
    # module without our test fixtures.
    SnoPUDDownloadError = _STUB_SnoPUDDownloadError
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


# ---------------------------------------------------------------------------
# recent_intervals merge (v0.2.7+)
# ---------------------------------------------------------------------------

# Pull the limit from the loaded const module rather than re-defining it, so a
# future change to the constant flows through the tests automatically.
SENSOR_RECENT_INTERVAL_LIMIT = sys.modules[
    "custom_components.snopud.const"
].SENSOR_RECENT_INTERVAL_LIMIT


def test_merge_recent_intervals_includes_every_new_bucket() -> None:
    """Regression test for the SnoPUD-lag bug.

    SnoPUD's portal can lag 6–8 hours. When a single refresh's parsed feed
    surfaces multiple newly-available 15-minute buckets at once (e.g. at
    8 PM the export newly contains 10 AM, 10:15 AM, 10:30 AM, …), the
    ``recent_intervals`` array must include EVERY one of them — not just
    the newest. This is the whole point of the redesign: the bar chart
    fills in retroactively as the portal catches up, instead of showing
    only the single newest bar.
    """
    c = _make_coord()
    # Simulate a refresh in which the export newly reveals four 15-minute
    # intervals from 10:00 AM to 11:00 AM. Each is 900 s long.
    feed = [
        _reading(10, 0,  wh=333),
        _reading(10, 15, wh=310),
        _reading(10, 30, wh=358),
        _reading(10, 45, wh=275),
    ]
    out = c._merge_recent_intervals("ACCT", feed)
    assert len(out) == 4, (
        f"expected all 4 newly-discovered buckets to be included, got "
        f"{len(out)}"
    )
    # Sorted chronologically, oldest first.
    starts = [item["start"] for item in out]
    assert starts == sorted(starts), "recent_intervals must be sorted by start"
    # kWh values round to mWh precision — meters report integer Wh.
    assert out[0]["kwh"] == 0.333
    assert out[1]["kwh"] == 0.310
    assert out[2]["kwh"] == 0.358
    assert out[3]["kwh"] == 0.275
    # Each entry carries an ``end`` matching ``start + 15min``.
    for item in out:
        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end"])
        assert end - start == timedelta(minutes=15)
    print("✓ recent_intervals includes every newly-discovered bucket")


def test_merge_recent_intervals_dedups_by_start_across_refreshes() -> None:
    """Across two refreshes whose 15-min feeds overlap, ``recent_intervals``
    must dedupe by interval start (newer fetch wins) — and brand-new buckets
    discovered in the second refresh must still appear."""
    c = _make_coord()

    # Refresh #1: portal currently exposes 09:00–10:00 (4 buckets).
    refresh1 = [_reading(9, m, wh=200) for m in (0, 15, 30, 45)]
    out1 = c._merge_recent_intervals("ACCT", refresh1)
    assert len(out1) == 4

    # Refresh #2: portal has caught up — feed now contains 09:30 through
    # 11:00 (overlapping the first refresh's 09:30 + 09:45, plus four new
    # 10:xx buckets and one 11:00 bucket). The 09:30/09:45 readings now
    # have *revised* values, so the new fetch should win on those keys.
    refresh2 = (
        [_reading(9, 30, wh=222), _reading(9, 45, wh=242)]
        + [_reading(10, m, wh=275) for m in (0, 15, 30, 45)]
        + [_reading(11, 0, wh=290)]
    )
    out2 = c._merge_recent_intervals("ACCT", refresh2)

    # 4 (from refresh #1) + 4 new 10:xx + 1 new 11:00 - 0 dropped = 9.
    assert len(out2) == 9, (
        f"expected 4 carried over (with 2 revised) + 5 new = 9 buckets, got "
        f"{len(out2)}"
    )
    # Verify the revised 09:30 and 09:45 values won out over the originals
    # — newer fetch wins on collision.
    by_start = {item["start"]: item for item in out2}
    assert by_start[refresh1[2].start.isoformat()]["kwh"] == 0.222
    assert by_start[refresh1[3].start.isoformat()]["kwh"] == 0.242
    # The 09:00 / 09:15 buckets that weren't in refresh #2 stayed put.
    assert by_start[refresh1[0].start.isoformat()]["kwh"] == 0.200
    assert by_start[refresh1[1].start.isoformat()]["kwh"] == 0.200
    # Brand-new buckets are present.
    assert refresh2[-1].start.isoformat() in by_start
    print("✓ recent_intervals dedups by start across refreshes; new wins")


def test_merge_recent_intervals_trims_to_retention_limit() -> None:
    """When the rolling window exceeds ``SENSOR_RECENT_INTERVAL_LIMIT``, the
    oldest entries are dropped — and the in-memory backing dict is rebuilt
    so it doesn't leak memory across refreshes."""
    c = _make_coord()
    # Fabricate (LIMIT + 50) consecutive 15-min readings.
    base = datetime(2026, 4, 22, 0, 0, tzinfo=timezone.utc)
    n = SENSOR_RECENT_INTERVAL_LIMIT + 50
    feed = [
        IntervalReading(
            start=base + timedelta(minutes=15 * i),
            duration_seconds=900,
            value_wh=200,
        )
        for i in range(n)
    ]
    out = c._merge_recent_intervals("ACCT", feed)
    assert len(out) == SENSOR_RECENT_INTERVAL_LIMIT, (
        f"expected output trimmed to {SENSOR_RECENT_INTERVAL_LIMIT}, got "
        f"{len(out)}"
    )
    # The retained slice is the most-recent ``LIMIT`` entries, in order.
    assert out[0]["start"] == feed[50].start.isoformat(), (
        "expected the oldest 50 buckets to be dropped"
    )
    assert out[-1]["start"] == feed[-1].start.isoformat()
    # And the backing dict was rebuilt to the trimmed set, not the full set.
    assert len(c._recent_intervals_by_start["ACCT"]) == SENSOR_RECENT_INTERVAL_LIMIT
    print("✓ recent_intervals trims to retention limit and rebuilds backing dict")


def test_merge_recent_intervals_skips_non_15min_readings() -> None:
    """If the 15-min download fails and the coordinator falls back to the
    hourly feed, those 3600-second readings must NOT pollute the bar chart's
    rolling window — every bucket in ``recent_intervals`` must be a true
    900-second slice."""
    c = _make_coord()
    mixed = [
        IntervalReading(
            start=datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc),
            duration_seconds=3600,           # hourly fallback — REJECTED
            value_wh=1000,
        ),
        IntervalReading(
            start=datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
            duration_seconds=900,            # genuine 15-min — accepted
            value_wh=250,
        ),
        IntervalReading(
            start=datetime(2026, 4, 22, 10, 15, tzinfo=timezone.utc),
            duration_seconds=1800,           # half-hour — REJECTED
            value_wh=500,
        ),
    ]
    out = c._merge_recent_intervals("ACCT", mixed)
    assert len(out) == 1, f"expected only the 900s reading, got {len(out)}"
    assert out[0]["kwh"] == 0.250
    print("✓ recent_intervals rejects non-15-minute readings")


def test_merge_recent_intervals_carries_cost_when_present() -> None:
    """When the SnoPUD feed carries cost data, each bucket should expose a
    ``cost_usd`` field. Buckets without cost data must not have the key
    (rather than ``None``) so consumers can rely on its presence."""
    c = _make_coord()
    feed = [
        _reading(10, 0,  wh=250, cost_cents=12),
        _reading(10, 15, wh=250),  # no cost
    ]
    out = c._merge_recent_intervals("ACCT", feed)
    assert "cost_usd" in out[0]
    assert out[0]["cost_usd"] == 0.12
    assert "cost_usd" not in out[1]
    print("✓ recent_intervals carries cost_usd only when present in feed")


# ---------------------------------------------------------------------------
# Bootstrap-deadlock regression (v0.2.8)
# ---------------------------------------------------------------------------

def test_seed_proceeds_when_recorder_block_till_done_hangs() -> None:
    """Regression test for the v0.2.8 bug.

    Repro from production: on a HACS upgrade restart, the recorder's worker
    task hadn't finished starting up by the time SnoPUD's first refresh
    fired. ``recorder.async_block_till_done()`` then waited forever for a
    queue that nobody was draining, and the integration's
    ``async_config_entry_first_refresh`` hung until HA's stage-2 setup
    timeout (5 minutes) tripped and cancelled the task — leaving the
    integration stuck on "Initializing".

    The fix: bound the ``block_till_done`` await with
    ``asyncio.wait_for(..., timeout=5.0)`` and proceed on TimeoutError. The
    seed read may then hit slightly-stale persisted stats; the unlatched
    re-seed on the next refresh self-heals.

    This test installs a recorder stub whose ``async_block_till_done``
    sleeps for 60 seconds (well past the 5-second cap), confirms that
    ``_seed_cumulative_from_stats`` returns within ~5 seconds rather than
    hanging, and confirms the seed read still happened (i.e. we proceeded
    past the timeout instead of erroring out).
    """
    c = _make_coord()

    # Recorder stub: ``async_block_till_done`` sleeps long enough that the
    # 5-second cap is the only reason the call returns. ``async_add_executor_job``
    # synchronously calls the wrapped function, returning {} from
    # ``get_last_statistics`` (no persisted stats).
    block_call_count = {"n": 0}
    block_completed = {"n": 0}

    class _HangingRecorder:
        async def async_block_till_done(self) -> None:
            block_call_count["n"] += 1
            try:
                # Simulate the bootstrap-stage-2 deadlock: nobody is draining
                # this queue, so the await would never complete on its own.
                await asyncio.sleep(60.0)
            finally:
                # If we got cancelled by wait_for's timeout, this still runs.
                block_completed["n"] += 1

        async def async_add_executor_job(self, fn, *args, **kwargs):
            # Real recorder runs the function on its executor pool. For the
            # test, just call it inline. Returns whatever
            # ``get_last_statistics`` returns; the stub default is {}.
            return fn(*args, **kwargs)

    # The coordinator module imported ``get_instance`` at module-load time
    # via ``from homeassistant.components.recorder.util import get_instance``,
    # so it holds its own bound reference. Patch THAT binding (not just the
    # source module's) so the seed code path actually sees our hanging
    # recorder.
    original_get_instance = coord_mod.get_instance
    coord_mod.get_instance = lambda hass: _HangingRecorder()

    try:
        # Wall-clock the call. Should complete in ~5s (the cap), not 60s
        # (what the hang would take), and not hang forever.
        import time as _time
        c.hass = object()  # placeholder; not used by the seed code path
        start = _time.monotonic()
        asyncio.run(c._seed_cumulative_from_stats("ACCT"))
        elapsed = _time.monotonic() - start
    finally:
        coord_mod.get_instance = original_get_instance

    # Must have invoked block_till_done exactly once.
    assert block_call_count["n"] == 1, (
        f"expected block_till_done called once, got {block_call_count['n']}"
    )
    # Must have returned around the 5s cap, definitely not the 60s the
    # hang would have taken if unbounded.
    assert 4.5 <= elapsed <= 10.0, (
        f"expected ~5s elapsed (the wait_for cap); got {elapsed:.2f}s — "
        f"if much higher, the timeout cap regressed; if much lower, the "
        f"call somehow returned before the cap fired"
    )
    # The seed code must have continued past the timeout: with no
    # persisted stats, both running totals seed to 0.
    assert c._cumulative_kwh.get("ACCT") == 0.0, (
        "seed must proceed past block_till_done timeout and call "
        "get_last_statistics; got no kWh seed"
    )
    print(
        f"✓ seed completes in {elapsed:.2f}s when block_till_done hangs "
        f"(bootstrap-deadlock fix verified)"
    )


if __name__ == "__main__":
    test_hourly_end_epsilon_math()
    test_advance_cumulative_no_cursor_adds_everything()
    test_advance_cumulative_with_cursor_skips_duplicates()
    test_advance_cumulative_empty_readings_noop()
    test_restart_scenario_without_cursor_seed_would_double_count()
    test_restart_scenario_does_not_double_count()
    test_chunked_backfill_partial_failure_does_not_mark_complete()
    test_chunked_backfill_full_success_marks_complete()
    test_seed_cumulative_self_heals_from_stale_initial_read()
    test_merge_recent_intervals_includes_every_new_bucket()
    test_merge_recent_intervals_dedups_by_start_across_refreshes()
    test_merge_recent_intervals_trims_to_retention_limit()
    test_merge_recent_intervals_skips_non_15min_readings()
    test_merge_recent_intervals_carries_cost_when_present()
    test_seed_proceeds_when_recorder_block_till_done_hangs()
    print("\nall coordinator tests passed")
