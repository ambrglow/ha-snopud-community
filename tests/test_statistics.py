"""Unit tests for the statistics import layer.

These tests specifically exercise the overlap/correction handling added to
``async_import_readings``. Prior to the fix, readings whose timestamp
fell on or before the last persisted point were silently dropped, which
meant SnoPUD's late revisions to recently-imported intervals never landed
in the persisted stats. The fix reroutes overlap-containing batches through
``_rebuild_series_with_supplement`` with ``new_wins=True`` so revisions
actually overwrite the stale values.

Home Assistant's recorder is stubbed in-memory: ``async_add_external_statistics``
is captured into a per-statistic_id history, and ``get_last_statistics`` /
``statistics_during_period`` read from that history. This lets us verify the
exact payloads and the running cumulative sums they carry.
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


# --- HA shim -----------------------------------------------------------------

homeassistant = types.ModuleType("homeassistant")
sys.modules.setdefault("homeassistant", homeassistant)

components = types.ModuleType("homeassistant.components")
homeassistant.components = components
sys.modules["homeassistant.components"] = components

recorder = types.ModuleType("homeassistant.components.recorder")
components.recorder = recorder
sys.modules["homeassistant.components.recorder"] = recorder

recorder_stats = types.ModuleType("homeassistant.components.recorder.statistics")
recorder_util = types.ModuleType("homeassistant.components.recorder.util")

# Shared in-memory recorder: { statistic_id: [ {start, state, sum}, ... ] }.
_RECORDER: dict[str, list[dict]] = {}


def _get_last_statistics(hass, count, statistic_id, convert_units, types_):
    rows = _RECORDER.get(statistic_id, [])
    if not rows:
        return {}
    # Return the last ``count`` rows (most recent last).
    sorted_rows = sorted(rows, key=lambda r: r["start"])
    return {statistic_id: sorted_rows[-count:][::-1]}


def _statistics_during_period(
    hass, start, end, stat_ids, period, units, types_
):
    out = {}
    for sid in stat_ids:
        rows = _RECORDER.get(sid, [])
        in_window = [
            r for r in rows
            if start <= r["start"] <= end
        ]
        out[sid] = sorted(in_window, key=lambda r: r["start"])
    return out


def _async_add_external_statistics(hass, metadata, payload):
    sid = metadata["statistic_id"]
    existing = _RECORDER.setdefault(sid, [])
    existing_by_start = {r["start"]: r for r in existing}
    for row in payload:
        existing_by_start[row["start"]] = dict(row)
    _RECORDER[sid] = list(existing_by_start.values())


recorder_stats.get_last_statistics = _get_last_statistics
recorder_stats.statistics_during_period = _statistics_during_period
recorder_stats.async_add_external_statistics = _async_add_external_statistics
sys.modules["homeassistant.components.recorder.statistics"] = recorder_stats


class _Recorder:
    async def async_add_executor_job(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _get_instance(hass):
    return _Recorder()


recorder_util.get_instance = _get_instance
sys.modules["homeassistant.components.recorder.util"] = recorder_util

core = types.ModuleType("homeassistant.core")


class _HomeAssistant:
    pass


core.HomeAssistant = _HomeAssistant
sys.modules["homeassistant.core"] = core


# --- MeterInfo stub ----------------------------------------------------------

client_stub = types.ModuleType("custom_components.snopud.snopud_client")


class _MeterInfo:
    def __init__(self, account_number):
        self.account_number = account_number


client_stub.MeterInfo = _MeterInfo
sys.modules["custom_components.snopud.snopud_client"] = client_stub


stats_mod = _load(
    "custom_components.snopud.statistics",
    ROOT / "custom_components/snopud/statistics.py",
)


# --- helpers -----------------------------------------------------------------

def _reset_recorder() -> None:
    _RECORDER.clear()


def _reading(dt: datetime, wh: int = 1000, cost_cents: int | None = None):
    return IntervalReading(
        start=dt,
        duration_seconds=3600,
        value_wh=wh,
        cost_cents=cost_cents,
    )


def _hour(h: int, day: int = 22) -> datetime:
    return datetime(2026, 4, day, h, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


# --- tests -------------------------------------------------------------------

def test_pure_append_fast_path() -> None:
    """No overlap with existing rows → fast append path, cumulative sum
    continues from prior sum."""
    _reset_recorder()
    meter = _MeterInfo("ACCT1")

    # Seed one existing hourly row at 14:00, sum=1000.
    _RECORDER["snopud:energy_consumption_ACCT1"] = [
        {"start": _hour(14), "state": 1.0, "sum": 1000.0},
    ]

    new_readings = [_reading(_hour(15), wh=500), _reading(_hour(16), wh=500)]
    _run(stats_mod.async_import_readings(
        hass=None, entry_id="x", meter=meter, readings=new_readings,
    ))

    rows = sorted(
        _RECORDER["snopud:energy_consumption_ACCT1"],
        key=lambda r: r["start"],
    )
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
    assert rows[1]["start"] == _hour(15)
    assert rows[1]["sum"] == 1000.5
    assert rows[2]["start"] == _hour(16)
    assert rows[2]["sum"] == 1001.0
    print("✓ pure-append path preserves cumulative continuity")


def test_overlap_correction_rewrites_prior_value() -> None:
    """An incoming reading at the same timestamp as an existing row with a
    different value triggers the rebuild path and the new value wins —
    previously these revisions were silently dropped."""
    _reset_recorder()
    meter = _MeterInfo("ACCT2")

    # Seed two existing hourly rows.
    _RECORDER["snopud:energy_consumption_ACCT2"] = [
        {"start": _hour(14), "state": 1.0, "sum": 1.0},
        {"start": _hour(15), "state": 1.0, "sum": 2.0},
    ]

    # New batch revises 15:00 to 3.0 kWh (up from 1.0). Pre-fix: dropped.
    # Post-fix: rebuild with new_wins=True so the correction lands.
    new_readings = [_reading(_hour(15), wh=3000), _reading(_hour(16), wh=1000)]
    _run(stats_mod.async_import_readings(
        hass=None, entry_id="x", meter=meter, readings=new_readings,
    ))

    rows = sorted(
        _RECORDER["snopud:energy_consumption_ACCT2"],
        key=lambda r: r["start"],
    )
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
    # 14:00 unchanged.
    assert rows[0]["state"] == 1.0
    assert rows[0]["sum"] == 1.0
    # 15:00 rewritten to 3.0.
    assert rows[1]["state"] == 3.0, (
        f"expected 15:00 state revised to 3.0, got {rows[1]['state']}"
    )
    # Cumulative sums recomputed from zero: 1 + 3 = 4 at 15:00.
    assert rows[1]["sum"] == 4.0, (
        f"expected 15:00 sum recomputed to 4.0, got {rows[1]['sum']}"
    )
    # 16:00 appended on top: 4 + 1 = 5.
    assert rows[2]["state"] == 1.0
    assert rows[2]["sum"] == 5.0
    print("✓ overlap correction rewrites stale values and recomputes sums")


def test_billing_supplement_preserves_existing_on_collision() -> None:
    """Billing supplement uses ``new_wins=False`` — existing finer-grain
    (hourly) data always wins over coarser billing-interval rows."""
    _reset_recorder()
    meter = _MeterInfo("ACCT3")

    # Existing hourly row at 14:00, state=2.5 kWh.
    _RECORDER["snopud:energy_consumption_ACCT3"] = [
        {"start": _hour(14), "state": 2.5, "sum": 2.5},
    ]

    # Incoming billing row at 14:00 with 9999 kWh (bogus, to prove it's dropped).
    billing_reading = _reading(_hour(14), wh=9_999_000)
    added = _run(stats_mod.async_import_billing_supplement(
        hass=None, meter=meter, readings=[billing_reading],
    ))
    assert added == 0, (
        f"expected no additions (existing wins on collision), got {added}"
    )
    rows = _RECORDER["snopud:energy_consumption_ACCT3"]
    assert rows[0]["state"] == 2.5, (
        f"billing supplement must NOT overwrite existing hourly data; "
        f"got {rows[0]['state']}"
    )
    print("✓ billing supplement preserves existing hourly values on collision")


def test_overlap_cost_correction() -> None:
    """Cost series corrections also land through the overlap-rebuild path."""
    _reset_recorder()
    meter = _MeterInfo("ACCT4")

    _RECORDER["snopud:energy_consumption_ACCT4"] = [
        {"start": _hour(14), "state": 1.0, "sum": 1.0},
    ]
    _RECORDER["snopud:energy_cost_ACCT4"] = [
        {"start": _hour(14), "state": 0.10, "sum": 0.10},
    ]

    # Revises 14:00 cost from $0.10 to $0.25.
    new_readings = [_reading(_hour(14), wh=1000, cost_cents=25)]
    _run(stats_mod.async_import_readings(
        hass=None, entry_id="x", meter=meter, readings=new_readings,
    ))

    cost_rows = sorted(
        _RECORDER["snopud:energy_cost_ACCT4"], key=lambda r: r["start"]
    )
    assert len(cost_rows) == 1
    assert cost_rows[0]["state"] == 0.25, (
        f"expected cost revised to 0.25, got {cost_rows[0]['state']}"
    )
    assert cost_rows[0]["sum"] == 0.25
    print("✓ cost-series overlap corrections land correctly")


if __name__ == "__main__":
    test_pure_append_fast_path()
    test_overlap_correction_rewrites_prior_value()
    test_billing_supplement_preserves_existing_on_collision()
    test_overlap_cost_correction()
    print("\nall statistics tests passed")
