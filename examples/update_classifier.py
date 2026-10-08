import datetime
import logging
import sys
from pathlib import Path

import requests_cache

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers.update_tagger import RIBTablePathHistory, UpdateTagsCounter

FORMAT = "%(asctime)s %(levelname)s %(message)s"
CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)
logging.basicConfig(
    format=FORMAT,
    handlers=[
        logging.FileHandler(CACHE / "example_update_classifier.log"),
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
    parser="bgpkit",
    rib_cls=RIBTablePathHistory
)
quickrib = QuickRIB.from_config(config)
        
# Use a built-in analysis module. `UpdateTagsCounter` accumulates the BLT tags
# and logs the totals once per `dump_res`; `UpdateTagger` is the same
# classification without the counting, and with `log_tags=True` it traces every
# individual message at DEBUG, which suits a filtered stream but not this one.
#
# `rib_cls=RIBTablePathHistory` above is what makes PATH_SWITCHING possible: it
# keeps each prefix's previous AS paths on the node, which route-flap detection
# needs and a plain RIBTable does not carry.
observer = UpdateTagsCounter()

# Bind observers to RIB
quickrib.rib.attach_observer(observer)

quickrib.run()