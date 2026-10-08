# Observers

An observer is an analysis module attached to the table. The pipeline notifies
it of every change and asks it for a result once per `dump_res`.

```python
quickrib.rib.attach_observer(my_observer)
```

## The interface

Inherit `Observer` and override the hooks you need; every method has a no-op
default. An analysis is usually written with the first four: the three update
hooks receive every change to the table, and `dump` returns the result.

| Method | Called | Arguments |
| --- | --- | --- |
| `update_rib(bgpelem)` | once per RIB-dump entry, while the table is built | the element |
| `update_announcement(bgpelem, data, old_data)` | on every announcement | `data` is the node after the change; `old_data` a snapshot before it, or `None` for a new prefix |
| `update_withdrawal(bgpelem, data)` | on every withdrawal | `data` is the withdrawn node, or `None` if the peer did not have the prefix |
| `dump(ts)` | at every boundary of the dump schedule | the boundary; **return** the result |

Three more hooks exist for the less common cases: `set_full_feed_peers(vps)`
hands over the full-feed selection before the replay, for an analysis defined
over that population; `set_rib(rib)` hands over a reference to the table once
it is built, for an analysis that must read the table rather than follow its
changes; `compare(other)` reports differences against another observer at the
end of a run.

Elements are `pybgpflux.BGPElement` values whose `as-path` has already been
parsed into a list of ASN strings. `quickrib.elements` types the two shapes an
observer meets, `ParsedElement` for a route and `WithdrawalElement` for a
withdrawal, which carries a prefix and nothing else.

## Writing one

```python
--8<-- "examples/custom_observer.py"
```

Three conventions keep an observer correct and cheap:

- **Observers return results; they do not write files.** Where output goes is
  the calling program's decision.
- **The update hooks are hot.** The replay runs at several hundred thousand
  elements per second and every observer runs inside that loop. Keep the hooks O(1), avoid
  per-element allocation, and push aggregation into `dump`.
- **Work from the notification, not the table.** `data` and `old_data` are
  enough for most analyses, and comparing them is how an observer tells a real
  change from a re-announcement that only moved an attribute it does not care
  about. `set_rib()` exists for the cases where that is not possible.

## Incremental or post-hoc

There are two ways to produce a result at every window. The **post-hoc** way
ignores the updates and recomputes the result from the finished table at each
dump. It is simple to write, but its cost is the size of the table every time:
a million prefixes have to be walked to account for the few thousand updates of
the last five minutes. The **incremental** way updates the result as each
message arrives, so a dump costs only what changed since the last one. That is
what makes a 5-minute, or finer, resolution affordable over a long window, and
it is the reason the table is exposed as a stream of changes rather than as a
finished object.

Two of the shipped observers come in both forms, and they agree:

| Quantity | Incremental | Recompute per dump |
| --- | --- | --- |
| IODA visible /24s | `IODAObserver` | `RibIODAObserver` |
| Link hegemony | `HegemonyObserver` | `RibHegemonyObserver` |

The incremental form pays a warm-up during the build, proportional to the size
of the table, and then costs only the changes. [Performance](../performance.md)
measures where it overtakes the recomputing form: within the first dump for
IODA, after about four for the deaggregating hegemony.

## Composing observers

Observers are ordinary objects, so one can drive another. `MADObserver` owns a
`HegemonyObserver`, forwards every update to it and reads its graph at each
dump; `UpdateTagsCounter` extends `UpdateTagger` and counts the flags the
parent returns; `examples/origin_hijack.py` extends the counter again to score
one tag against its recent past. Several observers can also share one upstream
observer, as `examples/mad_anomaly.py` does with three detectors over a single
hegemony graph, attached first so its memoised dump is computed once.

Observers are notified in attachment order. When one depends on another's
state within the same hook, attach the dependency first, or set the order
explicitly with `set_observers_announcement` and `set_observers_withdraw`.
