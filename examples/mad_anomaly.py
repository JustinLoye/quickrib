"""Score each window against its recent past with the MAD detectors.

`MADObserver` scores the whole hegemony graph; `MADStarObserver` scores each AS
separately, which localises an anomaly to the ASes it touches. Both read the
same `HegemonyObserver`, whose dump is memoised per timestamp.
"""

import datetime
from pathlib import Path

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers import HegemonyObserver, MADObserver, MADStarObserver

WINDOW = 3  # snapshots of history before a score can be produced

config = QuickRIBConfig(
    start_time=datetime.datetime(2010, 8, 31, 23, 58),
    end_time=datetime.datetime(2010, 9, 1, 1, 59),
    collectors=["route-views.wide"],
    dump_res=datetime.timedelta(minutes=5),
    cache_dir=Path("cache"),
    parser="bgpkit",
)

quickrib = QuickRIB.from_config(config)

hegemony = HegemonyObserver()
glob = MADObserver(hegemony, WINDOW, name="mad_glob", drives_hegemony=False)
star = MADStarObserver(hegemony, WINDOW, name="mad_star", drives_hegemony=False)

# Attach the hegemony observer first so its graph is computed before the
# detectors read it, and pass drives_hegemony=False so it is driven once.
for observer in (hegemony, glob, star):
    quickrib.rib.attach_observer(observer)

quickrib.run()

for ts, score in sorted(glob.scores.items()):
    per_as = star.node_to_scores.get(ts, {})
    worst = max(per_as.items(), key=lambda kv: kv[1], default=("n/a", 0.0))
    print(f"{ts:%H:%M}  graph {score:.4f}   worst AS{worst[0]} {worst[1]:.4f}")
