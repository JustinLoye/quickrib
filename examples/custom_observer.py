"""Writing an observer: a simple update counter.

Inherit :class:`~quickrib.observers.Observer` and override only the hooks you
care about. Every method has a no-op default, so this counter never has to
mention `compare`, `set_rib` or `set_full_feed_peers`.

Attach it with `quickrib.rib.attach_observer(UpdateCountObserver())` and the
pipeline will drive it: `update_rib` for every RIB-dump entry while the table is
being built, then `update_announcement` and `update_withdrawal` for every BGP
message, and `dump` once per `dump_res`.

Note what the update hooks are handed besides the BGP element. `data` is the
prefix's node in the RIB *after* the change, and `old_data` is a snapshot of it
before, or None if the peer did not have that prefix. Comparing the two is how
an observer tells a real change from a re-announcement that only moved an
attribute it does not care about; `hegemony.py` and `ioda.py` do this in
earnest.
"""

import datetime
from collections import defaultdict
from typing import Optional

from quickrib.elements import AnyElement, ParsedElement, RIBNodeData, WithdrawalElement
from quickrib.observers import Observer


class UpdateCountObserver(Observer):
    def __init__(self, name: str = "update_count"):
        super().__init__(name)
        self.n_rib_entries = defaultdict(int)
        self.n_updates = defaultdict(int)
        self.n_withdrawals = defaultdict(int)
        self.n_announcements = defaultdict(int)
        self.n_updates_per_peer = defaultdict(lambda: defaultdict(int))

    def _count_update(self, bgpelem: AnyElement):
        self.n_updates[bgpelem.collector] += 1
        peer = (bgpelem.peer_asn, bgpelem.peer_address)
        self.n_updates_per_peer[bgpelem.collector][peer] += 1

    def update_rib(self, bgpelem: ParsedElement):
        self.n_rib_entries[bgpelem.collector] += 1

    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData]):
        self.n_withdrawals[bgpelem.collector] += 1
        self._count_update(bgpelem)

    def update_announcement(
        self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData]
    ):
        self.n_announcements[bgpelem.collector] += 1
        self._count_update(bgpelem)

    def dump(self, ts: datetime.datetime) -> dict[str, int]:
        # Return the result rather than writing it. Where output goes is the
        # calling program's decision, not the observer's.
        return dict(self.n_updates)
