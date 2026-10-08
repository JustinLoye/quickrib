# This module is the seam between `pybgpflux` and QuickRIB. `ElementFields` marks
# every key optional because it is shared with record kinds pybgpflux never
# yields: its element type is Literal["R", "A", "W"] and its bgpdump parser drops
# BGP state changes, so a prefix is always there. Below the seam the narrowed
# types in quickrib.elements apply and this is enforced again.
# pyright: reportTypedDictNotRequiredAccess=false

import datetime
import logging
import os
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal, Optional, Self

from pybgpflux import BGPElement, BGPStream, BGPStreamConfig, FilterOptions
from pybgpflux.brokers.bgpbroker import BGPBroker, BrokerQueryError
from pybgpflux.brokers.bgpkit import BGPKITBroker
from pydantic import BaseModel, Field, field_validator, model_validator

from quickrib.rib_table import RIBTable

ParserName = Literal["pybgpkit", "bgpkit", "pybgpstream", "bgpdump"]

logger = logging.getLogger(__name__)


# The broker selects archive files with a half-open interval [ts_start, ts_end),
# so a file whose timestamp equals ts_end is excluded. RIB dump filenames are
# minute-aligned (see bgpflux `timestamp_from_project_url`, "%Y%m%d.%H%M"), which
# means `until_time` lands exactly on a dump boundary. We push ts_end one second
# past it so the boundary RIB file is included. Any value in (0, 120s) is safe
# because consecutive RIB dumps are at least two hours apart.
RIB_BOUNDARY_MARGIN = datetime.timedelta(seconds=1)

RIB_LOOKBACK = datetime.timedelta(hours=24)
"""How far back to look for a collector's most recent RIB dump.

Route Views dumps every 2 hours and RIPE RIS every 8, so a day covers several
of each. A collector with nothing in that span has a real gap in its archive.
"""

RIB_DUMP_MARGIN = datetime.timedelta(minutes=2)
"""Half-width of the window streamed around a dump whose time the broker gave.

Every entry of an MRT table dump carries the dump file's own timestamp, so this
only has to absorb the broker's rounding, not a spread of entries.
"""

FULL_FEED_RATIO = 0.8
"""A peer is full-feed when it carries more than this share of its collector's
unique prefixes. Relative, unlike IODA's absolute rule; see
`quickrib.observers.ioda`, which is defined over exactly this selection."""


class QuickRIBConfig(BaseModel):
    """QuickRIB analysis config"""

    start_time: datetime.datetime = Field(description="Start of the analysis")
    end_time: datetime.datetime = Field(description="End of the analysis")
    dump_res: datetime.timedelta = Field(
        description="Time between to dump of analysis modules"
    )
    collectors: list[str] = Field(description="List of collectors to get data from")
    filters: FilterOptions | None = Field(default=None, description="Optional filters")
    cache_dir: Path | None = Field(
        default=None,
        description="Directory for the downloaded archives, created if missing. Reuse it across runs.",
    )
    parser: ParserName | None = Field(
        default="pybgpkit",
        description="MRT files parser. Default `pybgpkit` is installed but slow, the others are system dependencies.",
    )

    fullfeed_only: bool = Field(default=True, description="Only use data from peer sharing their full routing table")
    rib_lookback: datetime.timedelta = Field(
        default=RIB_LOOKBACK,
        description=(
            "How far before `start_time` to look for each collector's most "
            "recent RIB dump. Widen it only for a collector with a gap in its "
            "archive."
        ),
    )
    rib_cls: type[RIBTable] = Field(
        default=RIBTable,
        description="Class used to store the RIB. A subclass such as `RIBTablePathHistory` enriches node data; see AGENTS.md 'RIB flavors'.",
    )

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def normalize_to_utc(cls, dt: datetime.datetime) -> datetime.datetime:
        return to_utc(dt)

    @model_validator(mode="after")
    def check_window(self) -> "QuickRIBConfig":
        if self.end_time <= self.start_time:
            raise ValueError(
                f"end_time ({self.end_time.isoformat()}) must be after "
                f"start_time ({self.start_time.isoformat()})"
            )
        if self.dump_res.total_seconds() <= 0:
            raise ValueError(f"dump_res must be positive, got {self.dump_res}")
        return self



class QuickRIB:
    def __init__(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        dump_res: datetime.timedelta,
        collectors: list[str],
        filters: Optional[FilterOptions] = None,
        cache_dir: Optional[str | os.PathLike[str]] = None,
        parser: Optional[ParserName] = "pybgpkit",
        fullfeed_only: bool = True,
        rib_cls: type[RIBTable] = RIBTable,
        rib_lookback: datetime.timedelta = RIB_LOOKBACK,
    ) -> None:
        # The dump schedule advances in `dump_res` steps until it reaches the
        # stream's clock, so a non-positive step would not merely disable
        # dumping, it would not terminate.
        if dump_res.total_seconds() <= 0:
            raise ValueError(f"dump_res must be positive, got {dump_res}")

        # The same normalisation `QuickRIBConfig` applies, so constructing a
        # pipeline directly behaves the same. Without it a naive datetime is
        # read in local time by `timestamp()`, which shifts every dump boundary,
        # and cannot be compared with the broker's aware dump times at all.
        self.start_time = to_utc(start_time)
        self.end_time = to_utc(end_time)
        if self.end_time <= self.start_time:
            raise ValueError(
                f"end_time ({self.end_time.isoformat()}) must be after "
                f"start_time ({self.start_time.isoformat()})"
            )
        self.dump_res = dump_res.total_seconds()
        self.filters = filters
        # Created here rather than left to the first download, so a typo in the
        # path fails before the broker is queried.
        if cache_dir is not None:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
        self.cache_dir = str(cache_dir) if cache_dir is not None else None
        self.parser = parser
        self.fullfeed_only = fullfeed_only
        self.rib_lookback = rib_lookback
        # Copied: collectors with no RIB dump in the window are dropped from
        # this list, and that must not reach back into the caller's.
        self.collectors = list(collectors)

        self.rib = rib_cls()

    @classmethod
    def from_config(cls, config: QuickRIBConfig) -> Self:
        return cls(
            start_time=config.start_time,
            end_time=config.end_time,
            dump_res=config.dump_res,
            collectors=config.collectors,
            filters=config.filters,
            cache_dir=config.cache_dir,
            parser=config.parser,
            fullfeed_only=config.fullfeed_only,
            rib_cls=config.rib_cls,
            rib_lookback=config.rib_lookback,
        )

    def initialize_processing(self) -> None:
        """
        Initialize route collector RIB processing.

        This function initializes the processing of Routing Information Base (RIB) data every route collectors.
        It returns the start and end times of each collectors's RIB and identifies the full-feed peers.

        Sets ``rc_to_rib_start``, ``rc_to_rib_end`` and ``rc_to_ff_peers`` on the
        instance, and hands the full-feed selection to the observers.
        """
        # Where each collector's table actually is. `start_time` does not have
        # to sit on a dump: the reconstruction starts from the last dump before
        # it, and `replay_updates` replays the interval in between.
        rib_dumps = latest_rib_dumps(self.collectors, self.start_time, self.rib_lookback)

        missing = [rc for rc in self.collectors if rc not in rib_dumps]
        for rc in missing:
            logger.warning(
                "No RIB dump for collector %s in the %s before %s, dropping it",
                rc, self.rib_lookback, self.start_time.isoformat(),
            )
            self.collectors.remove(rc)
        if not self.collectors:
            raise RuntimeError(
                f"No collector has a RIB dump in the {self.rib_lookback} before "
                f"{self.start_time.isoformat()}, so there is no table to build. "
                "Check the collector names, or widen `rib_lookback`."
            )

        for rc, dump_time in sorted(rib_dumps.items()):
            catch_up = self.start_time - dump_time
            logger.info(
                "%s dumps at %s, %s before start_time",
                rc, dump_time.isoformat(), catch_up,
            )

        # Stream #1: count unique prefixes and identify FF peers, one collector
        # at a time. Collectors dump at different instants, and a single window
        # spanning all of them would pull in every intermediate dump of the
        # collectors that dump more often.
        rc_to_rib_times = {rc: set() for rc in self.collectors}
        rc_to_peer_to_prefixes = {rc: defaultdict(set) for rc in self.collectors}
        rc_to_unique_prefixes = {rc: set() for rc in self.collectors}
        for rc in self.collectors:
            stream = BGPStream(
                ts_start=rib_dumps[rc] - RIB_DUMP_MARGIN,
                ts_end=rib_dumps[rc] + RIB_DUMP_MARGIN,
                data_types=["ribs"],
                collectors=[rc],
                filters=self.filters,
                cache_dir=self.cache_dir,
                parser_name=self.parser,
            )
            for elem in stream:
                ts, pfx = elem.time, elem.fields["prefix"]
                rc_to_rib_times[rc].add(ts)
                rc_to_unique_prefixes[rc].add(pfx)
                rc_to_peer_to_prefixes[rc][(elem.peer_asn, elem.peer_address)].add(pfx)

        logger.info(
            "Number of unique prefixes: %s",
            {rc: len(rc_to_unique_prefixes[rc]) for rc in self.collectors},
        )

        # Set ff peers
        rc_to_ff_peers = {rc: set() for rc in self.collectors}
        for rc, peer_to_prefixes in rc_to_peer_to_prefixes.items():
            for peer, prefixes in peer_to_prefixes.items():
                full_feed = len(prefixes) > FULL_FEED_RATIO * len(rc_to_unique_prefixes[rc])
                if full_feed:
                    rc_to_ff_peers[rc].add(peer)
                logger.info(
                    "Peer %s of collector %s is %sfull-feed with %d prefixes",
                    peer, rc, "" if full_feed else "NOT ", len(prefixes),
                )

        # Hand the selection to observers defined over it. IODA's visibility
        # quorum is a share of exactly these peers. This happens
        # before any element is replayed, so such an observer can accumulate
        # from the RIB build itself instead of reading the table back.
        self.rib.notify_full_feed_peers(
            {
                (rc, peer_asn, peer_ip)
                for rc, peers in rc_to_ff_peers.items()
                for peer_asn, peer_ip in peers
            }
        )

        rc_to_rib_start: dict[str, float] = {}
        rc_to_rib_end: dict[str, float] = {}
        # Iterate a copy: a collector whose dump turned out to be empty is
        # dropped from the list.
        for rc in list(self.collectors):
            try:
                rc_to_rib_start[rc] = min(rc_to_rib_times[rc])
                rc_to_rib_end[rc] = max(rc_to_rib_times[rc])
            except ValueError:
                logger.warning(
                    "The RIB dump the broker lists for %s at %s yielded no "
                    "entries, dropping the collector",
                    rc, rib_dumps[rc].isoformat(),
                )
                rc_to_rib_times.pop(rc)
                self.collectors.remove(rc)
                rc_to_ff_peers.pop(rc)

        if not self.collectors:
            raise RuntimeError(
                f"Every collector's RIB dump before {self.start_time.isoformat()} "
                "was empty, so there is no table to build."
            )

        logger.info(
            "RIBs start at %s",
            {rc: timestamp_to_iso(rc_to_rib_start[rc]) for rc in self.collectors},
        )
        logger.info(
            "RIBs end at %s",
            {rc: timestamp_to_iso(rc_to_rib_end[rc]) for rc in self.collectors},
        )

        self.rc_to_rib_start = rc_to_rib_start
        self.rc_to_rib_end = rc_to_rib_end
        self.rc_to_ff_peers = rc_to_ff_peers
        return

    def build_rib(self):
        start = time.time()
        n_elem = 0

        # Stream 2: one stream per collector, over that collector's dump instant
        # alone, in dump order. A single stream spanning every collector's dump
        # would also fetch and parse the update files between the earliest and
        # the latest dump, and any intermediate dump of a collector that dumps
        # more often, only to discard all of it below: a RIS collector dumping
        # at 00:00 next to a Route Views one dumping at 02:00 cost two hours of
        # everyone's updates and a whole extra table.
        for rc in sorted(self.collectors, key=self.rc_to_rib_start.__getitem__):
            from_time, until_time = self.rc_to_rib_start[rc], self.rc_to_rib_end[rc]
            logger.info("Building %s from its dump at %s", rc, timestamp_to_iso(from_time))
            stream = BGPStream(
                ts_start=datetime.datetime.fromtimestamp(from_time, tz=datetime.UTC),
                ts_end=datetime.datetime.fromtimestamp(until_time, tz=datetime.UTC)
                + RIB_BOUNDARY_MARGIN,
                data_types=["ribs", "updates"],
                collectors=[rc],
                filters=self.filters,
                cache_dir=self.cache_dir,
                parser_name=self.parser,
            )
            n_elem += self._apply_dump(stream, rc)

        # The table is complete: observers that need to read it back get their
        # reference now, before the first dump.
        self.rib.notify_built()
        logger.info(
            "Built RIB from %d BGP elements in %.2f seconds", n_elem, time.time() - start
        )
        return

    def _apply_dump(self, stream: Iterable[BGPElement], rc: str) -> int:
        """Load collector ``rc``'s dump instant from ``stream`` into the table.

        Returns the number of elements the stream yielded, kept or not.
        """
        n_elem = 0
        for elem in stream:
            n_elem += 1
            ts = elem.time

            # A collector's RIB dump is a single instant. Every entry in an MRT
            # table dump carries the dump's own timestamp, so `rib_start` and
            # `rib_end` are the same second (verified on every pinned window: one
            # distinct timestamp across 18M entries at route-views.eqix). Keep
            # that instant and nothing else; `replay_updates` picks up after it.
            if not self.rc_to_rib_start[rc] <= ts <= self.rc_to_rib_end[rc]:
                continue

            peer = (elem.peer_asn, elem.peer_address)

            # Optional peer filter
            if self.fullfeed_only and peer not in self.rc_to_ff_peers[rc]:
                continue

            # The stream types `as-path` as the wire string and every field as
            # optional; `process_path` has just rewritten it to a list of ASNs, and
            # only R/A/W elements exist. ParsedElement / WithdrawalElement say so.
            # Reinterpreting costs nothing at runtime; see quickrib/elements.py.
            # Both checkers see the seam here because `stream` is typed, where
            # `BGPStream.__iter__` upstream is not, so both are told.
            # Handle withdraw messages
            if elem.type == "W":
                # A withdrawal for a prefix the peer does not have is expected
                # while the RIB is still being built, and `update_withdrawal`
                # handles it. Anything else is a bug and must not be swallowed.
                self.rib.update_withdrawal(elem)  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]
                continue

            as_path = process_path(elem)
            if not as_path:
                continue
            elem.fields["as-path"] = as_path  # pyright: ignore[reportGeneralTypeIssues]  # ty: ignore[invalid-assignment]

            # Handle RIB message
            if elem.type == "R":
                self.rib.update_rib(elem)  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]
                continue

            # Handle announcement message
            if elem.type == "A":
                self.rib.update_announcement(elem)  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]
                continue
        return n_elem

    def replay_updates(self):
        start = time.time()

        start_ts = min(self.rc_to_rib_end.values())

        # Stream 3: Update the RIB
        stream = BGPStream(
            ts_start=datetime.datetime.fromtimestamp(start_ts, tz=datetime.UTC),
            ts_end=self.end_time,
            data_types=["updates"],
            collectors=self.collectors,
            filters=self.filters,
            cache_dir=self.cache_dir,
            parser_name=self.parser
        )
        
        n_elem = 0
        next_dump_time = self.start_time.timestamp() + self.dump_res
        for elem in stream:
            n_elem += 1

            rc = elem.collector
            ts = elem.time

            # Every boundary this element proves has passed, not just the first:
            # a gap in the stream wider than `dump_res` must not cost the windows
            # it spans, nor leave the schedule behind for the rest of the run.
            if ts > next_dump_time:
                for boundary in elapsed_dump_boundaries(next_dump_time, ts, self.dump_res):
                    self.rib.dump(datetime.datetime.fromtimestamp(boundary, tz=datetime.UTC))
                    next_dump_time = boundary + self.dump_res

            # Everything up to and including this collector's dump instant is
            # already in the table: `build_rib` applied the dump itself and any
            # update sharing its timestamp. The stream starts at the *earliest*
            # collector's dump, so without this a collector that dumps later has
            # its updates replayed a second time, and the ones predating its own
            # dump applied on top of a newer table. Measured on the pinned
            # 2010 window, that was 895 of route-views.wide's 31,956 updates
            # (2.8%) and 114 of rrc04's 70,044, purely because the two collectors
            # dump a minute apart.
            if ts <= self.rc_to_rib_end[rc]:
                continue

            peer = (elem.peer_asn, elem.peer_address)

            # Optional peer filter
            if self.fullfeed_only and peer not in self.rc_to_ff_peers[rc]:
                continue

            # The stream types `as-path` as the wire string and every field as
            # optional; `process_path` has just rewritten it to a list of ASNs, and
            # only R/A/W elements exist. ParsedElement / WithdrawalElement say so.
            # Reinterpreting costs nothing at runtime; see quickrib/elements.py.
            # Handle withdraw messages
            if elem.type == "W":
                self.rib.update_withdrawal(elem)  # pyright: ignore[reportArgumentType]
                continue

            as_path = process_path(elem)
            if not as_path:
                continue
            elem.fields["as-path"] = as_path  # pyright: ignore[reportGeneralTypeIssues]

            # Handle announcement message
            if elem.type == "A":
                self.rib.update_announcement(elem)  # pyright: ignore[reportArgumentType]

        # Boundaries the stream ran out before reaching. Their windows are over,
        # since `end_time` has passed them; they simply have no message left to
        # prove it. A series with a hole at the end is worse than one whose last
        # bins report a quiet network.
        for boundary in elapsed_dump_boundaries(
            next_dump_time, self.end_time.timestamp(), self.dump_res, inclusive=True
        ):
            self.rib.dump(datetime.datetime.fromtimestamp(boundary, tz=datetime.UTC))

        logger.info(
            "Updated RIB from %d BGP elements in %.2f seconds", n_elem, time.time() - start
        )
        return


    def run(self):
        logger.info("Step 1: initialization")
        self.initialize_processing()
        logger.info("Step 2: build rib")
        self.build_rib()
        logger.info("Step 3: replay updates")
        self.replay_updates()


def latest_rib_dumps(
    collectors: list[str],
    start_time: datetime.datetime,
    lookback: datetime.timedelta = RIB_LOOKBACK,
    broker: Optional[BGPBroker] = None,
) -> dict[str, datetime.datetime]:
    """The most recent RIB dump at or before ``start_time``, for each collector.

    The table at an instant is the last dump before it plus the updates since,
    so this is what the reconstruction has to start from. A collector with no
    dump in ``lookback`` is absent from the result.

    A dump landing exactly on ``start_time`` counts as being before it, which is
    the common case when a window is chosen to start on one. The broker selects
    files over a half-open interval, so the query reaches one second past
    ``start_time`` to include it; dumps are minute-aligned, so that cannot pull
    in a later one.

    The dump times come from the broker rather than from an assumed cadence.
    Cadence varies by project and has changed over time, and file timestamps are
    not where a rule of thumb puts them: in 2010 ``rrc04`` dumped at 15:59 and
    23:59, not on the 8-hour marks. Asking is both simpler and correct.
    """
    if broker is None:
        # The same broker `BGPStream` defaults to, so discovery and streaming
        # agree on which files exist.
        broker = BGPKITBroker()

    query = BGPStreamConfig(
        start_time=start_time - lookback,
        end_time=start_time + RIB_BOUNDARY_MARGIN,
        collectors=collectors,
        data_types=["ribs"],
    )
    try:
        items = broker.query(query)
    except BrokerQueryError:
        return {}

    latest: dict[str, datetime.datetime] = {}
    for item in items:
        if item.data_type != "ribs":
            continue
        dump_time = datetime.datetime.fromisoformat(item.ts_start)
        if dump_time.tzinfo is None:
            dump_time = dump_time.replace(tzinfo=datetime.UTC)
        if dump_time > start_time:
            continue
        current = latest.get(item.collector_id)
        if current is None or dump_time > current:
            latest[item.collector_id] = dump_time
    return latest


def elapsed_dump_boundaries(
    next_dump_time: float, until: float, dump_res: float, *, inclusive: bool = False
) -> Iterator[float]:
    """Yield every dump boundary from ``next_dump_time`` that ``until`` has passed.

    The schedule is absolute (``start_time + k * dump_res``), so a window is due
    once time has moved past its boundary, however many messages happened to
    arrive in it. Yielding *all* of them is what keeps a gap in the stream from
    costing the windows it spans: advancing the boundary only once per arriving
    element leaves the schedule permanently behind the stream, and dumping stops
    for the rest of the run.

    ``until`` is exclusive while the stream is running, because an element at
    exactly the boundary belongs to the window that boundary opens. It is
    inclusive for the final flush, where ``until`` is ``end_time`` and a boundary
    landing on it closes a window that is genuinely over.
    """
    while (next_dump_time <= until) if inclusive else (next_dump_time < until):
        yield next_dump_time
        next_dump_time += dump_res


def process_path(elem: BGPElement) -> list[str]:
    """The element's AS path as a list of ASNs, or ``[]`` to drop the element.

    Credit to CAIDA https://bgpstream.caida.org/docs/tutorials/pybgpstream

    An empty result is the caller's signal to skip the element entirely. Three
    things earn it:

    * **no path at all**: nothing to reconstruct a route from;
    * **an AS set** (``{64500,64501}``): an aggregate stands for several origins
      at once, and every consumer here assumes ``path[-1]`` is *the* origin, so
      these are dropped rather than guessed at;
    * **a path that does not start at the peer**, which is malformed: a route
      learned from a peer begins with that peer's ASN.

    A **single-ASN path** is none of those: it is well-formed, and means the peer
    originates the prefix itself. Those are kept. They are rare (0.02% of RIB
    entries on the pinned windows: 604 prefixes across 17 peers in 2010, 3,487
    across 28 peers in 2022) but they are real routes, and dropping them made a
    full-feed peer's own address space invisible to every observer downstream,
    which matters most to per-origin signals like IODA's. Such a path has no AS
    *link* in it, so anything reading consecutive pairs simply sees none; the
    origin (``path[-1]``) is the peer itself.
    """
    raw_path = elem.fields.get("as-path")
    if raw_path is None:
        return []
    # An AS set is written `{64500,64501}` on the wire, so one scan of the
    # string settles it before anything is split or allocated.
    if "{" in raw_path:
        return []
    as_path: list[str] = raw_path.split(" ")

    # Sanitize weird BGP data: a route learned from a peer begins with that
    # peer's ASN.
    if as_path[0] == str(elem.peer_asn):
        return as_path

    return []

def to_utc(dt: datetime.datetime) -> datetime.datetime:
    """``dt`` as an aware UTC datetime; a naive one is taken to already be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def timestamp_to_iso(timestamp: float) -> str:
    """A stream timestamp as an ISO string, **in UTC**.

    Without an explicit zone this renders in the machine's local time, which put
    a `2025-03-03T09:00:00` in the log of a window that starts at midnight UTC.
    """
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.UTC).isoformat()