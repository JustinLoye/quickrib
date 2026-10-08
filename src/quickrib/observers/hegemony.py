"""
AS hegemony.

Three ways to compute the same thing:

* :func:`node_hegemony` / :func:`edge_hegemony`: post-hoc functions that walk a
  fully reconstructed :data:`~quickrib.rib_table.RIBData` mapping. The reference
  implementation, validated on the PAM-paper topology in ``examples/hegemony.py``.
* :class:`RibHegemonyObserver`: an observer wrapper around :func:`edge_hegemony`
  that recomputes the whole edge-hegemony graph from the RIB at every ``dump``.
* :class:`HegemonyObserver`: an incremental observer that maintains
  per-vantage-point edge path-weight counts in the ``update_*`` hot path, for
  near-real-time use. This is what the MAD anomaly detector consumes.

With ``deaggregate=True`` the incremental :class:`HegemonyObserver` produces the
**same** edge-hegemony graph as :class:`RibHegemonyObserver` / :func:`edge_hegemony`
(up to floating-point accumulation order). ``deaggregate=False`` is a cheaper
approximation, the paper's ``ips_count`` variant, which does not account for
address-space deaggregation.
"""

from collections import defaultdict
from datetime import datetime
from typing import Optional, TypeVar

import numpy as np
import radix

from quickrib.elements import RIBNodeData
from quickrib.observers._gdecomp import Edge, Graph
from quickrib.observers.observer import Observer
from quickrib.radix_utils import block_size, direct_children, radix_prefix_size, radix_size
from quickrib.rib_table import RIBData

Key = TypeVar("Key")
"""Whatever a betweenness score is filed under: an ASN for node hegemony, a
canonical edge for edge hegemony."""

VP = tuple[str, int, str]
"""Vantage point: ``(collector, peer_asn, peer_ip)``. Betweenness centrality is
computed independently per VP and then trimmed-averaged across VPs."""


def _asn_lt(a: str, b: str) -> bool:
    try:
        return int(a) < int(b)
    except ValueError:
        return a < b


def _canon_edges(path: list[str]):
    """Yield the canonical consecutive AS links of ``path``.

    Consecutive duplicates (prepending) are collapsed, and each link ``(u, v)``
    is oriented so the numerically smaller ASN comes first, the same convention
    as :func:`edge_hegemony`.
    """
    prev = None
    for asn in path:
        if asn == prev:
            continue
        if prev is not None:
            yield (prev, asn) if _asn_lt(prev, asn) else (asn, prev)
        prev = asn


class HegemonyObserver(Observer):
    """Incremental edge AS-hegemony.

    For every vantage point (collector, peer ASN, peer IP) the observer keeps

    * ``edge_paths_count[vp][edge]``: the summed IP-space weight of the
      prefixes routed by that VP whose AS path contains ``edge``;
    * ``num_total_paths[vp]``: the VP's total routed IP space, which normalises
      its betweenness centrality.

    Both are updated in ``update_rib`` / ``update_announcement`` /
    ``update_withdrawal`` at a cost of O(path length) (plus O(#more-specifics)
    per update in ``deaggregate`` mode). :meth:`dump` turns the current counts
    into a :class:`~quickrib.observers._gdecomp.Graph` of per-edge hegemony
    (per-VP betweenness, then :func:`trim_mean` across VPs).

    Parameters
    ----------
    alpha:
        Trimmed-mean cut fraction passed to :func:`trim_mean`.
    deaggregate:
        ``False`` (default): weight each prefix by its plain block size
        (``2**(32-plen)``). This is the paper's ``ips_count`` variant: cheap,
        exactly reversible, but a prefix and its more-specifics both count their
        full block, so overlapping space is counted several times.

        ``True``: weight each prefix by its *exclusive* address space,
        maintained incrementally with a per-VP radix trie: announcing a
        more-specific transfers space away from its covering prefix (and from
        every edge on that prefix's path), withdrawing it gives the space back.
        Matches the intent of the deaggregation-aware :func:`radix_prefix_size`
        used by :func:`edge_hegemony`.
    min_hegemony:
        Edges whose trimmed-mean hegemony is at or below this are dropped from
        the dumped graph. **Zero by default: only weightless edges go.** Opt in
        to a real cut only if a smaller graph is worth what it costs, which is
        more than it looks. See the note below.

    Relation to :func:`edge_hegemony`
    --------------------------------
    ``deaggregate=True`` reproduces :func:`edge_hegemony` exactly (up to
    floating-point accumulation order): same per-VP betweenness weighted by
    deaggregation-aware prefix size, same normalisation by the VP's covered
    address count, same :func:`trim_mean` across VPs.

    ``deaggregate=False`` skips deaggregation: a prefix and each of its
    more-specifics both count their full block, so ``num_total_paths[vp]`` sums
    overlapping space. Cheaper and exactly reversible; absolute magnitudes
    differ from :func:`edge_hegemony` while rankings stay close.

    Why ``min_hegemony`` defaults to zero
    -------------------------------------
    A non-zero cut decides graph *membership* by comparing a float to a
    threshold, and consumers downstream are not continuous in membership: the
    MAD detector pads the edge vector to a power of two, so one edge entering or
    leaving can re-bin every wavelet coefficient. With a cut at 1e-15 an edge
    sitting on the threshold flipped on a last-bit difference, which made
    mathematically-equivalent changes look like regressions (see AGENTS.md, "A
    MAD score is not reproducible to the last bit").

    At zero, membership is decided by *sign* rather than by comparison with a
    small number: an edge is in the graph exactly when its hegemony is strictly
    positive. A sum of non-negative terms is positive or it is not, whatever
    order it is accumulated in, so no rounding can add or remove an edge.

    Zero is also the only cut at which the incremental and post-hoc paths agree
    on membership. An edge carrying no weight is absent from
    ``edge_paths_count``, which never stores a zero, but present in the post-hoc
    walk. In ``deaggregate`` mode this happens whenever a prefix is entirely
    covered by its own more-specifics, leaving it an effective size of zero. Dropping it on both sides is what
    keeps ``deaggregate=True`` equal to :func:`edge_hegemony`.

    Note
    ----
    This observer never needs a back-reference to the ``RIBTable``: it is
    warm-started automatically because ``QuickRIB.build_rib`` replays every RIB
    entry through ``update_rib`` before the first ``dump``, so ``set_rib`` is a
    no-op even though the pipeline does call it.
    """

    def __init__(
        self,
        name: str = "hegemony",
        alpha: float = 0.2,
        deaggregate: bool = False,
        min_hegemony: float = 0.0,
    ) -> None:
        self.name = name
        self.alpha = alpha
        self.deaggregate = deaggregate
        self.min_hegemony = min_hegemony

        # vp -> edge -> summed weight (effective space in deaggregate mode)
        self.edge_paths_count: dict[VP, dict[tuple[str, str], float]] = defaultdict(dict)
        # vp -> summed weight over all the VP's prefixes
        self.num_total_paths: dict[VP, float] = defaultdict(float)

        # deaggregate mode only: vp -> radix trie, node .data = {"path", "overlap"}
        # where overlap == summed block size of the prefix's direct more-specifics.
        self._tries: dict[VP, radix.Radix] = defaultdict(radix.Radix)

        # dump() memoisation so several readers (MAD variants) share one instance.
        self._last_dump_ts: Optional[datetime] = None
        self._last_graph: Optional[Graph] = None

    # ------------------------------------------------------------------ helpers

    def _edge_add(self, vp: VP, path: list[str], weight: float) -> None:
        ec = self.edge_paths_count[vp]
        for edge in _canon_edges(path):
            new = ec.get(edge, 0) + weight
            if new == 0:
                ec.pop(edge, None)
            else:
                ec[edge] = new

    def _plain_add(self, vp: VP, prefix: str, path: list[str]) -> None:
        w = block_size(prefix)
        self._edge_add(vp, path, w)
        self.num_total_paths[vp] += w

    def _plain_remove(self, vp: VP, prefix: str, path: list[str]) -> None:
        w = block_size(prefix)
        self._edge_add(vp, path, -w)
        self.num_total_paths[vp] -= w

    def _deagg_insert(self, vp: VP, prefix: str, path: list[str]) -> None:
        trie = self._tries[vp]
        if trie.search_exact(prefix) is not None:
            # already present (duplicate RIB entry): treat as a path move
            self._deagg_move(vp, prefix, path)
            return
        rnode = trie.add(prefix)
        nominal = block_size(prefix)
        # every direct child of the new node was, before insertion, a direct
        # child of its parent (nothing can sit between parent and new node).
        overlap_p = sum(block_size(c.prefix) for c in direct_children(trie, rnode))
        rnode.data["path"] = path
        rnode.data["overlap"] = overlap_p
        eff_p = nominal - overlap_p

        self._edge_add(vp, path, eff_p)
        self.num_total_paths[vp] += eff_p

        parent = rnode.parent
        if parent is not None:
            # parent gains the new node as a direct child (+nominal) and loses
            # the children that reparented to it (-overlap_p).
            delta_overlap = nominal - overlap_p
            parent.data["overlap"] += delta_overlap
            self._edge_add(vp, parent.data["path"], -delta_overlap)
            self.num_total_paths[vp] -= delta_overlap

    def _deagg_move(self, vp: VP, prefix: str, new_path: list[str]) -> None:
        trie = self._tries[vp]
        rnode = trie.search_exact(prefix)
        if rnode is None:
            self._deagg_insert(vp, prefix, new_path)
            return
        eff_p = block_size(prefix) - rnode.data["overlap"]
        self._edge_add(vp, rnode.data["path"], -eff_p)
        self._edge_add(vp, new_path, eff_p)
        rnode.data["path"] = new_path

    def _deagg_withdraw(self, vp: VP, prefix: str) -> None:
        trie = self._tries[vp]
        rnode = trie.search_exact(prefix)
        if rnode is None:
            return
        nominal = block_size(prefix)
        overlap_p = rnode.data["overlap"]
        eff_p = nominal - overlap_p

        self._edge_add(vp, rnode.data["path"], -eff_p)
        self.num_total_paths[vp] -= eff_p

        parent = rnode.parent
        if parent is not None:
            # children reparent to parent: parent.overlap += sum(nominal children)
            # - nominal(prefix) == overlap_p - nominal == -eff_p
            delta_overlap = overlap_p - nominal
            parent.data["overlap"] += delta_overlap
            self._edge_add(vp, parent.data["path"], -delta_overlap)
            self.num_total_paths[vp] -= delta_overlap

        trie.delete(prefix)

    # ---------------------------------------------------------------- observer


    def update_rib(self, bgpelem) -> None:
        vp = (bgpelem.collector, bgpelem.peer_asn, bgpelem.peer_address)
        path = bgpelem.fields["as-path"]
        if self.deaggregate:
            self._deagg_insert(vp, bgpelem.fields["prefix"], path)
        else:
            self._plain_add(vp, bgpelem.fields["prefix"], path)

    def update_announcement(self, bgpelem, data: RIBNodeData, old_data: Optional[RIBNodeData]) -> None:
        vp = (bgpelem.collector, bgpelem.peer_asn, bgpelem.peer_address)
        prefix = bgpelem.fields["prefix"]
        new_path = data["as-path"]
        if old_data is not None:
            old_path = old_data["as-path"]
            if old_path == new_path:
                return  # duplicate announcement, nothing changes
            if self.deaggregate:
                self._deagg_move(vp, prefix, new_path)
            else:
                self._plain_remove(vp, prefix, old_path)
                self._plain_add(vp, prefix, new_path)
        else:
            if self.deaggregate:
                self._deagg_insert(vp, prefix, new_path)
            else:
                self._plain_add(vp, prefix, new_path)

    def update_withdrawal(self, bgpelem, data: Optional[RIBNodeData]) -> None:
        if data is None:
            return  # prefix was not in the RIB
        vp = (bgpelem.collector, bgpelem.peer_asn, bgpelem.peer_address)
        if self.deaggregate:
            self._deagg_withdraw(vp, bgpelem.fields["prefix"])
        else:
            self._plain_remove(vp, bgpelem.fields["prefix"], data["as-path"])

    def dump(self, ts: datetime) -> Graph:
        """Return the current per-edge hegemony as a :class:`Graph` (bidirectional)."""
        if ts == self._last_dump_ts and self._last_graph is not None:
            return self._last_graph

        per_vp_scores: dict[tuple[str, str], list[float]] = defaultdict(list)
        for vp, ec in self.edge_paths_count.items():
            norm = self.num_total_paths.get(vp, 0.0)
            if norm <= 0:
                continue
            for edge, score in betweenness(ec, norm).items():
                per_vp_scores[edge].append(score)

        hegemony = _trim_across_vps(per_vp_scores, len(self.edge_paths_count), self.alpha)
        graph = _hegemony_graph(hegemony, self.min_hegemony)

        self._last_dump_ts, self._last_graph = ts, graph
        return graph

    def compare(self, other) -> None:  # pragma: no cover
        pass

    def set_rib(self, rib) -> None:  # pragma: no cover - not called by the pipeline
        pass


class RibHegemonyObserver(Observer):
    """Post-hoc edge hegemony as an observer.

    A thin wrapper around :func:`edge_hegemony`: :meth:`dump` recomputes the
    entire edge-hegemony graph from the current RIB (mirroring what
    ``examples/hegemony.py`` does by hand). ``update_*`` are no-ops: the
    observer holds no incremental state, it just needs a reference to the
    ``RIBTable``. ``QuickRIB.build_rib`` hands it one through
    :meth:`set_rib` once the table is built; passing ``rib=`` to the constructor
    is only needed outside the pipeline.

    Slower than the incremental :class:`HegemonyObserver` (a full RIB walk per
    dump) but the two agree when the latter runs with ``deaggregate=True``.
    """

    def __init__(
        self,
        rib=None,
        name: str = "hegemony",
        alpha: float = 0.2,
        min_hegemony: float = 0.0,
    ) -> None:
        self.rib = rib
        self.name = name
        self.alpha = alpha
        self.min_hegemony = min_hegemony
        self.last_edge_hegemony: dict[tuple[str, str], float] = {}
        self._last_dump_ts: Optional[datetime] = None
        self._last_graph: Optional[Graph] = None


    def update_rib(self, bgpelem) -> None:
        pass

    def update_announcement(self, bgpelem, data, old_data) -> None:
        pass

    def update_withdrawal(self, bgpelem, data) -> None:
        pass

    def set_rib(self, rib) -> None:
        self.rib = rib

    def compare(self, other) -> None:  # pragma: no cover
        pass

    def dump(self, ts: datetime) -> Graph:
        if ts == self._last_dump_ts and self._last_graph is not None:
            return self._last_graph
        if self.rib is None:
            raise RuntimeError(
                "RibHegemonyObserver has no RIB reference (pass rib= or call set_rib)"
            )

        self.last_edge_hegemony = edge_hegemony(self.rib.data, self.alpha)
        graph = _hegemony_graph(self.last_edge_hegemony, self.min_hegemony)

        self._last_dump_ts, self._last_graph = ts, graph
        return graph


def resize_list(initial_list: list[float], new_size: int) -> list[float]:
    """resize list to a bigger one and add trailing zeros"""
    return initial_list + [0.0] * (new_size - len(initial_list))


def _trim_count(n: int, alpha: float, force_trim: bool = True) -> int:
    """How many values are cut from **each** end of ``n`` sorted samples.

    The rule ``trim_mean`` has always applied, stated as a count rather than a
    proportion: normally ``floor(n * alpha)``, but when a single VP is worth more
    than ``alpha`` of the population, one VP is cut from each end instead, so a
    lone outlying vantage point cannot carry an edge on its own. Below three
    samples nothing is cut, because nothing would be left.
    """
    peer_share = 1.0 / n
    proportion = peer_share if (force_trim and peer_share > alpha and n > 2) else alpha
    return int(n * proportion)


def trim_mean(data, alpha: float, force_trim: bool = True) -> float:
    """Mean of ``data`` with its most extreme values cut from both ends.

    Kept as the single-sample entry point (and the reference the vectorised
    :func:`_trim_across_vps` is tested against); it no longer goes through
    ``scipy.stats.trim_mean``, whose per-call overhead dominated when hegemony
    called it once per edge.
    """
    values = sorted(data)
    cut = _trim_count(len(values), alpha, force_trim)
    kept = values[cut : len(values) - cut]
    return float(np.mean(kept))


def betweenness(paths_count: dict[Key, float], normalisation: float) -> dict[Key, float]:
    """Compute betweenness based on paths_count of asn"""
    return {node: weight / normalisation for node, weight in paths_count.items()}


def _trim_across_vps(
    per_vp_scores: dict[Key, list[float]], n_vps: int, alpha: float
) -> dict[Key, float]:
    """Trimmed mean of each key's per-VP score list, zero-padded to ``n_vps``.

    Every key is padded to the same ``n_vps``, so every key is trimmed by the
    same count. That makes this one sort and one mean over an ``n_keys x n_vps``
    array rather than a scipy call per key. On the pinned
    window that is ~35k calls per dump replaced by two array operations.
    """
    if not per_vp_scores:
        return {}
    if n_vps <= 0:
        return {key: 0.0 for key in per_vp_scores}

    keys = list(per_vp_scores)
    scores = np.zeros((len(keys), n_vps), dtype=float)
    for row, key in enumerate(keys):
        # A key seen by more VPs than the population would overflow the row; the
        # caller pads, never truncates, so this only guards against a miscount.
        values = per_vp_scores[key][:n_vps]
        scores[row, : len(values)] = values

    scores.sort(axis=1)
    cut = _trim_count(n_vps, alpha)
    kept = scores[:, cut : n_vps - cut] if cut else scores
    return dict(zip(keys, kept.mean(axis=1).tolist(), strict=True))


def _hegemony_graph(hegemony: dict[Edge, float], min_hegemony: float = 0.0) -> Graph:
    """Bidirectional :class:`Graph` from a ``{canonical_edge: hegemony}`` mapping.

    The cut is **exclusive**: an edge is kept when its hegemony is strictly
    greater than ``min_hegemony``. At the default of zero that keeps every edge
    carrying weight and drops the weightless ones, which is both what the
    incremental observer can represent and a decision no rounding can flip --
    a sum of non-negative terms is positive or it is not. See
    :class:`HegemonyObserver`.
    """
    graph = Graph()
    for (u, v), h in hegemony.items():
        if h <= min_hegemony:
            continue
        graph.add_edge((u, v))
        graph.set_edge_weight((u, v), h)
        graph.add_edge((v, u))
        graph.set_edge_weight((v, u), h)
    return graph


def node_hegemony(rib: RIBData, alpha=0.2) -> dict[str, float]:
    """Per-ASN hegemony over a reconstructed RIB.

    Each ASN is credited once per path (``set(as_path)``), where
    :func:`edge_hegemony` credits each *consecutive link* and so counts a link
    twice if a path traverses it twice. Paths that revisit an ASN are the only
    case where the two differ, and they are rare enough in real data that the
    asymmetry has never been worth the cost of de-duplicating edges per path.
    """

    node_bcs = defaultdict(list) # mapping ASN to list of their betweenness centrality (for each VP [vp1, vp2, ...])
    n_vps = sum(len(vps) for vps in rib.values())
    for vps in rib.values():
        for pfxs in vps.values():
            covered = radix_size(pfxs)
            if not covered:
                # A VP holding nothing routes nothing: it has no betweenness to
                # contribute, and normalising by its coverage would divide by zero.
                continue
            vp_bcs = defaultdict(float) # mapping ASN to betweenness centrality for a given VP
            score_increment = 1.0 / covered
            for rnode in pfxs.nodes():
                as_path = rnode.data["as-path"]
                weight = radix_prefix_size(pfxs, rnode)
                for asn in set(as_path):
                    vp_bcs[asn] += weight * score_increment
            for node, bc in vp_bcs.items():
                node_bcs[node].append(bc)

    return _trim_across_vps(node_bcs, n_vps, alpha)

def edge_hegemony(rib: RIBData, alpha=0.2) -> dict[tuple[str, str], float]:
    """Per-link hegemony over a reconstructed RIB. See :func:`node_hegemony` for
    how the two treat a path that revisits an ASN."""

    edge_bcs = defaultdict(list) # mapping edge to list of its betweenness centrality (for each VP)
    n_vps = sum(len(vps) for vps in rib.values())
    for vps in rib.values():
        for pfxs in vps.values():
            covered = radix_size(pfxs)
            if not covered:
                # See node_hegemony: an empty VP normalises by zero.
                continue
            vp_bcs = defaultdict(float) # mapping edge to betweenness centrality for a given VP
            score_increment = 1.0 / covered
            for rnode in pfxs.nodes():
                weight = radix_prefix_size(pfxs, rnode)
                for edge in _canon_edges(rnode.data["as-path"]):
                    vp_bcs[edge] += weight * score_increment
            for edge, bc in vp_bcs.items():
                edge_bcs[edge].append(bc)

    return _trim_across_vps(edge_bcs, n_vps, alpha)



