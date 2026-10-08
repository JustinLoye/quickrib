"""The observer interface.

An observer is an analysis module attached to a :class:`~quickrib.rib_table.RIBTable`,
which notifies it of every RIB mutation as the pipeline replays BGP. Inherit
:class:`Observer` and override only the hooks you need. Every method has a
no-op default, so an observer that ignores withdrawals simply does not define
one.

It is a :class:`~typing.Protocol`, so structural typing works too: anything with
the right methods can be attached. Inheriting is the recommended route because
it documents the intent and gives you the defaults.

Observers do not write files. ``dump`` returns its result and the calling
program decides what to do with it. Where output goes, and in what format, is
not the observer's business.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Protocol

from quickrib.elements import ParsedElement, RIBNodeData, WithdrawalElement

if TYPE_CHECKING:  # pragma: no cover
    from quickrib.rib_table import RIBTable

VP = tuple[str, int, str]
"""Vantage point: ``(collector, peer_asn, peer_ip)``."""


class Observer(Protocol):
    """Base class and structural type for everything attached to a ``RIBTable``.

    The three ``update_*`` hooks run inside the replay loop, once per BGP
    message, so keep them O(1) and allocation-free; push aggregation into
    :meth:`dump`. See the performance notes in AGENTS.md.

    ``RIBTable`` ignores whatever the hooks return, but they are typed as
    returning something so an observer can hand a result to a subclass, as
    :class:`~quickrib.observers.update_tagger.UpdateTagsCounter` does with the
    tagger's flags.
    """

    name: str = "observer"
    """Identifies the observer; :meth:`RIBTable.compare` pairs observers by it."""

    def __init__(self, name: str = "observer") -> None:
        self.name = name

    def update_rib(self, bgpelem: ParsedElement) -> Any:
        """A RIB-dump entry, replayed while the table is being built."""
        return None

    def update_announcement(
        self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData]
    ) -> Any:
        """An announcement.

        ``data`` is the prefix's new node ``.data``; ``old_data`` is a snapshot of
        the previous one, or ``None`` when the peer did not have this prefix.
        Comparing the two is how an observer tells a real change from a
        re-announcement that only moved an attribute it does not care about.
        """
        return None

    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData]) -> Any:
        """A withdrawal. ``data`` is the withdrawn node ``.data``, or ``None`` if
        the peer did not have the prefix, in which case nothing was counted and
        there is nothing to take back."""
        return None

    def dump(self, ts: datetime) -> Any:
        """Emit the result for the window ending at ``ts``, and return it.

        ``ts`` is the window *boundary* (``start_time + k * dump_res``), not the
        message that happened to cross it. Every elapsed boundary is reported
        even across a gap in the stream, so a series has one entry per window
        however busy the collectors were.
        """
        return None

    def compare(self, other: Observer) -> None:
        """Report how this observer differs from one built on a ground-truth RIB."""
        return None

    def set_rib(self, rib: RIBTable) -> None:
        """Receive a back-reference to the subject, once the RIB is built.

        The escape hatch for observers that genuinely cannot work from the
        notifications alone; treat the reference as read-only.
        """
        return None

    def set_full_feed_peers(self, ff_peers: set[VP]) -> None:
        """Receive the pipeline's full-feed selection, before any replay."""
        return None
