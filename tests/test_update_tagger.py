"""
Unit tests for the BLT update tagger observer (`quickrib.observers.update_tagger`).

The tagger labels BGP update messages following the BLT taxonomy from
Kitabatake, Fontugne & Esaki, "BLT: A Taxonomy and Classification Tool for
Mining BGP Update Messages". The reported (leaf) labels are:

    Change Size   : Remove Prefix, New Prefix
    AS Path       : Origin Change, Path Switching,
                    Prepending Add / Change / Remove
    Other Attrs   : Community Change, Other Change
    No Change     : Duplicate Withdrawal, Duplicate Announce

Note on coverage: "Other Change" (BLT's OTHER_ATTRIBUTE_CHANGE) is intentionally
NOT implemented. The RIB only stores `as-path` and `communities`, so a message
that changes only another attribute (next-hop, MED, ...) cannot be distinguished
from a duplicate announce. `test_other_change_is_never_emitted` documents this.

The tagger operates on the RIB node `data` dicts, not raw BGPElements:
    data      = new RIB entry  {"as-path": [...], "communities": [...], "time", ...}
    old_data  = previous RIB entry, or None for a brand new prefix
AS paths are lists of ASN strings (the post-`process_path` form used in
production), so path indexing / set() operations behave as intended.
"""


from collections import deque
from datetime import datetime
from typing import Literal

import pytest

from quickrib.elements import ParsedElement, RIBNodeData, WithdrawalElement
from quickrib.observers.update_tagger import (
    RIBTablePathHistory,
    UpdateTagFlags,
    UpdateTagger,
    UpdateTagsCounter,
)

RC = "rrc00"
PEER_ASN = 64500
PEER_IP = "10.0.0.1"
PFX = "192.0.2.0/24"
F = UpdateTagFlags


def make_elem(prefix=PFX, as_path=None, communities=None,
              elem_type: Literal["R", "A"] = "A", time=1000.0) -> ParsedElement:
    """Build a realistic BGPElement (as-path as a list of ASN strings)."""
    return ParsedElement(
        time=time,
        type=elem_type,
        collector=RC,
        peer_asn=PEER_ASN,
        peer_address=PEER_IP,
        fields={
            "prefix": prefix,
            "as-path": as_path if as_path is not None else ["64500", "64501"],
            "communities": communities if communities is not None else [],
        },
    )


def make_data(as_path, communities=None, history=None) -> RIBNodeData:
    """Build a RIB node `data` dict as stored by RIBTablePathHistory."""
    data: RIBNodeData = {
        "as-path": as_path,
        "communities": communities if communities is not None else [],
        "time": 1000.0,
    }
    if history is not None:
        data["as-path-history"] = history
    return data


def make_withdrawal(prefix, *, peer_asn=64500, peer_ip="10.0.0.1",
                    rc="rrc00", time=1000.0) -> WithdrawalElement:
    """A withdrawal names a prefix and nothing else."""
    return WithdrawalElement(
        time=time, type="W", collector=rc, peer_asn=peer_asn,
        peer_address=peer_ip, fields={"prefix": prefix},
    )


@pytest.fixture
def tagger():
    return UpdateTagger()


class AccumulatingTagsCounter(UpdateTagsCounter):
    """`UpdateTagsCounter` that never resets on `dump()`.

    The pipeline calls `dump()` every `dump_res`, and `UpdateTagsCounter.dump()`
    clears its counters. The e2e test wants whole-run totals, so this subclass
    keeps accumulating and is inspected once the run finishes.
    """

    def dump(self, ts: datetime) -> dict[str, int]:
        # deliberately does not clear, unlike the base
        return dict(self.tag_counts)


# ---------------------------------------------------------------------------
# Change Size: New Prefix / Remove Prefix
# ---------------------------------------------------------------------------

class TestChangeSize:
    def test_new_prefix(self, tagger):
        # No previous RIB entry -> the prefix is new.
        flags = tagger.update_announcement(make_elem(), make_data(["64500", "64502"]), None)
        assert flags == F.NEW_PREFIX | F.UPDATE_MESSAGE

    def test_remove_prefix(self, tagger):
        # Withdrawal for a prefix that WAS in the RIB (data is not None).
        flags = tagger.update_withdrawal(make_withdrawal(PFX), make_data(["64500", "64502"]))
        assert flags == F.REMOVE_PREFIX | F.UPDATE_MESSAGE


# ---------------------------------------------------------------------------
# No Change: Duplicate Announce / Duplicate Withdrawal
# ---------------------------------------------------------------------------

class TestNoChange:
    def test_duplicate_announce(self, tagger):
        path = ["64500", "64501", "64502"]
        flags = tagger.update_announcement(
            make_elem(as_path=path), make_data(path), make_data(path)
        )
        assert flags == F.DUPLICATE_ANNOUNCE | F.UPDATE_MESSAGE

    def test_duplicate_withdrawal(self, tagger):
        # Withdrawal for a prefix that is NOT in the RIB (data is None).
        flags = tagger.update_withdrawal(make_withdrawal(PFX), None)
        assert flags == F.DUPLICATE_WITHDRAWAL | F.UPDATE_MESSAGE


# ---------------------------------------------------------------------------
# AS Path: Origin Change / Transit Change
# ---------------------------------------------------------------------------

class TestAsPathChange:
    def test_origin_change(self, tagger):
        old = ["64500", "64501", "64510"]
        new = ["64500", "64501", "64520"]  # different last hop (origin)
        flags = tagger.update_announcement(
            make_elem(as_path=new), make_data(new), make_data(old)
        )
        assert flags == F.ORIGIN_CHANGE | F.UPDATE_MESSAGE

    def test_transit_change(self, tagger):
        old = ["64500", "64501", "64502"]
        new = ["64500", "64503", "64502"]  # different middle hop, same origin
        flags = tagger.update_announcement(
            make_elem(as_path=new), make_data(new), make_data(old)
        )
        assert flags == F.TRANSIT_CHANGE | F.UPDATE_MESSAGE

    def test_path_switching(self, tagger):
        # Flap back to a path seen two updates ago:
        # history[-2] == new_path and old_path != new_path.
        new = ["64500", "64501", "64502"]
        old = ["64500", "64503", "64502"]
        history = deque([new, old], maxlen=2)  # [-2] == new
        data = make_data(new, history=history)
        flags = tagger.update_announcement(make_elem(as_path=new), data, make_data(old))
        # Path Switching is a specialization of Transit Change (same origin),
        # so the implementation reports both.
        assert flags & F.PATH_SWITCHING
        assert flags == F.PATH_SWITCHING | F.TRANSIT_CHANGE | F.UPDATE_MESSAGE

    def test_path_switching_needs_two_history_entries(self, tagger):
        # With only one prior path, history[-2] raises IndexError -> no switch.
        new = ["64500", "64501", "64502"]
        old = ["64500", "64503", "64502"]
        data = make_data(new, history=deque([old], maxlen=2))
        flags = tagger.update_announcement(make_elem(as_path=new), data, make_data(old))
        assert not (flags & F.PATH_SWITCHING)
        assert flags & F.TRANSIT_CHANGE


# ---------------------------------------------------------------------------
# AS Path: Prepending Add / Remove / Change
# ---------------------------------------------------------------------------

class TestPrepending:
    def test_prepending_add(self, tagger):
        old = ["64500", "64501", "64502"]
        new = ["64500", "64501", "64501", "64502"]  # 64501 prepended
        flags = tagger.update_announcement(
            make_elem(as_path=new), make_data(new), make_data(old)
        )
        assert flags & F.PREPENDING_ADD
        assert flags & F.PREPENDING_CHANGE
        assert not (flags & F.PREPENDING_REMOVE)

    def test_prepending_remove(self, tagger):
        old = ["64500", "64501", "64501", "64502"]
        new = ["64500", "64501", "64502"]  # prepending removed
        flags = tagger.update_announcement(
            make_elem(as_path=new), make_data(new), make_data(old)
        )
        assert flags & F.PREPENDING_REMOVE
        assert flags & F.PREPENDING_CHANGE
        assert not (flags & F.PREPENDING_ADD)

    def test_prepending_change_same_amount(self, tagger):
        # Same amount of prepending, but on a different AS -> change only.
        old = ["64500", "64501", "64501", "64502"]
        new = ["64500", "64503", "64503", "64502"]
        flags = tagger.update_announcement(
            make_elem(as_path=new), make_data(new), make_data(old)
        )
        assert flags & F.PREPENDING_CHANGE
        assert not (flags & F.PREPENDING_ADD)
        assert not (flags & F.PREPENDING_REMOVE)


# ---------------------------------------------------------------------------
# Other Attributes: Community Change / Other Change (uncovered)
# ---------------------------------------------------------------------------

class TestOtherAttributes:
    def test_community_change(self, tagger):
        path = ["64500", "64501", "64502"]
        flags = tagger.update_announcement(
            make_elem(as_path=path, communities=["64500:2"]),
            make_data(path, communities=["64500:2"]),
            make_data(path, communities=["64500:1"]),
        )
        assert flags == F.COMMUNITY_CHANGE | F.UPDATE_MESSAGE

    def test_community_change_alongside_path_change(self, tagger):
        old = make_data(["64500", "64501", "64502"], communities=["64500:1"])
        new = make_data(["64500", "64503", "64502"], communities=["64500:2"])
        flags = tagger.update_announcement(make_elem(as_path=new["as-path"]), new, old)
        assert flags & F.COMMUNITY_CHANGE
        assert flags & F.TRANSIT_CHANGE

    def test_other_change_is_never_emitted(self, tagger):
        # BLT's "Other Change" is intentionally unimplemented: a message that
        # changes only a non-stored attribute looks identical to a duplicate
        # announce because the RIB tracks only as-path and communities.
        path = ["64500", "64501", "64502"]
        flags = tagger.update_announcement(
            make_elem(as_path=path), make_data(path), make_data(path)
        )
        assert not (flags & F.OTHER_ATTRIBUTE_CHANGE)
        assert flags & F.DUPLICATE_ANNOUNCE


# ---------------------------------------------------------------------------
# Tag name rendering
# ---------------------------------------------------------------------------

class TestTagNames:
    def test_get_tag_names_lists_active_flags(self):
        flags = F.UPDATE_MESSAGE | F.ORIGIN_CHANGE
        names = UpdateTagger._get_tag_names(flags)
        assert "ORIGIN_CHANGE" in names
        assert "UPDATE_MESSAGE" in names
        assert "NONE" not in names

    def test_get_tag_names_none(self):
        assert UpdateTagger._get_tag_names(F.NONE) == "NONE"


# ---------------------------------------------------------------------------
# UpdateTagsCounter + realistic flow through RIBTablePathHistory
# ---------------------------------------------------------------------------

class TestCounterIntegration:
    def _setup(self):
        rib = RIBTablePathHistory(history_size=2)
        counter = UpdateTagsCounter()
        rib.attach_observer(counter)
        return rib, counter

    def test_path_switching_built_from_real_flap(self):
        # Drive a realistic route flap and let the RIB build the path history.
        rib, counter = self._setup()
        path_a = ["64500", "64501", "64502"]
        path_b = ["64500", "64503", "64502"]

        rib.update_announcement(make_elem(as_path=path_a))  # New Prefix
        rib.update_announcement(make_elem(as_path=path_b))  # Transit Change
        rib.update_announcement(make_elem(as_path=path_a))  # Path Switching

        assert counter.tag_counts["NEW_PREFIX"] == 1
        assert counter.tag_counts["PATH_SWITCHING"] == 1
        assert counter.tag_counts["TRANSIT_CHANGE"] == 2

    def test_remove_and_duplicate_withdrawal_counts(self):
        rib, counter = self._setup()
        path = ["64500", "64501", "64502"]

        rib.update_announcement(make_elem(as_path=path))          # New Prefix
        rib.update_withdrawal(make_withdrawal(PFX))           # Remove Prefix
        rib.update_withdrawal(make_withdrawal(PFX))           # Duplicate Withdrawal

        assert counter.tag_counts["NEW_PREFIX"] == 1
        assert counter.tag_counts["REMOVE_PREFIX"] == 1
        assert counter.tag_counts["DUPLICATE_WITHDRAWAL"] == 1

    def test_dump_returns_counts_and_resets(self):
        rib, counter = self._setup()
        rib.update_announcement(make_elem(as_path=["64500", "64502"]))

        counts = counter.dump(ts=datetime(2020, 1, 1))
        assert counts["NEW_PREFIX"] == 1
        # Counter is cleared after a dump so the next window starts fresh.
        assert counter.tag_counts == {}


# ---------------------------------------------------------------------------
# End-to-end tier
# ---------------------------------------------------------------------------
#
# The tests above feed the tagger hand-built paths to pin its classification
# logic leaf by leaf. This class runs the real `QuickRIB` pipeline over the
# pinned window from `conftest`, with an `UpdateTagsCounter` attached to a
# `RIBTablePathHistory`, and asserts on the BLT tag totals it accumulates.
# Marked `e2e` and deselected by default; run with `uv run pytest -m e2e`
# (or `-m ""`).

@pytest.mark.e2e
class TestUpdateTaggerE2E:
    # Accumulated tag counts after `QuickRIB.run()` over the pinned window.
    EXPECTED_TAG_COUNTS = {
        "UPDATE_MESSAGE": 90841,
        "REMOVE_PREFIX": 8662,
        "NEW_PREFIX": 7791,
        "ORIGIN_CHANGE": 475,
        "TRANSIT_CHANGE": 55524,
        "PATH_SWITCHING": 18588,
        "PREPENDING_ADD": 5942,
        "PREPENDING_CHANGE": 23041,
        "PREPENDING_REMOVE": 5808,
        "COMMUNITY_CHANGE": 3449,
        "DUPLICATE_WITHDRAWAL": 20,
        "DUPLICATE_ANNOUNCE": 17726,
    }

    @pytest.fixture(scope="class")
    def tag_counts(self, e2e_config, make_quickrib):
        quickrib = make_quickrib(e2e_config, rib_cls=RIBTablePathHistory)
        counter = AccumulatingTagsCounter()
        quickrib.rib.attach_observer(counter)
        quickrib.run()
        return dict(counter.tag_counts)

    def test_tag_counts_match_pinned_window(self, tag_counts):
        assert tag_counts == self.EXPECTED_TAG_COUNTS

    def test_path_switching_is_a_subset_of_transit_change(self, tag_counts):
        # Path Switching is a specialization of Transit Change (same origin), so
        # the tagger always emits TRANSIT_CHANGE alongside it.
        assert tag_counts["PATH_SWITCHING"] <= tag_counts["TRANSIT_CHANGE"]

    def test_prepending_add_and_remove_sum_into_prepending_change(self, tag_counts):
        # Every ADD or REMOVE also sets PREPENDING_CHANGE, plus same-amount
        # changes that set only PREPENDING_CHANGE.
        assert (
            tag_counts["PREPENDING_ADD"] + tag_counts["PREPENDING_REMOVE"]
            <= tag_counts["PREPENDING_CHANGE"]
        )
