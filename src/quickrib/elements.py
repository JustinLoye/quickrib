"""What QuickRIB puts in front of its observers.

``pybgpflux`` yields a :class:`~pybgpflux.BGPElement` whose ``fields`` is a
``total=False`` TypedDict shared by every kind of MRT record it can parse, so
``as-path`` is typed as the wire string and everything is optional.

QuickRIB narrows both of those before an element reaches an observer:

* :func:`~quickrib.quickrib.process_path` rewrites ``as-path`` **in place** from
  the wire string to a list of ASNs, and drops the element entirely if the path
  is malformed or contains an AS set. :class:`ParsedElement` is that same
  element with the field typed truthfully.
* Only ``R``, ``A`` and ``W`` elements exist. ``pybgpflux`` never yields BGP
  *state-change* records: its ``bgpdump`` parser filters them and its element
  ``type`` is ``Literal["R", "A", "W"]``. A prefix is therefore always present on
  the elements QuickRIB handles, even though ``ElementFields`` allows its
  absence.
  The ``old-state`` / ``new-state`` keys there are vestigial, inherited from
  ``pybgpstream``, which does emit state changes.

:class:`RIBNodeData` is the other half: what a radix node's ``.data`` holds, and
what observers are handed as ``data`` / ``old_data``.
"""

from collections import deque
from typing import Literal, NamedTuple, NotRequired, TypedDict

ParsedFields = TypedDict(
    "ParsedFields",
    {
        "prefix": str,
        "as-path": list[str],
        "communities": list[str],
        "next-hop": NotRequired[str],
    },
)
"""``fields`` of a route (``R`` or ``A``), after QuickRIB has parsed the AS path.

Everything a route carries is *required*: ``process_path`` drops any element
whose path is missing or malformed, so by the time an observer sees one, the
prefix, the path and the community list are all there. Only ``next-hop``, which
QuickRIB never reads, may be absent.
"""


class WithdrawalFields(TypedDict):
    """``fields`` of a withdrawal, which names a prefix and nothing else."""

    prefix: str


class ParsedElement(NamedTuple):
    """A route whose ``as-path`` has been parsed into a list of ASNs.

    Structurally identical to :class:`~pybgpflux.BGPElement`. QuickRIB does not
    build a new object per message; it just stops lying about the field types
    once the path has been rewritten.
    """

    time: float
    type: Literal["R", "A"]
    collector: str
    peer_asn: int
    peer_address: str
    fields: ParsedFields


class WithdrawalElement(NamedTuple):
    """A withdrawal, whose ``fields`` hold only the prefix being withdrawn."""

    time: float
    type: Literal["W"]
    collector: str
    peer_asn: int
    peer_address: str
    fields: WithdrawalFields


AnyElement = ParsedElement | WithdrawalElement
"""Either kind. Use it for helpers that only touch what both carry: the peer,
the collector, the timestamp, and ``fields["prefix"]``."""


RIBNodeData = TypedDict(
    "RIBNodeData",
    {
        "as-path": list[str],
        "communities": list[str],
        "time": float,
        "as-path-history": NotRequired["deque[list[str]]"],
    },
)
"""What a radix node's ``.data`` holds, and what observers get as ``data``.

``RIBTable`` writes all three on every insert, so all three are required.
``as-path-history`` is the exception: it only exists when the RIB is a
:class:`~quickrib.observers.update_tagger.RIBTablePathHistory`.
"""
