"""Tests for AS hegemony.

* The incremental :class:`~quickrib.observers.hegemony.HegemonyObserver`
  (``deaggregate`` on/off).
* The post-hoc :class:`~quickrib.observers.hegemony.RibHegemonyObserver` and the
  :func:`~quickrib.observers.hegemony.node_hegemony` /
  :func:`~quickrib.observers.hegemony.edge_hegemony` functions it wraps.

The synthetic topology is the figure from Fontugne, Shah and Aben,
"The (thin) Bridges of AS Connectivity" (PAM 2018), reproduced in
``examples/hegemony.py``: ``Transit`` / ``Regional{1..4}`` / ``Stub{1..8}``
with ``Stub1`` / ``Stub3`` / ``Stub5`` as vantage points. Every hegemony backend
must agree on it, and ``HegemonyObserver(deaggregate=True)`` must match the
post-hoc computation exactly.

Two tiers (see AGENTS.md): standalone (no marker) and ``@pytest.mark.e2e``.
"""

import datetime
import random
from collections import defaultdict
from itertools import pairwise
from typing import Literal

import networkx as nx
import pytest
from scipy import stats

from quickrib.elements import ParsedElement, RIBNodeData, WithdrawalElement
from quickrib.observers.hegemony import (
    HegemonyObserver,
    RibHegemonyObserver,
    _trim_across_vps,
    edge_hegemony,
    node_hegemony,
    resize_list,
    trim_mean,
)
from quickrib.rib_table import RIBTable

TS = datetime.datetime(2010, 9, 1, tzinfo=datetime.UTC)
VP = ("rrc00", 64500, "10.0.0.1")


# --------------------------------------------------------------------- helpers


def make_elem(prefix, as_path, *, peer_asn=64500, peer_ip="10.0.0.1",
              rc="rrc00", etype: Literal["R", "A"] = "A", time=1000.0) -> ParsedElement:
    return ParsedElement(
        time=time,
        type=etype,
        collector=rc,
        peer_asn=peer_asn,
        peer_address=peer_ip,
        fields={
            "prefix": prefix,
            "as-path": [str(a) for a in as_path],
            "communities": [],
        },
    )


def make_withdrawal(prefix, *, peer_asn=64500, peer_ip="10.0.0.1",
                    rc="rrc00", time=1000.0) -> WithdrawalElement:
    return WithdrawalElement(
        time=time, type="W", collector=rc, peer_asn=peer_asn,
        peer_address=peer_ip, fields={"prefix": prefix},
    )


def make_data(as_path, *, communities=None, time=1000.0) -> RIBNodeData:
    return {
        "as-path": [str(a) for a in as_path],
        "communities": communities or [],
        "time": time,
    }


def canon(u, v):
    def key(x):
        try:
            return (0, int(x))
        except ValueError:
            return (1, x)
    return (u, v) if key(u) < key(v) else (v, u)


def assert_graphs_close(g1, g2, rel=1e-9):
    assert g1.to_edgelist() == g2.to_edgelist()
    for edge, w in g1.to_weightfn().items():
        assert w == pytest.approx(g2.get_weight(edge), rel=rel), edge


# ------------------------------------------------------- PAM synthetic topology


PAM_EDGES = [
    ("Stub1", "Regional1"), ("Stub2", "Regional1"),
    ("Stub3", "Regional2"), ("Stub4", "Regional2"),
    ("Stub5", "Regional3"), ("Stub6", "Regional3"),
    ("Stub7", "Regional4"), ("Stub8", "Regional4"),
    ("Regional1", "Transit"), ("Regional2", "Transit"),
    ("Regional3", "Transit"), ("Regional4", "Transit"),
]
PAM_VPS = ["Stub1", "Stub3", "Stub5"]
PAM_ALPHA = 0.4  # matches examples/hegemony.py


def pam_graph() -> nx.Graph:
    g = nx.Graph()
    g.add_edges_from(PAM_EDGES)
    return g


def vp_paths(g: nx.Graph, source: str) -> list[list[str]]:
    return list(
        nx.all_simple_paths(g, source=source, target=[n for n in g.nodes() if n != source])
    )


def example_node_betweenness(paths):
    d = defaultdict(float)
    inc = 1.0 / len(paths)
    for path in paths:
        for node in path:
            d[node] += inc
    return d


def example_edge_betweenness(paths):
    d = defaultdict(float)
    inc = 1.0 / len(paths)
    for path in paths:
        for u, v in pairwise(path):
            d[(u, v)] += inc
            d[(v, u)] += inc
    return d


@pytest.fixture
def pam():
    """A RIBTable populated with the PAM topology (each AS originates one
    disjoint /24, each VP has one route per destination), plus incremental
    observers fed the same stream."""
    g = pam_graph()
    prefixes = {n: f"10.{i}.0.0/24" for i, n in enumerate(sorted(g.nodes()))}

    rib = RIBTable()
    incr_plain = HegemonyObserver(deaggregate=False, alpha=PAM_ALPHA)
    incr_deagg = HegemonyObserver(deaggregate=True, alpha=PAM_ALPHA)
    rib.attach_observer(incr_plain)
    rib.attach_observer(incr_deagg)

    for vp_i, source in enumerate(PAM_VPS):
        for path in vp_paths(g, source):
            dest = path[-1]
            rib.update_rib(
                make_elem(prefixes[dest], path, peer_asn=1000 + vp_i, peer_ip=f"10.0.0.{vp_i}")
            )

    rib_obs = RibHegemonyObserver(rib=rib, alpha=PAM_ALPHA)
    return g, rib, incr_plain, incr_deagg, rib_obs


class TestPamTopology:
    def test_edge_hegemony_matches_example(self, pam):
        g, rib, _, _, _ = pam
        got = edge_hegemony(rib.data, alpha=PAM_ALPHA)

        vp_ebc = [example_edge_betweenness(vp_paths(g, s)) for s in PAM_VPS]
        expected = {
            e: stats.trim_mean([vp_ebc[0][e], vp_ebc[1][e], vp_ebc[2][e]], proportiontocut=PAM_ALPHA)
            for e in g.edges()
        }

        assert len(got) == g.number_of_edges()
        for (u, v), val in expected.items():
            assert got[canon(u, v)] == pytest.approx(val, rel=1e-9), f"{u}-{v}"

    def test_node_hegemony_matches_example(self, pam):
        g, rib, _, _, _ = pam
        got = node_hegemony(rib.data, alpha=PAM_ALPHA)

        vp_nbc = [example_node_betweenness(vp_paths(g, s)) for s in PAM_VPS]
        expected = {
            n: stats.trim_mean([vp_nbc[0][n], vp_nbc[1][n], vp_nbc[2][n]], proportiontocut=PAM_ALPHA)
            for n in g.nodes()
        }
        for node, val in expected.items():
            assert got.get(node, 0.0) == pytest.approx(val, rel=1e-9), node

    def test_all_backends_agree(self, pam):
        """No prefix nesting -> incremental (both modes), the RIB-dump observer
        and edge_hegemony() all produce the same graph."""
        g, rib, incr_plain, incr_deagg, rib_obs = pam
        g_plain = incr_plain.dump(TS)
        g_deagg = incr_deagg.dump(TS)
        g_rib = rib_obs.dump(TS)

        assert_graphs_close(g_plain, g_deagg)
        assert_graphs_close(g_deagg, g_rib)

        heg = edge_hegemony(rib.data, alpha=PAM_ALPHA)
        for (u, v), h in heg.items():
            assert g_rib.get_weight((u, v)) == pytest.approx(h, rel=1e-12)


class TestTrimmedMean:
    """`trim_mean` used to be a thin wrapper over `scipy.stats.trim_mean`, called
    once per edge per dump. It is now computed directly, and `_trim_across_vps`
    does the whole graph in one sorted array, so scipy stays in the test group
    as the reference both are pinned against."""

    @staticmethod
    def _reference(data, alpha, force_trim=True):
        """The scipy implementation this replaced, verbatim."""
        peer_share = 1.0 / len(data)
        if force_trim and peer_share > alpha and len(data) > 2:
            return float(stats.trim_mean(data, peer_share))
        return float(stats.trim_mean(data, alpha))

    @pytest.mark.parametrize("alpha", [0.0, 0.1, 0.2, 0.3])
    @pytest.mark.parametrize("n", [1, 2, 3, 5, 9, 24])
    def test_matches_scipy(self, n, alpha):
        rng = random.Random((n, alpha).__hash__())
        data = [rng.random() for _ in range(n)]
        assert trim_mean(data, alpha) == pytest.approx(
            self._reference(data, alpha), abs=1e-12
        )

    def test_trims_the_extremes_not_the_ends_of_the_input(self):
        # Unsorted input, and the outlier is in the middle: it is still cut.
        assert trim_mean([1.0, 1.0, 99.0, 1.0, 1.0], 0.2) == pytest.approx(1.0)

    def test_below_three_samples_nothing_is_trimmed(self):
        assert trim_mean([1.0, 3.0], 0.2) == pytest.approx(2.0)
        assert trim_mean([5.0], 0.2) == pytest.approx(5.0)

    @pytest.mark.parametrize("seed", range(4))
    def test_vectorised_matches_the_scalar_reference(self, seed):
        """What the dump path actually calls, against one scipy call per key."""
        rng = random.Random(seed)
        n_vps = rng.randint(1, 25)
        alpha = rng.choice([0.0, 0.1, 0.2, 0.3])
        per_vp = {
            f"k{i}": [rng.random() for _ in range(rng.randint(1, n_vps))]
            for i in range(20)
        }
        got = _trim_across_vps(per_vp, n_vps, alpha)
        for key, scores in per_vp.items():
            padded = resize_list(list(scores), n_vps)
            assert got[key] == pytest.approx(self._reference(padded, alpha), abs=1e-12)

    def test_zero_padding_is_what_an_unseen_vp_contributes(self):
        # A key seen by one VP out of two is averaged over two, not one. Two
        # samples are below the trimming floor, so nothing is cut.
        assert _trim_across_vps({"e": [1.0]}, 2, 0.0)["e"] == pytest.approx(0.5)

    def test_a_lone_vantage_point_is_trimmed_away(self):
        """What `force_trim` buys: above two VPs, an edge only one of them sees
        is cut as an outlier rather than being credited a fraction of its score.
        This is why a single peer cannot carry an edge into the graph."""
        assert _trim_across_vps({"e": [1.0]}, 4, 0.0)["e"] == pytest.approx(0.0)
        # Seen by three of four, it survives.
        assert _trim_across_vps({"e": [1.0, 1.0, 1.0]}, 4, 0.0)["e"] > 0.0

    def test_no_keys_and_no_vps_are_handled(self):
        assert _trim_across_vps({}, 5, 0.2) == {}
        assert _trim_across_vps({"e": [1.0]}, 0, 0.2) == {"e": 0.0}


class TestDeaggregation:
    def _setup(self, alpha=0.0):
        rib = RIBTable()
        plain = HegemonyObserver(deaggregate=False, alpha=alpha)
        deagg = HegemonyObserver(deaggregate=True, alpha=alpha)
        rib.attach_observer(plain)
        rib.attach_observer(deagg)
        # one VP: a /16 and a nested /24 taking different transit
        rib.update_rib(make_elem("10.0.0.0/16", ["A", "B", "C"], peer_asn=1, peer_ip="1"))
        rib.update_rib(make_elem("10.0.1.0/24", ["A", "B", "D"], peer_asn=1, peer_ip="1"))
        rib_obs = RibHegemonyObserver(rib=rib, alpha=alpha)
        return plain.dump(TS), deagg.dump(TS), rib_obs.dump(TS)

    def test_deaggregate_matches_rib_dump(self):
        _, g_deagg, g_rib = self._setup()
        assert_graphs_close(g_deagg, g_rib)

    def test_a_fully_covered_prefix_contributes_no_edge_either_way(self):
        """The one case where the two backends could disagree on *membership*.

        In deaggregate mode a prefix entirely covered by its own more-specifics
        has an effective size of zero. The incremental observer never stores a
        zero weight, so the edges unique to that prefix's path are simply absent;
        the post-hoc walk records them at weight zero. Both must end up dropping
        them, which is what `min_hegemony`'s exclusive cut at zero does, and is
        why a non-zero cut used to hide this.
        """
        rib = RIBTable()
        deagg = HegemonyObserver(deaggregate=True, alpha=0.0)
        rib.attach_observer(deagg)
        # A /16 whose space is entirely taken by two /17s on a different path.
        rib.update_rib(make_elem("10.0.0.0/16", ["A", "B", "COVERED"], peer_asn=1, peer_ip="1"))
        rib.update_rib(make_elem("10.0.0.0/17", ["A", "B", "REAL"], peer_asn=1, peer_ip="1"))
        rib.update_rib(make_elem("10.0.128.0/17", ["A", "B", "REAL"], peer_asn=1, peer_ip="1"))

        g_deagg = deagg.dump(TS)
        g_rib = RibHegemonyObserver(rib=rib, alpha=0.0).dump(TS)
        assert g_deagg.to_edgelist() == g_rib.to_edgelist()
        # The covered prefix's own link carries nothing and is in neither graph.
        assert ("B", "COVERED") not in g_deagg.to_edgelist()
        assert ("B", "REAL") in g_deagg.to_edgelist()

    def test_plain_differs_from_rib_dump(self):
        g_plain, _, g_rib = self._setup()
        # B-C carries only the /16 -> plain over-weights it (counts the /24's
        # space too), deaggregated does not
        assert g_plain.get_weight(("B", "C")) != pytest.approx(
            g_rib.get_weight(("B", "C"))
        )


# =============================================================
# incremental HegemonyObserver: hand-computed unit tests
# =============================================================


class TestIncrementalHegemonyPlain:
    def test_single_rib_entry(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        assert dict(obs.edge_paths_count[VP]) == {("2", "64500"): 256.0, ("2", "3"): 256.0}
        assert obs.num_total_paths[VP] == 256.0

    def test_block_size_by_prefixlen(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("10.0.0.0/22", [64500, 2]))
        assert obs.num_total_paths[VP] == 1024.0
        obs.update_rib(make_elem("2001:db8::/32", [64500, 2]))
        assert obs.num_total_paths[VP] == 1024.0 + 2 ** 96

    def test_prepending_collapsed(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 2, 2, 3]))
        assert set(obs.edge_paths_count[VP]) == {("2", "64500"), ("2", "3")}

    def test_edge_canonicalisation_across_peers(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 3, 1], peer_asn=64500, peer_ip="a"))
        obs.update_rib(make_elem("198.51.100.0/24", [64501, 1, 3], peer_asn=64501, peer_ip="b"))
        assert ("1", "3") in obs.edge_paths_count[("rrc00", 64500, "a")]
        assert ("1", "3") in obs.edge_paths_count[("rrc00", 64501, "b")]

    def test_withdrawal_reverses_add(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        obs.update_withdrawal(make_withdrawal("192.0.2.0/24"),
                              make_data([64500, 2, 3]))
        assert dict(obs.edge_paths_count[VP]) == {}
        assert obs.num_total_paths[VP] == 0.0

    def test_withdrawal_none_is_noop(self):
        obs = HegemonyObserver()
        obs.update_withdrawal(make_withdrawal("192.0.2.0/24"), None)
        assert dict(obs.edge_paths_count) == {}

    def test_announcement_replaces_path(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        obs.update_announcement(
            make_elem("192.0.2.0/24", [64500, 4, 3]),
            make_data([64500, 4, 3]),
            make_data([64500, 2, 3]),
        )
        assert dict(obs.edge_paths_count[VP]) == {
            ("4", "64500"): 256.0,
            ("3", "4"): 256.0,
        }
        assert obs.num_total_paths[VP] == 256.0

    def test_duplicate_announce_is_noop(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        before = dict(obs.edge_paths_count[VP])
        obs.update_announcement(
            make_elem("192.0.2.0/24", [64500, 2, 3]),
            make_data([64500, 2, 3]),
            make_data([64500, 2, 3]),
        )
        assert dict(obs.edge_paths_count[VP]) == before

    def test_dump_returns_bidirectional_graph(self):
        obs = HegemonyObserver(alpha=0.0)
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        graph = obs.dump(TS)
        assert graph.get_weight(("2", "3")) == pytest.approx(1.0)
        assert graph.get_weight(("3", "2")) == pytest.approx(1.0)

    def test_dump_memoised(self):
        obs = HegemonyObserver()
        obs.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        g1 = obs.dump(TS)
        assert obs.dump(TS) is g1
        assert obs.dump(TS + datetime.timedelta(minutes=5)) is not g1


class TestIncrementalHegemonyDeaggregate:
    def test_nested_prefix_transfers_space(self):
        obs = HegemonyObserver(deaggregate=True)
        obs.update_rib(make_elem("10.1.0.0/16", [64500, 2, 3]))
        assert obs.num_total_paths[VP] == 2 ** 16
        obs.update_rib(make_elem("10.1.1.0/24", [64500, 2, 3]))
        assert obs.num_total_paths[VP] == 2 ** 16
        assert obs.edge_paths_count[VP][("2", "3")] == pytest.approx(2 ** 16)

    def test_nested_prefix_withdrawal_restores_space(self):
        obs = HegemonyObserver(deaggregate=True)
        obs.update_rib(make_elem("10.1.0.0/16", [64500, 2, 3]))
        obs.update_rib(make_elem("10.1.1.0/24", [64500, 2, 3]))
        obs.update_withdrawal(make_withdrawal("10.1.1.0/24"),
                              make_data([64500, 2, 3]))
        assert obs.num_total_paths[VP] == 2 ** 16
        assert obs.edge_paths_count[VP][("2", "3")] == pytest.approx(2 ** 16)
        assert obs._tries[VP].search_exact("10.1.1.0/24") is None

    def test_less_specific_announced_after_more_specific(self):
        obs = HegemonyObserver(deaggregate=True)
        obs.update_rib(make_elem("10.1.1.0/24", [64500, 9]))
        assert obs.num_total_paths[VP] == 2 ** 8
        obs.update_rib(make_elem("10.1.0.0/16", [64500, 9]))
        assert obs.num_total_paths[VP] == 2 ** 16
        obs.update_withdrawal(make_withdrawal("10.1.0.0/16"),
                              make_data([64500, 9]))
        assert obs.num_total_paths[VP] == 2 ** 8

    def test_deaggregate_vs_plain_diverge(self):
        plain = HegemonyObserver(deaggregate=False)
        deagg = HegemonyObserver(deaggregate=True)
        for obs in (plain, deagg):
            obs.update_rib(make_elem("10.1.0.0/16", [64500, 2, 3]))
            obs.update_rib(make_elem("10.1.1.0/24", [64500, 2, 3]))
        assert plain.num_total_paths[VP] == 2 ** 16 + 2 ** 8
        assert deagg.num_total_paths[VP] == 2 ** 16

    @pytest.mark.parametrize("deaggregate", [False, True])
    def test_driven_through_rib_table(self, deaggregate):
        rib = RIBTable()
        obs = HegemonyObserver(deaggregate=deaggregate)
        rib.attach_observer(obs)

        rib.update_announcement(make_elem("203.0.113.0/24", [64500, 10, 20]))
        rib.update_announcement(make_elem("203.0.113.0/24", [64500, 30, 20]))  # path change
        rib.update_announcement(make_elem("198.51.100.0/24", [64500, 10, 20]))
        rib.update_withdrawal(make_withdrawal("198.51.100.0/24"))

        assert obs.num_total_paths[VP] == 256.0
        graph = obs.dump(TS)
        assert graph.get_weight(("20", "30")) == pytest.approx(1.0)
        assert ("10", "20") not in obs.edge_paths_count[VP]


# =============================================================
# end-to-end: incremental (deaggregate) == post-hoc, on real BGP
# =============================================================


@pytest.mark.e2e
class TestHegemonyE2E:
    def test_incremental_deaggregate_matches_rib_dump(self, e2e_config, make_quickrib):
        quickrib = make_quickrib(e2e_config)
        incr = HegemonyObserver(deaggregate=True, alpha=0.2)
        quickrib.rib.attach_observer(incr)
        quickrib.run()

        end_ts = quickrib.end_time
        g_incr = incr.dump(end_ts)
        g_rib = RibHegemonyObserver(rib=quickrib.rib, alpha=0.2).dump(end_ts)

        assert g_incr.to_edgelist() == g_rib.to_edgelist()
        assert g_incr.get_size() > 1000  # sanity: a real Internet graph
        for edge, w in g_incr.to_weightfn().items():
            assert w == pytest.approx(g_rib.get_weight(edge), rel=1e-6), edge
