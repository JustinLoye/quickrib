"""Reconstruct a routing table and count the updates applied to it.

The smallest complete run: configure a window, attach one observer, call
`run()`. Everything else in QuickRIB is a different observer on the same
pipeline.
"""

import datetime
from pathlib import Path

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers import UpdateTagsCounter

config = QuickRIBConfig(
    start_time=datetime.datetime(2010, 9, 1, 0, 0),
    end_time=datetime.datetime(2010, 9, 1, 1, 59),
    collectors=["route-views.wide"],
    dump_res=datetime.timedelta(minutes=15),
    cache_dir=Path("cache"),
    parser="bgpkit",
)

quickrib = QuickRIB.from_config(config)

counter = UpdateTagsCounter()
quickrib.rib.attach_observer(counter)

quickrib.run()

# The reconstructed table: collector -> (peer ASN, peer IP) -> radix tree.
for collector, peers in quickrib.rib.data.items():
    for (peer_asn, peer_ip), table in peers.items():
        print(f"{collector} AS{peer_asn} {peer_ip}: {len(table.prefixes())} prefixes")
