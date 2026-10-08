# Update Classification

`UpdateTagger` labels every BGP update with what it changed in the peer's
table, following the BLT taxonomy of Kitabatake, Fontugne and Esaki, [BLT: A
Taxonomy and Classification Tool for Mining BGP Update
Messages](https://www.semanticscholar.org/paper/be876cf8aa90c15e240a6dd7b65863f42da9225b)
(IEEE INFOCOM Workshops, Global Internet Symposium, 2018). The premise is that
an update message is only meaningful relative to the table it modifies: the
same announcement is a new route, a path change or a duplicate depending on
what the peer held before, which is exactly what the reconstruction provides.

Tags are not exclusive. One announcement can change the path, the prepending
and the communities at once.

| Tag | Meaning |
| --- | --- |
| `UPDATE_MESSAGE` | Every update. The denominator for the others. |
| `NEW_PREFIX` | The peer did not have this prefix. |
| `REMOVE_PREFIX` | A withdrawal of a prefix the peer had. |
| `ORIGIN_CHANGE` | The origin AS changed. |
| `TRANSIT_CHANGE` | The path changed, the origin did not. |
| `PATH_SWITCHING` | The path returned to where it was two updates ago. |
| `PREPENDING_ADD` / `PREPENDING_REMOVE` / `PREPENDING_CHANGE` | Repeated ASNs were added, removed or rearranged. |
| `COMMUNITY_CHANGE` | The community list changed. |
| `DUPLICATE_ANNOUNCE` | A re-announcement that changed nothing stored. |
| `DUPLICATE_WITHDRAWAL` | A withdrawal of a prefix the peer did not have. |

BLT's *other attribute change* is not produced: the table stores the AS path
and the communities only, so an update that changes another attribute is
indistinguishable from a duplicate.

## Usage

`UpdateTagsCounter` accumulates the tags and returns the totals of each window
from `dump`, as `{tag: count}` with every tag present:

```python
--8<-- "examples/update_classifier.py"
```

`UpdateTagger` performs the classification without counting and returns the
flags from each hook, for a subclass to consume.

`PATH_SWITCHING` needs to know where the path was before the previous update,
which a single `old_data` cannot say. `RIBTablePathHistory` stores that
history; without it the tag never fires.

## Building on the tags

Tag counts per window form a time series, and a surge in one class is the
anomaly BLT was designed to expose. `examples/origin_hijack.py` scores the
per-window `ORIGIN_CHANGE` count against its recent past, a minimal hijack
detector.
