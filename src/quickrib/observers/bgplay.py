"""BGPlay-compatible observer.

Reproduces the payload of the RIPEstat ``bgplay`` data call
(https://stat.ripe.net/docs/data-api/api-endpoints/bgplay), the feed behind
https://bgplay.massimocandela.com: for one *resource* (a prefix, an IP address
or an origin AS) it records the routing state at ``start_time`` and every BGP
update seen afterwards, so a client can animate how the paths towards that
resource evolve.

The observer emits the same ``data`` object the API does::

    {
      "resource": "1205",
      "query_starttime": "2025-03-03T00:00:00",
      "query_endtime":   "2025-03-03T02:00:00",
      "initial_state": [{"target_prefix", "source_id", "path", "community"}, ...],
      "events":        [{"seq", "timestamp", "type", "attrs"}, ...],
      "nodes":         [{"as_number", "owner"}, ...],
      "sources":       [{"id", "as_number", "ip", "rrc"}, ...],
      "targets":       [{"prefix"}, ...]
    }

with the same invariants the live API holds (verified against it):

* ``initial_state`` is sorted by ``(source_id, target_prefix)``, the prefix
  ordered numerically with IPv4 before IPv6, and holds one entry per
  (peer, prefix) pair;
* ``events`` is sorted by timestamp, ``attrs`` carries ``path``/``community``
  for ``A`` and only ``source_id``/``target_prefix`` for ``W``;
* ``nodes`` is every ASN appearing in an ``initial_state`` or announcement path,
  sorted numerically;
* ``sources`` is every peer appearing in ``initial_state`` or ``events``, sorted
  by ``id``;
* ``targets`` is every prefix actually observed for the resource, in the same
  numeric order;
* AS-path prepending is preserved (paths are *not* de-duplicated), and a
  repeated announcement is reported twice, exactly as the API reports it;
* ``community`` holds only *standard* communities. The API drops extended
  (``0:2:20965:0000014D``) and large (``13335:28000:16276``) ones, which the MRT
  parser does report;
* an AS resource matches on the **origin of each path**, not merely on the
  prefixes that AS originates, and carries no withdrawals, since a withdrawal
  has no path to attribute to an origin. Query the prefix instead to see its
  withdrawals.

Known deviations from the live API, all of which the e2e test accounts for:

* ``seq`` is a 0-based index into ``events`` rather than RIPE's opaque internal
  sequence number.
* Events sharing a timestamp come out in collector-archive order rather than in
  RIS's ingestion order, so a same-second run can be permuted relative to the
  API. The set of events is identical; only the order within one second is not.
* ``source_id`` is derived from the collector name: ``rrc04`` -> ``"04"``, so a
  RIS peer gets the exact API id. Non-RIS collectors (route-views, which the API
  does not carry at all) keep their full name, e.g.
  ``"route-views.wide-1.2.3.4"``.
* ``owner`` in ``nodes`` is empty unless ``resolve_owners=True``, which resolves
  names through the RIPEstat ``as-names`` data call. Those names are close to
  but not byte-identical with BGPlay's (BGPlay appends a country code).
* A bare IP resource expands to *every* covering prefix observed, whereas the
  API resolves it to a single prefix using its own view of RIS.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import logging
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Optional, cast

from quickrib.elements import (
    AnyElement,
    ParsedElement,
    ParsedFields,
    RIBNodeData,
    WithdrawalElement,
)
from quickrib.observers.observer import Observer

if TYPE_CHECKING:  # pragma: no cover
    from quickrib.rib_table import RIBTable

logger = logging.getLogger(__name__)

Payload = dict[str, Any]
"""One BGPlay ``data`` object, or one of the records inside it."""

EventRecord = tuple[float, str, str, str, list[str], list[str]]
"""An event as accumulated on the hot path, before it is rendered."""

BGPLAY_TIME_FMT = "%Y-%m-%dT%H:%M:%S"
"""Timestamp format used by the API when ``unix_timestamps`` is not set."""


def source_id(collector: str, peer_address: str) -> str:
    """Build the BGPlay ``source_id`` for a peer.

    RIS collectors are named ``rrcNN`` and the API keys peers by the bare,
    zero-padded collector number (``rrc04`` -> ``"04-192.65.185.119"``). Any
    other collector keeps its full name so ids stay unambiguous; route-views is
    the case in point, and the API does not carry it at all.
    """
    if collector.startswith("rrc"):
        return f"{collector[3:]}-{peer_address}"
    return f"{collector}-{peer_address}"


def _rrc_of(sid: str) -> str:
    """The ``rrc`` field of a ``sources`` entry: everything before the peer IP."""
    return sid.rsplit("-", 1)[0]


def prefix_sort_key(prefix: str) -> tuple[int, int, int]:
    """Numeric prefix ordering: IPv4 before IPv6, then network address, then length.

    The API orders prefixes this way, so ``8.18.50.0/24`` precedes
    ``103.143.32.0/23``. Plain string sorting gets that backwards.
    """
    network = ipaddress.ip_network(prefix)
    return (network.version, int(network.network_address), network.prefixlen)


def _covering_prefixes(ip: str) -> set[str]:
    """Every prefix that contains ``ip``, from /0 to the host route.

    Pre-expanding the IP into its (33 or 129) covering prefix strings keeps the
    hot path a single set lookup instead of a per-element containment test.
    """
    addr = ipaddress.ip_address(ip)
    return {
        str(ipaddress.ip_network(f"{ip}/{plen}", strict=False))
        for plen in range(addr.max_prefixlen + 1)
    }


def _parse_resource(resource: str | Iterable[str]) -> tuple[str, set[str], set[str]]:
    """Split a BGPlay ``resource`` into prefix and origin-AS filters.

    Accepts the same shapes as the API (a prefix, an IP address, ``ASxxx`` or a
    bare AS number, or a comma-separated or iterable list of those) and returns
    ``(normalized_resource, prefix_filter, origin_filter)``.
    """
    if isinstance(resource, str):
        items = [item.strip() for item in resource.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in resource if str(item).strip()]
    if not items:
        raise ValueError("resource must name at least one prefix, IP or AS")

    normalized: list[str] = []
    prefix_filter: set[str] = set()
    origin_filter: set[str] = set()

    for item in items:
        low = item.lower()
        if low.startswith("as"):
            asn = low[2:]
            if not asn.isdigit():
                raise ValueError(f"malformed AS resource {item!r}")
            origin_filter.add(asn)
            normalized.append(asn)
        elif item.isdigit():
            origin_filter.add(item)
            normalized.append(item)
        elif "/" in item:
            network = ipaddress.ip_network(item, strict=False)
            prefix_filter.add(str(network))
            normalized.append(str(network))
        else:
            # Bare IP: the API resolves it to one prefix using its own RIS view;
            # we keep every covering prefix and report the ones actually seen.
            prefix_filter |= _covering_prefixes(item)
            normalized.append(item)

    return ",".join(normalized), prefix_filter, origin_filter


def _standard_communities(communities: Iterable[str]) -> list[str]:
    """Keep only the standard ``asn:value`` communities, the way the API does.

    An MRT parser also hands back extended (``0:2:20965:0000014D``) and large
    (``13335:28000:16276``) communities; BGPlay reports neither.
    """
    out = []
    for community in communities:
        parts = community.split(":")
        if len(parts) != 2:
            continue
        try:
            if all(0 <= int(part) <= 0xFFFF for part in parts):
                out.append(community)
        except ValueError:
            continue
    return out


def _int_path(path: Iterable[str]) -> list[int | str]:
    """Render an AS path the way the API does: a list of integers.

    ``process_path`` already drops AS-set paths, so this only has to cope with
    hand-built elements; anything non-numeric is left as-is rather than raising.
    """
    out: list[int | str] = []
    for asn in path:
        try:
            out.append(int(asn))
        except (TypeError, ValueError):
            out.append(asn)
    return out


def _iso(ts: float | datetime.datetime) -> str:
    if isinstance(ts, datetime.datetime):
        return ts.astimezone(datetime.UTC).strftime(BGPLAY_TIME_FMT)
    return datetime.datetime.fromtimestamp(int(ts), datetime.UTC).strftime(
        BGPLAY_TIME_FMT
    )


class BGPlayObserver(Observer):
    """Collect the BGPlay view of one resource while the pipeline replays BGP.

    Parameters
    ----------
    resource
        Prefix, IP address, ``ASxxx`` / bare AS number, or a comma-separated
        list of those, following the API's ``resource`` grammar.
        A prefix matches *exactly* (querying ``8.0.0.0/9`` does not pull in
        its more specifics, matching the API); an AS matches every path whose
        **origin** it is, so a path towards one of its prefixes that originates
        elsewhere is excluded. Query that prefix to see the hijack.
    start_time
        The ``query_starttime``. It is also the boundary between the two halves
        of the output: an ``A``/``W`` at or before it folds into
        ``initial_state``, one after it becomes an ``event``. RIB-dump entries
        always fold into ``initial_state``, whatever their timestamp, since they
        describe the table rather than a change to it.
    end_time
        The ``query_endtime``, reported as-is. Updates past it are still
        recorded; bound the replay with ``QuickRIB``'s own ``end_time``.
    resolve_owners
        Fill ``nodes[].owner`` from the RIPEstat ``as-names`` data call when
        serialising. Off by default, since it is a network round-trip and the
        rest of the payload is computed entirely offline.

    Notes
    -----
    The hot path is one set lookup on the prefix plus, in AS mode, one on the
    path origin, and nothing is allocated before an element matches. All the
    formatting (ISO timestamps, integer paths, community filtering, sorting)
    happens once in :meth:`to_dict`.

    A withdrawal carries no AS path, so it can only be matched by the prefix
    filter. In pure AS mode the payload therefore has no ``W`` events at all,
    which is exactly what the API returns.
    """

    def __init__(
        self,
        resource: str | Iterable[str],
        start_time: datetime.datetime,
        end_time: Optional[datetime.datetime] = None,
        name: str = "bgplay",
        resolve_owners: bool = False,
    ) -> None:
        self.name = name
        self.resolve_owners = resolve_owners

        self.resource, self._prefix_filter, self._origin_filter = _parse_resource(
            resource
        )
        self.start_time = start_time
        self.end_time = end_time
        self._start_ts = start_time.timestamp()

        # Prefixes actually observed for the resource: the API's `targets`.
        self._targets: set[str] = set()
        # (source_id, prefix) -> (path, communities); the API's `initial_state`.
        self._initial: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
        # (time, type, source_id, prefix, path, communities); the API's `events`.
        self._events: list[tuple[float, str, str, str, list[str], list[str]]] = []
        # source_id -> (peer_asn, peer_ip), for the API's `sources`.
        self._sources: dict[str, tuple[int, str]] = {}

    # ------------------------------------------------------------------ hot path

    def _record(self, bgpelem: AnyElement, pfx: str) -> None:
        """Route a matched element into ``initial_state`` or ``events``."""
        sid = source_id(bgpelem.collector, bgpelem.peer_address)
        self._sources[sid] = (bgpelem.peer_asn, bgpelem.peer_address)
        etype = bgpelem.type

        # Discriminate on `type`, never on the Python class: `ParsedElement` and
        # `WithdrawalElement` are static narrowings of the *same*
        # `pybgpflux.BGPElement` the pipeline yields, so an isinstance check
        # against them is false for every real element. A withdrawal's `fields`
        # then carries only the prefix, since no parser puts an `as-path` on a
        # withdrawal, and reading the path off it raises.
        if bgpelem.type == "W":
            path: list[str] = []
            communities: list[str] = []
        else:
            fields = bgpelem.fields
            path = fields["as-path"]
            communities = fields["communities"]

        # A RIB-dump entry is the table, not a change to it, so it always lands
        # in initial_state even when its dump timestamp is past start_time.
        if etype == "R" or bgpelem.time <= self._start_ts:
            if etype == "W":
                self._initial.pop((sid, pfx), None)
            else:
                self._initial[(sid, pfx)] = (path, communities)
            return

        self._events.append((bgpelem.time, etype, sid, pfx, path, communities))

    def _match(self, fields: ParsedFields) -> Optional[str]:
        """Return the matched prefix, or ``None`` if the element is unrelated.

        A prefix resource matches on the prefix alone; an AS resource matches on
        the origin of this very path, so the same prefix can match one
        announcement and not the next.
        """
        pfx = fields["prefix"]
        if pfx in self._prefix_filter:
            return pfx
        if self._origin_filter:
            path = fields["as-path"]
            if path and path[-1] in self._origin_filter:
                return pfx
        return None

    def update_rib(self, bgpelem: ParsedElement) -> None:
        pfx = self._match(bgpelem.fields)
        if pfx is not None:
            self._targets.add(pfx)
            self._record(bgpelem, pfx)

    def update_announcement(
        self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData] = None
    ) -> None:
        pfx = self._match(bgpelem.fields)
        if pfx is not None:
            self._targets.add(pfx)
            self._record(bgpelem, pfx)

    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData] = None) -> None:
        # A withdrawal carries no path, so only the prefix filter can match it.
        pfx = bgpelem.fields["prefix"]
        if pfx in self._prefix_filter:
            self._targets.add(pfx)
            self._record(bgpelem, pfx)


    def set_rib(self, rib: RIBTable) -> None:  # pragma: no cover
        pass

    # ---------------------------------------------------------------- rendering

    def _sorted_events(self) -> list[EventRecord]:
        """Events in timestamp order.

        Nothing is de-duplicated. A repeated announcement is real and the API
        reports it twice, so the observer must too. This used to collapse exact
        repeats inside the window where QuickRIB's build and update streams
        overlapped. That overlap is gone: `update_rib` starts strictly after
        each collector's dump instant, so an element reaches an observer once.
        """
        return sorted(self._events, key=lambda event: event[0])

    def as_numbers(self, events: Optional[list[EventRecord]] = None) -> set[int | str]:
        """Every ASN on an ``initial_state`` or announcement path.

        ``events`` lets :meth:`to_dict` pass the list it has already sorted
        rather than have it sorted again.
        """
        if events is None:
            events = self._sorted_events()
        asns: set[int | str] = set()
        for path, _ in self._initial.values():
            asns.update(_int_path(path))
        for _, etype, _, _, path, _ in events:
            if etype != "W":
                asns.update(_int_path(path))
        return asns

    def to_dict(self, resolve_owners: Optional[bool] = None) -> Payload:
        """Render the collected data as the API's ``data`` object."""
        if resolve_owners is None:
            resolve_owners = self.resolve_owners

        # One key cache for both `initial_state` and `targets`: parsing a prefix
        # is far more expensive than the sort itself.
        sort_keys = {pfx: prefix_sort_key(pfx) for pfx in self._targets}
        initial_state = [
            {
                "target_prefix": pfx,
                "source_id": sid,
                "path": _int_path(path),
                "community": _standard_communities(communities),
            }
            for (sid, pfx), (path, communities) in sorted(
                self._initial.items(), key=lambda item: (item[0][0], sort_keys[item[0][1]])
            )
        ]

        events = []
        # Stable sort on time keeps same-second updates in stream order, the way
        # the API keeps them in arrival order.
        ordered = self._sorted_events()
        for seq, (ts, etype, sid, pfx, path, communities) in enumerate(ordered):
            attrs: Payload = {"source_id": sid, "target_prefix": pfx}
            if etype != "W":
                attrs["path"] = _int_path(path)
                attrs["community"] = _standard_communities(communities)
            events.append(
                {
                    "seq": seq,
                    "timestamp": _iso(ts),
                    "type": etype,
                    "attrs": attrs,
                }
            )

        asns = sorted(self.as_numbers(ordered))
        owners = fetch_as_owners(asns) if (resolve_owners and asns) else {}

        return {
            "resource": self.resource,
            "query_starttime": _iso(self.start_time),
            "query_endtime": _iso(self.end_time) if self.end_time else None,
            "initial_state": initial_state,
            "events": events,
            "nodes": [
                {"as_number": asn, "owner": owners.get(asn, "") if isinstance(asn, int) else ""}
                for asn in asns
            ],
            "sources": [
                {
                    "id": sid,
                    "as_number": self._sources[sid][0],
                    "ip": self._sources[sid][1],
                    "rrc": _rrc_of(sid),
                }
                for sid in sorted(self._sources)
            ],
            "targets": [
                {"prefix": pfx} for pfx in sorted(self._targets, key=lambda p: sort_keys[p])
            ],
        }

    def dump(self, ts: datetime.datetime) -> Payload:
        """Return the BGPlay payload accumulated so far.

        Unlike a windowed observer this is cumulative: ``initial_state`` is the
        table at ``start_time`` and ``events`` grows for the whole run, which is
        exactly what one API response holds. Use :meth:`write_json` to persist
        it.
        """
        return self.to_dict()

    def compare(self, other: Observer | Payload) -> None:
        """Log how this payload differs from ``other`` (another observer or a payload)."""
        if isinstance(other, BGPlayObserver):
            theirs: Payload = other.to_dict()
        else:
            assert isinstance(other, dict), "compare() takes a BGPlayObserver or a payload"
            theirs = other
        report = diff_bgplay(self.to_dict(), theirs)
        for line in format_diff(report):
            logger.info(line)


def fetch_as_owners(asns: Iterable[int | str]) -> dict[int, str]:
    """Resolve AS names through the RIPEstat ``as-names`` data call.

    BGPlay's ``owner`` comes from a slightly different source (it appends a
    country code), so these names are close to but not byte-identical with the
    API's. Only used when ``resolve_owners=True``.
    """
    from urllib.request import urlopen

    asns = list(asns)
    if not asns:
        return {}
    resource = ",".join(f"AS{asn}" for asn in asns)
    url = f"https://stat.ripe.net/data/as-names/data.json?resource={resource}"
    with urlopen(url, timeout=60) as response:
        payload = json.load(response)
    return {int(asn): name for asn, name in payload["data"]["names"].items()}


def _state_key(entry: Payload) -> tuple[str, str]:
    return (cast(str, entry["source_id"]), cast(str, entry["target_prefix"]))


def _event_key(event: Payload) -> tuple[object, ...]:
    attrs = event["attrs"]
    return (
        event["timestamp"],
        event["type"],
        attrs["source_id"],
        attrs["target_prefix"],
        tuple(attrs.get("path", ())),
        frozenset(attrs.get("community", ())),
    )


def diff_bgplay(ours: Payload, theirs: Payload) -> Payload:
    """Compare two BGPlay ``data`` objects, ours against a reference.

    ``seq`` and ``nodes[].owner`` are ignored (see the module docstring), and
    each section is compared as a **multiset**, so a lost or invented duplicate
    event shows up instead of collapsing. The same peer re-announcing the same
    path in the same second is real, and the API does report it twice.
    Communities are compared as sets, since the two sides may order them
    differently.

    Returns, per section, ``only_ours`` / ``only_theirs`` (``Counter`` of keys),
    ``mismatched`` (a set of keys present on both sides with differing content)
    and a ``jaccard`` similarity. Event ordering *within* one second is not
    compared; see the module docstring.
    """

    def _compare(ours_keys: Counter[Any], theirs_keys: Counter[Any]) -> Payload:
        union = (ours_keys | theirs_keys).total()
        return {
            "only_ours": ours_keys - theirs_keys,
            "only_theirs": theirs_keys - ours_keys,
            "jaccard": (ours_keys & theirs_keys).total() / union if union else 1.0,
        }

    our_state = {_state_key(entry): entry for entry in ours["initial_state"]}
    their_state = {_state_key(entry): entry for entry in theirs["initial_state"]}
    state = _compare(Counter(our_state.keys()), Counter(their_state.keys()))
    state["mismatched"] = {
        key
        for key in set(our_state) & set(their_state)
        if our_state[key]["path"] != their_state[key]["path"]
        or set(our_state[key]["community"]) != set(their_state[key]["community"])
    }

    events = _compare(
        Counter(_event_key(event) for event in ours["events"]),
        Counter(_event_key(event) for event in theirs["events"]),
    )
    events["mismatched"] = set()

    report = {"initial_state": state, "events": events}
    for section, key in (
        ("nodes", "as_number"),
        ("sources", "id"),
        ("targets", "prefix"),
    ):
        entry = _compare(
            Counter(item[key] for item in ours[section]),
            Counter(item[key] for item in theirs[section]),
        )
        entry["mismatched"] = set()
        report[section] = entry
    return report


def is_equivalent(report: Payload) -> bool:
    """True when :func:`diff_bgplay` found no difference at all."""
    return all(
        not entry["only_ours"] and not entry["only_theirs"] and not entry["mismatched"]
        for entry in report.values()
    )


def format_diff(report: Payload) -> list[str]:
    """Render :func:`diff_bgplay` output as human-readable lines."""
    lines = []
    for section, entry in report.items():
        lines.append(
            f"{section}: jaccard={entry['jaccard']:.4f} "
            f"only_ours={entry['only_ours'].total()} "
            f"only_theirs={entry['only_theirs'].total()} "
            f"mismatched={len(entry['mismatched'])}"
        )
    return lines
