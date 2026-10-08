"""Reproduce the RIPEstat bgplay payload for one prefix.

The payload is the routing state at `start_time` plus every update afterwards,
which is what drives the BGPlay animation. `diff_bgplay` compares it against the
live API response.
"""

import datetime
import json
import urllib.parse
import urllib.request
from pathlib import Path

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers import BGPlayObserver, diff_bgplay, format_diff

RESOURCE = "140.78.0.0/16"

config = QuickRIBConfig(
    start_time=datetime.datetime(2025, 3, 3, 0, 0),
    end_time=datetime.datetime(2025, 3, 3, 2, 0),
    collectors=["rrc04"],
    dump_res=datetime.timedelta(minutes=5),
    cache_dir=Path("cache"),
    parser="bgpkit",
    # The RIPE APIs report partial-feed peers too, so keep them.
    fullfeed_only=False,
)

quickrib = QuickRIB.from_config(config)
observer = BGPlayObserver(RESOURCE, config.start_time, config.end_time)
quickrib.rib.attach_observer(observer)
quickrib.run()

ours = observer.to_dict()
print(f"{len(ours['initial_state'])} initial paths, {len(ours['events'])} events, "
      f"{len(ours['sources'])} peers")

# Compare against the live API.
query = urllib.parse.urlencode({
    "resource": RESOURCE,
    "starttime": config.start_time.strftime("%Y-%m-%dT%H:%M:%S"),
    "endtime": config.end_time.strftime("%Y-%m-%dT%H:%M:%S"),
    "rrcs": "4",
})
url = f"https://stat.ripe.net/data/bgplay/data.json?{query}"
with urllib.request.urlopen(url, timeout=300) as response:
    theirs = json.load(response)["data"]

print("\n".join(format_diff(diff_bgplay(ours, theirs))))
