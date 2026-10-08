"""Both tiers for the pipeline itself, :mod:`quickrib.quickrib`.

* **Standalone** (no marker, offline): the pure decisions the pipeline makes per
  element and per window: when a dump is due (`elapsed_dump_boundaries`), what
  makes an AS path usable (`process_path`), and what a config will accept. These
  used to have no tier at all: the module was covered only end to end, which is
  how a dump-scheduling bug sat in it unnoticed.
* **End-to-end** (``e2e``): replays the pinned window from ``conftest``
  (2010-08-31 23:58 -> 2010-09-01 01:59 UTC, collectors ``route-views.wide`` and
  ``rrc04``) through ``QuickRIB.run()`` and asserts exact RIB-entry / update /
  withdrawal / announcement counts via :class:`TesterObserver`.

The dump schedule is absolute: boundaries are ``start_time + k * dump_res``,
never "one ``dump_res`` after whatever message last triggered a dump".
"""

import datetime
from typing import Any, cast

import pytest
from pybgpflux import BGPElement
from pybgpflux.brokers.bgpbroker import BrokerQueryError
from pydantic import ValidationError

from quickrib.observers import TesterObserver
from quickrib.quickrib import (
    QuickRIB,
    QuickRIBConfig,
    elapsed_dump_boundaries,
    latest_rib_dumps,
    process_path,
)
from quickrib.rib_table import RIBTable

DUMP_RES = 300.0
START = 1_000.0


class TestElapsedDumpBoundaries:
    def test_nothing_is_due_before_the_first_boundary(self):
        # A message one second into the run: the first window is still open.
        assert list(elapsed_dump_boundaries(START + DUMP_RES, START + 1, DUMP_RES)) == []

    def test_one_boundary_per_elapsed_window(self):
        assert list(elapsed_dump_boundaries(START, START + 900, DUMP_RES)) == [
            START,
            START + 300,
            START + 600,
        ]

    def test_a_gap_yields_every_window_it_spans(self):
        """The regression: a gap used to advance the schedule by one window only,
        which left it permanently behind the stream and stopped dumping."""
        assert list(elapsed_dump_boundaries(START, START + 1800, DUMP_RES)) == [
            START + step * 300 for step in range(6)
        ]

    def test_a_boundary_equal_to_the_element_is_not_yet_due(self):
        # An element at exactly the boundary belongs to the window it opens.
        assert list(elapsed_dump_boundaries(START, START, DUMP_RES)) == []

    def test_inclusive_closes_a_window_landing_on_the_bound(self):
        # The final flush: `until` is end_time, and a boundary on it closes a
        # window that is genuinely over.
        assert list(elapsed_dump_boundaries(START, START, DUMP_RES, inclusive=True)) == [
            START
        ]

    def test_boundaries_do_not_drift_with_the_trigger(self):
        # Whatever the triggering timestamps, the boundaries stay on the grid.
        emitted = []
        next_dump_time = START
        for ts in (START + 7, START + 611, START + 613, START + 2_000):
            for boundary in elapsed_dump_boundaries(next_dump_time, ts, DUMP_RES):
                emitted.append(boundary)
                next_dump_time = boundary + DUMP_RES
        assert emitted == [START + step * 300 for step in range(7)]
        assert all((b - START) % DUMP_RES == 0 for b in emitted)


class FakeRIB:
    """Enough of a `RIBTable` to record when the pipeline asks for a dump."""

    def __init__(self):
        self.dumps: list[datetime.datetime] = []

    def dump(self, ts: datetime.datetime) -> None:
        self.dumps.append(ts)

    def update_withdrawal(self, bgpelem) -> None:
        pass

    def update_announcement(self, bgpelem) -> None:
        pass

    def update_rib(self, bgpelem) -> None:
        pass

    def notify_full_feed_peers(self, ff_peers) -> None:
        pass

    def notify_built(self) -> None:
        pass


class FakeStream:
    """A `BGPStream` stand-in yielding pre-built elements."""

    def __init__(self, elements):
        self._elements = elements

    def __iter__(self):
        return iter(self._elements)


def make_pipeline(*, dump_res=300, end_offset=3_600) -> tuple[QuickRIB, FakeRIB]:
    """A pipeline whose RIB only records the instants it was asked to dump."""
    start = datetime.datetime(2022, 1, 26, 4, 0, tzinfo=datetime.UTC)
    quickrib = QuickRIB(
        start_time=start,
        end_time=start + datetime.timedelta(seconds=end_offset),
        dump_res=datetime.timedelta(seconds=dump_res),
        collectors=["rrc04"],
    )
    fake = FakeRIB()
    quickrib.rib = cast(RIBTable, fake)
    quickrib.rc_to_rib_end = {"rrc04": start.timestamp()}
    quickrib.rc_to_ff_peers = {"rrc04": set()}
    return quickrib, fake


def withdrawal_at(ts: float):
    return BGPElement(
        time=ts, type="W", collector="rrc04", peer_asn=1,
        peer_address="10.0.0.1", fields={"prefix": "192.0.2.0/24"},
    )


def rib_entry_at(ts: float, prefix: str = "192.0.2.0/24"):
    fields: Any = {"prefix": prefix, "as-path": "1 64500", "communities": []}
    return BGPElement(
        time=ts, type="R", collector="rrc04", peer_asn=1,
        peer_address="10.0.0.1", fields=fields,
    )


class FakeBrokerItem:
    """The fields `latest_rib_dumps` reads off a broker item."""

    def __init__(self, collector_id: str, ts_start: str, data_type: str = "ribs"):
        self.collector_id = collector_id
        self.ts_start = ts_start
        self.data_type = data_type


class FakeBroker:
    """A broker that lists whatever it was handed."""

    def __init__(self, items, raises: bool = False):
        self.items = items
        self.raises = raises
        self.queries: list[Any] = []

    def query(self, config):
        self.queries.append(config)
        if self.raises:
            raise BrokerQueryError("nothing there")
        return self.items


class TestLatestRibDumps:
    """`start_time` is not required to sit on a dump: the pipeline finds the last
    one before it. The times come from the broker, because cadence varies by
    project and over time. In 2010 `rrc04` dumped at 15:59 and 23:59, not on the
    8-hour marks a rule of thumb would assume.
    """

    START = datetime.datetime(2010, 9, 1, 0, 30, tzinfo=datetime.UTC)

    def _dumps(self, items, **kwargs):
        return latest_rib_dumps(
            ["route-views.wide", "rrc04"], self.START, broker=FakeBroker(items), **kwargs
        )

    def test_picks_the_latest_dump_before_start_time(self):
        dumps = self._dumps([
            FakeBrokerItem("route-views.wide", "2010-08-31T22:00:00"),
            FakeBrokerItem("route-views.wide", "2010-09-01T00:00:00"),
            FakeBrokerItem("rrc04", "2010-08-31T15:59:00"),
            FakeBrokerItem("rrc04", "2010-08-31T23:59:00"),
        ])
        assert dumps == {
            "route-views.wide": datetime.datetime(2010, 9, 1, 0, 0, tzinfo=datetime.UTC),
            "rrc04": datetime.datetime(2010, 8, 31, 23, 59, tzinfo=datetime.UTC),
        }

    def test_a_dump_after_start_time_is_ignored(self):
        # The table at an instant cannot be built from a dump taken after it.
        dumps = self._dumps([
            FakeBrokerItem("route-views.wide", "2010-08-31T22:00:00"),
            FakeBrokerItem("route-views.wide", "2010-09-01T02:00:00"),
        ])
        assert dumps["route-views.wide"] == datetime.datetime(
            2010, 8, 31, 22, 0, tzinfo=datetime.UTC
        )

    def test_a_dump_exactly_at_start_time_is_used(self):
        dumps = self._dumps([FakeBrokerItem("rrc04", "2010-09-01T00:30:00")])
        assert dumps["rrc04"] == self.START

    def test_a_collector_with_no_dump_is_absent(self):
        dumps = self._dumps([FakeBrokerItem("rrc04", "2010-08-31T23:59:00")])
        assert "route-views.wide" not in dumps

    def test_non_rib_items_are_ignored(self):
        dumps = self._dumps([
            FakeBrokerItem("rrc04", "2010-09-01T00:29:00", data_type="updates"),
            FakeBrokerItem("rrc04", "2010-08-31T23:59:00"),
        ])
        assert dumps["rrc04"] == datetime.datetime(2010, 8, 31, 23, 59, tzinfo=datetime.UTC)

    def test_an_empty_broker_answer_is_not_an_error(self):
        # The caller reports which collectors were dropped and why.
        assert latest_rib_dumps(["rrc04"], self.START, broker=FakeBroker([], raises=True)) == {}

    def test_the_lookback_bounds_the_query(self):
        broker = FakeBroker([])
        latest_rib_dumps(["rrc04"], self.START, lookback=datetime.timedelta(hours=6),
                         broker=broker)
        query = broker.queries[0]
        assert query.start_time == self.START - datetime.timedelta(hours=6)
        assert query.data_types == ["ribs"]

    def test_the_query_reaches_past_start_time_for_a_dump_landing_on_it(self):
        """The broker's interval is half-open, so asking up to `start_time`
        exactly excludes a dump at `start_time`. That is the common case when a
        window is chosen to start on a dump, and it sent every such collector
        back to its previous one."""
        broker = FakeBroker([])
        latest_rib_dumps(["rrc04"], self.START, broker=broker)
        assert broker.queries[0].end_time > self.START
        # Dumps are minute-aligned, so the overshoot cannot reach a later one.
        assert broker.queries[0].end_time - self.START < datetime.timedelta(minutes=1)


class TestBuildRibWindow:
    """`build_rib` keeps each collector's dump instant, and must keep *all* of it.

    Every entry of an MRT table dump carries the dump file's timestamp, so a
    collector's `rib_start` and `rib_end` are the same second and every RIB entry
    sits exactly on it. A filter that excluded that instant would silently build
    an empty table, which no offline test noticed until this one.
    """

    def _run(self, monkeypatch, offsets):
        quickrib, _ = make_pipeline(end_offset=1_200)
        start_ts = quickrib.start_time.timestamp()
        quickrib.rc_to_rib_start = {"rrc04": start_ts}
        quickrib.rc_to_rib_end = {"rrc04": start_ts}
        quickrib.fullfeed_only = False
        seen: list[float] = []

        class Recorder(FakeRIB):
            def update_rib(self, bgpelem):
                seen.append(bgpelem.time - start_ts)

        quickrib.rib = cast(RIBTable, Recorder())
        elements = [rib_entry_at(start_ts + off, f"10.0.{i}.0/24")
                    for i, off in enumerate(offsets)]
        monkeypatch.setattr(
            "quickrib.quickrib.BGPStream", lambda **_: FakeStream(elements)
        )
        quickrib.build_rib()
        return seen

    def test_every_entry_at_the_dump_instant_is_kept(self, monkeypatch):
        assert self._run(monkeypatch, [0, 0, 0]) == [0.0, 0.0, 0.0]

    def test_entries_outside_the_dump_instant_are_dropped(self, monkeypatch):
        assert self._run(monkeypatch, [-30, 0, 30]) == [0.0]

    def test_each_collector_is_streamed_over_its_own_dump_instant(self, monkeypatch):
        """One stream per collector, in dump order, each spanning only that
        collector's instant. A single stream from the earliest dump to the
        latest also fetched and parsed every update file in between, and any
        intermediate dump of a collector that dumps more often, to discard it."""
        quickrib, _ = make_pipeline(end_offset=1_200)
        quickrib.collectors = ["route-views.wide", "rrc04"]
        start_ts = quickrib.start_time.timestamp()
        # rrc04 dumped two hours before route-views.wide.
        quickrib.rc_to_rib_start = {"route-views.wide": start_ts, "rrc04": start_ts - 7_200}
        quickrib.rc_to_rib_end = dict(quickrib.rc_to_rib_start)
        quickrib.rc_to_ff_peers = {"route-views.wide": set(), "rrc04": set()}
        quickrib.rib = cast(RIBTable, FakeRIB())
        streams: list[dict[str, Any]] = []

        def fake_stream(**kwargs):
            streams.append(kwargs)
            return FakeStream([])

        monkeypatch.setattr("quickrib.quickrib.BGPStream", fake_stream)
        quickrib.build_rib()

        assert [s["collectors"] for s in streams] == [["rrc04"], ["route-views.wide"]]
        for kwargs in streams:
            rc = kwargs["collectors"][0]
            assert kwargs["ts_start"].timestamp() == quickrib.rc_to_rib_start[rc]
            # One second past the instant: the broker's interval is half-open.
            assert kwargs["ts_end"].timestamp() == quickrib.rc_to_rib_end[rc] + 1
            assert set(kwargs["data_types"]) == {"ribs", "updates"}


class TestReplaySchedule:
    """`replay_updates` driven over a fake stream, so only the schedule is exercised."""

    def _run(self, monkeypatch, offsets, **kwargs) -> list[float]:
        """Run `update_rib` over `offsets` seconds past start, and return the
        dump instants it produced, also as seconds past start."""
        quickrib, fake = make_pipeline(**kwargs)
        start_ts = quickrib.start_time.timestamp()
        elements = [withdrawal_at(start_ts + offset) for offset in offsets]
        monkeypatch.setattr(
            "quickrib.quickrib.BGPStream", lambda **_: FakeStream(elements)
        )
        quickrib.replay_updates()
        return [dump.timestamp() - start_ts for dump in fake.dumps]

    def test_elements_up_to_the_dump_instant_are_skipped(self, monkeypatch):
        """A1: `build_rib` already applied everything up to each collector's dump.

        The update stream starts at the *earliest* collector's dump instant, so a
        collector that dumps later would otherwise have its updates replayed a
        second time, and the ones predating its own dump applied on top of a
        newer table.
        """
        quickrib, fake = make_pipeline(end_offset=1_200)
        start_ts = quickrib.start_time.timestamp()
        # This collector dumped 60s into the stream; the stream starts at 0.
        quickrib.rc_to_rib_end = {"rrc04": start_ts + 60}
        # The peer filter is not what is under test here.
        quickrib.fullfeed_only = False
        seen: list[float] = []

        class Recorder(FakeRIB):
            def update_withdrawal(self, bgpelem):
                seen.append(bgpelem.time - start_ts)

        recorder = Recorder()
        quickrib.rib = cast(RIBTable, recorder)
        elements = [withdrawal_at(start_ts + off) for off in (0, 30, 60, 61, 120)]
        monkeypatch.setattr(
            "quickrib.quickrib.BGPStream", lambda **_: FakeStream(elements)
        )
        quickrib.replay_updates()

        # 0, 30 and 60 are at or before the dump instant; only what follows it
        # is new.
        assert seen == [61.0, 120.0]

    def test_dense_stream_dumps_every_window(self, monkeypatch):
        # `end_offset` stops just after the last message, so nothing is left for
        # the end-of-run flush and only the in-stream schedule is under test.
        dumps = self._run(monkeypatch, range(0, 1_800, 10), end_offset=1_790)
        assert dumps == [300.0, 600.0, 900.0, 1_200.0, 1_500.0]

    def test_a_gap_does_not_cost_the_windows_it_spans(self, monkeypatch):
        """The C3 regression, end to end over the loop: ten minutes of messages,
        a twenty-minute hole, then ten more. Every elapsed window is still
        reported, and the schedule survives the hole."""
        offsets = list(range(0, 600, 10)) + list(range(1_800, 2_400, 10))
        dumps = self._run(monkeypatch, offsets, end_offset=2_390)
        assert dumps == [300.0, 600.0, 900.0, 1_200.0, 1_500.0, 1_800.0, 2_100.0]

    def test_dumps_land_on_the_boundary_not_on_the_element(self, monkeypatch):
        # The trigger is a message at 307s; the window that closed is the one at
        # 300s, and that is the instant the observers are told about.
        dumps = self._run(monkeypatch, [0, 307, 613], end_offset=613)
        assert dumps == [300.0, 600.0]

    def test_windows_after_the_last_message_are_still_reported(self, monkeypatch):
        # The stream stops at 10 minutes but the run is booked to the hour, so
        # the remaining windows are complete and must not leave a hole.
        dumps = self._run(monkeypatch, range(0, 600, 10), end_offset=1_800)
        assert dumps == [300.0, 600.0, 900.0, 1_200.0, 1_500.0, 1_800.0]

    def test_an_empty_stream_still_reports_its_windows(self, monkeypatch):
        dumps = self._run(monkeypatch, [], end_offset=900)
        assert dumps == [300.0, 600.0, 900.0]


def test_non_positive_dump_res_is_rejected():
    """The schedule steps by `dump_res` until it catches up with the stream, so a
    non-positive step would not terminate."""
    start = datetime.datetime(2022, 1, 26, 4, 0, tzinfo=datetime.UTC)
    with pytest.raises(ValueError, match="dump_res must be positive"):
        QuickRIB(
            start_time=start,
            end_time=start + datetime.timedelta(hours=1),
            dump_res=datetime.timedelta(0),
            collectors=["rrc04"],
        )


# ---------------------------------------------------------------------------
# `process_path`: the other pure decision the pipeline makes per element, and
# the one that decides whether an element reaches the observers at all.
# ---------------------------------------------------------------------------


def path_elem(as_path: str | None, peer_asn: int = 64500) -> BGPElement:
    """A raw element as a parser yields it: `as-path` is still the wire string,
    and absent entirely when there is none."""
    fields: Any = {"prefix": "192.0.2.0/24"}
    if as_path is not None:
        fields["as-path"] = as_path
    return BGPElement(
        time=0.0, type="A", collector="rrc04", peer_asn=peer_asn,
        peer_address="10.0.0.1", fields=fields,
    )


class TestProcessPath:
    def test_splits_a_well_formed_path(self):
        assert process_path(path_elem("64500 64501 64502")) == ["64500", "64501", "64502"]

    def test_prepending_is_preserved(self):
        # Path length is a routing signal; collapsing it here would erase it.
        assert process_path(path_elem("64500 64501 64501 64502")) == [
            "64500", "64501", "64501", "64502",
        ]

    def test_missing_path_is_dropped(self):
        assert process_path(path_elem(None)) == []

    @pytest.mark.parametrize(
        "as_path",
        ["64500 {64501,64502}", "64500 {64501} 64502", "{64500,64501} 64502"],
    )
    def test_as_sets_are_dropped(self, as_path):
        # `path[-1]` is read as *the* origin everywhere downstream, and an
        # aggregate stands for several at once.
        assert process_path(path_elem(as_path)) == []

    def test_a_path_not_starting_at_the_peer_is_dropped(self):
        assert process_path(path_elem("64999 64501", peer_asn=64500)) == []

    def test_a_single_asn_path_is_kept(self):
        """A prefix the peer originates itself: a path of just the peer, which is
        well-formed and was previously discarded with the malformed ones.

        Rare but real (0.02% of RIB entries on the pinned windows, including
        default routes) and dropping them hid a full-feed peer's own address
        space from every observer.
        """
        assert process_path(path_elem("64500", peer_asn=64500)) == ["64500"]

    def test_a_single_asn_path_from_a_different_as_is_still_dropped(self):
        # Still has to start at the peer.
        assert process_path(path_elem("64999", peer_asn=64500)) == []


class TestConstructorNormalisation:
    """`QuickRIB(...)` applies the same time normalisation as `QuickRIBConfig`.

    A naive `start_time` used to reach `latest_rib_dumps`, where comparing it
    with the broker's aware dump times raises, and `timestamp()`, which reads
    a naive datetime in local time and shifts every dump boundary with it.
    """

    def test_naive_times_are_read_as_utc(self):
        quickrib = QuickRIB(
            start_time=datetime.datetime(2010, 9, 1, 0, 0),
            end_time=datetime.datetime(2010, 9, 1, 1, 0),
            dump_res=datetime.timedelta(minutes=5),
            collectors=["rrc04"],
        )
        assert quickrib.start_time == datetime.datetime(2010, 9, 1, 0, 0, tzinfo=datetime.UTC)
        assert quickrib.end_time.tzinfo == datetime.UTC

    def test_aware_times_are_converted_to_utc(self):
        tz = datetime.timezone(datetime.timedelta(hours=9))
        quickrib = QuickRIB(
            start_time=datetime.datetime(2010, 9, 1, 9, 0, tzinfo=tz),
            end_time=datetime.datetime(2010, 9, 1, 10, 0, tzinfo=tz),
            dump_res=datetime.timedelta(minutes=5),
            collectors=["rrc04"],
        )
        assert quickrib.start_time == datetime.datetime(2010, 9, 1, 0, 0, tzinfo=datetime.UTC)

    def test_the_cache_directory_is_created(self, tmp_path):
        """A fresh clone has no `cache/`, and the examples all point at one. The
        config used to require the directory to exist before validating."""
        cache = tmp_path / "new" / "cache"
        config = QuickRIBConfig(
            start_time=datetime.datetime(2010, 9, 1, 0, 0),
            end_time=datetime.datetime(2010, 9, 1, 1, 0),
            collectors=["rrc04"],
            dump_res=datetime.timedelta(minutes=5),
            cache_dir=cache,
        )
        assert not cache.exists()
        QuickRIB.from_config(config)
        assert cache.is_dir()

    def test_a_backwards_window_is_rejected(self):
        with pytest.raises(ValueError, match="must be after"):
            QuickRIB(
                start_time=datetime.datetime(2010, 9, 1, 1, 0),
                end_time=datetime.datetime(2010, 9, 1, 0, 0),
                dump_res=datetime.timedelta(minutes=5),
                collectors=["rrc04"],
            )


class TestConfigValidation:
    def test_naive_times_are_read_as_utc(self):
        config = QuickRIBConfig(
            start_time=datetime.datetime(2010, 9, 1, 0, 0),
            end_time=datetime.datetime(2010, 9, 1, 1, 0),
            collectors=["rrc04"],
            dump_res=datetime.timedelta(minutes=5),
        )
        assert config.start_time.tzinfo == datetime.UTC
        assert config.start_time.hour == 0

    def test_aware_times_are_converted_to_utc(self):
        tz = datetime.timezone(datetime.timedelta(hours=9))
        config = QuickRIBConfig(
            start_time=datetime.datetime(2010, 9, 1, 9, 0, tzinfo=tz),
            end_time=datetime.datetime(2010, 9, 1, 10, 0, tzinfo=tz),
            collectors=["rrc04"],
            dump_res=datetime.timedelta(minutes=5),
        )
        assert config.start_time == datetime.datetime(2010, 9, 1, 0, 0, tzinfo=datetime.UTC)

    def test_a_backwards_window_is_rejected(self):
        with pytest.raises(ValidationError, match="must be after"):
            QuickRIBConfig(
                start_time=datetime.datetime(2010, 9, 1, 1, 0),
                end_time=datetime.datetime(2010, 9, 1, 0, 0),
                collectors=["rrc04"],
                dump_res=datetime.timedelta(minutes=5),
            )

    def test_a_non_positive_dump_res_is_rejected(self):
        with pytest.raises(ValidationError, match="dump_res must be positive"):
            QuickRIBConfig(
                start_time=datetime.datetime(2010, 9, 1, 0, 0),
                end_time=datetime.datetime(2010, 9, 1, 1, 0),
                collectors=["rrc04"],
                dump_res=datetime.timedelta(0),
            )


# ---------------------------------------------------------------------------
# End-to-end tier
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_pipeline_counts_match_pinned_window(e2e_config, make_quickrib):
    quickrib = make_quickrib(e2e_config)

    tester = TesterObserver(config=e2e_config)
    quickrib.rib.attach_observer(tester)

    quickrib.run()

    assert tester.n_rib_entries["route-views.wide"] == 983438, "Number of RIBs entries does not match for route-views.wide"
    assert tester.n_updates["route-views.wide"] == 24187, "Number of Updates does not match for route-views.wide"
    assert tester.n_withdrawals["route-views.wide"] == 1939, "Number of Withdrawals does not match for route-views.wide"
    assert tester.n_announcements["route-views.wide"] == 22248, "Number of Announcements does not match for route-views.wide"

    assert tester.n_rib_entries["rrc04"] == 1962974, "Number of RIBs entries does not match for rrc04"
    assert tester.n_updates["rrc04"] == 66654, "Number of Updates does not match for rrc04"
    assert tester.n_withdrawals["rrc04"] == 6743, "Number of Withdrawals does not match for rrc04"
    assert tester.n_announcements["rrc04"] == 59911, "Number of Announcements does not match for rrc04"
