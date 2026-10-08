"""Tests for :mod:`quickrib.observers.bgplay`, at the suite's two tiers.

* **Standalone** (no marker, offline): hand-built ``BGPElement``s driven through
  a real ``RIBTable`` pin down every rule the observer encodes: resource
  parsing, prefix vs origin matching, the initial-state/event split, community
  filtering, ordering and re-delivery collapsing.
* **End-to-end** (``e2e``): replays the pinned ``ripe_config`` window through
  ``QuickRIB`` and compares the payload against a **live call to the public
  RIPEstat ``bgplay`` API**, the thing this observer exists to reproduce.
"""

import datetime
import json
import urllib.parse
import urllib.request
from typing import Any, Literal, overload

import pytest
from pybgpflux import BGPElement

from quickrib.elements import ParsedElement, ParsedFields, WithdrawalElement
from quickrib.observers.bgplay import (
    BGPlayObserver,
    _covering_prefixes,
    _parse_resource,
    _standard_communities,
    diff_bgplay,
    format_diff,
    is_equivalent,
    prefix_sort_key,
    source_id,
)
from quickrib.rib_table import RIBTable

START = datetime.datetime(2025, 3, 3, 0, 0, tzinfo=datetime.UTC)
END = datetime.datetime(2025, 3, 3, 2, 0, tzinfo=datetime.UTC)
T0 = START.timestamp()


@overload
def elem(etype: Literal["R", "A"], prefix: str, path=(), *, ts: float = ...,
         collector: str = ..., peer_ip: str = ..., communities=...,
         peer_asn: int | None = ...) -> ParsedElement: ...
@overload
def elem(etype: Literal["W"], prefix: str, path=(), *, ts: float = ...,
         collector: str = ..., peer_ip: str = ..., communities=...,
         peer_asn: int | None = ...) -> WithdrawalElement: ...
def elem(etype, prefix, path=(), *, ts=T0 + 60, collector="rrc04", peer_ip="10.0.0.1",
         communities=(), peer_asn=None):
    """A ``BGPElement`` shaped the way ``QuickRIB`` hands them to observers.

    ``process_path`` has already turned ``as-path`` into a list of ASN strings
    whose first element is the peer ASN, so that is what the fakes carry. A
    withdrawal has no path, so its ``peer_asn`` has to be given explicitly.
    """
    peer = peer_asn if peer_asn is not None else (int(path[0]) if path else 65000)
    if etype == "W":
        return WithdrawalElement(
            time=ts,
            type="W",
            collector=collector,
            peer_asn=peer,
            peer_address=peer_ip,
            fields={"prefix": prefix},
        )
    fields: ParsedFields = {
        "prefix": prefix,
        "as-path": [str(asn) for asn in path],
        "communities": list(communities),
    }
    return ParsedElement(
        time=ts,
        type=etype,
        collector=collector,
        peer_asn=peer_asn if peer_asn is not None else (int(path[0]) if path else 65000),
        peer_address=peer_ip,
        fields=fields,
    )


def parser_element(etype, prefix, path=None, *, ts=T0) -> Any:
    """A raw ``pybgpflux.BGPElement``, shaped the way a parser really builds one.

    The counterpart to :func:`elem` above. ``elem`` returns the narrowed
    ``quickrib.elements`` types, which is what the *static* contract says an
    observer receives; this returns what the pipeline actually hands it: a
    different class, whose withdrawal ``fields`` carry the prefix alone. The
    return type is deliberately ``Any``: the mismatch is the point of the test,
    and the pipeline papers over the same seam with two suppressions of its own.
    """
    if etype == "W":
        return BGPElement(
            time=ts, type="W", collector="rrc04", peer_asn=21320,
            peer_address="10.0.0.1", fields={"prefix": prefix},
        )
    # `as-path` is already a list of ASNs: `process_path` rewrites it in place
    # before the element reaches an observer, and the wire string is gone by then.
    fields: Any = {"prefix": prefix, "as-path": list(path or ()), "communities": []}
    return BGPElement(
        time=ts, type=etype, collector="rrc04", peer_asn=21320,
        peer_address="10.0.0.1", fields=fields,
    )


def feed(observer, elements):
    """Drive ``elements`` through a real ``RIBTable`` with ``observer`` attached."""
    rib = RIBTable()
    rib.attach_observer(observer)
    for element in elements:
        if element.type == "R":
            rib.update_rib(element)
        elif element.type == "A":
            rib.update_announcement(element)
        else:
            rib.update_withdrawal(element)
    return observer.to_dict()


# --------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "resource, normalized, prefixes, origins",
    [
        ("140.78.0.0/16", "140.78.0.0/16", {"140.78.0.0/16"}, set()),
        ("AS1205", "1205", set(), {"1205"}),
        ("as1205", "1205", set(), {"1205"}),
        ("1205", "1205", set(), {"1205"}),
        ("2001:678:8d4::/48", "2001:678:8d4::/48", {"2001:678:8d4::/48"}, set()),
        # The API normalizes a host-bits-set prefix to its network.
        ("140.78.1.2/16", "140.78.0.0/16", {"140.78.0.0/16"}, set()),
    ],
)
def test_parse_resource(resource, normalized, prefixes, origins):
    assert _parse_resource(resource) == (normalized, prefixes, origins)


def test_parse_resource_accepts_a_list_like_the_api():
    normalized, prefixes, origins = _parse_resource("140.78.0.0/16,AS1205")
    assert normalized == "140.78.0.0/16,1205"
    assert prefixes == {"140.78.0.0/16"}
    assert origins == {"1205"}
    assert _parse_resource(["140.78.0.0/16", "AS1205"]) == (normalized, prefixes, origins)


@pytest.mark.parametrize("resource", ["", "  ", "ASfoo", []])
def test_parse_resource_rejects_junk(resource):
    with pytest.raises(ValueError):
        _parse_resource(resource)


def test_covering_prefixes_of_an_ip():
    covering = _covering_prefixes("8.8.8.8")
    assert len(covering) == 33
    assert {"0.0.0.0/0", "8.0.0.0/9", "8.8.8.0/24", "8.8.8.8/32"} <= covering
    assert "8.8.9.0/24" not in covering


def test_source_id_matches_the_api_for_ris_and_stays_unique_elsewhere():
    assert source_id("rrc04", "192.65.185.119") == "04-192.65.185.119"
    assert source_id("rrc00", "2001:db8::1") == "00-2001:db8::1"
    assert source_id("route-views.wide", "1.2.3.4") == "route-views.wide-1.2.3.4"


def test_standard_communities_drops_extended_and_large():
    assert _standard_communities(
        [
            "1853:1853",
            "20965:65533",
            "0:2:20965:0000014D",  # extended
            "13335:28000:16276",  # large
            "not:a:number",
        ]
    ) == ["1853:1853", "20965:65533"]


def test_prefix_sort_key_orders_numerically_ipv4_first():
    prefixes = ["103.143.32.0/23", "8.18.50.0/24", "2001:db8::/32", "8.18.0.0/16"]
    assert sorted(prefixes, key=prefix_sort_key) == [
        "8.18.0.0/16",
        "8.18.50.0/24",
        "103.143.32.0/23",
        "2001:db8::/32",
    ]


# ------------------------------------------------------------ prefix resources


def test_prefix_resource_splits_rib_and_updates_into_state_and_events():
    observer = BGPlayObserver("140.78.0.0/16", START, END)
    data = feed(
        observer,
        [
            # An update at or before start_time folds into the initial state,
            # where the RIB dump that follows it supersedes it.
            elem("A", "140.78.0.0/16", (21320, 6939, 1205), ts=T0 - 30,
                 peer_ip="192.65.185.119"),
            elem("R", "140.78.0.0/16", (21320, 1853, 1205), ts=T0,
                 peer_ip="192.65.185.119", communities=["1853:1853"]),
            elem("A", "140.78.0.0/16", (25091, 2603, 1205), ts=T0 + 60,
                 peer_ip="192.65.185.244"),
            elem("W", "140.78.0.0/16", ts=T0 + 120, peer_ip="192.65.185.119",
                 peer_asn=21320),
            # Unrelated prefixes never appear.
            elem("A", "8.8.8.0/24", (25091, 15169), ts=T0 + 60),
            elem("W", "8.8.8.0/24", ts=T0 + 60),
        ],
    )

    assert data["resource"] == "140.78.0.0/16"
    assert data["query_starttime"] == "2025-03-03T00:00:00"
    assert data["query_endtime"] == "2025-03-03T02:00:00"
    assert data["initial_state"] == [
        {
            "target_prefix": "140.78.0.0/16",
            "source_id": "04-192.65.185.119",
            "path": [21320, 1853, 1205],
            "community": ["1853:1853"],
        }
    ]
    assert data["events"] == [
        {
            "seq": 0,
            "timestamp": "2025-03-03T00:01:00",
            "type": "A",
            "attrs": {
                "source_id": "04-192.65.185.244",
                "target_prefix": "140.78.0.0/16",
                "path": [25091, 2603, 1205],
                "community": [],
            },
        },
        {
            "seq": 1,
            "timestamp": "2025-03-03T00:02:00",
            "type": "W",
            # A withdrawal carries no path or community, exactly as in the API.
            "attrs": {
                "source_id": "04-192.65.185.119",
                "target_prefix": "140.78.0.0/16",
            },
        },
    ]
    assert data["targets"] == [{"prefix": "140.78.0.0/16"}]
    assert data["sources"] == [
        {"id": "04-192.65.185.119", "as_number": 21320, "ip": "192.65.185.119", "rrc": "04"},
        {"id": "04-192.65.185.244", "as_number": 25091, "ip": "192.65.185.244", "rrc": "04"},
    ]
    assert [node["as_number"] for node in data["nodes"]] == [1205, 1853, 2603, 21320, 25091]
    assert all(node["owner"] == "" for node in data["nodes"])


def test_rib_entry_after_start_time_still_counts_as_initial_state():
    # RIS dumps at 00:00 but route-views collectors dump on their own schedule,
    # so a RIB entry can be stamped after start_time. It describes the table, not
    # a change to it, so it must never become an event.
    observer = BGPlayObserver("140.78.0.0/16", START, END)
    data = feed(observer, [elem("R", "140.78.0.0/16", (21320, 1205), ts=T0 + 600)])
    assert len(data["initial_state"]) == 1
    assert data["events"] == []


def test_withdrawal_before_start_time_clears_the_initial_state():
    observer = BGPlayObserver("140.78.0.0/16", START, END)
    data = feed(
        observer,
        [
            elem("R", "140.78.0.0/16", (21320, 1205), ts=T0 - 120),
            elem("W", "140.78.0.0/16", ts=T0 - 60, peer_ip="10.0.0.1", peer_asn=21320),
        ],
    )
    assert data["initial_state"] == []
    assert data["events"] == []
    # The peer is still a source: it was seen carrying the resource.
    assert [source["id"] for source in data["sources"]] == ["04-10.0.0.1"]


def test_prefix_resource_matches_exactly_not_more_specifics():
    # Querying 8.0.0.0/9 must not pull in 8.8.8.0/24, matching the API.
    observer = BGPlayObserver("8.0.0.0/9", START, END)
    data = feed(
        observer,
        [
            elem("R", "8.0.0.0/9", (25091, 3356), ts=T0),
            elem("R", "8.8.8.0/24", (25091, 15169), ts=T0),
        ],
    )
    assert data["targets"] == [{"prefix": "8.0.0.0/9"}]


def test_ip_resource_matches_every_covering_prefix():
    observer = BGPlayObserver("8.8.8.8", START, END)
    data = feed(
        observer,
        [
            elem("R", "8.0.0.0/9", (25091, 3356), ts=T0),
            elem("R", "8.8.8.0/24", (25091, 15169), ts=T0),
            elem("R", "8.9.0.0/16", (25091, 3356), ts=T0),
        ],
    )
    assert data["resource"] == "8.8.8.8"
    assert data["targets"] == [{"prefix": "8.0.0.0/9"}, {"prefix": "8.8.8.0/24"}]


# ---------------------------------------------------------------- AS resources


def test_as_resource_matches_the_origin_of_each_path():
    observer = BGPlayObserver("AS1205", START, END)
    data = feed(
        observer,
        [
            elem("R", "140.78.0.0/16", (21320, 1853, 1205), ts=T0),
            elem("R", "193.186.172.0/22", (21320, 1853, 1205), ts=T0),
            # Same peer, a prefix originated by somebody else: not our resource.
            elem("R", "8.8.8.0/24", (21320, 15169), ts=T0),
            # A path towards one of our targets that no longer originates at
            # AS1205 is excluded too; query the prefix to see that hijack.
            elem("A", "140.78.0.0/16", (21320, 6939, 64500), ts=T0 + 60),
        ],
    )
    assert data["resource"] == "1205"
    assert data["targets"] == [
        {"prefix": "140.78.0.0/16"},
        {"prefix": "193.186.172.0/22"},
    ]
    assert data["events"] == []
    assert [node["as_number"] for node in data["nodes"]] == [1205, 1853, 21320]


def test_as_resource_carries_no_withdrawals():
    # A withdrawal has no AS path, so it cannot be attributed to an origin; the
    # API reports none for an AS resource either.
    observer = BGPlayObserver("AS1205", START, END)
    data = feed(
        observer,
        [
            elem("R", "140.78.0.0/16", (21320, 1853, 1205), ts=T0),
            elem("W", "140.78.0.0/16", ts=T0 + 60),
        ],
    )
    assert data["events"] == []


def test_mixed_resource_keeps_withdrawals_of_its_prefix_half():
    observer = BGPlayObserver("140.78.0.0/16,AS15169", START, END)
    data = feed(
        observer,
        [
            elem("R", "140.78.0.0/16", (21320, 1205), ts=T0),
            elem("R", "8.8.8.0/24", (21320, 15169), ts=T0),
            elem("W", "140.78.0.0/16", ts=T0 + 60),
            elem("W", "8.8.8.0/24", ts=T0 + 60),
        ],
    )
    assert data["resource"] == "140.78.0.0/16,15169"
    assert [event["attrs"]["target_prefix"] for event in data["events"]] == [
        "140.78.0.0/16"
    ]


def test_withdrawal_shaped_the_way_a_parser_really_emits_it():
    """Regression: the observer must not depend on the element's Python class.

    Every other test here builds a `quickrib.elements` fake, but the pipeline
    yields a `pybgpflux.BGPElement`, a different class, whose withdrawal
    `fields` carry the prefix and nothing else (both the `bgpkit` and `bgpdump`
    parsers build it that way). Discriminating with `isinstance` silently sent
    those down the route branch, which read `fields["as-path"]` and raised.
    """
    observer = BGPlayObserver("140.78.0.0/16", START, END)
    rib = RIBTable()
    rib.attach_observer(observer)

    rib.update_rib(parser_element("R", "140.78.0.0/16", ["21320", "1205"], ts=T0))
    rib.update_withdrawal(parser_element("W", "140.78.0.0/16", ts=T0 + 60))

    data = observer.to_dict()
    assert [(e["type"], e["attrs"]["target_prefix"]) for e in data["events"]] == [
        ("W", "140.78.0.0/16")
    ]
    # A withdrawal carries no path, so the event must not claim one.
    assert "path" not in data["events"][0]["attrs"]


# -------------------------------------------------------------- shape and order


def test_paths_keep_prepending_and_communities_are_filtered():
    observer = BGPlayObserver("140.78.0.0/16", START, END)
    data = feed(
        observer,
        [
            elem(
                "R",
                "140.78.0.0/16",
                (328977, 6939, 1853, 1853, 1205),
                ts=T0,
                communities=["1853:1853", "0:2:20965:0000014D", "13335:28000:16276"],
            )
        ],
    )
    entry = data["initial_state"][0]
    assert entry["path"] == [328977, 6939, 1853, 1853, 1205]
    assert entry["community"] == ["1853:1853"]


def test_output_is_ordered_the_way_the_api_orders_it():
    observer = BGPlayObserver("AS1205", START, END)
    data = feed(
        observer,
        [
            elem("R", "103.143.32.0/23", (25091, 1205), ts=T0, peer_ip="10.0.0.2"),
            elem("R", "8.18.50.0/24", (25091, 1205), ts=T0, peer_ip="10.0.0.2"),
            elem("R", "2001:db8::/32", (21320, 1205), ts=T0, peer_ip="10.0.0.1"),
            elem("R", "8.18.50.0/24", (21320, 1205), ts=T0, peer_ip="10.0.0.1"),
            elem("A", "8.18.50.0/24", (21320, 1205), ts=T0 + 120, peer_ip="10.0.0.1"),
            elem("A", "8.18.50.0/24", (21320, 1205), ts=T0 + 60, peer_ip="10.0.0.1"),
        ],
    )
    # initial_state: by source_id, then prefix numerically with IPv4 first.
    assert [(e["source_id"], e["target_prefix"]) for e in data["initial_state"]] == [
        ("04-10.0.0.1", "8.18.50.0/24"),
        ("04-10.0.0.1", "2001:db8::/32"),
        ("04-10.0.0.2", "8.18.50.0/24"),
        ("04-10.0.0.2", "103.143.32.0/23"),
    ]
    assert [t["prefix"] for t in data["targets"]] == [
        "8.18.50.0/24",
        "103.143.32.0/23",
        "2001:db8::/32",
    ]
    assert [e["timestamp"] for e in data["events"]] == [
        "2025-03-03T00:01:00",
        "2025-03-03T00:02:00",
    ]
    assert [e["seq"] for e in data["events"]] == [0, 1]


def test_every_repeated_announcement_is_reported():
    """Nothing is collapsed: the API reports a repeat twice, so we must too.

    The observer used to de-duplicate inside the window where QuickRIB's build
    and update streams overlapped, because an update there reached it twice.
    That overlap is gone: `update_rib` starts strictly after each collector's
    dump instant, so every announcement an observer sees is one the collector
    really recorded.
    """
    observer = BGPlayObserver("140.78.0.0/16", START, END)
    rib = RIBTable()
    rib.attach_observer(observer)

    rib.update_rib(elem("R", "140.78.0.0/16", (21320, 1205), ts=T0))
    # Two identical announcements in the same second, and two more later.
    for _ in range(2):
        rib.update_announcement(elem("A", "140.78.0.0/16", (21320, 1205), ts=T0 + 30))
    for _ in range(2):
        rib.update_announcement(elem("A", "140.78.0.0/16", (21320, 1205), ts=T0 + 600))

    data = observer.to_dict()
    assert [e["timestamp"] for e in data["events"]] == [
        "2025-03-03T00:00:30",
        "2025-03-03T00:00:30",
        "2025-03-03T00:10:00",
        "2025-03-03T00:10:00",
    ]
    assert [e["seq"] for e in data["events"]] == [0, 1, 2, 3]


def test_dump_returns_the_payload_and_it_is_serialisable():
    """The observer hands back the payload; writing it is the caller's business."""
    observer = BGPlayObserver("140.78.0.0/16", START, END, name="bgplay")
    feed(observer, [elem("R", "140.78.0.0/16", (21320, 1205), ts=T0)])

    payload = observer.dump(END)
    assert payload == observer.to_dict()
    assert json.loads(json.dumps(payload)) == payload


# ------------------------------------------------------------------------ diff


def test_diff_reports_equivalence_and_every_kind_of_difference():
    observer = BGPlayObserver("140.78.0.0/16", START, END)
    ours = feed(
        observer,
        [
            elem("R", "140.78.0.0/16", (21320, 1205), ts=T0),
            elem("A", "140.78.0.0/16", (21320, 1205), ts=T0 + 60),
        ],
    )
    report = diff_bgplay(ours, ours)
    assert is_equivalent(report)
    assert all(entry["jaccard"] == 1.0 for entry in report.values())
    assert format_diff(report)[0].startswith("initial_state: jaccard=1.0000")

    # A duplicate event must not be collapsed away by the comparison.
    theirs = json.loads(json.dumps(ours))
    theirs["events"].append(dict(theirs["events"][0], seq=1))
    assert not is_equivalent(diff_bgplay(ours, theirs))

    # Same keys, different path: caught as `mismatched`, not as a missing row.
    theirs = json.loads(json.dumps(ours))
    theirs["initial_state"][0]["path"] = [21320, 6939, 1205]
    report = diff_bgplay(ours, theirs)
    assert not report["initial_state"]["only_ours"]
    assert report["initial_state"]["mismatched"]

    # `seq` and `owner` are deliberately ignored.
    theirs = json.loads(json.dumps(ours))
    theirs["events"][0]["seq"] = 578409750528002
    theirs["nodes"][0]["owner"] = "JKU-LINZ-AS University Linz, AT"
    assert is_equivalent(diff_bgplay(ours, theirs))


# ------------------------------------------------------------------------- e2e

BGPLAY_URL = "https://stat.ripe.net/data/bgplay/data.json"

# Resources covering the three shapes that behave differently: an exact prefix,
# a small origin AS, and a busy origin AS whose window holds withdrawals (which
# an AS resource must suppress) and genuine duplicate announcements.
RIPE_RESOURCES = ["140.78.0.0/16", "AS15169", "AS13335"]


def fetch_bgplay(resource, config):
    """Call the public RIPEstat ``bgplay`` endpoint for the pinned window."""
    query = urllib.parse.urlencode(
        {
            "resource": resource,
            "starttime": config.start_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "endtime": config.end_time.strftime("%Y-%m-%dT%H:%M:%S"),
            # The API takes bare collector numbers, not `rrcNN` names.
            "rrcs": ",".join(rc.removeprefix("rrc").lstrip("0") for rc in config.collectors),
        }
    )
    with urllib.request.urlopen(f"{BGPLAY_URL}?{query}", timeout=300) as response:
        return json.load(response)["data"]


def events_by_second(data):
    """Events grouped and sorted within each second.

    The API orders same-second events by RIS ingestion order, which a replay of
    the collector archives cannot reproduce; everything else about them must
    match exactly.
    """
    grouped = {}
    for event in data["events"]:
        attrs = event["attrs"]
        grouped.setdefault(event["timestamp"], []).append(
            (
                event["type"],
                attrs["source_id"],
                attrs["target_prefix"],
                tuple(attrs.get("path", ())),
                tuple(sorted(attrs.get("community", ()))),
            )
        )
    return {ts: sorted(events) for ts, events in grouped.items()}


@pytest.fixture(scope="module")
def bgplay_run(ripe_config, make_quickrib):
    """Replay the pinned window once, with one observer per resource."""
    quickrib = make_quickrib(ripe_config)
    observers = {
        resource: BGPlayObserver(resource, ripe_config.start_time, ripe_config.end_time)
        for resource in RIPE_RESOURCES
    }
    for observer in observers.values():
        quickrib.rib.attach_observer(observer)
    quickrib.run()
    return observers


@pytest.mark.e2e
@pytest.mark.parametrize("resource", RIPE_RESOURCES)
def test_matches_the_public_bgplay_api(resource, bgplay_run, ripe_config):
    ours = bgplay_run[resource].to_dict()
    theirs = fetch_bgplay(resource, ripe_config)

    # Guard against an empty API answer silently passing everything below.
    assert theirs["initial_state"], f"the API returned no state for {resource}"

    report = diff_bgplay(ours, theirs)
    assert is_equivalent(report), "\n".join(format_diff(report))

    assert ours["resource"] == theirs["resource"]
    assert ours["query_starttime"] == theirs["query_starttime"]
    assert ours["query_endtime"] == theirs["query_endtime"]

    # Beyond set equality: the rows and their order are byte-identical.
    assert ours["initial_state"] == theirs["initial_state"]
    assert ours["targets"] == theirs["targets"]
    assert ours["sources"] == theirs["sources"]
    assert [n["as_number"] for n in ours["nodes"]] == [n["as_number"] for n in theirs["nodes"]]
    assert events_by_second(ours) == events_by_second(theirs)


@pytest.mark.e2e
def test_as_resource_suppresses_withdrawals_the_prefix_resource_reports(
    bgplay_run, ripe_config
):
    """The AS-mode withdrawal rule, checked against the API rather than assumed.

    The busy AS sees withdrawals in this window, and our AS-resource payload
    drops them exactly as the API's does, while the API's *prefix* query for
    the same prefixes does report them.
    """
    ours = bgplay_run["AS13335"].to_dict()
    assert all(event["type"] == "A" for event in ours["events"])

    withdrawn = "103.31.4.0/23"
    assert {"prefix": withdrawn} in ours["targets"]
    by_prefix = fetch_bgplay(withdrawn, ripe_config)
    assert any(event["type"] == "W" for event in by_prefix["events"])
