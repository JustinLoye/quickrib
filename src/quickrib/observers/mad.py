"""Multi-Scale Anomaly Detection (MAD) over AS-hegemony temporal graphs.

Ports the MAD anomaly detector to QuickRIB's observer interface. Every 5-minute
window the observer takes the edge-hegemony graph produced by a
:class:`~quickrib.observers.hegemony.HegemonyObserver`, keeps a rolling history
of such graphs, and scores how well the newest one is explained by its recent
past via a Haar wavelet multi-resolution decomposition (see
:mod:`quickrib.observers._gdecomp`).

Three variants (all in the paper):

* :class:`MADObserver`, the whole-graph query (``MADglob``): one anomaly score
  per window for the entire Internet graph.
* :class:`MADStaticObserver`, the fast approximation (``MADstatic``): the edge
  dictionary and ordering are frozen at the first window, so each later snapshot
  is only projected onto that fixed basis.
* :class:`MADStarObserver`, per-AS star sub-graphs (``MADstar``): one anomaly
  score per AS per window, for pinpointing which ASes an anomaly touches.

The hegemony observer is *owned* by the MAD observer and driven through it
(``update_*`` are forwarded). To share one hegemony observer between several MAD
variants, attach the hegemony observer to the RIB *first* and pass
``drives_hegemony=False`` to the MAD observers; ``HegemonyObserver.dump`` is
memoised per timestamp so the shared reads are cheap.
"""

import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Literal, Optional

import numpy as np
from pydantic import BaseModel, Field

from quickrib.observers._gdecomp import (
    Graph,
    SubgraphDictionary,
    decomposition_coefficients,
)
from quickrib.observers.hegemony import HegemonyObserver
from quickrib.observers.observer import Observer

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- MAD math


def aggregated_graph_list(graphs: list[Graph]) -> Graph:
    """Edge-wise sum of a list of graphs (the projection of the history)."""
    aggregated = Graph()
    for graph in graphs:
        for edge, weight in graph.to_weightfn().items():
            aggregated.add_weighted_edge(edge, weight)
    return aggregated


def compute_Z_score(dec, past_dec, H_stats_dampening: float):
    """Per-coefficient squared Z-score of ``dec`` against history ``past_dec``."""
    past_mean = np.mean(past_dec, axis=0)
    past_var = np.var(past_dec, axis=0)
    return (dec - past_mean) ** 2 / np.maximum(past_var, H_stats_dampening)


def compute_mZ_score(dec, past_dec, H_stats_dampening: float):
    """Per-coefficient modified Z-score (median / MAD based, outlier robust)."""
    past_median = np.median(past_dec, axis=0)
    mad = np.median(np.abs(past_dec - past_median), axis=0)
    return 0.6745 * (dec - past_median) / np.maximum(mad, H_stats_dampening)


def anomaly_scoring_subgraph(
    Q: Graph,
    H: list[Graph],
    stats_func,
    H_stats_dampening: float = 1e-2,
):
    """Per-coefficient anomaly scores of query graph ``Q`` given history ``H``.

    The edge dictionary is ordered by the aggregated historical activity, both
    ``Q`` and every history snapshot are Haar-decomposed against it, and
    ``stats_func`` compares ``Q``'s coefficients to the history's.
    """
    aggregated = aggregated_graph_list(H)
    links = set(aggregated.to_edgelist()).union(Q.to_edgelist())
    dictionary = SubgraphDictionary(links, aggregated.to_weightfn())

    past_dec = np.vstack(
        [np.hstack(decomposition_coefficients(past_graph, dictionary)) for past_graph in H]
    )

    if len(Q.to_edgelist()) == 0:
        dec = np.zeros(past_dec.shape[1])
    else:
        dec = np.hstack(decomposition_coefficients(Q, dictionary))

    return stats_func(dec, past_dec, H_stats_dampening)


def _add_bidir_weighted_link(graph: Graph, u, v, weight: float) -> None:
    graph.add_edge((u, v))
    graph.set_edge_weight((u, v), weight)
    graph.add_edge((v, u))
    graph.set_edge_weight((v, u), weight)


def graph2graphstar(
    graph: Graph, node_subset: Optional[set[str]] = None
) -> dict[str, Graph]:
    """Split ``graph`` into per-node star sub-graphs (edges incident to a node)."""
    nodes = node_subset if node_subset is not None else graph.get_nodes()
    node_to_graph = {node: Graph() for node in nodes}
    for (u, v), weight in graph.to_weightfn().items():
        if u in node_to_graph:
            _add_bidir_weighted_link(node_to_graph[u], u, v, weight)
        if v in node_to_graph:
            _add_bidir_weighted_link(node_to_graph[v], u, v, weight)
    return node_to_graph


# ------------------------------------------------------------------- observers


class MADConfig(BaseModel):
    """Hyper-parameters for a MAD observer."""

    window_size: int = Field(description="Number of historical snapshots in the window")
    H_stats_dampening: float = Field(
        default=1e-2, description="Denominator floor for the scoring statistic"
    )
    mode: Literal["Z-score", "mZ-score"] = Field(
        default="mZ-score",
        description="Statistic used to compare a snapshot's coefficients to its history",
    )


class MADObserver(Observer):
    """Whole-graph MAD (``MADglob``)."""

    def __init__(
        self,
        hegemony_observer: HegemonyObserver,
        window_size: int,
        H_stats_dampening: float = 1e-2,
        mode: Literal["Z-score", "mZ-score"] = "mZ-score",
        name: str = "mad",
        *,
        drives_hegemony: bool = True,
    ) -> None:
        self.hegemony_observer = hegemony_observer
        self.window_size = window_size
        self.H_stats_dampening = H_stats_dampening
        self.mode = mode
        self.stats_func = compute_Z_score if mode == "Z-score" else compute_mZ_score
        self.drives_hegemony = drives_hegemony
        self.name = name

        self.graphs: deque[Graph] = deque(maxlen=window_size + 1)
        self.scores: dict[datetime, float] = {}

    @classmethod
    def from_config(
        cls, hegemony_observer: HegemonyObserver, config: MADConfig, **kwargs
    ) -> "MADObserver":
        return cls(
            hegemony_observer,
            config.window_size,
            config.H_stats_dampening,
            config.mode,
            **kwargs,
        )

    # ---- hot path: forwarded to the owned hegemony observer ------------------


    def update_rib(self, bgpelem) -> None:
        if self.drives_hegemony:
            self.hegemony_observer.update_rib(bgpelem)

    def update_withdrawal(self, bgpelem, data) -> None:
        if self.drives_hegemony:
            self.hegemony_observer.update_withdrawal(bgpelem, data)

    def update_announcement(self, bgpelem, data, old_data) -> None:
        if self.drives_hegemony:
            self.hegemony_observer.update_announcement(bgpelem, data, old_data)

    def set_rib(self, rib) -> None:  # pragma: no cover - nothing to do
        pass

    def compare(self, other) -> None:  # pragma: no cover
        pass

    # ---- scoring -------------------------------------------------------------

    def _score_window(self, graphs: list[Graph]) -> float:
        scores = anomaly_scoring_subgraph(
            graphs[-1], graphs[:-1], self.stats_func, H_stats_dampening=self.H_stats_dampening
        )
        return float(np.sum(np.abs(scores)))

    def dump(self, ts: datetime) -> Any:
        """The whole-graph anomaly score for this window, or ``None`` until the
        history window is full.

        Typed as ``Any`` because the family disagrees on the shape of a result:
        :class:`MADStarObserver` returns one score *per AS*. Each subclass says
        what it returns.
        """
        graph = self.hegemony_observer.dump(ts)
        self.graphs.append(graph)
        if len(self.graphs) < self.window_size + 1:
            return None  # the window is not full yet, so there is nothing to score
        score = self._score_window(list(self.graphs))
        logger.info("MAD anomaly score at %s: %s", ts.isoformat(), score)
        self.scores[ts] = score
        return score


class MADStaticObserver(MADObserver):
    """Fast approximation (``MADstatic``): the edge dictionary is frozen at the
    first window, so later snapshots are only projected onto that fixed basis
    (edges that appear later are ignored)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._first_dump = True
        self.links: Optional[set[tuple[str, str]]] = None
        self.dictionary: Optional[SubgraphDictionary] = None
        self.rolling_coeffs: deque[np.ndarray] = deque(maxlen=self.window_size + 1)

    def dump(self, ts: datetime) -> Any:
        """The anomaly score for this window, or ``None`` while the window is not
        full (or before a non-empty first window has fixed the basis)."""
        graph = self.hegemony_observer.dump(ts)

        if self._first_dump:
            if not graph.to_edgelist():
                # The basis is whatever the first window holds, so an empty first
                # window would freeze an empty basis and score everything after it
                # as zero. Wait for a window with edges instead.
                logger.info("MADstatic: first window at %s is empty, deferring the "
                            "dictionary to the next one", ts.isoformat())
                return None
            self.links = graph.to_edgelist()
            self.dictionary = SubgraphDictionary(self.links, graph.to_weightfn())
            self._first_dump = False
        # Both are set on the first dump and never cleared afterwards.
        assert self.links is not None and self.dictionary is not None

        coeffs = np.hstack(
            decomposition_coefficients(graph.get_subgraph(self.links), self.dictionary)
        )
        self.rolling_coeffs.append(coeffs)
        if len(self.rolling_coeffs) < self.window_size + 1:
            return None
        rolling = list(self.rolling_coeffs)
        scores = self.stats_func(
            rolling[-1], np.vstack(rolling[:-1]), self.H_stats_dampening
        )
        score = float(np.sum(np.abs(scores)))
        logger.info("MADstatic anomaly score at %s: %s", ts.isoformat(), score)
        self.scores[ts] = score
        return score


class MADStarObserver(MADObserver):
    """Per-AS star sub-graphs (``MADstar``): an anomaly score per AS per window."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.node_to_graphs: defaultdict[str, deque[Graph]] = defaultdict(
            lambda: deque(maxlen=self.window_size + 1)
        )
        self.node_to_scores: defaultdict[datetime, dict[str, float]] = defaultdict(dict)

    def dump(self, ts: datetime) -> Any:
        """This window's anomaly score per AS, empty until the window is full."""
        full_graph = self.hegemony_observer.dump(ts)
        stars = graph2graphstar(full_graph)
        logger.debug("MADstar: scoring %d star sub-graphs at %s", len(stars), ts.isoformat())

        # A node absent from this window gets an empty star, not a skipped slot.
        # Appending only for the nodes present would splice non-consecutive
        # windows into one history. An AS *vanishing* from the graph is itself
        # an anomaly, so it is the one case that must not go unscored.
        for node in self.node_to_graphs.keys() - stars.keys():
            stars[node] = Graph()

        for node, star in stars.items():
            history = self.node_to_graphs[node]
            history.append(star)
            if len(history) == self.window_size + 1:
                self.node_to_scores[ts][node] = self._score_window(list(history))
        # `.get`, not `[ts]`: reading a defaultdict creates the key, which would
        # record a window that scored nothing as an empty one.
        return self.node_to_scores.get(ts, {})
