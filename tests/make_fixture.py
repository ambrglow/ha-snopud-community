"""Generate a synthetic Green Button fixture for unit tests.

Run once to create ``tests/fixtures/sample_green_button.xml``. All data
(timestamps, IDs, readings) is fabricated locally — nothing here came from
a real account.

Shape:
  - Atom feed, 6 entries
  - LocalTimeParameters, UsagePoint, MeterReading, ReadingType, IntervalBlock(s)
  - 15-min intervals, Wh readings starting 2026-04-16 00:00 PT
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

# Fixture start: midnight PT on 2026-04-16 == 07:00 UTC
START_EPOCH = int(
    datetime(2026, 4, 16, 7, 0, 0, tzinfo=timezone.utc).timestamp()
)
INTERVAL_SECONDS = 900
NUM_READINGS = 144  # 1.5 days of 15-min intervals

random.seed(42)

# Typical Pacific NW residential daily shape: low overnight, morning peak,
# midday dip, evening peak. We model each 15-min Wh value around that shape.
def _wh_for_offset(offset_s: int) -> int:
    """Return a plausible 15-min Wh reading for time-of-day."""
    # minute-of-day at PT (subtract 7h from UTC offset seconds)
    pt_minute = ((offset_s // 60) - 7 * 60) % (24 * 60)
    hour = pt_minute / 60.0
    # baseload ~150 Wh per 15min (~600W)
    base = 150
    # morning bump 6–9am
    morning = 250 * max(0, 1 - abs(hour - 7.5) / 2.5) if 5 <= hour <= 10 else 0
    # evening peak 5–9pm
    evening = 400 * max(0, 1 - abs(hour - 19) / 3) if 16 <= hour <= 22 else 0
    noise = random.randint(-40, 80)
    return max(50, int(base + morning + evening + noise))


def _interval_readings_xml(start_epoch: int, count: int) -> str:
    rows = []
    for i in range(count):
        s = start_epoch + i * INTERVAL_SECONDS
        v = _wh_for_offset(s - START_EPOCH)
        rows.append(
            f"""      <espi:IntervalReading>
        <espi:timePeriod>
          <espi:duration>{INTERVAL_SECONDS}</espi:duration>
          <espi:start>{s}</espi:start>
        </espi:timePeriod>
        <espi:value>{v}</espi:value>
      </espi:IntervalReading>"""
        )
    return "\n".join(rows)


def build_feed() -> str:
    # Split readings across 2 IntervalBlocks like the real response did
    split = NUM_READINGS * 68 // 144  # 68/76 split like real sample
    block1_start = START_EPOCH
    block1_dur = split * INTERVAL_SECONDS
    block2_start = START_EPOCH + block1_dur
    block2_dur = (NUM_READINGS - split) * INTERVAL_SECONDS

    block1 = _interval_readings_xml(block1_start, split)
    block2 = _interval_readings_xml(block2_start, NUM_READINGS - split)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:espi="http://naesb.org/espi" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <id>urn:uuid:sample-snopud-fixture</id>
  <title>SnoPUD Green Button sample</title>
  <updated>{now}</updated>
  <link href="https://my.snopud.com/Usage/Download" rel="self"/>

  <entry>
    <id>urn:uuid:ltp</id>
    <link href="https://my.snopud.com/Usage/Download/LocalTimeParameters/1000000000000000" rel="self"/>
    <link href="https://my.snopud.com/Usage/Download/LocalTimeParameters" rel="up"/>
    <title></title>
    <content>
      <espi:LocalTimeParameters>
        <espi:dstEndRule>B40E2000</espi:dstEndRule>
        <espi:dstOffset>3600</espi:dstOffset>
        <espi:dstStartRule>360E2000</espi:dstStartRule>
        <espi:tzOffset>-28800</espi:tzOffset>
      </espi:LocalTimeParameters>
    </content>
    <published>{now}</published>
    <updated>{now}</updated>
  </entry>

  <entry>
    <id>urn:uuid:up</id>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001" rel="self"/>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint" rel="up"/>
    <link href="https://my.snopud.com/Usage/Download/LocalTimeParameters/1000000000000000" rel="related"/>
    <title>UsagePoint</title>
    <content>
      <espi:UsagePoint>
        <espi:ServiceCategory><espi:kind>0</espi:kind></espi:ServiceCategory>
      </espi:UsagePoint>
    </content>
    <published>{now}</published>
    <updated>{now}</updated>
  </entry>

  <entry>
    <id>urn:uuid:mr</id>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/MeterReading/09000003" rel="self"/>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/MeterReading" rel="up"/>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/ReadingType/09000003" rel="related"/>
    <title>MeterReading</title>
    <content>
      <espi:MeterReading/>
    </content>
    <published>{now}</published>
    <updated>{now}</updated>
  </entry>

  <entry>
    <id>urn:uuid:rt</id>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/ReadingType/09000003" rel="self"/>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/ReadingType" rel="up"/>
    <title>ReadingType</title>
    <content>
      <espi:ReadingType>
        <espi:accumulationBehaviour>4</espi:accumulationBehaviour>
        <espi:commodity>1</espi:commodity>
        <espi:currency>0</espi:currency>
        <espi:dataQualifier>12</espi:dataQualifier>
        <espi:flowDirection>1</espi:flowDirection>
        <espi:intervalLength>900</espi:intervalLength>
        <espi:kind>12</espi:kind>
        <espi:phase>0</espi:phase>
        <espi:powerOfTenMultiplier>0</espi:powerOfTenMultiplier>
        <espi:timeAttribute>2</espi:timeAttribute>
        <espi:uom>72</espi:uom>
      </espi:ReadingType>
    </content>
    <published>{now}</published>
    <updated>{now}</updated>
  </entry>

  <entry>
    <id>urn:uuid:ib1</id>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/MeterReading/09000003/IntervalBlock/1000000001" rel="self"/>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/MeterReading/09000003/IntervalBlock" rel="up"/>
    <title>IntervalBlock</title>
    <content>
      <espi:IntervalBlock>
        <espi:interval>
          <espi:duration>{block1_dur}</espi:duration>
          <espi:start>{block1_start}</espi:start>
        </espi:interval>
{block1}
      </espi:IntervalBlock>
    </content>
    <published>{now}</published>
    <updated>{now}</updated>
  </entry>

  <entry>
    <id>urn:uuid:ib2</id>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/MeterReading/09000003/IntervalBlock/1000000002" rel="self"/>
    <link href="https://my.snopud.com/Usage/Download/UsagePoint/09000001/MeterReading/09000003/IntervalBlock" rel="up"/>
    <title>IntervalBlock</title>
    <content>
      <espi:IntervalBlock>
        <espi:interval>
          <espi:duration>{block2_dur}</espi:duration>
          <espi:start>{block2_start}</espi:start>
        </espi:interval>
{block2}
      </espi:IntervalBlock>
    </content>
    <published>{now}</published>
    <updated>{now}</updated>
  </entry>
</feed>
"""


if __name__ == "__main__":
    out = Path(__file__).parent / "fixtures" / "sample_green_button.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_feed(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
