"""Original author: Esteban Bautista.

Only the pieces the MAD observer needs are kept:

* :class:`Graph`: a directed weighted graph backed by an insertion-ordered
  ``{edge: weight}`` dict.
* :class:`SubgraphDictionary`: ``gdecomp``'s "dictionary of subgraphs, ordered
  by value", collapsed into a single class.
* :func:`decomposition_coefficients` / :func:`tree_leaves`: the Haar wavelet
  multi-resolution decomposition of a graph's leaf vector.

Two deliberate deviations from upstream ``gdecomp``, both to make the edge
ordering deterministic so end-to-end anomaly scores can be pinned:

1. ``SubgraphDictionary`` does not fall back to a random default ordering.
2. Edges are ranked by ``(-weight, edge_key)`` with an explicit tie-break, and
   edges absent from the ranking value are appended in ``edge_key`` order rather
   than in ``set`` iteration order.

``networkx`` (upstream only used it for ``Graph.from_networkx``), the biclique
mappers, ``multiscale_approximation`` and the ``Coefficients`` helper are not
vendored, since the MAD observer does not use them.
"""

import math

import pywt

__all__ = [
    "Graph",
    "SubgraphDictionary",
    "tree_leaves",
    "decomposition_coefficients",
]


Node = str
"""Node identity. ASN strings in the BGP case; the algorithms only need hashable."""
Edge = tuple[Node, Node]


class Graph:
    """Directed weighted graph: ``{(origin, destin): weight}``.

    Node identities are whatever hashable objects the edges carry (ASN strings
    in the BGP case). ``add_edge`` sets the weight to ``1``; ``set_edge_weight``
    overwrites; ``add_weighted_edge`` accumulates.
    """

    def __init__(self) -> None:
        self._data: dict[Edge, float] = {}
        self._origin_set: set[Node] = set()
        self._destin_set: set[Node] = set()

    def add_edge(self, edge: Edge) -> None:
        self._data[edge] = 1
        self._origin_set.add(edge[0])
        self._destin_set.add(edge[1])

    def add_weighted_edge(self, edge: Edge, weight: float) -> None:
        if edge in self._data:
            self._data[edge] += weight
        else:
            self._data[edge] = weight
        self._origin_set.add(edge[0])
        self._destin_set.add(edge[1])

    def set_edge_weight(self, edge: Edge, weight: float) -> None:
        self._data[edge] = weight

    def get_weight(self, edge: Edge) -> float:
        return self._data[edge]

    def to_edgelist(self) -> set[Edge]:
        return set(self._data.keys())

    def to_weightfn(self) -> dict[Edge, float]:
        """The live ``{edge: weight}`` mapping, **not** a copy.

        Read-only by convention: it is walked once per dump over every edge in
        the graph, and copying it there would cost more than the walk. Mutating
        it mutates the graph.
        """
        return self._data

    def get_nodes(self) -> set[Node]:
        return self._origin_set | self._destin_set

    def get_origin_nodes(self) -> set[Node]:
        return self._origin_set

    def get_destin_nodes(self) -> set:
        return self._destin_set

    def get_size(self) -> int:
        return len(self._data)

    def get_subgraph(self, subgraph_edges: set[Edge]) -> "Graph":
        """Copy of this graph restricted to ``subgraph_edges`` (weights kept)."""
        graph = Graph()
        for edge, weight in self._data.items():
            if edge in subgraph_edges:
                graph.add_edge(edge)
                graph.set_edge_weight(edge, weight)
        return graph


def _edge_key(edge: tuple) -> tuple:
    """Total order on ``(node, node)`` edges: numeric when both ends parse as
    ints (ASNs), lexical otherwise. The leading 0/1 keeps the two regimes from
    interleaving."""
    try:
        return (0, int(edge[0]), int(edge[1]))
    except (ValueError, TypeError):
        return (1, str(edge[0]), str(edge[1]))


class SubgraphDictionary:
    """Assigns every edge of a fixed edge space a contiguous integer index.

    ``order_value`` ranks edges by descending value (e.g. aggregated historical
    hegemony); that index is the leaf position in the Haar decomposition tree.
    """

    def __init__(self, edgespace, value: dict | None = None) -> None:
        """``value`` ranks the edge space up front; without it every edge is
        ordered by :func:`_edge_key` alone. Passing it here rather than calling
        :meth:`order_value` afterwards saves ranking the whole edge space twice,
        which at a full graph is the dominant cost of building a dictionary."""
        self._edgespace: set = set(edgespace)
        self._mapping: dict[tuple, int] = {}
        self.order_value(value or {})

    def order_value(self, value: dict) -> None:
        """(Re)build the index from ``{edge: value}``, highest value first."""
        mapping = {
            edge: i
            for i, edge in enumerate(
                sorted(value, key=lambda e: (-value[e], _edge_key(e)))
            )
        }
        for edge in sorted(self._edgespace, key=_edge_key):
            if edge not in mapping:
                mapping[edge] = len(mapping)
        self._mapping = mapping

    def get_index(self, edge: tuple) -> int:
        return self._mapping[edge]

    def get_size(self) -> int:
        return len(self._mapping)


def tree_leaves(graph: Graph, dictionary: SubgraphDictionary) -> list[float]:
    """Dense leaf vector of the decomposition tree: the graph's edge weights
    placed at their dictionary index, zero-padded to a power of two."""
    graphfn = graph.to_weightfn()
    size = dictionary.get_size()
    memory_size = 0 if size == 0 else 1 << math.ceil(math.log2(size))
    data: list[float] = [0.0] * memory_size  # 2**n so the Haar transform halves cleanly
    for edge in graphfn:
        data[dictionary.get_index(edge)] = graphfn[edge]
    return data


def decomposition_coefficients(graph: Graph, dictionary: SubgraphDictionary) -> list:
    """Full-depth Haar wavelet transform of ``tree_leaves(graph, dictionary)``.

    Returns pywt's list ``[cA_n, cD_n, ..., cD_1]``; callers ``np.hstack`` it to
    get one flat coefficient vector.
    """
    return pywt.wavedec(tree_leaves(graph, dictionary), "haar")
