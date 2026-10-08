"""Compute AS hegemony over a reconstructed routing table.

`edge_hegemony` walks a finished RIB, which is the reference implementation.
`HegemonyObserver` maintains the same graph incrementally as messages arrive;
with `deaggregate=True` the two agree.

For the algorithm validated against the paper's topology, see
`examples/hegemony.py`.
"""

import datetime
from pathlib import Path

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers.hegemony import edge_hegemony, node_hegemony

config = QuickRIBConfig(
    start_time=datetime.datetime(2010, 9, 1, 0, 0),
    end_time=datetime.datetime(2010, 9, 1, 0, 30),
    collectors=["route-views.wide"],
    dump_res=datetime.timedelta(minutes=15),
    cache_dir=Path("cache"),
    parser="bgpkit",
)

quickrib = QuickRIB.from_config(config)
quickrib.run()

# Per-ASN hegemony: how much of the address space every vantage point reaches
# through this AS. Tier-1 transit providers dominate.
nodes = node_hegemony(quickrib.rib.data)
print("Top transit ASes")
for asn, score in sorted(nodes.items(), key=lambda kv: -kv[1])[:10]:
    print(f"  AS{asn:<8} {score:.4f}")

# Per-link hegemony, over the same table.
edges = edge_hegemony(quickrib.rib.data)
print("\nTop AS links")
for (left, right), score in sorted(edges.items(), key=lambda kv: -kv[1])[:10]:
    print(f"  AS{left}-AS{right:<10} {score:.4f}")
