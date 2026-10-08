"""Small helpers with no home of their own."""

from collections.abc import Mapping
from typing import TypeVar

K = TypeVar("K")
V = TypeVar("V")


def dict_diff(
    dict1: Mapping[K, V], dict2: Mapping[K, V]
) -> tuple[dict[K, V], dict[K, V], dict[K, tuple[V, V]]]:
    """Compare two mappings.

    Returns:
    - `added`: key-value pairs present in the second mapping but not the first.
    - `removed`: key-value pairs present in the first but not the second.
    - `modified`: keys present in both, mapped to `(before, after)`.
    """
    added = {key: value for key, value in dict2.items() if key not in dict1}
    removed = {key: value for key, value in dict1.items() if key not in dict2}
    modified = {
        key: (value, dict2[key])
        for key, value in dict1.items()
        if key in dict2 and value != dict2[key]
    }
    return added, removed, modified
