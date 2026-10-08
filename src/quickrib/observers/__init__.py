"""Analysis modules attached to a :class:`~quickrib.rib_table.RIBTable`."""

from quickrib.observers.bgplay import (
    BGPlayObserver,
    diff_bgplay,
    format_diff,
    is_equivalent,
)
from quickrib.observers.hegemony import HegemonyObserver, RibHegemonyObserver
from quickrib.observers.ioda import (
    IODAObserver,
    RibIODAObserver,
    detect_alerts,
    detect_events,
    evaluate_series,
    full_feed_peers,
    visible_slash24s,
)
from quickrib.observers.mad import (
    MADConfig,
    MADObserver,
    MADStarObserver,
    MADStaticObserver,
)
from quickrib.observers.observer import Observer
from quickrib.observers.tester import TesterObserver
from quickrib.observers.update_tagger import UpdateTagger, UpdateTagsCounter

__all__ = [
    "Observer",
    "BGPlayObserver",
    "diff_bgplay",
    "format_diff",
    "is_equivalent",
    "IODAObserver",
    "RibIODAObserver",
    "detect_alerts",
    "detect_events",
    "evaluate_series",
    "full_feed_peers",
    "visible_slash24s",
    "TesterObserver",
    "UpdateTagger",
    "UpdateTagsCounter",
    "HegemonyObserver",
    "RibHegemonyObserver",
    "MADConfig",
    "MADObserver",
    "MADStaticObserver",
    "MADStarObserver",
]
