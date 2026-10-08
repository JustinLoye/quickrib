"""Everything that reads a ``radix.Radix`` prefix tree.

The RIB is a radix tree per vantage point (see
:data:`~quickrib.rib_table.RIBData`), and several observers measure the address
space one holds. Those measurements used to be spread across the observers that
happened to need them first; they live here so there is one implementation of
each and one place to test them (``tests/test_radix.py``).

Two questions come up, and they are not the same one:

* **How much space does this one node contribute?** :func:`radix_prefix_size`
  subtracts the node's *direct children*, so a prefix and the more specifics
  announced inside it do not both claim the same addresses. This is
  what AS hegemony weights a path by.
* **How much space does the whole tree cover?** :func:`radix_size` (in
  addresses) and :func:`radix_block_count` (in /24s or /48s) sum only the
  *least specific* nodes, since anything nested inside one of them is already
  counted. This is the union, and it is what IODA's visible-/24s signal reports.

Every function reads the address family off each node, so a tree holding both
IPv4 and IPv6 prefixes is measured correctly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import radix

if TYPE_CHECKING:  # pragma: no cover - stubs only, see typings/radix.pyi
    from radix import RadixNode

PRECOMPUTED_BLOCK_SIZES_IPV4: dict[int, int] = {plen: 2 ** (32 - plen) for plen in range(33)}
"""Number of **addresses** in an IPv4 prefix, by prefix length."""
PRECOMPUTED_BLOCK_SIZES_IPV6: dict[int, int] = {plen: 2 ** (128 - plen) for plen in range(129)}
"""Number of **addresses** in an IPv6 prefix, by prefix length."""

# The same quantity at the granularity operators actually route in. A prefix
# longer than the block is deliberately absent rather than fractional: it does
# not fill a /24, and is not globally routable either.
PRECOMPUTED_BLOCK_COUNTS_IPV4: dict[int, int] = {plen: 1 << (24 - plen) for plen in range(25)}
"""Number of **/24s** in an IPv4 prefix, by prefix length. Empty above /24."""
PRECOMPUTED_BLOCK_COUNTS_IPV6: dict[int, int] = {plen: 1 << (48 - plen) for plen in range(49)}
"""Number of **/48s** in an IPv6 prefix, by prefix length. Empty above /48."""


def tree_size(rtree: radix.Radix) -> int:
    """Number of prefixes in ``rtree``.

    ``py-radix`` defines no ``__len__``. ``nodes()`` materialises a list, but it
    does so in C and is several times faster than iterating the tree from
    Python (16ms against 60ms for 160k prefixes).
    """
    return len(rtree.nodes())


def block_size(prefix: str) -> int:
    """Nominal number of addresses in ``prefix``, ignoring any tree it sits in.

    Cheap enough for a hot path: one string split and one dict lookup.
    """
    plen = int(prefix.rsplit("/", 1)[1])
    if ":" in prefix:
        return PRECOMPUTED_BLOCK_SIZES_IPV6[plen]
    return PRECOMPUTED_BLOCK_SIZES_IPV4[plen]


def direct_children(rtree: radix.Radix, rnode: RadixNode) -> Iterator[RadixNode]:
    """Yield the nodes whose *immediate* parent in ``rtree`` is ``rnode``.

    ``search_covered`` returns the whole subtree, including ``rnode`` itself and
    every grandchild; the parent test keeps only the first generation. A node is
    never its own parent, so ``rnode`` drops out without a special case.
    """
    prefix = rnode.prefix
    for child in rtree.search_covered(prefix):
        if child.parent is not None and child.parent.prefix == prefix:
            yield child


def radix_prefix_size(rtree: radix.Radix, rnode: RadixNode) -> int:
    """Addresses ``rnode`` contributes, after deaggregation.

    Its own block minus its direct children's: the space they cover is theirs to
    account for, and counting it twice is what makes a naive sum over a peer's
    table exceed the address space it actually routes. Grandchildren are already
    subtracted from their own parent, so they must not be subtracted again here.
    """
    sizes = (
        PRECOMPUTED_BLOCK_SIZES_IPV6
        if ":" in rnode.prefix
        else PRECOMPUTED_BLOCK_SIZES_IPV4
    )
    return sizes[rnode.prefixlen] - sum(
        sizes[child.prefixlen] for child in direct_children(rtree, rnode)
    )


def _union(rtree: radix.Radix, ipv4_sizes: dict[int, int], ipv6_sizes: dict[int, int]) -> int:
    """Sum ``sizes`` over the least-specific nodes only, which is the tree's union.

    A node is least specific when no other node in the tree covers it, which
    ``py-radix`` exposes as ``parent is None`` (``parent`` skips the tree's
    internal glue nodes). Anything with a parent is nested inside a node that
    is already counted. A prefix length missing from the table contributes
    nothing.

    Reading ``parent`` is three times cheaper than the ``search_worst`` lookup
    it replaced, and this runs once per node of every tree it measures.
    """
    total = 0
    for rnode in rtree.nodes():
        if rnode.parent is None:
            sizes = ipv6_sizes if ":" in rnode.prefix else ipv4_sizes
            total += sizes.get(rnode.prefixlen, 0)
    return total


def radix_size(rtree: radix.Radix) -> int:
    """Number of **addresses** the union of ``rtree``'s prefixes covers."""
    return _union(rtree, PRECOMPUTED_BLOCK_SIZES_IPV4, PRECOMPUTED_BLOCK_SIZES_IPV6)


def radix_block_count(rtree: radix.Radix) -> int:
    """Number of **/24s** (or /48s for IPv6) the union of ``rtree`` covers.

    :func:`radix_size` in the unit operators route in. A prefix longer than the
    block contributes nothing, so a lone /25 counts as zero /24s.
    """
    return _union(rtree, PRECOMPUTED_BLOCK_COUNTS_IPV4, PRECOMPUTED_BLOCK_COUNTS_IPV6)
