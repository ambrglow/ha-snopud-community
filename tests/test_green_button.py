"""Smoke tests for green_button.py.

Loads modules directly to bypass the package __init__.py (which imports HA).
Run directly: python3 tests/test_green_button.py
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
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
gb = _load(
    "custom_components.snopud.green_button",
    ROOT / "custom_components/snopud/green_button.py",
)
parse_green_button = gb.parse_green_button

FIXTURE = ROOT / "tests" / "fixtures" / "sample_green_button.xml"


def test_parses_fixture() -> None:
    xml = FIXTURE.read_bytes()
    feed = parse_green_button(xml)

    assert feed.reading_type is not None
    assert feed.reading_type.is_expected_electricity_consumption, (
        f"ReadingType unexpected: {feed.reading_type}"
    )
    assert feed.usage_point_id == "09000001"
    assert len(feed.readings) == 144

    for a, b in zip(feed.readings, feed.readings[1:]):
        assert a.start < b.start, "readings must be time-ordered"

    first = feed.readings[0]
    assert first.duration_seconds == 900
    assert first.start == datetime(2026, 4, 16, 7, 0, 0, tzinfo=timezone.utc)
    assert first.value_wh > 0
    assert 0 < first.value_kwh < 2

    total_kwh = sum(r.value_kwh for r in feed.readings)
    assert 5 < total_kwh < 100, f"implausible total: {total_kwh} kWh"

    print(f"✓ parsed {len(feed.readings)} readings")
    print(
        f"  reading_type: commodity={feed.reading_type.commodity} "
        f"uom={feed.reading_type.uom} interval={feed.reading_type.interval_length_seconds}s"
    )
    print(f"  usage_point_id: {feed.usage_point_id}")
    print(
        f"  first reading:  {first.start.isoformat()}  "
        f"{first.value_wh} Wh  ({first.value_kwh:.3f} kWh)"
    )
    last = feed.readings[-1]
    print(
        f"  last reading:   {last.start.isoformat()}  "
        f"{last.value_wh} Wh  ({last.value_kwh:.3f} kWh)"
    )
    print(f"  total:          {total_kwh:.2f} kWh across {len(feed.readings)} intervals")


def test_rejects_malformed() -> None:
    try:
        parse_green_button(b"not xml")
    except ValueError:
        print("✓ malformed input rejected")
        return
    raise AssertionError("should have raised ValueError")


def test_rejects_non_atom() -> None:
    try:
        parse_green_button(b"<?xml version='1.0'?><root/>")
    except ValueError as e:
        assert "feed" in str(e)
        print("✓ non-Atom root rejected")
        return
    raise AssertionError("should have raised ValueError")


if __name__ == "__main__":
    test_parses_fixture()
    test_rejects_malformed()
    test_rejects_non_atom()
    print("\nall tests passed")
