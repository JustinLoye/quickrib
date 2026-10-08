"""Minimal stubs for `py-radix`, which ships none.

Only what QuickRIB uses. The RIB is a `Radix` per vantage point, so without
these every prefix-tree read is `Unknown` and nothing downstream can be checked.
"""

from collections.abc import Iterator
from typing import Any

class RadixNode:
    prefix: str
    """The CIDR string, e.g. "192.168.1.0/24"."""
    prefixlen: int
    """The integer mask, e.g. 24."""
    network: str
    """The base address string, e.g. "192.168.1.0"."""
    packed: bytes
    """The binary address: 4 bytes for IPv4, 16 for IPv6."""
    family: int
    """Address family: 2 for IPv4 (AF_INET), 10 or 30 for IPv6 (AF_INET6)."""
    data: Any
    """Free-form payload. QuickRIB narrows it to
    :class:`~quickrib.elements.RIBNodeData` at the point it is read."""
    parent: RadixNode | None
    """The next less-specific node present in the same tree, if any."""

class Radix:
    def __init__(self) -> None: ...
    def __iter__(self) -> Iterator[RadixNode]: ...
    # No `__len__`: `py-radix` does not define one, and promising it here would
    # type-check `len(tree)` and fail at runtime. Use `radix_utils.tree_size`.
    def add(self, network: str, masklen: int = ..., packed: bytes = ...) -> RadixNode: ...
    def delete(self, network: str, masklen: int = ...) -> None: ...
    def search_exact(self, network: str, masklen: int = ...) -> RadixNode | None: ...
    def search_best(self, network: str, masklen: int = ...) -> RadixNode | None: ...
    def search_worst(self, network: str, masklen: int = ...) -> RadixNode | None: ...
    def search_covered(self, network: str, masklen: int = ...) -> list[RadixNode]: ...
    def search_covering(self, network: str, masklen: int = ...) -> list[RadixNode]: ...
    def nodes(self) -> list[RadixNode]: ...
    def prefixes(self) -> list[str]: ...
