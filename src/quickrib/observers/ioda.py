"""IODA's BGP signal, and the outage alerts derived from it.

Reproduces the ``bgp`` data source of the IODA API
(https://api.ioda.inetintel.cc.gatech.edu/v2/), whose units are *visible /24s*.
Following IODA's method (Internet Outage Detection and Analysis, GT/CAIDA):

1. A peer is **full-feed** if it carries most of the routing table.
2. A prefix is **visible** if more than 50% of the full-feed peers observe it.
3. The signal for an entity is the number of /24 blocks its visible prefixes
   cover. This is a union, not a sum: a more specific nested inside a visible
   less specific is not counted twice
   (:func:`~quickrib.radix_utils.radix_block_count`).
4. An entity is **alerting** when its value drops below 99% of the median of the
   previous 24 hours; the span from that alert to the next return to normal is
   an outage **event** (``/v2/outages/alerts`` and ``/v2/outages/events``).

QuickRIB attributes a prefix to the **origin AS** of the paths towards it, so
``entityType=asn`` is the entity this module reproduces; country and region
would need the prefix-to-geolocation database IODA layers on top.

Three ways to compute the same thing, mirroring
:mod:`quickrib.observers.hegemony`:

* :func:`full_feed_peers` / :func:`visible_slash24s`: post-hoc functions over a
  reconstructed :data:`~quickrib.rib_table.RIBData`. The reference
  implementation.
* :class:`RibIODAObserver`: an observer wrapper around :func:`visible_slash24s`
  that rescans the whole RIB at every ``dump``.
* :class:`IODAObserver`: an incremental observer that scans the RIB once, then
  maintains per-prefix visibility and per-AS block counts in the ``update_*``
  hot path. It agrees with the post-hoc version exactly.

**Full-feed selection is QuickRIB's, not IODA's.** IODA defines full-feed by an
absolute count (more than 400k IPv4 and/or 10k IPv6 prefixes). QuickRIB uses a
relative rule, more than ``0.8`` times the number of unique prefixes at that
collector, and ``QuickRIB.initialize_processing()`` has already applied it
before the RIB build. :class:`IODAObserver` is handed that selection through
``set_full_feed_peers`` and therefore never has to count prefixes itself, never
needs a reference to the RIB, and can be run with ``fullfeed_only=True`` so
non-full-feed peers never enter the table at all.

The two rules do not select the same peers. QuickRIB's denominator mixes address
families, so at a dual-stack collector the IPv6 prefixes inflate it and IPv4-only
sessions carrying a genuine full table can fall just under the bar. At
``route-views.eqix`` in the pinned window the rule keeps 9 peers where IODA's
would keep 18. The quorum is simply taken over the smaller population; see
``tests/test_ioda.py`` for what that costs in accuracy. For the same reason an
IPv6 signal needs an explicit ``ff_peers`` set at a dual-stack collector, since
no IPv6-only session will ever clear a mixed-family threshold.

Caveats worth knowing when comparing against the live API:

* IODA processes **all** Route Views and RIPE RIS collectors. A QuickRIB run
  over a few collectors sees a smaller full-feed population, so the 50% quorum
  is taken over fewer peers; ``tests/test_ioda.py`` quantifies the resulting
  deviation rather than assuming it away.
* A prefix longer than the block size (an IPv4 /25+) contributes nothing, since
  it does not fill a /24 and is not globally routable.
* Under MOAS the prefix is credited to *every* origin AS seen for it.
"""

from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from statistics import median
from typing import (
    TYPE_CHECKING,
    Literal,
    Optional,
    TypeAlias,
    TypedDict,
)

import radix

from quickrib.elements import AnyElement, ParsedElement, RIBNodeData, WithdrawalElement
from quickrib.observers.observer import Observer
from quickrib.quickrib import FULL_FEED_RATIO
from quickrib.radix_utils import radix_block_count

if TYPE_CHECKING:  # pragma: no cover
    from quickrib.rib_table import RIBData, RIBTable

logger = logging.getLogger(__name__)

VP: TypeAlias = tuple[str, int, str]
"""Vantage point: ``(collector, peer_asn, peer_ip)``, as in hegemony.py."""

# `FULL_FEED_RATIO` is imported from the pipeline that applies it. This module's
# whole method is defined over exactly that selection, so the two must not drift
# apart.
VISIBLE_RATIO = 0.5
"""A prefix is visible when strictly more than this share of full-feed peers see it."""
IODA_STEP = 300
"""The API's native time-series step, in seconds."""
ALERT_THRESHOLD = 0.99
"""Alert when the value drops below this fraction of the 24h median."""
ALERT_HISTORY = 86_400
"""Length of the median window backing an alert, in seconds."""
SCORE_SCALE = 500
"""IODA's event-score scale factor: ``score = 500 * sum(1 - value / history)``."""

def full_feed_peers(
    rib_data: RIBData, *, ratio: float = FULL_FEED_RATIO
) -> set[VP]:
    """The full-feed vantage points of ``rib_data``, by QuickRIB's rule.

    A peer is full-feed if it carries more than ``ratio`` times the number of
    unique prefixes its collector saw, across both address families. This is the
    same computation ``QuickRIB.initialize_processing`` performs on the RIB-dump
    stream. Deriving it here is the post-hoc path; a pipeline run hands the
    already-computed selection to the observer instead.
    """
    full_feed: set[VP] = set()
    for collector, peers in rib_data.items():
        unique: set[str] = set()
        sizes: dict[tuple[int, str], int] = {}
        for peer, peer_table in peers.items():
            prefixes = peer_table.prefixes()
            sizes[peer] = len(prefixes)
            unique.update(prefixes)
        threshold = ratio * len(unique)
        for (peer_asn, peer_ip), size in sizes.items():
            if size > threshold:
                full_feed.add((collector, peer_asn, peer_ip))
    return full_feed


def visible_slash24s(
    rib_data: RIBData,
    *,
    ip_version: Literal[4, 6] = 4,
    ff_peers: Optional[set[VP]] = None,
    visible_ratio: float = VISIBLE_RATIO,
    full_feed_ratio: float = FULL_FEED_RATIO,
) -> dict[str, int]:
    """The IODA BGP signal: origin ASN -> number of visible /24s (or /48s).

    ``ff_peers`` pins the full-feed population instead of deriving it from
    ``rib_data``; :class:`IODAObserver` uses that to keep the two
    implementations comparable at a shared instant.
    """
    if ff_peers is None:
        ff_peers = full_feed_peers(rib_data, ratio=full_feed_ratio)
    quorum = visible_ratio * len(ff_peers)
    want_v6 = ip_version == 6

    # Pass 1: how many full-feed peers see each prefix, and who originates it.
    pfx_peers: dict[str, int] = defaultdict(int)
    pfx_origins: dict[str, set[str]] = defaultdict(set)
    for collector, peers in rib_data.items():
        for (peer_asn, peer_ip), peer_table in peers.items():
            if (collector, peer_asn, peer_ip) not in ff_peers:
                continue
            for rnode in peer_table.nodes():
                prefix = rnode.prefix
                if (":" in prefix) != want_v6:
                    continue
                pfx_peers[prefix] += 1
                pfx_origins[prefix].add(rnode.data["as-path"][-1])

    # Pass 2: keep the prefixes above the quorum, filed under every origin seen
    # for them. Under MOAS each origin is credited the whole prefix.
    trees: dict[str, radix.Radix] = defaultdict(radix.Radix)
    for prefix, seen_by in pfx_peers.items():
        if seen_by > quorum:
            for origin in pfx_origins[prefix]:
                trees[origin].add(prefix)

    # Pass 3: each AS's signal is the union of its visible prefixes, in blocks.
    return {origin: radix_block_count(tree) for origin, tree in trees.items()}


def _bin_of(ts: datetime.datetime | float, step: int = IODA_STEP) -> int:
    """The IODA time bin a dump falls in: the step boundary at or before ``ts``."""
    seconds = ts.timestamp() if isinstance(ts, datetime.datetime) else ts
    return int(seconds) // step * step


class RibIODAObserver(Observer):
    """Post-hoc IODA signal: recompute :func:`visible_slash24s` at every dump.

    The reference observer. It rescans the whole RIB each time, which is
    O(peers x prefixes) per dump: correct, but far too slow to run at IODA's
    5-minute cadence over a long window. Use :class:`IODAObserver` for that.
    """

    def __init__(
        self,
        rib: Optional[RIBTable] = None,
        *,
        ip_version: Literal[4, 6] = 4,
        ff_peers: Optional[set[VP]] = None,
        visible_ratio: float = VISIBLE_RATIO,
        full_feed_ratio: float = FULL_FEED_RATIO,
        entities: Optional[Iterable[str]] = None,
        step: int = IODA_STEP,
        name: str = "ioda_rib",
    ) -> None:
        self.rib = rib
        self.ip_version: Literal[4, 6] = ip_version
        self.ff_peers = ff_peers
        self.visible_ratio = visible_ratio
        self.full_feed_ratio = full_feed_ratio
        self.entities = set(entities) if entities is not None else None
        self.step = step
        self.name = name
        self.series: dict[int, dict[str, int]] = {}


    def update_rib(self, bgpelem: ParsedElement) -> None:
        pass

    def update_announcement(
        self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData] = None
    ) -> None:
        pass

    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData] = None) -> None:
        pass

    def set_rib(self, rib: RIBTable) -> None:
        self.rib = rib

    def set_full_feed_peers(self, ff_peers: set[VP]) -> None:
        """Receive the pipeline's full-feed selection (see :class:`IODAObserver`)."""
        self.ff_peers = set(ff_peers)

    def dump(self, ts: datetime.datetime) -> dict[str, int]:
        if self.rib is None:
            raise RuntimeError(
                "RibIODAObserver has no RIB reference (pass rib= or call set_rib)"
            )
        totals = visible_slash24s(
            self.rib.data,
            ip_version=self.ip_version,
            ff_peers=self.ff_peers,
            visible_ratio=self.visible_ratio,
            full_feed_ratio=self.full_feed_ratio,
        )
        if self.entities is not None:
            totals = {asn: totals.get(asn, 0) for asn in self.entities}
        self.series[_bin_of(ts, self.step)] = totals
        return totals

    def compare(self, other) -> None:  # pragma: no cover
        pass


class IODAObserver(Observer):
    """Incremental IODA signal.

    The observer is told which peers are full-feed before the RIB build.
    ``QuickRIB.initialize_processing`` has already made that selection and hands
    it over through :meth:`set_full_feed_peers`, so the observer can count how
    many of them see each prefix directly in ``update_rib`` and never needs to
    read the RIB back. The first ``dump`` turns those counts into the visible set and the
    per-AS trees; from then on the ``update_*`` hot path maintains them in O(1)
    per element and a dump only recomputes the block count of the ASes whose
    visible set actually changed.

    The state is a chain, each level derived from the one above it::

        _pfx_peers    prefix -> how many full-feed peers currently see it
        _pfx_origins  prefix -> origin ASN -> how many of them report that origin
        _visible      the prefixes whose peer count is above the quorum
        _trees        origin ASN -> radix holding *its* visible prefixes
        _totals       origin ASN -> block count of that radix, i.e. the signal

    An update touches the top two directly. Only a change that crosses the
    quorum, or that adds or drops an origin for an already-visible prefix,
    reaches ``_trees``, and even then it only marks the AS dirty rather than
    recounting it. The block count is paid once per dump, not once per message.

    Because the criterion is QuickRIB's own, running the pipeline with
    ``fullfeed_only=True`` is not just allowed but cheaper: the peers the
    observer would ignore never enter the RIB in the first place.

    The full-feed population is fixed at that hand-over rather than recomputed
    every bin as IODA does. Peer table sizes move by a fraction of a percent over
    a few hours, so the membership does not drift; :meth:`full_feed_drift`
    reports it against a RIB if it ever does.
    """

    def __init__(
        self,
        ff_peers: Optional[Iterable[VP]] = None,
        *,
        ip_version: Literal[4, 6] = 4,
        visible_ratio: float = VISIBLE_RATIO,
        full_feed_ratio: float = FULL_FEED_RATIO,
        entities: Optional[Iterable[str]] = None,
        step: int = IODA_STEP,
        name: str = "ioda",
    ) -> None:
        self.ip_version: Literal[4, 6] = ip_version
        self.visible_ratio = visible_ratio
        self.full_feed_ratio = full_feed_ratio
        self.entities = set(entities) if entities is not None else None
        self.step = step
        self.name = name
        self.series: dict[int, dict[str, int]] = {}

        self._want_v6 = ip_version == 6
        self.ff_peers: set[VP] = set()
        self._quorum = 0.0
        self._have_peers = False
        self._built = False

        self._pfx_peers: dict[str, int] = {}
        "prefix -> number of full-feed peers seeing it"
        self._pfx_origins: dict[str, dict[str, int]] = {}
        "prefix -> origin ASN -> number of full-feed peers reporting that origin"
        self._visible: set[str] = set()
        "prefixes currently above the quorum: the only ones present in the trees"
        self._trees: dict[str, radix.Radix] = {}
        "origin ASN -> radix of its visible prefixes, and the block count of it"
        self._totals: dict[str, int] = {}
        "origin ASN -> its block count as of the last dump; the emitted signal"
        self._dirty: set[str] = set()
        "ASes whose tree changed since the last dump; only these are recomputed"

        if ff_peers is not None:
            self.set_full_feed_peers(ff_peers)

    # ------------------------------------------------------------------- setup

    def set_full_feed_peers(self, ff_peers: Iterable[VP]) -> None:
        """Adopt the full-feed selection, as ``(collector, peer_asn, peer_ip)``.

        Called by :class:`~quickrib.rib_table.RIBTable` once ``QuickRIB`` has
        made its selection, which is before any element is replayed.
        """
        self.ff_peers = set(ff_peers)
        self._quorum = self.visible_ratio * len(self.ff_peers)
        self._have_peers = True
        logger.info(
            "IODA: %d full-feed peers, an IPv%d prefix is visible above %.1f of them",
            len(self.ff_peers),
            self.ip_version,
            self._quorum,
        )

    def set_rib(self, rib: RIBTable) -> None:  # pragma: no cover - nothing needed
        pass

    def full_feed_drift(self, rib: RIBTable) -> set[VP]:
        """Vantage points whose full-feed status in ``rib`` differs from the pinned set.

        Empty in a well-behaved run. A non-empty result means the window is long
        enough that a per-bin re-evaluation would have picked a different peer
        population than the one pinned before the build.
        """
        current = full_feed_peers(rib.data, ratio=self.full_feed_ratio)
        return current ^ self.ff_peers

    # ---------------------------------------------------------------- hot path

    def _attach(self, origin: str, prefix: str) -> None:
        """Put a now-visible ``prefix`` into ``origin``'s tree and defer the recount."""
        tree = self._trees.get(origin)
        if tree is None:
            tree = self._trees[origin] = radix.Radix()
        tree.add(prefix)
        self._dirty.add(origin)

    def _detach(self, origin: str, prefix: str) -> None:
        """The inverse of :meth:`_attach`; a prefix absent from the tree is not an error."""
        tree = self._trees.get(origin)
        if tree is None:
            return
        try:
            tree.delete(prefix)
        except KeyError:
            return
        self._dirty.add(origin)

    def _set_visible(self, prefix: str, visible: bool) -> None:
        """Move ``prefix`` in or out of every origin AS's tree when it crosses the quorum.

        Most updates leave visibility where it was, since one peer gaining or
        losing a route rarely moves a prefix across half the fleet. The common
        case is therefore the early return, with no tree touched at all.
        """
        if visible == (prefix in self._visible):
            return
        if visible:
            self._visible.add(prefix)
            for origin in self._pfx_origins.get(prefix, ()):
                self._attach(origin, prefix)
        else:
            self._visible.discard(prefix)
            for origin in self._pfx_origins.get(prefix, ()):
                self._detach(origin, prefix)

    def _add_origin(self, prefix: str, origin: str) -> None:
        """Reference-count ``origin`` for ``prefix`` across the full-feed peers.

        The tree only changes on the 0 -> 1 transition, and only while the prefix
        is visible: an origin that some other peer still reports is already filed,
        and an invisible prefix is in no tree to begin with.
        """
        origins = self._pfx_origins.get(prefix)
        if origins is None:
            self._pfx_origins[prefix] = {origin: 1}
            if prefix in self._visible:
                self._attach(origin, prefix)
            return
        count = origins.get(origin, 0)
        origins[origin] = count + 1
        if count == 0 and prefix in self._visible:
            self._attach(origin, prefix)

    def _remove_origin(self, prefix: str, origin: str) -> None:
        """Drop one peer's vote for ``origin``; the last one out clears the tree."""
        origins = self._pfx_origins.get(prefix)
        if origins is None:
            return
        count = origins.get(origin, 0) - 1
        if count > 0:
            origins[origin] = count
            return
        origins.pop(origin, None)
        if not origins:
            self._pfx_origins.pop(prefix, None)
        if prefix in self._visible:
            self._detach(origin, prefix)


    def _tracked(self, bgpelem: AnyElement, prefix: str) -> bool:
        """Whether this element is an IPv4/IPv6 route from a full-feed peer."""
        if (":" in prefix) != self._want_v6:
            return False
        return (
            bgpelem.collector,
            bgpelem.peer_asn,
            bgpelem.peer_address,
        ) in self.ff_peers

    def update_rib(self, bgpelem: ParsedElement) -> None:
        # Counting only: visibility and the trees are derived in one go by
        # _build_trees once the whole dump has been read (see `dump`).
        fields = bgpelem.fields
        prefix = fields["prefix"]
        if not self._tracked(bgpelem, prefix):
            return
        self._pfx_peers[prefix] = self._pfx_peers.get(prefix, 0) + 1
        self._add_origin(prefix, fields["as-path"][-1])

    def update_announcement(
        self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData] = None
    ) -> None:
        fields = bgpelem.fields
        prefix = fields["prefix"]
        if not self._tracked(bgpelem, prefix):
            return

        origin = fields["as-path"][-1]
        if old_data is None:
            # The peer did not have this prefix, so it is one more peer seeing
            # it. This is the only case where the visibility count can rise.
            seen_by = self._pfx_peers.get(prefix, 0) + 1
            self._pfx_peers[prefix] = seen_by
            self._add_origin(prefix, origin)
            if self._built:
                self._set_visible(prefix, seen_by > self._quorum)
        else:
            # A re-announcement: the peer already counted, so only the origin can
            # have moved. A path or community change that keeps the origin is a
            # no-op here, and that is the overwhelming majority of updates.
            old_origin = old_data["as-path"][-1]
            if old_origin != origin:
                self._add_origin(prefix, origin)
                self._remove_origin(prefix, old_origin)

    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData] = None) -> None:
        # `data` is None when the peer did not have the prefix: nothing counted,
        # so nothing to take back. Otherwise it carries the path being withdrawn,
        # which is the only place the origin to decrement can come from.
        if data is None:
            return
        prefix = bgpelem.fields["prefix"]
        if not self._tracked(bgpelem, prefix):
            return

        seen_by = self._pfx_peers.get(prefix, 0) - 1
        if seen_by > 0:
            self._pfx_peers[prefix] = seen_by
        else:
            # Nobody sees it any more: drop it rather than keep a zero (or a
            # negative, if a withdrawal ever outruns its announcement) for the
            # rest of the run.
            self._pfx_peers.pop(prefix, None)
            seen_by = 0
        if self._built:
            self._set_visible(prefix, seen_by > self._quorum)
        self._remove_origin(prefix, data["as-path"][-1])

    # --------------------------------------------------------------- reporting

    def _build_trees(self) -> None:
        """Turn the counts gathered during the build into the visible set and trees.

        Deferred to the first dump because that is the first moment the counts
        are complete: until the RIB dump has been read in full, a prefix's peer
        count is still climbing and testing it against the quorum would be
        meaningless.
        """
        if not self._have_peers:
            raise RuntimeError(
                "IODAObserver does not know the full-feed peers (pass ff_peers=, "
                "call set_full_feed_peers, or run it through the QuickRIB pipeline)"
            )
        for prefix, seen_by in self._pfx_peers.items():
            if seen_by > self._quorum:
                self._visible.add(prefix)
                for origin in self._pfx_origins[prefix]:
                    self._attach(origin, prefix)
        self._built = True

    def dump(self, ts: datetime.datetime) -> dict[str, int]:
        if not self._built:
            self._build_trees()

        # Only the ASes whose tree moved since the last dump are recounted; the
        # rest keep the total they already had. An AS also keeps its slot once
        # seen, so a total falling to zero is reported as a zero rather than
        # vanishing. A zero is what a complete blackout looks like in the series.
        for origin in self._dirty:
            self._totals[origin] = radix_block_count(self._trees[origin])
        self._dirty.clear()

        if self.entities is None:
            totals = dict(self._totals)
        else:
            totals = {asn: self._totals.get(asn, 0) for asn in self.entities}
        self.series[_bin_of(ts, self.step)] = totals
        return totals

    def compare(self, other) -> None:  # pragma: no cover
        pass


# ------------------------------------------------------------ outage detection


class AlertRecord(TypedDict):
    """One bin scored against its baseline, shaped like ``/v2/outages/alerts``."""

    time: int
    value: int
    historyValue: float
    level: Literal["critical", "normal"]
    condition: str
    method: Literal["median"]


class OutageEvent(TypedDict):
    """One outage, shaped like ``/v2/outages/events``."""

    location: str
    start: int
    duration: int
    uncertainty: None
    method: Literal["median"]
    datasource: str
    status: int
    fraction: None
    score: float
    location_name: str
    overlaps_window: bool


def evaluate_series(
    series: Mapping[int, int],
    *,
    step: int = IODA_STEP,
    history: int = ALERT_HISTORY,
    threshold: float = ALERT_THRESHOLD,
) -> list[AlertRecord]:
    """Score one entity's time series bin by bin against its own recent past.

    Returns one record per bin that has a **full** ``history`` behind it, with
    the ``value``, the ``historyValue`` (the median baseline) and the resulting
    ``level``. Bins without enough history are skipped, exactly as IODA cannot
    alert before it has a baseline.

    A bin that is itself alerting does **not** feed the baseline: otherwise a
    long outage erodes its own median and silently declares itself normal while
    still down. Matching the API's ``historyValue`` on real series is what
    settles this; ``tests/test_ioda.py`` pins the agreement.
    """
    window = history // step
    if window <= 0:
        raise ValueError(
            f"history ({history}s) must be at least one step ({step}s) long; "
            "a zero-length baseline would score every bin against the whole series"
        )
    baseline_values: list[int] = []
    records: list[AlertRecord] = []
    # Carried across bins: while the level is critical the value is withheld from
    # the baseline, so an outage cannot lower the bar it is being judged against.
    level = "normal"
    for ts in sorted(series):
        value = series[ts]
        if len(baseline_values) >= window:
            baseline = median(baseline_values[-window:])
            alerting = baseline > 0 and value < threshold * baseline
            level = "critical" if alerting else "normal"
            records.append(
                {
                    "time": ts,
                    "value": value,
                    "historyValue": baseline,
                    "level": level,
                    "condition": f"< {threshold:g}" if alerting else "normal",
                    "method": "median",
                }
            )
        if level == "normal":
            baseline_values.append(value)
            # Trim in batches rather than per bin: only the last `window` values
            # are ever read, and popping from the front of a list is O(n).
            if len(baseline_values) > 2 * window:
                del baseline_values[:-window]
    return records


def detect_alerts(series: Mapping[int, int], **kwargs) -> list[AlertRecord]:
    """IODA-shaped alerts for one entity: the bins where its level *changes*.

    Mirrors ``/v2/outages/alerts``, which reports transitions rather than every
    bin. The first evaluated bin is reported only if it is already alerting.
    """
    alerts: list[AlertRecord] = []
    level = "normal"
    for record in evaluate_series(series, **kwargs):
        if record["level"] != level:
            level = record["level"]
            alerts.append(record)
    return alerts


def detect_events(
    series: Mapping[int, int],
    *,
    location: str = "",
    location_name: str = "",
    datasource: str = "bgp",
    step: int = IODA_STEP,
    **kwargs,
) -> list[OutageEvent]:
    """IODA-shaped outage events for one entity, as ``/v2/outages/events`` returns them.

    An event spans from a bin turning ``critical`` to the bin that turns
    ``normal`` again; one still alerting at the end of the series is closed on
    its last bin. ``score`` follows IODA's formula, ``500 * sum(1 - value /
    historyValue)`` over the event's bins.
    """
    records = evaluate_series(series, step=step, **kwargs)
    events: list[OutageEvent] = []
    start: Optional[int] = None  # None while no event is open
    drop = 0.0  # running sum of (1 - value / historyValue) over the open event

    def close(start: int, drop: float, end: int) -> None:
        events.append(
            {
                "location": location,
                "start": start,
                "duration": end - start,
                "uncertainty": None,
                "method": "median",
                "datasource": datasource,
                "status": 0,
                "fraction": None,
                "score": SCORE_SCALE * drop,
                "location_name": location_name,
                "overlaps_window": False,
            }
        )

    for record in records:
        if record["level"] == "critical":
            if start is None:
                start, drop = record["time"], 0.0
            drop += 1 - record["value"] / record["historyValue"]
        elif start is not None:
            close(start, drop, record["time"])
            start = None
    # An event still alerting when the series runs out is closed on the bin after
    # the last one, so its duration covers every bin it actually spans.
    if start is not None and records:
        close(start, drop, records[-1]["time"] + step)
    return events
