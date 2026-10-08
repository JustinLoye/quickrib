"""Tests for :mod:`quickrib.observers.ioda`, at the suite's two tiers.

* **Standalone** (no marker, offline): hand-built RIBs and ``BGPElement``s pin
  down the block arithmetic, the full-feed rule, the visibility quorum and the
  alerting rule. The centrepiece is a randomised stream that holds the
  incremental :class:`IODAObserver` to the post-hoc :func:`visible_slash24s`
  reference at every dump.
* **End-to-end** (``e2e``): replays the pinned ``ioda_config`` window through
  ``QuickRIB`` and compares the signal against the **live IODA API**, and runs
  the alert rule against the live alerts/events endpoints. QuickRIB reads a
  single collector where IODA reads them all, so the signal test asserts a
  bounded deviation rather than equality, and reports the measured one.
"""

import datetime
import json
import random
import urllib.parse
import urllib.request

import pytest
from conftest import parser_element

from quickrib.elements import ParsedElement, WithdrawalElement
from quickrib.observers import Observer
from quickrib.observers.ioda import (
    ALERT_HISTORY,
    FULL_FEED_RATIO,
    IODA_STEP,
    IODAObserver,
    RibIODAObserver,
    detect_alerts,
    detect_events,
    evaluate_series,
    full_feed_peers,
    visible_slash24s,
)
from quickrib.rib_table import RIBTable


def peers_of(*vps):
    """Pin a full-feed selection the way the pipeline hands one over."""
    return {vp for vp in vps}


def build_rib(entries, ts=1000.0):
    """A ``RIBTable`` seeded from ``{(collector, asn, ip): {prefix: path}}``."""
    rib = RIBTable()
    for (collector, peer_asn, peer_ip), prefixes in entries.items():
        for prefix, path in prefixes.items():
            rib.update_rib(
                ParsedElement(
                    time=ts,
                    type="R",
                    collector=collector,
                    peer_asn=peer_asn,
                    peer_address=peer_ip,
                    fields={
                        "prefix": prefix,
                        "as-path": [str(a) for a in path],
                        "communities": [],
                    },
                )
            )
    return rib


# The block arithmetic the signal is built on lives in tests/test_radix.py.


# ---------------------------------------------------------------- full feeds


def test_full_feed_rule_is_quickribs_relative_one():
    # Ten unique prefixes at the collector, so the bar is more than eight.
    rib = build_rib(
        {
            ("rc", 1, "a"): {f"10.{i}.0.0/24": [1, 100] for i in range(10)},
            ("rc", 2, "b"): {f"10.{i}.0.0/24": [2, 100] for i in range(9)},
            ("rc", 3, "c"): {f"10.{i}.0.0/24": [3, 100] for i in range(8)},
        }
    )
    assert full_feed_peers(rib.data) == {("rc", 1, "a"), ("rc", 2, "b")}
    assert full_feed_peers(rib.data, ratio=1.0) == set()


def test_full_feed_denominator_mixes_address_families():
    """The consequence of QuickRIB's rule at a dual-stack collector.

    The unique-prefix count spans both families, so an IPv4-only session
    carrying every IPv4 route can still fall under the bar. It is why the IPv6
    signal needs an explicit ``ff_peers`` set, and why this rule keeps fewer
    peers than IODA's absolute one on a real collector.
    """
    rib = build_rib(
        {
            ("rc", 1, "a"): {f"10.{i}.0.0/24": [1, 100] for i in range(8)},
            ("rc", 2, "b"): {
                **{f"10.{i}.0.0/24": [2, 100] for i in range(8)},
                **{f"2001:db8:{i}::/48": [2, 100] for i in range(4)},
            },
        }
    )
    # 12 unique prefixes, bar is 9.6: the IPv4-complete peer misses it.
    assert full_feed_peers(rib.data) == {("rc", 2, "b")}
    assert FULL_FEED_RATIO == 0.8


# ------------------------------------------------------------------ the signal


def test_signal_counts_only_prefixes_above_the_quorum():
    # Three full-feed peers: a prefix needs more than 1.5 of them, i.e. two.
    peers = [("rc", i, f"p{i}") for i in (1, 2, 3)]
    entries = {vp: {f"10.{i}.0.0/24": [vp[1], 64500] for i in range(3)} for vp in peers}
    # 10.9.0.0/24 is seen by one peer only -> below quorum, not counted.
    entries[peers[0]]["10.9.0.0/24"] = [1, 64500]
    # 10.8.0.0/24 is seen by two -> counted.
    entries[peers[0]]["10.8.0.0/24"] = [1, 64500]
    entries[peers[1]]["10.8.0.0/24"] = [2, 64500]

    assert visible_slash24s(build_rib(entries).data, ff_peers=peers_of(*peers)) == {
        "64500": 4
    }


def test_signal_credits_every_origin_under_moas():
    peers = [("rc", i, f"p{i}") for i in (1, 2, 3)]
    entries = {vp: {"10.0.0.0/24": [vp[1], 64500]} for vp in peers}
    entries[peers[2]]["10.0.0.0/24"] = [3, 64501]
    assert visible_slash24s(build_rib(entries).data, ff_peers=peers_of(*peers)) == {
        "64500": 1,
        "64501": 1,
    }


def test_signal_deaggregates_within_an_origin():
    peers = [("rc", i, f"p{i}") for i in (1, 2)]
    entries = {
        vp: {"10.0.0.0/22": [vp[1], 64500], "10.0.1.0/24": [vp[1], 64500]} for vp in peers
    }
    assert visible_slash24s(build_rib(entries).data, ff_peers=peers_of(*peers)) == {
        "64500": 4
    }


def test_signal_selects_the_requested_address_family():
    peers = [("rc", i, f"p{i}") for i in (1, 2)]
    entries = {
        vp: {"10.0.0.0/24": [vp[1], 64500], "2001:db8::/48": [vp[1], 64500]}
        for vp in peers
    }
    data = build_rib(entries).data
    ff = peers_of(*peers)
    assert visible_slash24s(data, ip_version=4, ff_peers=ff) == {"64500": 1}
    assert visible_slash24s(data, ip_version=6, ff_peers=ff) == {"64500": 1}


# ------------------------------------------- incremental vs post-hoc reference


def announce(rib, collector, peer_asn, peer_ip, prefix, path, ts):
    rib.update_announcement(
        ParsedElement(
            time=ts,
            type="A",
            collector=collector,
            peer_asn=peer_asn,
            peer_address=peer_ip,
            fields={
                "prefix": prefix,
                "as-path": [str(a) for a in path],
                "communities": [],
            },
        )
    )


def withdraw(rib, collector, peer_asn, peer_ip, prefix, ts):
    rib.update_withdrawal(
        WithdrawalElement(
            time=ts,
            type="W",
            collector=collector,
            peer_asn=peer_asn,
            peer_address=peer_ip,
            fields={"prefix": prefix},
        )
    )


@pytest.mark.parametrize("seed", range(6))
def test_incremental_matches_the_post_hoc_reference(seed):
    """The whole point of the incremental observer: it must not drift.

    A randomised stream of announcements, origin changes and withdrawals over a
    small RIB, with the incremental totals checked against a full recomputation
    after every batch.
    """
    rng = random.Random(seed)
    peers = [("rc", 100 + i, f"10.1.1.{i}") for i in range(5)]
    prefixes = [
        "10.0.0.0/22", "10.0.1.0/24", "10.0.4.0/24", "10.1.0.0/16",
        "10.1.2.0/23", "10.2.0.0/24", "10.3.0.0/25", "192.0.2.0/24",
    ]
    origins = ["64500", "64501", "64502"]

    rib = RIBTable()
    observer = IODAObserver(peers_of(*peers))
    rib.attach_observer(observer)
    # The pipeline hands the selection over before the build, so the observer
    # counts the RIB dump itself rather than reading the table back.
    for vp in peers:
        for prefix in prefixes[:4]:
            rib.update_rib(
                ParsedElement(
                    time=0.0, type="R", collector=vp[0], peer_asn=vp[1],
                    peer_address=vp[2],
                    fields={"prefix": prefix, "as-path": [str(vp[1]), "64500"],
                            "communities": []},
                )
            )

    ts = 1.0
    assert observer.dump(datetime.datetime.fromtimestamp(ts, datetime.UTC))

    for step in range(40):
        for _ in range(rng.randint(1, 6)):
            vp = rng.choice(peers)
            prefix = rng.choice(prefixes)
            ts += 1.0
            if rng.random() < 0.35:
                withdraw(rib, vp[0], vp[1], vp[2], prefix, ts)
            else:
                announce(rib, vp[0], vp[1], vp[2], prefix,
                         [vp[1], rng.choice(origins)], ts)

        incremental = observer.dump(datetime.datetime.fromtimestamp(ts, datetime.UTC))
        reference = visible_slash24s(rib.data, ff_peers=observer.ff_peers)
        assert {a: v for a, v in incremental.items() if v} == {
            a: v for a, v in reference.items() if v
        }, f"drifted at step {step}"


def test_rib_observer_and_incremental_observer_agree_through_the_pipeline():
    peers = [("rc", 100 + i, f"10.1.1.{i}") for i in range(3)]
    rib = RIBTable()
    incremental = IODAObserver(peers_of(*peers))
    posthoc = RibIODAObserver(rib=rib, ff_peers=peers_of(*peers))
    rib.attach_observer(incremental)
    rib.attach_observer(posthoc)
    for vp in peers:
        for i in range(3):
            rib.update_rib(
                ParsedElement(
                    time=1000.0, type="R", collector=vp[0], peer_asn=vp[1],
                    peer_address=vp[2],
                    fields={"prefix": f"10.{i}.0.0/24",
                            "as-path": [str(vp[1]), "64500"], "communities": []},
                )
            )

    ts = datetime.datetime(2022, 1, 26, 4, 0, tzinfo=datetime.UTC)
    assert incremental.dump(ts) == posthoc.dump(ts) == {"64500": 3}

    # Two of three peers drop a prefix: it falls below the quorum for both.
    for vp in peers[:2]:
        withdraw(rib, vp[0], vp[1], vp[2], "10.2.0.0/24", 2000.0)
    ts += datetime.timedelta(seconds=IODA_STEP)
    assert incremental.dump(ts) == posthoc.dump(ts) == {"64500": 2}
    assert incremental.series[int(ts.timestamp())] == {"64500": 2}


def test_incremental_observer_needs_the_full_feed_selection():
    observer = IODAObserver()
    with pytest.raises(RuntimeError, match="full-feed peers"):
        observer.dump(datetime.datetime.now(datetime.UTC))
    observer.set_full_feed_peers({("rc", 1, "a")})
    assert observer.dump(datetime.datetime.now(datetime.UTC)) == {}


def test_pipeline_hands_the_full_feed_selection_to_the_observer():
    """`RIBTable.notify_full_feed_peers` is the hand-over `QuickRIB` performs."""
    rib = RIBTable()
    observer = IODAObserver()
    other = RibIODAObserver()  # also accepts it
    rib.attach_observer(observer)
    rib.attach_observer(other)

    selection = {("rc", 100, "10.1.1.0"), ("rc", 101, "10.1.1.1")}
    rib.notify_full_feed_peers(selection)
    assert observer.ff_peers == other.ff_peers == selection
    # Observers without the hook are simply left alone.
    class Bare(Observer):
        pass

    rib.attach_observer(Bare())
    rib.notify_full_feed_peers(selection)


def test_full_feed_drift_is_reported():
    peers = [("rc", 100 + i, f"10.1.1.{i}") for i in range(3)]
    rib = build_rib({vp: {f"10.{i}.0.0/24": [vp[1], 64500] for i in range(3)}
                     for vp in peers})
    observer = IODAObserver(peers_of(*peers))
    rib.attach_observer(observer)
    observer.dump(datetime.datetime(2022, 1, 26, 4, 0, tzinfo=datetime.UTC))
    assert observer.full_feed_drift(rib) == set()

    # Strip one peer down to a single prefix: it is no longer full-feed.
    for i in (1, 2):
        withdraw(rib, "rc", 100, "10.1.1.0", f"10.{i}.0.0/24", 2000.0)
    assert observer.full_feed_drift(rib) == {("rc", 100, "10.1.1.0")}


# -------------------------------------------------------------------- alerting


def flat(value, bins, start=0):
    return {start + i * IODA_STEP: value for i in range(bins)}


def test_no_alert_before_a_full_day_of_history():
    assert evaluate_series(flat(100, 200)) == []
    records = evaluate_series({**flat(100, 300)})
    assert len(records) == 300 - ALERT_HISTORY // IODA_STEP
    assert all(r["level"] == "normal" for r in records)


def test_alert_fires_on_a_drop_and_clears_on_recovery():
    day = ALERT_HISTORY // IODA_STEP
    series = flat(100, day)
    ts = day * IODA_STEP
    for value in [90] * 3 + [100] * 3:
        series[ts] = value
        ts += IODA_STEP
    alerts = detect_alerts(series)
    assert [(a["level"], a["value"], a["historyValue"]) for a in alerts] == [
        ("critical", 90, 100.0),
        ("normal", 100, 100.0),
    ]
    assert alerts[0]["condition"] == "< 0.99"
    assert alerts[0]["time"] == day * IODA_STEP


def test_a_one_percent_dip_does_not_alert():
    day = ALERT_HISTORY // IODA_STEP
    series = flat(100, day)
    series[day * IODA_STEP] = 99  # 99 is not < 0.99 * 100
    assert detect_alerts(series) == []


def test_an_outage_does_not_erode_its_own_baseline():
    """A long drop must stay critical: alerting bins are kept out of the median.

    Without that, half a day at the lower level drags the median down and the
    outage silently declares itself over, which is not what the API does.
    """
    day = ALERT_HISTORY // IODA_STEP
    series = flat(100, day)
    ts = day * IODA_STEP
    for _ in range(day):  # a full day at the lower level
        series[ts] = 50
        ts += IODA_STEP
    records = evaluate_series(series)
    assert all(r["level"] == "critical" for r in records)
    assert {r["historyValue"] for r in records} == {100.0}


def test_event_pairs_alerts_and_scores_them_like_the_api():
    day = ALERT_HISTORY // IODA_STEP
    series = flat(122, day)
    ts = day * IODA_STEP
    for _ in range(17):
        series[ts] = 90
        ts += IODA_STEP
    series[ts] = 122

    events = detect_events(series, location="asn/43160", location_name="AS43160")
    assert len(events) == 1
    event = events[0]
    assert event["start"] == day * IODA_STEP
    assert event["duration"] == 17 * IODA_STEP
    # score = 500 * sum over the event's bins of (1 - value / historyValue)
    assert event["score"] == pytest.approx(500 * 17 * (1 - 90 / 122))
    assert event["method"] == "median" and event["datasource"] == "bgp"


def test_an_event_still_open_at_the_end_is_closed_on_the_last_bin():
    day = ALERT_HISTORY // IODA_STEP
    series = flat(100, day)
    ts = day * IODA_STEP
    for _ in range(4):
        series[ts] = 10
        ts += IODA_STEP
    event = detect_events(series)[0]
    assert event["start"] == day * IODA_STEP
    assert event["duration"] == 4 * IODA_STEP


# ------------------------------------------------------------------------- e2e

IODA_URL = "https://api.ioda.inetintel.cc.gatech.edu/v2"

# The pinned window brackets a clean, well-documented BGP outage: AS43160
# (ES-MDC-DATACENTER) loses about a quarter of its visible /24s at 04:20 UTC and
# recovers at 05:45. AS262908 blacks out completely for a few bins.
OUTAGE_ASN = "43160"
# ASes that are stable across the window, for the signal comparison.
CONTROL_ASNS = ["2497", "13335", "3320", "4766", "17676", "6167", "3269"]


def ioda_get(path, **params):
    url = f"{IODA_URL}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=300) as response:
        return json.load(response)["data"]


def ioda_signal(asn, start, end):
    """The live IODA BGP series for one AS, as ``{bin timestamp: visible /24s}``."""
    data = ioda_get(
        f"/signals/raw/asn/{asn}",
        **{"from": int(start.timestamp()), "until": int(end.timestamp()),
           "datasource": "bgp"},
    )
    if not data or not data[0]:
        return {}
    signal = data[0][0]
    return {
        signal["from"] + i * signal["step"]: value
        for i, value in enumerate(signal["values"])
        if value is not None
    }


@pytest.fixture(scope="module")
def ioda_run(ioda_config, make_quickrib):
    """Replay the pinned window once with the incremental observer attached."""
    quickrib = make_quickrib(ioda_config)
    # No arguments: the pipeline hands over its full-feed selection during
    # `initialize_processing`, before a single element is replayed.
    observer = IODAObserver()
    quickrib.rib.attach_observer(observer)
    quickrib.run()
    return quickrib, observer


@pytest.mark.e2e
def test_incremental_matches_post_hoc_on_a_real_rib(ioda_run, ioda_config):
    """The equivalence of the two implementations, on a million-prefix table."""
    quickrib, observer = ioda_run
    # Dump again: the last in-stream dump landed on a bin boundary and more
    # updates were applied after it, so only a fresh dump shares the reference's
    # instant.
    incremental = observer.dump(ioda_config.end_time)
    reference = visible_slash24s(quickrib.rib.data, ff_peers=observer.ff_peers)
    assert {a: v for a, v in incremental.items() if v} == {
        a: v for a, v in reference.items() if v
    }
    # The observer took the pipeline's selection; recomputing QuickRIB's rule on
    # the finished table still yields it.
    assert observer.ff_peers
    assert observer.full_feed_drift(quickrib.rib) == set()


@pytest.mark.e2e
def test_signal_tracks_the_live_ioda_api(ioda_run, ioda_config):
    """Quantify how far one collector lands from IODA's all-collector signal.

    IODA reads every Route Views and RIPE RIS collector; the pinned window reads
    one. The quorum is therefore taken over a much smaller full-feed population,
    so the bound here is a deviation budget, not equality, and the measured
    deviation is reported either way.
    """
    _, observer = ioda_run
    bins = sorted(observer.series)
    assert len(bins) >= 20, "the replay produced too few bins to compare"

    report, errors = [], []
    for asn in CONTROL_ASNS:
        theirs = ioda_signal(asn, ioda_config.start_time, ioda_config.end_time)
        common = [b for b in bins if b in theirs and theirs[b]]
        assert common, f"the API returned no overlapping bins for AS{asn}"
        ours = [observer.series[b].get(asn, 0) for b in common]
        relative = [abs(o - theirs[b]) / theirs[b] for o, b in zip(ours, common, strict=True)]
        worst = max(relative)
        errors.extend(relative)
        report.append(f"AS{asn}: median={theirs[common[0]]} max_rel_err={worst:.4%}")
        assert worst < 0.02, f"AS{asn} deviates too far\n" + "\n".join(report)

    mean_error = sum(errors) / len(errors)
    assert mean_error < 0.01, f"mean deviation {mean_error:.4%}\n" + "\n".join(report)
    print("\nIODA signal deviation:\n" + "\n".join(report)
          + f"\nmean relative error {mean_error:.4%}")


@pytest.mark.e2e
def test_outage_shows_up_in_the_replayed_signal(ioda_run, ioda_config):
    """The window's outage, drop and recovery, reconstructed from one collector.

    This is the strongest claim the signal side makes: not just that something
    dropped, but that a single collector puts the baseline and the outage floor
    on the API's own numbers, and both transitions within one bin of its.
    """
    _, observer = ioda_run
    theirs_by_bin = ioda_signal(OUTAGE_ASN, ioda_config.start_time, ioda_config.end_time)
    bins = [b for b in sorted(observer.series) if b in theirs_by_bin]
    assert bins, "no bins overlap the API's series"
    ours = [observer.series[b].get(OUTAGE_ASN, 0) for b in bins]
    theirs = [theirs_by_bin[b] for b in bins]
    context = f"\nours  ={ours}\ntheirs={theirs}"

    assert min(theirs) < 0.9 * max(theirs), "the pinned window no longer holds an outage"
    # The same two levels: the baseline and the outage floor, to the block.
    assert (min(ours), max(ours)) == (min(theirs), max(theirs)), context
    assert ours[0] == max(ours) and ours[-1] == max(ours), "no drop and recovery" + context

    def transitions(values):
        # One (bin, before, after) per consecutive pair, so all three are n-1
        # long: `bins[1:]` labels the bin the change lands in.
        return [b for b, before, after in zip(bins[1:], values[:-1], values[1:], strict=True)
                if before != after]

    ours_at, theirs_at = transitions(ours), transitions(theirs)
    assert len(ours_at) == len(theirs_at) == 2, "expected one drop and one recovery" + context
    drop, recovery = zip(ours_at, theirs_at, strict=True)
    # QuickRIB dumps the table *at* a bin boundary while IODA's bin covers the
    # interval that follows it, so even a perfectly reconstructed transition
    # lands one bin late.
    assert abs(drop[0] - drop[1]) <= IODA_STEP, "drop is off by more than a bin" + context
    # Recovery is looser: withdrawn routes come back to one collector over
    # several minutes, and IODA sees whichever of its collectors relearns first.
    assert abs(recovery[0] - recovery[1]) <= 4 * IODA_STEP, (
        "recovery is off by more than 20 minutes" + context
    )


@pytest.mark.e2e
def test_alert_rule_reproduces_the_live_outage_endpoints(ioda_config):
    """Run the detector on IODA's own signal and match its alerts and event.

    The rule needs 24 hours of history, which a short replay cannot provide, so
    it is fed the API's series for the same AS, isolating the detector from
    the signal reconstruction the other e2e tests cover.
    """
    end = ioda_config.end_time
    start = end - datetime.timedelta(seconds=2 * ALERT_HISTORY)
    series = ioda_signal(OUTAGE_ASN, start, end)
    assert series, "the API returned no signal for the outage AS"

    window = {"from": int(ioda_config.start_time.timestamp()), "until": int(end.timestamp())}
    api_alerts = ioda_get("/outages/alerts", entityType="asn", entityCode=OUTAGE_ASN,
                          datasource="bgp", **window)
    api_events = ioda_get("/outages/events", entityType="asn", entityCode=OUTAGE_ASN,
                          datasource="bgp", **window)
    assert api_alerts and api_events, "the API reports no outage in the pinned window"

    ours = [a for a in detect_alerts(series) if window["from"] <= a["time"] < window["until"]]
    assert [(a["time"], a["level"], a["value"], a["historyValue"]) for a in ours] == [
        (a["time"], a["level"], a["value"], float(a["historyValue"])) for a in api_alerts
    ]

    theirs = api_events[0]
    event = [e for e in detect_events(series, location=theirs["location"],
                                      location_name=theirs["location_name"])
             if e["start"] == theirs["start"]]
    assert event, f"no event at {theirs['start']}"
    assert event[0]["duration"] == theirs["duration"]
    assert event[0]["score"] == pytest.approx(theirs["score"])


def test_observers_handle_a_real_parser_shaped_withdrawal():
    """Withdrawals as the pipeline actually delivers them, not as a fake shapes them.

    A parser builds a withdrawal with `fields == {"prefix": ...}` and no path, in
    a class that is *not* `WithdrawalElement`. `IODAObserver` reads the origin
    off the RIB's `data` rather than the element, so it must cope, and the RIB
    must still hand it the withdrawn data after deleting the node.
    """
    peers = [("rc", 100 + i, f"10.1.1.{i}") for i in range(3)]
    rib = RIBTable()
    observer = IODAObserver(peers_of(*peers))
    rib.attach_observer(observer)

    for collector, peer_asn, peer_ip in peers:
        rib.update_rib(
            parser_element(
                "R", "10.0.0.0/24", ["64500"],
                collector=collector, peer_asn=peer_asn, peer_ip=peer_ip,
            )
        )
    ts = datetime.datetime(2022, 1, 26, 4, 0, tzinfo=datetime.UTC)
    assert observer.dump(ts) == {"64500": 1}

    for collector, peer_asn, peer_ip in peers[:2]:
        rib.update_withdrawal(
            parser_element(
                "W", "10.0.0.0/24",
                collector=collector, peer_asn=peer_asn, peer_ip=peer_ip,
            )
        )
    ts += datetime.timedelta(seconds=IODA_STEP)
    assert observer.dump(ts) == {"64500": 0}
