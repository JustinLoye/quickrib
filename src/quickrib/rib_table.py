from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, TypeAlias

import radix

from quickrib.elements import ParsedElement, RIBNodeData, WithdrawalElement
from quickrib.radix_utils import tree_size
from quickrib.utils import dict_diff

if TYPE_CHECKING:  # pragma: no cover
    # Only ever an annotation here, and importing it for real would be a cycle:
    # `quickrib.observers.observer` runs the observers package `__init__`, which
    # imports the observers that import this module. The subject knows the
    # observer interface by shape, not by import.
    from quickrib.observers.observer import Observer


logger = logging.getLogger(__name__)

RIBData: TypeAlias = Mapping[str, Mapping[tuple[int, str], radix.Radix]]
"""
MAPPING STRUCTURE:
rc: str -> VP (peer_asn: int, peer_ip: str) -> prefix radix.Radix -> data (path: list[str], communities list[str]})
"""


class RIBTable:
    """The **subject**: the reconstructed routing table, and what notifies observers.

    :class:`~quickrib.quickrib.QuickRIB` drives it: ``update_rib`` for every
    RIB-dump entry while the table is being built, then ``update_announcement``
    and ``update_withdrawal`` for every BGP message, and ``dump`` once per
    window. Each of those notifies the attached observers, which is what keeps
    analysis decoupled from reconstruction: the same replay drives any number of
    them.

    Attributes
    ----------
    data : RIBData
        ``collector -> (peer_asn, peer_ip) -> radix.Radix``, each node's ``.data``
        holding ``as-path``, ``communities`` and ``time``.

    Examples
    --------
    >>> quickrib = QuickRIB.from_config(config)
    >>> observer = IODAObserver()
    >>> quickrib.rib.attach_observer(observer)
    >>> quickrib.run()
    >>> observer.series                     # what the observer accumulated

    Announcement and withdrawal observers are kept in two lists so their update
    order can be set independently, or one observer can be made to drive another.
    Those lists govern update ordering only. ``dump``, ``set_rib`` and
    ``set_full_feed_peers`` go to every attached observer either way.
    """

    def __init__(self):
        # Every attached observer, in attachment order. The two lists below
        # govern *update* ordering only; lifecycle notifications go to everyone.
        self._observers: list[Observer] = []
        self._observers_announcement: list[Observer] = []
        self._observers_withdraw: list[Observer] = []

        # collector -> peer -> radix of prefixes -> {as-path, communities, time}
        self.data: RIBData = defaultdict(lambda: defaultdict(radix.Radix))

    def __iter__(self):
        for rc, peers_to_pfxs in self.data.items():
            for peer, peer_table in peers_to_pfxs.items():
                for rnode in peer_table.nodes():
                    yield (rc, peer, rnode.prefix, rnode.data)

    def get_bgpelem(self, rc: str, peer_asn: int, peer_ip: str, pfx: str) -> ParsedElement:
        """Rebuild the element that would have produced this RIB entry."""
        rnode = self.data[rc][(peer_asn, peer_ip)].search_exact(pfx)
        if rnode is None:
            raise KeyError(f"{pfx} is not in {rc}'s table for peer {peer_asn}")

        return ParsedElement(
            type="R",
            collector=rc,
            peer_asn=peer_asn,
            peer_address=peer_ip,
            time=rnode.data["time"],
            fields={
                "prefix": pfx,
                "as-path": rnode.data["as-path"],
                "communities": rnode.data["communities"],
            },
        )
    
    def iter_bgpelems(self) -> Iterator[ParsedElement]:
        for rc, peer, pfx, _ in self:
            yield self.get_bgpelem(rc, peer[0], peer[1], pfx)

    def attach_observer(self, observer: Observer):
        self._observers.append(observer)
        self._observers_announcement.append(observer)
        self._observers_withdraw.append(observer)

    def detach_observer(self, observer: Observer):
        self._observers.remove(observer)
        self._observers_announcement.remove(observer)
        self._observers_withdraw.remove(observer)

    def _track(self, observers: list[Observer]) -> None:
        """Register observers that were installed by setting an update list.

        Setting one of the update lists directly is how update *ordering* is
        customised. It must not decide who exists: an observer installed that way
        still gets `dump`, `set_rib` and `set_full_feed_peers` like any other.
        """
        for observer in observers:
            if observer not in self._observers:
                self._observers.append(observer)

    def set_observers_announcement(self, observers: list[Observer]):
        self._observers_announcement = observers
        self._track(observers)

    def set_observers_withdraw(self, observers: list[Observer]):
        self._observers_withdraw = observers
        self._track(observers)

    def _notify_update_rib(self, bgpelem: ParsedElement):
        # A RIB entry is a lifecycle event like `dump`, not an update: every
        # attached observer gets it, whichever update list it was installed in.
        for observer in self._observers:
            observer.update_rib(bgpelem)

    def _notify_dump(self, ts: datetime.datetime):
        for observer in self._observers:
            observer.dump(ts)

    def notify_built(self):
        """Hand observers a back-reference once the table is built."""
        for observer in self._observers:
            observer.set_rib(self)

    def notify_full_feed_peers(self, ff_peers: set[tuple[str, int, str]]):
        """Hand the pipeline's full-feed selection to observers that want it.

        Called once, before any element is replayed, with the vantage points
        `QuickRIB.initialize_processing` selected. `set_full_feed_peers` is an
        optional hook: observers that do not define it are left alone.
        """
        for observer in self._observers:
            setter = getattr(observer, "set_full_feed_peers", None)
            if setter is not None:
                setter(ff_peers)

    def update_rib(self, bgpelem: ParsedElement):
        """Note that update notification is delegated to the private method"""
        rnode = self.data[bgpelem.collector][(bgpelem.peer_asn, bgpelem.peer_address)].add(bgpelem.fields["prefix"])
        rnode.data["as-path"] = bgpelem.fields["as-path"]
        rnode.data["communities"] = bgpelem.fields["communities"]
        rnode.data["time"] = bgpelem.time
        self._notify_update_rib(bgpelem)

    def update_withdrawal(self, bgpelem: WithdrawalElement):
        """Remove the prefix from the peer's table, then notify.

        The delete happens *before* the notification, not after: an observer
        raising must not be able to leave a withdrawn prefix in the table for the
        rest of the run. The node's ``.data`` dict is captured first and outlives
        the node it came from, so observers still see exactly what was withdrawn.
        ``data`` is ``None`` when the peer did not have the prefix.
        """
        # Read, do not index: a withdrawal from a peer that has no table must not
        # create an empty one.
        peer_table = self.peer_table(bgpelem.collector, bgpelem.peer_asn, bgpelem.peer_address)
        pfx = bgpelem.fields["prefix"]

        data = None
        if peer_table is not None:
            rnode = peer_table.search_exact(pfx)
            if rnode is not None:
                data = rnode.data
                peer_table.delete(pfx)

        for observer in self._observers_withdraw:
            observer.update_withdrawal(bgpelem, data)

    def update_announcement(self, bgpelem: ParsedElement):
        pfx = bgpelem.fields["prefix"]
        peer_table = self.data[bgpelem.collector][(bgpelem.peer_asn, bgpelem.peer_address)]

        # Need to make a copy to send the old state to observers
        existing_node = peer_table.search_exact(pfx)
        old_data = existing_node.data.copy() if existing_node else None

        rnode = peer_table.add(pfx)
        rnode.data["as-path"] = bgpelem.fields["as-path"]
        rnode.data["communities"] = bgpelem.fields["communities"]
        rnode.data["time"] = bgpelem.time

        self._enrich_announcement(rnode.data, old_data)

        for observer in self._observers_announcement:
            observer.update_announcement(bgpelem, rnode.data, old_data)

    def _enrich_announcement(
        self, data: RIBNodeData, old_data: RIBNodeData | None
    ) -> None:
        """Hook for a RIB *flavor* to add to a node between the write and the notify.

        A subclass that enriches ``.data``, such as
        :class:`~quickrib.observers.update_tagger.RIBTablePathHistory`, overrides
        this rather than the whole of :meth:`update_announcement`, so it inherits
        the write and the notification contract instead of restating them.
        """

    def dump(self, ts):
        # The RIB itself is not written anywhere; a dump is what the observers
        # make of it.
        logger.info(
            "Dumping at %s: %d collectors, %s peers",
            ts,
            len(self.data),
            {rc: len(peers) for rc, peers in self.data.items()},
        )
        # Per-peer prefix counts are worth a line but not their price: counting
        # them walks every node of every tree, which at a full table is far more
        # work than the dump it is describing. Only pay it if DEBUG is on.
        if logger.isEnabledFor(logging.DEBUG):
            for rc, peers in self.data.items():
                for peer, peer_table in peers.items():
                    logger.debug(
                        "peer %s at rc %s sees %d prefixes", peer, rc, tree_size(peer_table)
                    )

        self._notify_dump(ts)

    def get_peer_table(self, rc: str, peer_asn: int, peer_ip: str) -> radix.Radix:
        """The peer's table, **creating** an empty one if it has none.

        Convenient for building; see :meth:`peer_table` for the read that does
        not insert.
        """
        return self.data[rc][(peer_asn, peer_ip)]

    def peer_table(self, rc: str, peer_asn: int, peer_ip: str) -> radix.Radix | None:
        """The peer's table, or ``None``: a read that leaves the RIB alone.

        ``data`` is a nested ``defaultdict``, so indexing it for a peer that does
        not exist silently creates one. Anything merely *inspecting* a RIB (and
        especially anything inspecting somebody else's) should come through here.
        """
        peers = self.data.get(rc)
        return None if peers is None else peers.get((peer_asn, peer_ip))

    @staticmethod
    def _peer_table_to_dict(peer_table: radix.Radix) -> dict[str, list[str] | None]:
        """Flatten a peer's radix table into a `{prefix: as-path}` mapping."""
        return {
            rnode.prefix: rnode.data.get("as-path")
            for rnode in peer_table.nodes()
        }

    def compare(self, other: RIBTable):
        """Report how this RIB and its observers differ from a ground-truth pair.

        ``other`` is read through :meth:`peer_table`, never indexed: comparing
        against a RIB must not add peers to it.
        """
        for rc, peers_to_pfxs in self.data.items():
            for peer in peers_to_pfxs:
                other_table = other.peer_table(rc, *peer)
                if other_table is None:
                    logger.error("peer %s at %s is not in the ground truth", peer, rc)
                    continue

                other_peer_dict = self._peer_table_to_dict(other_table)
                if not other_peer_dict:
                    continue

                logger.info("Performing RIB check for peer %s at %s", peer, rc)
                own_peer_dict = self._peer_table_to_dict(peers_to_pfxs[peer])
                added, removed, modified = dict_diff(own_peer_dict, other_peer_dict)

                if not (added or removed or modified):
                    logger.info("No RIB reconstruction error")
                    continue

                total = len(other_peer_dict)
                logger.info(
                    "%d (%.2f %%) pfx present only in ground truth",
                    len(added), 100 * len(added) / total,
                )
                logger.info(
                    "%d (%.2f %%) pfx present only in my processed version",
                    len(removed), 100 * len(removed) / total,
                )
                logger.info(
                    "%d (%.2f %%) pfx present in both but with different as-paths",
                    len(modified), 100 * len(modified) / total,
                )

        # Compare observers, paired by name.
        own_by_name = {observer.name: observer for observer in self._observers}
        for other_observer in other._observers:
            own_observer = own_by_name.get(other_observer.name)
            if own_observer is not None:
                own_observer.compare(other_observer)
