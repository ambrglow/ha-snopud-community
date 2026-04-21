"""Smoke tests for snopud_client.py that don't require network access or HA.

Loads the relevant modules directly from disk rather than via the
`custom_components.snopud` package, because importing the package triggers
__init__.py which pulls in Home Assistant (not available in plain-Python envs).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Register shell packages so relative imports inside snopud_client.py
# ("from .const import ...") resolve without running the real __init__.py.
pkg_cc = types.ModuleType("custom_components")
pkg_cc.__path__ = [str(ROOT / "custom_components")]
sys.modules["custom_components"] = pkg_cc

pkg_snopud = types.ModuleType("custom_components.snopud")
pkg_snopud.__path__ = [str(ROOT / "custom_components" / "snopud")]
sys.modules["custom_components.snopud"] = pkg_snopud

_load("custom_components.snopud.const", ROOT / "custom_components/snopud/const.py")
_load(
    "custom_components.snopud.green_button",
    ROOT / "custom_components/snopud/green_button.py",
)
client_mod = _load(
    "custom_components.snopud.snopud_client",
    ROOT / "custom_components/snopud/snopud_client.py",
)
MeterInfo = client_mod.MeterInfo
SnoPUDClient = client_mod.SnoPUDClient


SAMPLE_SETTINGS_HTML_ESCAPED = r'''
  <form id=\"downloadOptions\" class=\"form-horizontal\" method=\"post\" action=\"/Usage/Download\">
    <input id=\"Meters_0__Value\" name=\"Meters[0].Value\" type=\"hidden\" value=\"9000001\"/>
    <input id=\"Meters_0__Selected\" class=\"DownloadMeterSelectItem form-check-input\" name=\"Meters[0].Selected\" type=\"checkbox\" value=\"true\" checked=checked/>
    <label class=\"form-check-label fs-7\" for=\"Meters_0__Selected\">Meter #1000000001 (Electric) - Residential Schedule 7</label>
    <input id=\"Meters_1__Value\" name=\"Meters[1].Value\" type=\"hidden\" value=\"9000002\"/>
    <input id=\"Meters_1__Selected\" class=\"DownloadMeterSelectItem form-check-input\" name=\"Meters[1].Selected\" type=\"checkbox\" value=\"true\" checked=checked/>
    <label class=\"form-check-label fs-7\" for=\"Meters_1__Selected\">Meter #1000000002 (Electric) - Residential Schedule 7</label>
    <input name=\"__RequestVerificationToken\" type=\"hidden\" value=\"CfDJ8FAKE_TOKEN_VALUE_FOR_TESTS_abc123\"/>
  </form>
'''


def test_parse_meters() -> None:
    meters = SnoPUDClient._parse_meters(SAMPLE_SETTINGS_HTML_ESCAPED)
    assert len(meters) == 2, f"got {len(meters)} meters"
    by_acct = {m.account_number: m for m in meters}
    assert "1000000001" in by_acct
    assert by_acct["1000000001"].internal_id == "9000001"
    assert by_acct["1000000001"].service_type == "Electric"
    assert by_acct["1000000001"].rate_schedule == "Residential Schedule 7"
    assert by_acct["1000000002"].internal_id == "9000002"
    print(f"✓ parsed {len(meters)} meters from sample HTML")


def test_build_download_form_single_meter_selected() -> None:
    meters = [
        MeterInfo("1000000001", "9000001", "Electric", "Residential Schedule 7"),
        MeterInfo("1000000002", "9000002", "Electric", "Residential Schedule 7"),
    ]
    body = SnoPUDClient._build_download_form(
        token="TEST_TOKEN",
        meters=meters,
        target_internal_id="9000001",
        start=date(2026, 4, 15),
        end=date(2026, 4, 17),
    )
    pairs = parse_qsl(body, keep_blank_values=True)
    d: dict[str, list[str]] = {}
    for k, v in pairs:
        d.setdefault(k, []).append(v)

    assert d["Meters[0].Value"] == ["9000001"]
    assert d["Meters[0].Selected"] == ["true"]
    assert d["Meters[1].Value"] == ["9000002"]
    assert "Meters[1].Selected" not in d, "unselected meter must not send .Selected"

    assert d["SelectedFormat"] == ["1"]
    # Default interval is hourly now ("5"). HA's external statistics API
    # requires hour-aligned timestamps, so hourly is the natural grain.
    assert d["SelectedInterval"] == ["5"], (
        f"expected hourly interval '5', got {d['SelectedInterval']}"
    )
    assert d["SelectedUsageType"] == ["1"]
    assert d["Start"] == ["2026-04-15"]
    assert d["End"] == ["2026-04-17"]
    assert d["__RequestVerificationToken"] == ["TEST_TOKEN"]

    for i in range(8):
        assert f"ColumnOptions[{i}].Value" in d, f"missing ColumnOptions[{i}].Value"
        assert f"ColumnOptions[{i}].Name" in d, f"missing ColumnOptions[{i}].Name"
    assert d["ColumnOptions[6].Checked"] == ["true"]
    assert d["ColumnOptions[7].Checked"] == ["true"]
    for i in range(6):
        assert d[f"ColumnOptions[{i}].Checked"] == ["false"]
    for i in range(3):
        assert f"RowOptions[{i}].Value" in d

    print(f"✓ form body: {len(pairs)} fields, {len(body)} bytes")


def test_build_download_form_with_billing_interval() -> None:
    meters = [MeterInfo("1000000001", "9000001", "Electric", "Residential Schedule 7")]
    body = SnoPUDClient._build_download_form(
        token="TEST_TOKEN",
        meters=meters,
        target_internal_id="9000001",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        interval="7",  # INTERVAL_BILLING
    )
    pairs = dict(parse_qsl(body, keep_blank_values=True))
    assert pairs["SelectedInterval"] == "7", (
        f"expected billing interval '7', got {pairs['SelectedInterval']!r}"
    )
    print("✓ billing-interval override round-trips")


def test_build_download_form_with_fifteen_min_interval() -> None:
    """Pin the 15-minute sensor-entity path: the coordinator fetches both
    hourly (for LTS) and 15-min (for the sensor) on every refresh."""
    meters = [MeterInfo("1000000001", "9000001", "Electric", "Residential Schedule 7")]
    body = SnoPUDClient._build_download_form(
        token="TEST_TOKEN",
        meters=meters,
        target_internal_id="9000001",
        start=date(2026, 4, 15),
        end=date(2026, 4, 18),
        interval="3",  # INTERVAL_15MIN
    )
    pairs = dict(parse_qsl(body, keep_blank_values=True))
    assert pairs["SelectedInterval"] == "3", (
        f"expected 15-min interval '3', got {pairs['SelectedInterval']!r}"
    )
    print("✓ 15-minute sensor-path interval round-trips")


def test_default_user_agent_is_honest() -> None:
    # User-Agent must identify the integration, not spoof a browser.
    client = SnoPUDClient.__new__(SnoPUDClient)
    client._default_headers = {}
    SnoPUDClient.__init__(
        client,
        http_session=None,  # type: ignore[arg-type]
        email="x@example.com",
        password="pw",
    )
    ua = client._default_headers["User-Agent"]
    assert ua.startswith("ha-snopud-community/"), f"bad UA: {ua!r}"
    assert "Mozilla" not in ua, f"UA must not spoof a browser: {ua!r}"
    assert "Chrome" not in ua, f"UA must not spoof a browser: {ua!r}"
    assert "github.com" in ua, f"UA should link to the repo: {ua!r}"
    print(f"✓ honest UA: {ua}")


if __name__ == "__main__":
    test_parse_meters()
    test_build_download_form_single_meter_selected()
    test_build_download_form_with_billing_interval()
    test_build_download_form_with_fifteen_min_interval()
    test_default_user_agent_is_honest()
    print("\nall client tests passed")
