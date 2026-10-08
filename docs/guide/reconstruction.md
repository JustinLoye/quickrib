# Reconstruction

## Vantage points, dumps and updates

A route collector maintains a BGP session with each of its *peers*, and records
what every peer announces to it. Each (collector, peer) pair is a *vantage
point*: a view of the routing system from one network. The archives hold two
kinds of record per collector:

- **RIB dumps**, a snapshot of every peer's table, published every two hours by
  Route Views and every eight hours by RIPE RIS;
- **updates**, the announcements and withdrawals received from each peer, in
  files covering five or fifteen minutes.

The table a peer held at an instant \(t\) is the dump taken at the last dump
time \(t_0 \le t\), with every update received in \((t_0, t]\) applied in order.
QuickRIB computes this per vantage point, then keeps applying updates up to
`end_time`, which is how the observers see the table evolve.

## Full-feed peers

Not every peer shares a full routing table. Many sessions carry only a customer
cone or a default route, and a partial view would bias every quantity computed
over it. A peer is **full-feed** when it carries more than 80% of the distinct
prefixes seen at its collector. The rule is relative, so it needs no absolute
table size and holds across the two decades of archives, though it counts both
address families together, so an IPv4-only session at a dual-stack collector
can fall just under the bar.

With `fullfeed_only=True` (the default) only full-feed peers enter the table.
The selection is handed to every observer before the replay starts, so an
observer defined over the full-feed population, such as `IODAObserver`, never
has to count prefixes itself. It is computed on the stream after `filters`
have been applied, so with `FilterOptions(ip_version=4)` the rule is evaluated
on IPv4 prefixes alone.

## The three passes

1. **Initialization.** QuickRIB asks the broker which RIB dumps exist and takes
   each collector's most recent one at or before `start_time`. Dump times are
   looked up rather than assumed, because the cadence varies by project and
   has changed over time. Each dump is then streamed once to count prefixes
   per peer and select the full-feed peers.
2. **Build.** The dumps are streamed again and loaded into the table. A dump is
   a single instant: every entry carries the dump file's timestamp, and the
   build keeps that instant and nothing else.
3. **Replay.** Updates are replayed from each collector's dump instant to
   `end_time`. Collectors dump at different instants, so each collector skips
   the updates that predate its own dump; otherwise they would be applied on
   top of a newer table.

## Results on a fixed schedule

Observers are asked for a result at the boundaries `start_time + k * dump_res`.
`dump(ts)` receives the boundary, not the timestamp of the message that
happened to cross it, every boundary is reported even across a gap in the
stream, and the boundaries left when the stream ends are flushed. A run over a
given window therefore yields the same number of results whatever the
collectors were doing.

## The data model

```
data: collector -> (peer_asn, peer_ip) -> radix.Radix
```

Each radix node's `.data` holds `as-path`, `communities` and `time`. AS paths
are lists of ASN strings whose first element is the peer's ASN. Two kinds of
path never enter the table:

- **AS sets** (`{64500,64501}`): an aggregate stands for several origins at
  once, and every analysis here reads the last ASN as *the* origin;
- **paths that do not start at the peer**, which are malformed.

A path consisting of the peer alone is kept: it is a prefix the peer originates
itself, rare but real, and it carries no AS link, so link-based analyses see
none for it.

## RIB flavors

Some analyses need more per prefix than the table stores. A flavor subclasses
`RIBTable` and overrides `_enrich_announcement(data, old_data)`, the hook that
runs between the write and the notification. `RIBTablePathHistory` is the
shipped example: it keeps a bounded history of each prefix's previous AS paths,
which route-flap detection needs.

```python
config = QuickRIBConfig(..., rib_cls=RIBTablePathHistory)
```

## Limitations

- BGP session state changes are not part of the stream, so a peer that resets
  its session and silently loses its table is not detected.
- The archives record what the collector received, with second granularity.
  Messages within the same second are replayed in archive order.
- A replay is only as complete as the archive. A missing update file leaves
  the table as it was until the next dump; QuickRIB does not reconcile with the
  next dump.
