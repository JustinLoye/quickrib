"""Reproduce IODA's BGP signal and detect an outage in it.

The window brackets a real outage: AS43160 loses roughly a quarter of its
visible /24s at 04:20 UTC and recovers at 05:45.

`IODAObserver` needs no reference to the RIB. The pipeline hands it the
full-feed selection before the replay starts, so it accumulates straight out of
`update_rib`. Running with `fullfeed_only=True` (the default) is therefore both
correct and roughly twice as fast, because the peers it would ignore never enter
the table.
"""

import datetime
from pathlib import Path

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers import IODAObserver, detect_events

OUTAGE_ASN = "43160"

config = QuickRIBConfig(
    start_time=datetime.datetime(2022, 1, 26, 4, 0),
    end_time=datetime.datetime(2022, 1, 26, 6, 30),
    collectors=["route-views.eqix"],
    dump_res=datetime.timedelta(minutes=5),  # IODA's native step
    cache_dir=Path("cache"),
    parser="bgpkit",
)

quickrib = QuickRIB.from_config(config)
observer = IODAObserver(entities=[OUTAGE_ASN])
quickrib.rib.attach_observer(observer)
quickrib.run()

# observer.series maps a 5-minute bin to {origin ASN: visible /24s}.
series = {bin_ts: totals[OUTAGE_ASN] for bin_ts, totals in observer.series.items()}
for bin_ts, visible in sorted(series.items()):
    when = datetime.datetime.fromtimestamp(bin_ts, tz=datetime.UTC)
    print(f"{when:%H:%M}  {visible:>4} visible /24s")

# The alert rule needs 24 hours of history to establish a baseline, so a short
# replay cannot trigger it. Feed it a longer series to get IODA-shaped events.
for event in detect_events(series, location=f"AS{OUTAGE_ASN}"):
    print(f"outage at {event['start']} lasting {event['duration']}s, "
          f"score {event['score']:.0f}")
