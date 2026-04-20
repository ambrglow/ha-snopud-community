"""Parser for Green Button (ESPI) Atom feeds returned by the MySnoPUD portal.

This is a narrow, purpose-built parser — not a full ESPI implementation. It
handles the shape of feeds the portal's public export produces:

    feed
      entry (LocalTimeParameters)   — ignored; we use tz-aware timestamps directly
      entry (UsagePoint)            — identifies the meter
      entry (MeterReading)
      entry (ReadingType)           — units, interval length, accumulation behavior
      entry (IntervalBlock)+        — one or more, each containing IntervalReadings

Values are delivered as watt-hours per interval (delta-style) and optionally
include cost in currency subunits. We expose them as kWh deltas (plus $ if
present) along with absolute start timestamps.

Expected ReadingType for hourly electricity consumption:
    intervalLength = 3600  (1 hour; also accepts 900, 1800, etc.)
    uom            = 72    (Wh)
    commodity      = 1     (electricity)
    flowDirection  = 1     (delivered)
    accumulation   = 4     (delta)
    powerOfTen     = 0     (×1)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator
from xml.etree import ElementTree as ET

_LOGGER = logging.getLogger(__name__)

# Namespaces used in the feed
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "espi": "http://naesb.org/espi",
}


@dataclass(frozen=True)
class IntervalReading:
    """A single meter reading over a fixed interval."""

    start: datetime  # UTC, aware
    duration_seconds: int
    value_wh: int  # raw reading in watt-hours (before powerOfTen scaling)
    power_of_ten: int = 0  # typically 0 for SnoPUD; kept for correctness
    cost_cents: int | None = None  # optional ESPI <cost> in currency subunits

    @property
    def value_kwh(self) -> float:
        """Return the reading in kWh, applying the ESPI power-of-ten multiplier."""
        return (self.value_wh * (10**self.power_of_ten)) / 1000.0

    @property
    def value_dollars(self) -> float | None:
        """Return the cost in dollars, if present."""
        if self.cost_cents is None:
            return None
        return self.cost_cents / 100.0

    @property
    def end(self) -> datetime:
        """Interval end timestamp (exclusive)."""
        return datetime.fromtimestamp(
            self.start.timestamp() + self.duration_seconds, tz=timezone.utc
        )


@dataclass(frozen=True)
class ReadingType:
    """ESPI ReadingType metadata extracted from the feed."""

    commodity: int | None
    uom: int | None
    flow_direction: int | None
    accumulation_behaviour: int | None
    interval_length_seconds: int | None
    power_of_ten_multiplier: int

    @property
    def is_expected_electricity_consumption(self) -> bool:
        """Return True if this matches the expected electricity-delivered delta shape.

        Interval length is intentionally not constrained here: we request
        hourly data by default, but fall back to billing-interval for
        non-smart / retired meters, and the parser handles both.
        """
        from .const import (  # local import avoids HA import at module load
            ESPI_ACCUMULATION_DELTA,
            ESPI_COMMODITY_ELECTRICITY,
            ESPI_FLOW_DIRECTION_DELIVERED,
            ESPI_UOM_WATT_HOURS,
        )
        return (
            self.commodity == ESPI_COMMODITY_ELECTRICITY
            and self.uom == ESPI_UOM_WATT_HOURS
            and self.flow_direction == ESPI_FLOW_DIRECTION_DELIVERED
            and self.accumulation_behaviour == ESPI_ACCUMULATION_DELTA
            and (self.interval_length_seconds or 0) > 0
        )


@dataclass
class GreenButtonFeed:
    """A parsed Green Button feed."""

    reading_type: ReadingType | None
    readings: list[IntervalReading]
    usage_point_id: str | None

    def __len__(self) -> int:
        return len(self.readings)


def _text(elem: ET.Element | None, path: str) -> str | None:
    if elem is None:
        return None
    found = elem.find(path, _NS)
    return found.text.strip() if found is not None and found.text else None


def _int(elem: ET.Element | None, path: str) -> int | None:
    val = _text(elem, path)
    try:
        return int(val) if val is not None else None
    except ValueError:
        return None


def parse_green_button(xml_bytes: bytes | str) -> GreenButtonFeed:
    """Parse a Green Button Atom feed into structured readings.

    Parameters
    ----------
    xml_bytes : bytes or str
        The raw XML body from the /Usage/Download endpoint.

    Returns
    -------
    GreenButtonFeed
        Parsed feed. May be empty if no IntervalBlocks are present.

    Raises
    ------
    ValueError
        If the XML is malformed or not an Atom feed.
    """
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as err:
        raise ValueError(f"malformed Green Button XML: {err}") from err

    # Accept either namespaced or non-namespaced root — real SnoPUD responses use Atom namespace
    if not root.tag.endswith("feed"):
        raise ValueError(f"expected Atom <feed> root, got <{root.tag}>")

    reading_type: ReadingType | None = None
    usage_point_id: str | None = None
    readings: list[IntervalReading] = []

    for entry in root.findall("atom:entry", _NS):
        content = entry.find("atom:content", _NS)
        if content is None:
            continue

        # ReadingType
        rt_elem = content.find("espi:ReadingType", _NS)
        if rt_elem is not None:
            reading_type = ReadingType(
                commodity=_int(rt_elem, "espi:commodity"),
                uom=_int(rt_elem, "espi:uom"),
                flow_direction=_int(rt_elem, "espi:flowDirection"),
                accumulation_behaviour=_int(rt_elem, "espi:accumulationBehaviour"),
                interval_length_seconds=_int(rt_elem, "espi:intervalLength"),
                power_of_ten_multiplier=_int(rt_elem, "espi:powerOfTenMultiplier") or 0,
            )
            continue

        # UsagePoint — pull the ID from the self link rather than the body
        up_elem = content.find("espi:UsagePoint", _NS)
        if up_elem is not None:
            self_link = entry.find("atom:link[@rel='self']", _NS)
            if self_link is not None:
                href = self_link.get("href", "")
                # e.g. .../UsagePoint/09000001 -> "09000001"
                usage_point_id = href.rsplit("/", 1)[-1] or None
            continue

        # IntervalBlock(s)
        ib_elem = content.find("espi:IntervalBlock", _NS)
        if ib_elem is not None:
            readings.extend(
                _iter_interval_readings(
                    ib_elem,
                    power_of_ten=reading_type.power_of_ten_multiplier
                    if reading_type
                    else 0,
                )
            )
            continue

    # Keep readings time-ordered and de-duplicated (some feeds repeat interval boundaries)
    seen: set[float] = set()
    deduped: list[IntervalReading] = []
    for r in sorted(readings, key=lambda x: x.start):
        ts = r.start.timestamp()
        if ts in seen:
            continue
        seen.add(ts)
        deduped.append(r)

    if reading_type and not reading_type.is_expected_electricity_consumption:
        _LOGGER.warning(
            "SnoPUD Green Button ReadingType has unexpected shape "
            "(commodity=%s uom=%s flow=%s accum=%s interval=%ss); "
            "parsing continues but values may be miscalibrated",
            reading_type.commodity,
            reading_type.uom,
            reading_type.flow_direction,
            reading_type.accumulation_behaviour,
            reading_type.interval_length_seconds,
        )

    return GreenButtonFeed(
        reading_type=reading_type,
        readings=deduped,
        usage_point_id=usage_point_id,
    )


def _iter_interval_readings(
    block: ET.Element, power_of_ten: int
) -> Iterator[IntervalReading]:
    for reading in block.findall("espi:IntervalReading", _NS):
        period = reading.find("espi:timePeriod", _NS)
        start_s = _int(period, "espi:start")
        duration_s = _int(period, "espi:duration")
        value = _int(reading, "espi:value")
        cost = _int(reading, "espi:cost")  # optional, currency subunits (cents)
        if start_s is None or duration_s is None or value is None:
            continue
        yield IntervalReading(
            start=datetime.fromtimestamp(start_s, tz=timezone.utc),
            duration_seconds=duration_s,
            value_wh=value,
            power_of_ten=power_of_ten,
            cost_cents=cost,
        )
