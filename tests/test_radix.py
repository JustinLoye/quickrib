"""Unit tests for :mod:`quickrib.radix_utils`.

Every helper that reads a prefix tree is tested here rather than alongside the
observer that happens to use it, so the two questions the module answers stay
side by side: what one node contributes (:func:`radix_prefix_size`) and what the
whole tree covers (:func:`radix_size`, :func:`radix_block_count`).

These tests exercise a real ``radix.Radix()`` so that ``search_covered``,
``search_worst`` and node ``.parent`` behave exactly as they do in production.
"""

import socket

import pytest
import radix

from quickrib.radix_utils import (
    PRECOMPUTED_BLOCK_COUNTS_IPV4,
    PRECOMPUTED_BLOCK_COUNTS_IPV6,
    PRECOMPUTED_BLOCK_SIZES_IPV4,
    PRECOMPUTED_BLOCK_SIZES_IPV6,
    block_size,
    direct_children,
    radix_block_count,
    radix_prefix_size,
    radix_size,
    tree_size,
)


def make_tree(*prefixes):
    tree = radix.Radix()
    for prefix in prefixes:
        tree.add(prefix)
    return tree


# ---------------------------------------------------------------------------
# Precomputed block size tables
# ---------------------------------------------------------------------------

class TestPrecomputedBlockSizes:
    def test_ipv4_full_range(self):
        assert PRECOMPUTED_BLOCK_SIZES_IPV4[0] == 2 ** 32
        assert PRECOMPUTED_BLOCK_SIZES_IPV4[24] == 256
        assert PRECOMPUTED_BLOCK_SIZES_IPV4[32] == 1

    def test_ipv4_has_all_prefix_lengths(self):
        assert set(PRECOMPUTED_BLOCK_SIZES_IPV4.keys()) == set(range(33))

    def test_ipv6_full_range(self):
        assert PRECOMPUTED_BLOCK_SIZES_IPV6[0] == 2 ** 128
        assert PRECOMPUTED_BLOCK_SIZES_IPV6[64] == 2 ** 64
        assert PRECOMPUTED_BLOCK_SIZES_IPV6[128] == 1

    def test_ipv6_has_all_prefix_lengths(self):
        assert set(PRECOMPUTED_BLOCK_SIZES_IPV6.keys()) == set(range(129))

    def test_block_count_tables_stop_at_the_block(self):
        # A prefix longer than the block does not fill one, so it has no entry
        # rather than a fractional one.
        assert PRECOMPUTED_BLOCK_COUNTS_IPV4[24] == 1
        assert PRECOMPUTED_BLOCK_COUNTS_IPV4[16] == 256
        assert set(PRECOMPUTED_BLOCK_COUNTS_IPV4.keys()) == set(range(25))
        assert PRECOMPUTED_BLOCK_COUNTS_IPV6[48] == 1
        assert set(PRECOMPUTED_BLOCK_COUNTS_IPV6.keys()) == set(range(49))


class TestStubMatchesTheLibrary:
    """`typings/radix.pyi` is hand-written and pyright treats it as the truth, so
    a member it promises that `py-radix` does not have type-checks clean and
    fails at runtime. Exercise each one.

    This caught a declared `__len__` the library does not define.
    """

    @staticmethod
    def _tree() -> radix.Radix:
        rtree = radix.Radix()
        rtree.add("10.0.0.0/8")
        rtree.add("10.1.0.0/16")
        return rtree

    def test_every_stubbed_radix_method_exists(self):
        rtree = self._tree()
        assert [node.prefix for node in rtree] == ["10.0.0.0/8", "10.1.0.0/16"]
        assert rtree.search_exact("10.1.0.0/16") is not None
        assert rtree.search_best("10.1.2.3") is not None
        assert rtree.search_worst("10.1.2.3") is not None
        assert len(rtree.search_covered("10.0.0.0/8")) == 2
        assert len(rtree.search_covering("10.1.0.0/16")) == 2
        assert len(rtree.nodes()) == 2
        assert sorted(rtree.prefixes()) == ["10.0.0.0/8", "10.1.0.0/16"]
        rtree.delete("10.1.0.0/16")
        assert rtree.prefixes() == ["10.0.0.0/8"]

    def test_every_stubbed_node_attribute_exists(self):
        # Keep the tree alive: a node does not own the tree it came from, and
        # `parent` walks it, and on a temporary it comes back None.
        rtree = self._tree()
        node = rtree.search_exact("10.1.0.0/16")
        assert node is not None
        assert node.prefix == "10.1.0.0/16"
        assert node.prefixlen == 16
        assert node.network == "10.1.0.0"
        assert isinstance(node.packed, bytes) and len(node.packed) == 4
        assert node.family == socket.AF_INET
        assert isinstance(node.data, dict)
        parent = node.parent
        assert parent is not None and parent.prefix == "10.0.0.0/8"

    def test_radix_has_no_len_so_tree_size_is_the_way_to_count(self):
        # The stub declared a `__len__` the library does not have, so `len(tree)`
        # type-checked and raised at runtime.
        rtree = self._tree()
        assert not hasattr(rtree, "__len__")
        assert tree_size(rtree) == 2

    def test_tree_size_is_zero_for_an_empty_tree(self):
        assert tree_size(radix.Radix()) == 0


class TestBlockSize:
    """`block_size` reads a prefix string, with no tree involved."""

    def test_reads_the_family_off_the_prefix(self):
        assert block_size("192.0.2.0/24") == 256
        assert block_size("10.0.0.0/22") == 1024
        assert block_size("2001:db8::/32") == 2 ** 96
        assert block_size("0.0.0.0/0") == 2 ** 32


class TestDirectChildren:
    def test_only_the_first_generation(self):
        rtree = make_tree("192.0.2.0/24", "192.0.2.0/25", "192.0.2.0/26")
        parent = rtree.search_exact("192.0.2.0/24")
        assert parent is not None
        assert [c.prefix for c in direct_children(rtree, parent)] == ["192.0.2.0/25"]

    def test_node_is_not_its_own_child(self):
        rtree = make_tree("192.0.2.0/24")
        node = rtree.search_exact("192.0.2.0/24")
        assert node is not None
        assert list(direct_children(rtree, node)) == []

    def test_unrelated_prefixes_are_not_children(self):
        rtree = make_tree("192.0.2.0/24", "198.51.100.0/25")
        node = rtree.search_exact("192.0.2.0/24")
        assert node is not None
        assert list(direct_children(rtree, node)) == []


# ---------------------------------------------------------------------------
# IPv4: radix_prefix_size
# ---------------------------------------------------------------------------

class TestRadixPrefixSizeIPv4:
    def test_no_children_returns_full_block(self):
        rtree = radix.Radix()
        node = rtree.add("192.0.2.0/24")

        assert radix_prefix_size(rtree, node) == 256

    def test_single_direct_child_is_subtracted(self):
        rtree = radix.Radix()
        parent = rtree.add("192.0.2.0/24")
        rtree.add("192.0.2.0/25")  # direct child, half the space

        assert radix_prefix_size(rtree, parent) == 256 - 128

    def test_two_direct_children_fully_cover_parent(self):
        rtree = radix.Radix()
        parent = rtree.add("192.0.2.0/24")
        rtree.add("192.0.2.0/25")
        rtree.add("192.0.2.128/25")

        assert radix_prefix_size(rtree, parent) == 0

    def test_grandchild_is_not_subtracted_from_parent(self):
        # 192.0.2.0/26 is a child of 192.0.2.0/25, NOT a direct child of /24.
        # It must not be double counted against the /24 node.
        rtree = radix.Radix()
        parent = rtree.add("192.0.2.0/24")
        child = rtree.add("192.0.2.0/25")
        rtree.add("192.0.2.0/26")

        # parent only loses the /25 block, not the /26 nested inside it
        assert radix_prefix_size(rtree, parent) == 256 - 128
        # the /25 node itself loses the /26 as its own direct child
        assert radix_prefix_size(rtree, child) == 128 - 64

    def test_unrelated_prefix_elsewhere_in_tree_is_ignored(self):
        rtree = radix.Radix()
        parent = rtree.add("192.0.2.0/24")
        rtree.add("198.51.100.0/25")  # unrelated prefix, different block

        assert radix_prefix_size(rtree, parent) == 256

    def test_host_route(self):
        rtree = radix.Radix()
        node = rtree.add("192.0.2.1/32")

        assert radix_prefix_size(rtree, node) == 1

    def test_default_route_with_no_children(self):
        rtree = radix.Radix()
        node = rtree.add("0.0.0.0/0")

        assert radix_prefix_size(rtree, node) == 2 ** 32

    def test_multiple_direct_children_partial_deaggregation(self):
        # /24 split into a /25 and two /26s covering the other half
        rtree = radix.Radix()
        parent = rtree.add("192.0.2.0/24")
        rtree.add("192.0.2.0/25")
        rtree.add("192.0.2.128/26")
        rtree.add("192.0.2.192/26")

        expected = 256 - 128 - 64 - 64
        assert radix_prefix_size(rtree, parent) == expected


# ---------------------------------------------------------------------------
# IPv6: radix_prefix_size
# ---------------------------------------------------------------------------

class TestRadixPrefixSizeIPv6:
    def test_no_children_returns_full_block(self):
        rtree = radix.Radix()
        node = rtree.add("2001:db8::/32")

        assert radix_prefix_size(rtree, node) == 2 ** 96

    def test_single_direct_child_is_subtracted(self):
        rtree = radix.Radix()
        parent = rtree.add("2001:db8::/32")
        rtree.add("2001:db8::/33")

        assert radix_prefix_size(rtree, parent) == 2 ** 96 - 2 ** 95

    def test_grandchild_is_not_subtracted_from_parent(self):
        rtree = radix.Radix()
        parent = rtree.add("2001:db8::/32")
        child = rtree.add("2001:db8::/33")
        rtree.add("2001:db8::/34")

        assert radix_prefix_size(rtree, parent) == 2 ** 96 - 2 ** 95
        assert radix_prefix_size(rtree, child) == 2 ** 95 - 2 ** 94

    def test_host_route(self):
        rtree = radix.Radix()
        node = rtree.add("2001:db8::1/128")

        assert radix_prefix_size(rtree, node) == 1

    def test_default_route_with_no_children(self):
        rtree = radix.Radix()
        node = rtree.add("::/0")

        assert radix_prefix_size(rtree, node) == 2 ** 128


# ---------------------------------------------------------------------------
# Mixed tree (both families present): ensures the correct table is chosen
# ---------------------------------------------------------------------------

class TestMixedFamilyTree:
    def test_ipv4_and_ipv6_do_not_interfere(self):
        rtree = radix.Radix()
        v4_parent = rtree.add("192.0.2.0/24")
        rtree.add("192.0.2.0/25")

        v6_parent = rtree.add("2001:db8::/32")
        rtree.add("2001:db8::/33")

        assert radix_prefix_size(rtree, v4_parent) == 256 - 128
        assert radix_prefix_size(rtree, v6_parent) == 2 ** 96 - 2 ** 95


# ---------------------------------------------------------------------------
# IPv4: radix_size
#
# radix_size sums the block size of every node that is its own "worst"
# (i.e. least-specific) match, meaning every node that is not
# covered by some shorter/less-specific prefix already present in the
# tree. This yields the total address space covered by the tree without
# double-counting deaggregated (more-specific) announcements nested
# inside a covering less-specific one.
# ---------------------------------------------------------------------------

class TestRadixSizeIPv4:
    def test_empty_tree_is_zero(self):
        rtree = radix.Radix()

        assert radix_size(rtree) == 0

    def test_single_prefix(self):
        rtree = radix.Radix()
        rtree.add("192.0.2.0/24")

        assert radix_size(rtree) == 256

    def test_parent_and_child_only_parent_counted(self):
        # The child is fully covered by the less-specific parent, so it
        # must not be counted again; the total is just the parent block.
        rtree = radix.Radix()
        rtree.add("192.0.2.0/24")
        rtree.add("192.0.2.0/25")

        assert radix_size(rtree) == 256

    def test_parent_and_grandchild_only_parent_counted(self):
        rtree = radix.Radix()
        rtree.add("192.0.2.0/24")
        rtree.add("192.0.2.0/25")
        rtree.add("192.0.2.0/26")

        assert radix_size(rtree) == 256

    def test_child_only_no_parent_in_tree(self):
        # If the less-specific prefix was never inserted, the more-specific
        # prefix is its own worst match and must be counted at its own size.
        rtree = radix.Radix()
        rtree.add("192.0.2.0/25")

        assert radix_size(rtree) == 128

    def test_two_disjoint_prefixes_are_summed(self):
        rtree = radix.Radix()
        rtree.add("192.0.2.0/24")
        rtree.add("198.51.100.0/25")

        assert radix_size(rtree) == 256 + 128

    def test_two_children_without_parent_are_both_counted(self):
        # Neither child covers the other, and there's no less-specific
        # prefix in the tree, so both are their own worst match.
        rtree = radix.Radix()
        rtree.add("192.0.2.0/25")
        rtree.add("192.0.2.128/25")

        assert radix_size(rtree) == 128 + 128

    def test_host_route_only(self):
        rtree = radix.Radix()
        rtree.add("192.0.2.1/32")

        assert radix_size(rtree) == 1

    def test_default_route_absorbs_everything(self):
        # The default route is the least specific possible prefix, so it
        # is the worst match for every node, including itself; only its
        # own block size is counted.
        rtree = radix.Radix()
        rtree.add("0.0.0.0/0")
        rtree.add("192.0.2.0/24")
        rtree.add("198.51.100.0/25")

        assert radix_size(rtree) == 2 ** 32

    def test_siblings_under_a_glue_node_are_counted_once_each(self):
        # 10.1.0.0/16 and 10.2.0.0/16 share an internal glue node that is not a
        # prefix of the tree. The union reads `parent`, which must skip it and
        # land on the /8 (or on nothing, once the /8 is gone).
        rtree = radix.Radix()
        rtree.add("10.0.0.0/8")
        rtree.add("10.1.0.0/16")
        rtree.add("10.2.0.0/16")
        assert radix_size(rtree) == 2 ** 24

        rtree.delete("10.0.0.0/8")
        assert radix_size(rtree) == 2 * 2 ** 16

    def test_mixed_aggregated_and_disjoint_prefixes(self):
        # 192.0.2.0/24 fully covers its /25 child (not double-counted),
        # while 198.51.100.0/25 is unrelated and counted separately.
        rtree = radix.Radix()
        rtree.add("192.0.2.0/24")
        rtree.add("192.0.2.0/25")
        rtree.add("198.51.100.0/25")

        assert radix_size(rtree) == 256 + 128


# ---------------------------------------------------------------------------
# IPv6: radix_size
# ---------------------------------------------------------------------------

class TestRadixSizeIPv6:
    def test_empty_tree_is_zero(self):
        rtree = radix.Radix()

        assert radix_size(rtree) == 0

    def test_single_prefix(self):
        rtree = radix.Radix()
        rtree.add("2001:db8::/32")

        assert radix_size(rtree) == 2 ** 96

    def test_parent_and_child_only_parent_counted(self):
        rtree = radix.Radix()
        rtree.add("2001:db8::/32")
        rtree.add("2001:db8::/33")

        assert radix_size(rtree) == 2 ** 96

    def test_child_only_no_parent_in_tree(self):
        rtree = radix.Radix()
        rtree.add("2001:db8::/33")

        assert radix_size(rtree) == 2 ** 95

    def test_two_disjoint_prefixes_are_summed(self):
        rtree = radix.Radix()
        rtree.add("2001:db8::/32")
        rtree.add("2001:db9::/32")

        assert radix_size(rtree) == 2 ** 96 + 2 ** 96

    def test_host_route_only(self):
        rtree = radix.Radix()
        rtree.add("2001:db8::1/128")

        assert radix_size(rtree) == 1

    def test_default_route_absorbs_everything(self):
        rtree = radix.Radix()
        rtree.add("::/0")
        rtree.add("2001:db8::/32")

        assert radix_size(rtree) == 2 ** 128


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# ---------------------------------------------------------------------------
# radix_block_count: the same union, counted in /24s (or /48s)
# ---------------------------------------------------------------------------

class TestRadixBlockCount:
    def test_is_the_union_not_the_sum(self):
        # A /22 is 4 /24s; the more specifics nested in it are already counted.
        assert radix_block_count(make_tree("10.0.0.0/22")) == 4
        assert radix_block_count(make_tree("10.0.0.0/22", "10.0.1.0/24")) == 4
        assert radix_block_count(make_tree("10.0.0.0/22", "10.9.0.0/24")) == 5
        assert radix_block_count(make_tree("10.0.0.0/24", "10.0.1.0/24")) == 2

    def test_ignores_prefixes_longer_than_the_block(self):
        assert radix_block_count(make_tree("10.0.0.0/25")) == 0
        assert radix_block_count(make_tree("10.0.0.0/24", "10.0.0.0/25")) == 1

    def test_ipv6_counts_slash48s(self):
        assert radix_block_count(make_tree("2001:db8::/32")) == 1 << 16
        assert radix_block_count(make_tree("2001:db8::/48")) == 1
        assert radix_block_count(make_tree("2001:db8::/64")) == 0

    def test_empty_tree_is_zero(self):
        assert radix_block_count(radix.Radix()) == 0


# ---------------------------------------------------------------------------
# Mixed-family trees
# ---------------------------------------------------------------------------

class TestMixedFamilyTotals:
    """The family is read off each node, not passed in for the whole tree.

    A peer's table holds both families, so a total that had to be told which one
    it was looking at could only ever be right about half of it.
    """

    def test_radix_size_adds_both_families(self):
        rtree = make_tree("192.0.2.0/24", "2001:db8::/32")
        assert radix_size(rtree) == 256 + 2 ** 96

    def test_radix_block_count_adds_both_families(self):
        rtree = make_tree("10.0.0.0/22", "2001:db8::/48")
        assert radix_block_count(rtree) == 4 + 1

    def test_deaggregation_stays_within_a_family(self):
        rtree = make_tree("192.0.2.0/24", "192.0.2.0/25", "2001:db8::/32", "2001:db8::/33")
        assert radix_size(rtree) == 256 + 2 ** 96
