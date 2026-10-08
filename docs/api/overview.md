# API Reference

## Pipeline

[QuickRIB](quickrib.md) drives the three passes; `QuickRIBConfig` is the
validated configuration.

```python
from quickrib import QuickRIB, QuickRIBConfig

quickrib = QuickRIB.from_config(config)
quickrib.rib.attach_observer(observer)
quickrib.run()
```

[RIBTable](rib_table.md) is the reconstructed table and the subject observers
attach to. `quickrib.rib.data` maps a collector to its peers and a peer to a
radix tree of prefixes.

## Analysis

[Observers](observers.md): the `Observer` interface and the observers shipped
with QuickRIB.

| Observer | Quantity |
| --- | --- |
| `HegemonyObserver` / `RibHegemonyObserver` | AS and link hegemony |
| `IODAObserver` / `RibIODAObserver` | visible /24s per origin AS |
| `BGPlayObserver` | the RIPEstat bgplay payload |
| `UpdateTagger` / `UpdateTagsCounter` | BLT update classification |
| `MADObserver` / `MADStaticObserver` / `MADStarObserver` | anomaly detection |
| `TesterObserver` | reconstruction checks |

[Utilities](utilities.md): prefix-tree measurement and the element types
observers are handed.
