"""
Unit tests for `quickrib.rib_table.RIBTable`.

These tests focus on the core RIB behaviour and deliberately ignore the
observer machinery (attach/detach/notify). They exercise:

- building the RIB from RIB-dump entries (`update_rib`)
- applying updates (`update_announcement`, `update_withdrawal`)
- dumping / printing information (`dump`, `__iter__`, `get_bgpelem`,
  `get_peer_table`)
- comparing two RIB tables (`compare`)

`BGPElement` comes from `pybgpflux` and is a namedtuple:
    ParsedElement(time, type, collector, peer_asn, peer_address, fields)
where `fields` is a dict containing e.g. `prefix`, `as-path`, `communities`.
"""

import datetime
from typing import Literal

import pytest

from quickrib.elements import ParsedElement, WithdrawalElement
from quickrib.observers.observer import Observer
from quickrib.rib_table import RIBTable


def make_withdrawal(prefix, *, peer_asn=64500, peer_ip="10.0.0.1",
                    rc="rrc00", time=1000.0) -> WithdrawalElement:
    """A withdrawal names a prefix and nothing else."""
    return WithdrawalElement(
        time=time, type="W", collector=rc, peer_asn=peer_asn,
        peer_address=peer_ip, fields={"prefix": prefix},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RC = "rrc00"
PEER_ASN = 64500
PEER_IP = "10.0.0.1"


def make_elem(
    prefix,
    as_path=None,
    communities=None,
    *,
    type: Literal["R", "A"] = "A",
    collector=RC,
    peer_asn=PEER_ASN,
    peer_address=PEER_IP,
    time=1000.0,
):
    """Build a ParsedElement for the given prefix/path."""
    if as_path is None:
        as_path = ["64500", "64501"]
    if communities is None:
        communities = []
    return ParsedElement(
        time=time,
        type=type,
        collector=collector,
        peer_asn=peer_asn,
        peer_address=peer_address,
        fields={
            "prefix": prefix,
            "as-path": as_path,
            "communities": communities,
        },
    )


@pytest.fixture
def rib():
    return RIBTable()


# ---------------------------------------------------------------------------
# Build (update_rib)
# ---------------------------------------------------------------------------

class TestBuild:
    def test_update_rib_adds_prefix(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 64501"))

        node = rib.get_peer_table(RC, PEER_ASN, PEER_IP).search_exact("192.0.2.0/24")
        assert node is not None
        assert node.data["as-path"] == "64500 64501"
        assert node.data["communities"] == []
        assert node.data["time"] == 1000.0

    def test_update_rib_stores_communities_and_time(self, rib):
        rib.update_rib(
            make_elem(
                "198.51.100.0/24",
                as_path="64500 64502",
                communities=["64500:100"],
                time=1234.5,
            )
        )

        node = rib.get_peer_table(RC, PEER_ASN, PEER_IP).search_exact("198.51.100.0/24")
        assert node.data["communities"] == ["64500:100"]
        assert node.data["time"] == 1234.5

    def test_build_multiple_prefixes_same_peer(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24"))
        rib.update_rib(make_elem("198.51.100.0/24"))

        peer_table = rib.get_peer_table(RC, PEER_ASN, PEER_IP)
        assert set(peer_table.prefixes()) == {"192.0.2.0/24", "198.51.100.0/24"}

    def test_build_separates_peers_and_collectors(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24"))
        rib.update_rib(
            make_elem("192.0.2.0/24", collector="rrc01", peer_asn=64510, peer_address="10.0.0.2")
        )

        assert len(rib.data) == 2
        assert len(rib.get_peer_table(RC, PEER_ASN, PEER_IP).prefixes()) == 1
        assert len(rib.get_peer_table("rrc01", 64510, "10.0.0.2").prefixes()) == 1

    def test_get_peer_table_creates_empty_table(self, rib):
        table = rib.get_peer_table(RC, PEER_ASN, PEER_IP)
        assert table.prefixes() == []


# ---------------------------------------------------------------------------
# Update announcement
# ---------------------------------------------------------------------------

class TestUpdateAnnouncement:
    def test_announcement_of_new_prefix(self, rib):
        rib.update_announcement(make_elem("192.0.2.0/24", as_path="64500 64501"))

        node = rib.get_peer_table(RC, PEER_ASN, PEER_IP).search_exact("192.0.2.0/24")
        assert node is not None
        assert node.data["as-path"] == "64500 64501"

    def test_announcement_replaces_existing_path(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 64501"))
        rib.update_announcement(
            make_elem("192.0.2.0/24", as_path="64500 64999", time=2000.0)
        )

        node = rib.get_peer_table(RC, PEER_ASN, PEER_IP).search_exact("192.0.2.0/24")
        assert node.data["as-path"] == "64500 64999"
        assert node.data["time"] == 2000.0

    def test_announcement_updates_communities(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24", communities=["64500:1"]))
        rib.update_announcement(
            make_elem("192.0.2.0/24", communities=["64500:2", "64500:3"])
        )

        node = rib.get_peer_table(RC, PEER_ASN, PEER_IP).search_exact("192.0.2.0/24")
        assert node.data["communities"] == ["64500:2", "64500:3"]

    def test_announcement_does_not_affect_other_prefixes(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 1"))
        rib.update_announcement(make_elem("198.51.100.0/24", as_path="64500 2"))

        peer_table = rib.get_peer_table(RC, PEER_ASN, PEER_IP)
        assert peer_table.search_exact("192.0.2.0/24").data["as-path"] == "64500 1"
        assert peer_table.search_exact("198.51.100.0/24").data["as-path"] == "64500 2"


# ---------------------------------------------------------------------------
# Update withdrawal
# ---------------------------------------------------------------------------

class TestUpdateWithdrawal:
    def test_withdrawal_removes_prefix(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24"))
        rib.update_withdrawal(make_withdrawal("192.0.2.0/24"))

        node = rib.get_peer_table(RC, PEER_ASN, PEER_IP).search_exact("192.0.2.0/24")
        assert node is None

    def test_withdrawal_of_missing_prefix_is_noop(self, rib):
        # Should not raise even though the prefix was never announced.
        rib.update_withdrawal(make_withdrawal("203.0.113.0/24"))

        assert rib.get_peer_table(RC, PEER_ASN, PEER_IP).prefixes() == []

    def test_withdrawal_only_removes_target_prefix(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24"))
        rib.update_rib(make_elem("198.51.100.0/24"))
        rib.update_withdrawal(make_withdrawal("192.0.2.0/24"))

        peer_table = rib.get_peer_table(RC, PEER_ASN, PEER_IP)
        assert peer_table.prefixes() == ["198.51.100.0/24"]

    def test_announce_withdraw_announce_cycle(self, rib):
        rib.update_announcement(make_elem("192.0.2.0/24", as_path="64500 1"))
        rib.update_withdrawal(make_withdrawal("192.0.2.0/24"))
        rib.update_announcement(make_elem("192.0.2.0/24", as_path="64500 2"))

        node = rib.get_peer_table(RC, PEER_ASN, PEER_IP).search_exact("192.0.2.0/24")
        assert node is not None
        assert node.data["as-path"] == "64500 2"

    def test_observers_see_the_withdrawn_data(self, rib):
        """The node is deleted before the notification, but its data survives it."""
        seen = []

        class Recorder(Observer):
            def update_withdrawal(self, bgpelem, data):
                seen.append(data)

        rib.attach_observer(Recorder())
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 64501"))
        rib.update_withdrawal(make_withdrawal("192.0.2.0/24"))

        assert len(seen) == 1
        assert seen[0] is not None
        assert seen[0]["as-path"] == "64500 64501"

    def test_a_raising_observer_does_not_leave_the_prefix_in_the_rib(self, rib):
        """Regression: the delete used to run *after* the notification loop, so an
        observer raising left the withdrawn prefix in the table for the rest of
        the run, and the pipeline swallowed the exception."""

        class Broken(Observer):
            def update_withdrawal(self, bgpelem, data):
                raise RuntimeError("observer is broken")

        rib.attach_observer(Broken())
        rib.update_rib(make_elem("192.0.2.0/24"))

        with pytest.raises(RuntimeError, match="observer is broken"):
            rib.update_withdrawal(make_withdrawal("192.0.2.0/24"))

        peer_table = rib.get_peer_table(RC, PEER_ASN, PEER_IP)
        assert peer_table.search_exact("192.0.2.0/24") is None


    def test_withdrawal_from_an_unknown_peer_creates_no_table(self, rib):
        """`data` is a defaultdict, so indexing it for the withdrawing peer would
        leave an empty table behind, and the peer would be counted from then on."""
        rib.update_withdrawal(make_withdrawal("203.0.113.0/24", peer_asn=64999))

        assert rib.peer_table(RC, 64999, PEER_IP) is None
        assert RC not in rib.data


# ---------------------------------------------------------------------------
# Observer lifecycle
# ---------------------------------------------------------------------------

class TestObserverNotifications:
    class Recorder(Observer):
        def __init__(self, name):
            super().__init__(name)
            self.seen: list[str] = []

        def update_rib(self, bgpelem):
            self.seen.append("rib")

        def update_announcement(self, bgpelem, data, old_data):
            self.seen.append("announcement")

        def update_withdrawal(self, bgpelem, data):
            self.seen.append("withdrawal")

        def dump(self, ts):
            self.seen.append("dump")

    def test_update_lists_order_the_update_hooks_only(self, rib):
        """An observer installed through one update list still receives
        `update_rib` and `dump`, like `set_rib` and `set_full_feed_peers`."""
        announce_only = self.Recorder("a")
        withdraw_only = self.Recorder("w")
        rib.set_observers_announcement([announce_only])
        rib.set_observers_withdraw([withdraw_only])

        rib.update_rib(make_elem("192.0.2.0/24"))
        rib.update_announcement(make_elem("198.51.100.0/24"))
        rib.update_withdrawal(make_withdrawal("192.0.2.0/24"))
        rib.dump(datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))

        assert announce_only.seen == ["rib", "announcement", "dump"]
        assert withdraw_only.seen == ["rib", "withdrawal", "dump"]

    def test_attach_order_is_notification_order(self, rib):
        order = []

        class Tagged(Observer):
            def __init__(self, tag):
                super().__init__(tag)

            def update_rib(self, bgpelem):
                order.append(self.name)

        for tag in ("first", "second", "third"):
            rib.attach_observer(Tagged(tag))
        rib.update_rib(make_elem("192.0.2.0/24"))
        assert order == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Dump / print information
# ---------------------------------------------------------------------------

class TestDumpAndInfo:
    def test_dump_empty_rib_does_not_raise(self, rib):
        rib.dump(datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))

    def test_dump_populated_rib_does_not_raise(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24"))
        rib.update_rib(make_elem("198.51.100.0/24"))
        rib.dump(datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))

    def test_iter_yields_all_entries(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 1"))
        rib.update_rib(make_elem("198.51.100.0/24", as_path="64500 2"))

        entries = list(rib)
        assert len(entries) == 2

        prefixes = {pfx for (_, _, pfx, _) in entries}
        assert prefixes == {"192.0.2.0/24", "198.51.100.0/24"}

        for rc, peer, _, _ in entries:
            assert rc == RC
            assert peer == (PEER_ASN, PEER_IP)

    def test_get_bgpelem_reconstructs_element(self, rib):
        rib.update_rib(
            make_elem("192.0.2.0/24", as_path="64500 64501", time=1500.0)
        )

        elem = rib.get_bgpelem(RC, PEER_ASN, PEER_IP, "192.0.2.0/24")
        assert isinstance(elem, ParsedElement)
        assert elem.type == "R"
        assert elem.collector == RC
        assert elem.peer_asn == PEER_ASN
        assert elem.peer_address == PEER_IP
        assert elem.time == 1500.0
        assert elem.fields["prefix"] == "192.0.2.0/24"
        assert elem.fields["as-path"] == "64500 64501"

    def test_iter_bgpelems_yields_elements_for_all_prefixes(self, rib):
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 1"))
        rib.update_rib(make_elem("198.51.100.0/24", as_path="64500 2"))

        elems = list(rib.iter_bgpelems())
        assert len(elems) == 2
        assert all(isinstance(e, ParsedElement) for e in elems)
        assert {e.fields["prefix"] for e in elems} == {
            "192.0.2.0/24",
            "198.51.100.0/24",
        }


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

class TestCompare:
    def test_compare_identical_ribs(self, rib):
        other = RIBTable()
        for r in (rib, other):
            r.update_rib(make_elem("192.0.2.0/24", as_path="64500 1"))
            r.update_rib(make_elem("198.51.100.0/24", as_path="64500 2"))

        # No reconstruction error expected; must not raise.
        rib.compare(other)

    def test_compare_detects_missing_and_extra_prefixes(self, rib):
        other = RIBTable()
        # rib has an extra prefix, other has a different extra prefix.
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 1"))
        rib.update_rib(make_elem("203.0.113.0/24", as_path="64500 3"))
        other.update_rib(make_elem("192.0.2.0/24", as_path="64500 1"))
        other.update_rib(make_elem("198.51.100.0/24", as_path="64500 2"))

        # compare() logs differences rather than returning them; ensure no raise.
        rib.compare(other)

    def test_compare_detects_modified_paths(self, rib):
        other = RIBTable()
        rib.update_rib(make_elem("192.0.2.0/24", as_path="64500 1"))
        other.update_rib(make_elem("192.0.2.0/24", as_path="64500 999"))

        rib.compare(other)

    def test_compare_skips_peers_absent_in_ground_truth(self, rib):
        other = RIBTable()
        # only rib has this peer; other has nothing -> peer skipped, no raise
        rib.update_rib(make_elem("192.0.2.0/24"))

        rib.compare(other)


# ---------------------------------------------------------------------------
# End-to-end tier
# ---------------------------------------------------------------------------
#
# The tests above drive `RIBTable` with hand-built elements. This class runs the
# real `QuickRIB` pipeline over the pinned window from `conftest` and asserts on
# the reconstructed `RIBTable`: which collectors and full-feed peers survive, how
# many prefixes each peer ends up with, and the total entry count. Marked `e2e`
# and deselected by default; run with `uv run pytest -m e2e` (or `-m ""`).

@pytest.mark.e2e
class TestReconstructionE2E:
    # Reconstructed RIB state after `QuickRIB.run()` over the pinned window.
    EXPECTED_PEER_PREFIXES = {
        "route-views.wide": {
            (2497, "202.249.2.169"): 324599,
            (4777, "202.249.2.20"): 329591,
            (7500, "202.249.2.86"): 329042,
        },
        "rrc04": {
            (513, "192.65.185.3"): 336807,
            (12350, "192.65.185.157"): 324867,
            (20932, "192.65.185.142"): 323534,
            (25091, "192.65.185.244"): 324635,
            (29222, "192.65.185.140"): 325987,
            (35054, "192.65.185.243"): 326479,
        },
    }
    EXPECTED_TOTAL_ENTRIES = 2945541

    @pytest.fixture(scope="class")
    def reconstructed(self, e2e_config, make_quickrib):
        quickrib = make_quickrib(e2e_config)
        quickrib.run()
        return quickrib.rib

    def test_collectors_present(self, reconstructed):
        assert set(reconstructed.data) == set(self.EXPECTED_PEER_PREFIXES)

    def test_fullfeed_peers_per_collector(self, reconstructed):
        got = {rc: set(peers) for rc, peers in reconstructed.data.items()}
        expected = {rc: set(peers) for rc, peers in self.EXPECTED_PEER_PREFIXES.items()}
        assert got == expected

    def test_prefix_count_per_peer(self, reconstructed):
        got = {
            rc: {peer: len(table.prefixes()) for peer, table in peers.items()}
            for rc, peers in reconstructed.data.items()
        }
        assert got == self.EXPECTED_PEER_PREFIXES

    def test_total_entry_count(self, reconstructed):
        assert sum(1 for _ in reconstructed) == self.EXPECTED_TOTAL_ENTRIES

    def test_iter_yields_the_standalone_entry_shape(self, reconstructed):
        # Same (rc, peer, prefix, data) shape the standalone tests rely on.
        rc, peer, prefix, data = next(iter(reconstructed))
        assert isinstance(rc, str)
        assert isinstance(peer, tuple) and len(peer) == 2
        assert {"as-path", "communities", "time"} <= set(data)
