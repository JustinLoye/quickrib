"""Tests for the MAD anomaly detector.

Two tiers (see AGENTS.md "Testing policy"):

* **standalone** (no marker): hand-built graphs and fake ``BGPElement``s exercise
  the vendored ``_gdecomp`` helpers, the MAD math and the windowing logic against
  values computed by hand.
* **end-to-end** (``@pytest.mark.e2e``): the real pipeline over the pinned 2010
  window; observed anomaly scores are frozen once and then enforced.

The ``HegemonyObserver`` itself is tested in ``tests/test_hegemony.py``.
"""

import datetime
import random
from typing import Any, Literal

import numpy as np
import pytest

from quickrib.elements import ParsedElement
from quickrib.observers._gdecomp import (
    Edge,
    Graph,
    SubgraphDictionary,
    decomposition_coefficients,
    tree_leaves,
)
from quickrib.observers.hegemony import HegemonyObserver
from quickrib.observers.mad import (
    MADConfig,
    MADObserver,
    MADStarObserver,
    MADStaticObserver,
    aggregated_graph_list,
    anomaly_scoring_subgraph,
    compute_mZ_score,
    compute_Z_score,
    graph2graphstar,
)

TS = datetime.datetime(2010, 9, 1, 0, 5, tzinfo=datetime.UTC)


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


def wgraph(edges: dict[Edge, float]) -> Graph:
    """Build a bidirectional weighted graph from ``{(u, v): w}``."""
    graph = Graph()
    for (u, v), w in edges.items():
        graph.add_edge((u, v))
        graph.set_edge_weight((u, v), w)
        graph.add_edge((v, u))
        graph.set_edge_weight((v, u), w)
    return graph


class ScriptedHegemony(HegemonyObserver):
    """Stand-in hegemony observer whose ``dump`` replays a fixed list of graphs."""

    def __init__(self, graphs: list[Graph]):
        super().__init__(name="hegemony")
        self._graphs = list(graphs)
        self._i = 0

    def update_rib(self, bgpelem):  # pragma: no cover
        pass

    def update_withdrawal(self, bgpelem, data):  # pragma: no cover
        pass

    def update_announcement(self, bgpelem, data, old_data):  # pragma: no cover
        pass

    def dump(self, ts):
        graph = self._graphs[self._i]
        self._i += 1
        return graph


# ===================================================================
# _gdecomp
# ===================================================================


class TestGdecompGraph:
    def test_add_and_weight(self):
        g = Graph()
        g.add_edge(("1", "2"))
        assert g.get_weight(("1", "2")) == 1
        g.set_edge_weight(("1", "2"), 5.0)
        assert g.get_weight(("1", "2")) == 5.0
        g.add_weighted_edge(("1", "2"), 2.0)
        assert g.get_weight(("1", "2")) == 7.0
        g.add_weighted_edge(("2", "3"), 3.0)
        assert g.get_size() == 2
        assert g.to_edgelist() == {("1", "2"), ("2", "3")}
        assert g.get_nodes() == {"1", "2", "3"}
        assert g.get_origin_nodes() == {"1", "2"}

    def test_get_subgraph(self):
        g = wgraph({("1", "2"): 1.0, ("2", "3"): 2.0, ("3", "4"): 3.0})
        sub = g.get_subgraph({("1", "2"), ("2", "1")})
        assert sub.to_edgelist() == {("1", "2"), ("2", "1")}
        assert sub.get_weight(("1", "2")) == 1.0


class TestSubgraphDictionary:
    def test_order_value_ranks_by_weight_desc(self):
        value = {("1", "2"): 3.0, ("2", "3"): 1.0, ("3", "4"): 2.0}
        d = SubgraphDictionary(value.keys())
        d.order_value(value)
        assert d.get_index(("1", "2")) == 0
        assert d.get_index(("3", "4")) == 1
        assert d.get_index(("2", "3")) == 2

    def test_leftover_edges_appended_in_edge_key_order(self):
        edgespace = {("9", "9"), ("1", "1"), ("5", "5")}
        d = SubgraphDictionary(edgespace)
        d.order_value({})
        assert d.get_index(("1", "1")) == 0
        assert d.get_index(("5", "5")) == 1
        assert d.get_index(("9", "9")) == 2

    def test_deterministic_across_runs(self):
        edgespace = {("3", "7"), ("1", "2"), ("8", "1"), ("4", "4")}
        value = {("1", "2"): 2.0, ("4", "4"): 2.0}  # tie on value
        maps = []
        for _ in range(5):
            d = SubgraphDictionary(set(edgespace))
            d.order_value(dict(value))
            maps.append(dict(d._mapping))
        assert all(m == maps[0] for m in maps)


class TestDecomposition:
    def test_tree_leaves_pads_to_power_of_two(self):
        g = wgraph({("1", "2"): 4.0})  # 2 directed edges -> dict size 2
        d = SubgraphDictionary(g.to_edgelist())
        d.order_value(g.to_weightfn())
        leaves = tree_leaves(g, d)
        assert len(leaves) == 2
        assert sorted(leaves) == [4.0, 4.0]

    def test_haar_decomposition_known_value(self):
        # weights [4, 0, 0] padded to [4, 0, 0, 0]
        edges = {("1", "2"): 4.0, ("2", "3"): 0.0, ("3", "4"): 0.0}
        g = Graph()
        for e, w in edges.items():
            g.add_edge(e)
            g.set_edge_weight(e, w)
        d = SubgraphDictionary(g.to_edgelist())
        d.order_value(g.to_weightfn())
        flat = np.hstack(decomposition_coefficients(g, d))
        assert flat == pytest.approx([2.0, 2.0, 2.0 * np.sqrt(2), 0.0])

    def test_empty_dictionary(self):
        d = SubgraphDictionary(set())
        assert tree_leaves(Graph(), d) == []


# ===================================================================
# MAD math
# ===================================================================


class TestMadMath:
    def test_aggregated_graph_list_sums(self):
        g1 = wgraph({("1", "2"): 1.0})
        g2 = wgraph({("1", "2"): 2.0, ("2", "3"): 5.0})
        agg = aggregated_graph_list([g1, g2])
        assert agg.get_weight(("1", "2")) == 3.0
        assert agg.get_weight(("2", "3")) == 5.0

    def test_compute_Z_score_formula(self):
        past = np.array([[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]])
        dec = np.array([3.0, 1.0])
        # var == 0 -> floored to dampening 0.25; (3-1)^2/0.25 = 16 ; (1-1)^2/... = 0
        assert compute_Z_score(dec, past, 0.25) == pytest.approx([16.0, 0.0])

    def test_compute_mZ_score_formula(self):
        past = np.array([[0.0], [0.0], [0.0]])
        dec = np.array([2.0])
        # MAD == 0 -> floored to 0.5 ; 0.6745 * (2-0) / 0.5 = 2.698
        assert compute_mZ_score(dec, past, 0.5) == pytest.approx([2.698])

    def test_graph2graphstar_star_extraction(self):
        g = wgraph({("1", "2"): 1.0, ("2", "3"): 2.0, ("3", "4"): 3.0})
        stars = graph2graphstar(g)
        assert stars["2"].to_edgelist() == {("1", "2"), ("2", "1"), ("2", "3"), ("3", "2")}
        only2 = graph2graphstar(g, node_subset={"2"})
        assert set(only2) == {"2"}

    def test_anomaly_scoring_identical_history_is_zero(self):
        g = wgraph({("1", "2"): 0.1, ("2", "3"): 0.2, ("3", "4"): 0.05})
        H = [wgraph({("1", "2"): 0.1, ("2", "3"): 0.2, ("3", "4"): 0.05}) for _ in range(4)]
        for stats_func in (compute_Z_score, compute_mZ_score):
            scores = anomaly_scoring_subgraph(g, H, stats_func, H_stats_dampening=1e-9)
            assert float(np.sum(np.abs(scores))) == pytest.approx(0.0, abs=1e-6)

    def test_anomaly_scoring_perturbation_is_large(self):
        base = {("1", "2"): 0.1, ("2", "3"): 0.1, ("3", "4"): 0.1, ("4", "5"): 0.1}
        H = [wgraph(dict(base)) for _ in range(4)]
        perturbed = dict(base)
        perturbed[("2", "3")] = 2.0
        Q = wgraph(perturbed)
        score = float(np.sum(np.abs(
            anomaly_scoring_subgraph(Q, H, compute_mZ_score, H_stats_dampening=1e-9)
        )))
        assert score > 5.0

    def test_anomaly_scoring_empty_query(self):
        H = [wgraph({("1", "2"): 0.1, ("2", "3"): 0.2}) for _ in range(4)]
        scores = anomaly_scoring_subgraph(Graph(), H, compute_mZ_score)
        assert np.all(np.isfinite(scores))

    def test_scoring_is_deterministic(self):
        base = {("1", "2"): 0.1, ("2", "3"): 0.3, ("3", "4"): 0.2, ("4", "5"): 0.15}
        H = [wgraph(dict(base)) for _ in range(4)]
        Q = wgraph({**base, ("3", "4"): 1.5})
        runs = [
            float(np.sum(np.abs(
                anomaly_scoring_subgraph(Q, H, compute_mZ_score, H_stats_dampening=1e-9)
            )))
            for _ in range(5)
        ]
        assert all(r == runs[0] for r in runs)


VP = ("rrc00", 64500, "10.0.0.1")  # used by TestMadObservers.test_delegation_toggle


# ===================================================================
# MAD observers (windowing, isolated from real hegemony)
# ===================================================================


def _ts(i):
    return datetime.datetime(2010, 9, 1, 0, 0, tzinfo=datetime.UTC) + datetime.timedelta(minutes=5 * i)


class TestScoreStability:
    """What a MAD score is, and is not, stable against.

    The decomposition pads the edge vector to a power of two
    (:func:`tree_leaves`), so the *number* of edges in the graph decides the
    length of the coefficient vector. One edge crossing that boundary re-bins
    every coefficient and moves the score by orders of magnitude more than the
    edge itself is worth.

    That matters because `HegemonyObserver` decides membership with a hard
    `min_hegemony` cut (1e-15 by default), so an edge whose hegemony sits on that
    threshold can enter or leave the graph on a last-bit difference, and if the
    edge count is near a power of two, the score jumps with it. Any pinned MAD
    value is only reproducible to that jump.
    """

    @staticmethod
    def _graph(n_edges: int, seed: int, extra: tuple[str, str] | None = None) -> Graph:
        rng = random.Random(seed)
        graph = Graph()
        for i in range(n_edges):
            edge = (str(i), str(i + 1))
            graph.add_edge(edge)
            graph.set_edge_weight(edge, rng.random())
        if extra is not None:
            graph.add_edge(extra)
            graph.set_edge_weight(extra, 1e-15)
        return graph

    def _score(self, history, query) -> float:
        return float(
            np.sum(np.abs(anomaly_scoring_subgraph(query, history, compute_mZ_score)))
        )

    def test_a_last_bit_change_in_the_weights_barely_moves_the_score(self):
        history = [self._graph(200, seed) for seed in range(3)]
        query = self._graph(200, 99)
        base = self._score(history, query)

        def nudge(graph):
            out = Graph()
            for edge, weight in graph.to_weightfn().items():
                out.add_edge(edge)
                out.set_edge_weight(edge, np.nextafter(weight, np.inf))
            return out

        moved = self._score([nudge(g) for g in history], nudge(query))
        assert moved == pytest.approx(base, rel=1e-12)

    @pytest.mark.parametrize("n_edges", [200, 255, 300])
    def test_one_extra_edge_away_from_a_power_of_two_is_harmless(self, n_edges):
        history = [self._graph(n_edges, seed) for seed in range(3)]
        base = self._score(history, self._graph(n_edges, 99))
        moved = self._score(history, self._graph(n_edges, 99, extra=("999", "1000")))
        assert moved == pytest.approx(base, rel=1e-12)

    def test_one_extra_edge_across_a_power_of_two_moves_the_score(self):
        """The documented fragility, pinned so it cannot change unnoticed."""
        history = [self._graph(256, seed) for seed in range(3)]
        base = self._score(history, self._graph(256, 99))
        moved = self._score(history, self._graph(256, 99, extra=("999", "1000")))
        # An edge worth 1e-15 shifts the score by ~1e-4 relative: four orders of
        # magnitude of padding, not of signal.
        assert abs(moved - base) / base > 1e-5


class TestMadObservers:
    def test_window_not_full_no_score(self):
        heg = ScriptedHegemony([wgraph({("1", "2"): 0.1}) for _ in range(3)])
        mad = MADObserver(heg, window_size=3)
        for i in range(3):
            mad.dump(_ts(i))
        assert mad.scores == {}

    def test_stable_then_spike_then_settle(self):
        base = {("1", "2"): 0.1, ("2", "3"): 0.1, ("3", "4"): 0.1, ("4", "5"): 0.1}
        spike = {**base, ("2", "3"): 3.0}
        graphs = (
            [wgraph(dict(base)) for _ in range(5)]   # dumps 0..4 -> score at 4 ~ 0
            + [wgraph(dict(spike))]                  # dump 5 -> large
            + [wgraph(dict(base))]                   # dump 6 -> smaller again
        )
        heg = ScriptedHegemony(graphs)
        mad = MADObserver(heg, window_size=3, H_stats_dampening=1e-9)
        for i in range(7):
            mad.dump(_ts(i))
        # stable window -> ~0; window containing the spike as the query -> large
        assert mad.scores[_ts(4)] == pytest.approx(0.0, abs=1e-6)
        assert mad.scores[_ts(5)] > 1e6
        # mZ-score is outlier-robust: once the spike is only in the *history* it
        # is median-filtered out, so the next stable query scores ~0 again.
        assert mad.scores[_ts(5)] > mad.scores[_ts(6)]
        assert mad.scores[_ts(6)] == pytest.approx(0.0, abs=1e-6)

    def test_scores_deterministic(self):
        base = {("1", "2"): 0.1, ("2", "3"): 0.2, ("3", "4"): 0.1}
        spike = {**base, ("2", "3"): 2.0}
        script = [wgraph(dict(base)) for _ in range(4)] + [wgraph(dict(spike))]

        def run():
            heg = ScriptedHegemony([wgraph(dict(g.to_weightfn())) for g in script])
            mad = MADObserver(heg, window_size=3, H_stats_dampening=1e-9)
            for i in range(5):
                mad.dump(_ts(i))
            return dict(mad.scores)

        assert run() == run()

    def test_madstatic_freezes_dictionary(self):
        base = {("1", "2"): 0.1, ("2", "3"): 0.2}
        graphs = [wgraph(dict(base)) for _ in range(4)]
        graphs.append(wgraph({**base, ("5", "6"): 9.0}))  # new edge later
        heg = ScriptedHegemony(graphs)
        mad = MADStaticObserver(heg, window_size=3, H_stats_dampening=1e-9)
        for i in range(5):
            mad.dump(_ts(i))
        assert mad.dictionary is not None
        assert mad.links is not None
        # the late ("5", "6") edge is outside the frozen edge space
        assert ("5", "6") not in mad.links
        assert _ts(4) in mad.scores

    def test_madstar_records_no_window_before_it_can_score(self):
        """Reading `node_to_scores[ts]` on a defaultdict would create the key, so
        a window that scored nothing would be recorded as an empty one."""
        heg = ScriptedHegemony([wgraph({("1", "2"): 1.0}) for _ in range(6)])
        star = MADStarObserver(heg, window_size=3)
        for i in range(3):
            assert star.dump(TS + datetime.timedelta(minutes=5 * i)) == {}
        assert star.node_to_scores == {}

    def test_madstar_per_node_scores(self):
        base = {("1", "2"): 0.1, ("2", "3"): 0.1, ("3", "4"): 0.1}
        spike = {**base, ("3", "4"): 3.0}
        graphs = [wgraph(dict(base)) for _ in range(4)] + [wgraph(dict(spike))]
        heg = ScriptedHegemony(graphs)
        mad = MADStarObserver(heg, window_size=3, H_stats_dampening=1e-9)
        for i in range(5):
            mad.dump(_ts(i))
        scores_at_spike = mad.node_to_scores[_ts(4)]
        assert set(scores_at_spike).issubset({"1", "2", "3", "4"})
        # nodes 3 and 4 touch the perturbed edge -> highest scores
        assert max(scores_at_spike, key=lambda n: scores_at_spike[n]) in {"3", "4"}

    def test_from_config(self):
        mad = MADObserver.from_config(ScriptedHegemony([]), MADConfig(window_size=2))
        assert mad.window_size == 2
        assert mad.stats_func is compute_mZ_score

    def test_delegation_toggle(self):
        heg = HegemonyObserver()
        driven = MADObserver(heg, window_size=3, drives_hegemony=True)
        driven.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        assert heg.num_total_paths[VP] == 256.0

        heg2 = HegemonyObserver()
        passive = MADObserver(heg2, window_size=3, drives_hegemony=False)
        passive.update_rib(make_elem("192.0.2.0/24", [64500, 2, 3]))
        assert dict(heg2.num_total_paths) == {}


# ===================================================================
# end-to-end
# ===================================================================


@pytest.mark.e2e
class TestMADE2E:
    WINDOW = 3

    # Frozen after one observed run (see AGENTS.md testing policy). Populated by
    # test_capture_expectations, which writes them to $MAD_E2E_CAPTURE.
    #
    # `rel=1e-3`, not the 1e-9 these used to carry: a MAD score is discontinuous
    # in the *number* of edges in the graph, because the decomposition pads to a
    # power of two and `min_hegemony` decides membership on a knife edge. A change
    # that is mathematically a no-op moves the score by up to ~3e-4 in the windows
    # near that boundary. TestScoreStability pins both halves of that.
    EXPECTED_GLOB = {
        '2010-09-01T00:20:00+00:00': pytest.approx(0.11105815663645796, rel=1e-3),
        '2010-09-01T00:25:00+00:00': pytest.approx(0.1355039355145196, rel=1e-3),
        '2010-09-01T00:30:00+00:00': pytest.approx(0.08193446269183358, rel=1e-3),
        '2010-09-01T00:35:00+00:00': pytest.approx(0.06936760865864994, rel=1e-3),
        '2010-09-01T00:40:00+00:00': pytest.approx(0.07993880825176401, rel=1e-3),
        '2010-09-01T00:45:00+00:00': pytest.approx(0.13732826858436414, rel=1e-3),
        '2010-09-01T00:50:00+00:00': pytest.approx(0.17903418347396977, rel=1e-3),
        '2010-09-01T00:55:00+00:00': pytest.approx(0.1121699437787462, rel=1e-3),
        '2010-09-01T01:00:00+00:00': pytest.approx(0.07631160122021335, rel=1e-3),
        '2010-09-01T01:05:00+00:00': pytest.approx(0.037299113311499134, rel=1e-3),
        '2010-09-01T01:10:00+00:00': pytest.approx(0.04330985616644044, rel=1e-3),
        '2010-09-01T01:15:00+00:00': pytest.approx(0.039867684481300715, rel=1e-3),
        '2010-09-01T01:20:00+00:00': pytest.approx(0.08104895101117879, rel=1e-3),
        '2010-09-01T01:25:00+00:00': pytest.approx(0.08175045187868078, rel=1e-3),
        '2010-09-01T01:30:00+00:00': pytest.approx(0.11028835081417947, rel=1e-3),
        '2010-09-01T01:35:00+00:00': pytest.approx(0.11742004715743248, rel=1e-3),
        '2010-09-01T01:40:00+00:00': pytest.approx(0.03935158154097563, rel=1e-3),
        '2010-09-01T01:45:00+00:00': pytest.approx(0.044031851046111015, rel=1e-3),
        '2010-09-01T01:50:00+00:00': pytest.approx(0.1014543540924634, rel=1e-3),
        '2010-09-01T01:55:00+00:00': pytest.approx(0.09070556571735003, rel=1e-3),
    }
    EXPECTED_STATIC = {
        '2010-09-01T00:20:00+00:00': pytest.approx(0.10748620655311297, rel=1e-3),
        '2010-09-01T00:25:00+00:00': pytest.approx(0.13605528203434109, rel=1e-3),
        '2010-09-01T00:30:00+00:00': pytest.approx(0.08416283680506031, rel=1e-3),
        '2010-09-01T00:35:00+00:00': pytest.approx(0.06804079551087393, rel=1e-3),
        '2010-09-01T00:40:00+00:00': pytest.approx(0.08086203448784114, rel=1e-3),
        '2010-09-01T00:45:00+00:00': pytest.approx(0.1322126232009828, rel=1e-3),
        '2010-09-01T00:50:00+00:00': pytest.approx(0.16189374947021495, rel=1e-3),
        '2010-09-01T00:55:00+00:00': pytest.approx(0.1001180972637259, rel=1e-3),
        '2010-09-01T01:00:00+00:00': pytest.approx(0.07462321978938788, rel=1e-3),
        '2010-09-01T01:05:00+00:00': pytest.approx(0.036995018030806404, rel=1e-3),
        '2010-09-01T01:10:00+00:00': pytest.approx(0.04050961432271277, rel=1e-3),
        '2010-09-01T01:15:00+00:00': pytest.approx(0.03728792601542984, rel=1e-3),
        '2010-09-01T01:20:00+00:00': pytest.approx(0.06978440111575397, rel=1e-3),
        '2010-09-01T01:25:00+00:00': pytest.approx(0.07329283160209946, rel=1e-3),
        '2010-09-01T01:30:00+00:00': pytest.approx(0.09190528463899418, rel=1e-3),
        '2010-09-01T01:35:00+00:00': pytest.approx(0.09691677632313771, rel=1e-3),
        '2010-09-01T01:40:00+00:00': pytest.approx(0.039614283932442526, rel=1e-3),
        '2010-09-01T01:45:00+00:00': pytest.approx(0.04303275615330682, rel=1e-3),
        '2010-09-01T01:50:00+00:00': pytest.approx(0.09657141450469162, rel=1e-3),
        '2010-09-01T01:55:00+00:00': pytest.approx(0.08734299049624658, rel=1e-3),
    }
    EXPECTED_STAR = {
        '2010-09-01T00:20:00+00:00': {"n": 35286, "top": '3549', "top_score": pytest.approx(0.029923726621774793, rel=1e-3), "total": pytest.approx(0.1853723215070162, rel=1e-3)},
        '2010-09-01T00:25:00+00:00': {"n": 35290, "top": '3549', "top_score": pytest.approx(0.029716767857836064, rel=1e-3), "total": pytest.approx(0.23170850143888475, rel=1e-3)},
        '2010-09-01T00:30:00+00:00': {"n": 35291, "top": '3549', "top_score": pytest.approx(0.012946212729916376, rel=1e-3), "total": pytest.approx(0.13759861813617744, rel=1e-3)},
        '2010-09-01T00:35:00+00:00': {"n": 35291, "top": '3549', "top_score": pytest.approx(0.013499131503561156, rel=1e-3), "total": pytest.approx(0.11208567163269298, rel=1e-3)},
        '2010-09-01T00:40:00+00:00': {"n": 35292, "top": '174', "top_score": pytest.approx(0.019721610439064043, rel=1e-3), "total": pytest.approx(0.13309721477465153, rel=1e-3)},
        '2010-09-01T00:45:00+00:00': {"n": 35292, "top": '174', "top_score": pytest.approx(0.019730498530078494, rel=1e-3), "total": pytest.approx(0.22464742419159175, rel=1e-3)},
        '2010-09-01T00:50:00+00:00': {"n": 35292, "top": '3320', "top_score": pytest.approx(0.02180038776548699, rel=1e-3), "total": pytest.approx(0.2692777064069383, rel=1e-3)},
        '2010-09-01T00:55:00+00:00': {"n": 35292, "top": '174', "top_score": pytest.approx(0.016083499465828856, rel=1e-3), "total": pytest.approx(0.18349365670595819, rel=1e-3)},
        '2010-09-01T01:00:00+00:00': {"n": 35294, "top": '9198', "top_score": pytest.approx(0.008963346524648223, rel=1e-3), "total": pytest.approx(0.12191067694751878, rel=1e-3)},
        '2010-09-01T01:05:00+00:00': {"n": 35294, "top": '7018', "top_score": pytest.approx(0.004365939695608161, rel=1e-3), "total": pytest.approx(0.06224603053162921, rel=1e-3)},
        '2010-09-01T01:10:00+00:00': {"n": 35294, "top": '3549', "top_score": pytest.approx(0.00776822023500748, rel=1e-3), "total": pytest.approx(0.0732650408286067, rel=1e-3)},
        '2010-09-01T01:15:00+00:00': {"n": 35294, "top": '3549', "top_score": pytest.approx(0.005509204435829728, rel=1e-3), "total": pytest.approx(0.06436306239200258, rel=1e-3)},
        '2010-09-01T01:20:00+00:00': {"n": 35295, "top": '3356', "top_score": pytest.approx(0.010835795643261575, rel=1e-3), "total": pytest.approx(0.1380978963068323, rel=1e-3)},
        '2010-09-01T01:25:00+00:00': {"n": 35295, "top": '3549', "top_score": pytest.approx(0.01150524061884401, rel=1e-3), "total": pytest.approx(0.13708926359797122, rel=1e-3)},
        '2010-09-01T01:30:00+00:00': {"n": 35295, "top": '38193', "top_score": pytest.approx(0.020908577642572333, rel=1e-3), "total": pytest.approx(0.17043537063431213, rel=1e-3)},
        '2010-09-01T01:35:00+00:00': {"n": 35295, "top": '2497', "top_score": pytest.approx(0.019636309086123654, rel=1e-3), "total": pytest.approx(0.18674857931406755, rel=1e-3)},
        '2010-09-01T01:40:00+00:00': {"n": 35295, "top": '2497', "top_score": pytest.approx(0.009557036005612949, rel=1e-3), "total": pytest.approx(0.06520578088028563, rel=1e-3)},
        '2010-09-01T01:45:00+00:00': {"n": 35295, "top": '3549', "top_score": pytest.approx(0.005372085607168424, rel=1e-3), "total": pytest.approx(0.07413721839878208, rel=1e-3)},
        '2010-09-01T01:50:00+00:00': {"n": 35295, "top": '3561', "top_score": pytest.approx(0.015663109945300388, rel=1e-3), "total": pytest.approx(0.16904250646186228, rel=1e-3)},
        '2010-09-01T01:55:00+00:00': {"n": 35295, "top": '3561', "top_score": pytest.approx(0.015640439439257872, rel=1e-3), "total": pytest.approx(0.14739093751908203, rel=1e-3)},
    }

    @pytest.fixture(scope="class")
    def mad_results(self, e2e_config, make_quickrib):
        quickrib = make_quickrib(e2e_config)
        heg = HegemonyObserver(name="hegemony", alpha=0.2)
        glob = MADObserver(heg, self.WINDOW, name="mad_glob", drives_hegemony=False)
        static = MADStaticObserver(heg, self.WINDOW, name="mad_static", drives_hegemony=False)
        star = MADStarObserver(heg, self.WINDOW, name="mad_star", drives_hegemony=False)
        # hegemony observer attached first -> its memoised dump() runs before the
        # MAD observers read it; no set_rib needed (warm-started via build_rib).
        quickrib.rib.attach_observer(heg)
        quickrib.rib.attach_observer(glob)
        quickrib.rib.attach_observer(static)
        quickrib.rib.attach_observer(star)
        quickrib.run()

        def iso(d):
            return {k.isoformat(): v for k, v in d.items()}

        def summarise(scores: dict[str, float]) -> dict[str, Any]:
            """One window of MADstar, reduced to what is worth freezing."""
            if not scores:
                return {"n": 0, "top": None, "top_score": 0.0, "total": 0.0}
            top_score = max(scores.values())
            return {
                "n": len(scores),
                # Lexicographically smallest AS among those tied for the top
                # score, so the frozen value is deterministic.
                "top": min(n for n, s in scores.items() if s == top_score),
                "top_score": top_score,
                "total": float(sum(scores.values())),
            }

        return {
            "glob": iso(glob.scores),
            "static": iso(static.scores),
            "star": {t: summarise(v) for t, v in iso(star.node_to_scores).items()},
        }

    def test_capture_expectations(self, mad_results):
        """Not an assertion. Writes the observed values so they can be frozen
        into the EXPECTED_* dicts, then enforced by the tests below."""
        import json
        import os

        out = os.environ.get("MAD_E2E_CAPTURE")
        if out:
            with open(out, "w") as fh:
                json.dump(mad_results, fh, indent=2, sort_keys=True)
        print("\nOBSERVED MAD e2e results:\n" + json.dumps(mad_results, indent=2, sort_keys=True))

    def test_glob_scores_match(self, mad_results):
        if not self.EXPECTED_GLOB:
            pytest.skip("EXPECTED_GLOB not frozen yet; run test_capture_expectations")
        assert mad_results["glob"] == self.EXPECTED_GLOB

    def test_static_scores_match(self, mad_results):
        if not self.EXPECTED_STATIC:
            pytest.skip("EXPECTED_STATIC not frozen yet")
        assert mad_results["static"] == self.EXPECTED_STATIC

    def test_star_aggregates_match(self, mad_results):
        if not self.EXPECTED_STAR:
            pytest.skip("EXPECTED_STAR not frozen yet")
        assert mad_results["star"] == self.EXPECTED_STAR

    def test_scores_finite_and_nonnegative(self, mad_results):
        for series in ("glob", "static"):
            for v in mad_results[series].values():
                assert np.isfinite(v) and v >= 0
        assert set(mad_results["glob"]) == set(mad_results["static"])
