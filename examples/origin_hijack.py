import datetime
import logging
import sys
from collections import deque
from pathlib import Path

import numpy as np
import requests_cache

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers import UpdateTagsCounter

FORMAT = "%(asctime)s %(levelname)s %(message)s"
CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)
logging.basicConfig(
    format=FORMAT,
    handlers=[
        logging.FileHandler(CACHE / "example_origin_hijack.log"),
        logging.StreamHandler(sys.stdout),
    ],
    # INFO, not DEBUG: the pipeline logs per-peer prefix counts at DEBUG, and
    # counting them walks every tree.
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Beside the MRT archives, rather than in whatever directory this was run from.
requests_cache.install_cache(str(CACHE / "http_cache"))

# Define study parameters 
config = QuickRIBConfig(
    start_time=datetime.datetime(2010, 9, 1, 0, 0),
    end_time=datetime.datetime(2010, 9, 1, 1, 59),
    collectors=["route-views.wide", "rrc04"],
    dump_res=datetime.timedelta(minutes=5),
    cache_dir=Path("cache"),
    parser="bgpkit"
)
quickrib = QuickRIB.from_config(config)

class PrefixHijackAD(UpdateTagsCounter):
    def __init__(self, name: str="prefix_hijack_detector", window_size: int=5, **kwargs):
        super().__init__(name, **kwargs)
        self.window_size = window_size
        self.origin_changes = deque(maxlen=window_size + 1)
    
    @staticmethod
    def _z_score(query: int, history: list[int]):
        print(f"Query {query}, History {history}")
        return (query - np.mean(history)) / (np.std(history) + 0.001)
        
    def dump(self, ts: datetime.datetime) -> dict[str, int]:
        counts = super().dump(ts)
        origin_change = counts.get("ORIGIN_CHANGE", 0)
        self.origin_changes.append(origin_change)
        if len(self.origin_changes) == self.window_size + 1:
            anomaly_score = self._z_score(self.origin_changes[-1], list(self.origin_changes)[:-1])
            print(f"Anomaly score: {anomaly_score:.2f}")
        return counts
        
prefix_hijack_detector = PrefixHijackAD()
quickrib.rib.attach_observer(prefix_hijack_detector)

quickrib.run()
