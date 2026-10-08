# Performance

## What a run costs

A collector's RIB dump dominates. The pipeline streams each dump twice, once to
select the full-feed peers and once to build the table, and a full table is
tens of millions of records: `route-views.eqix` holds about 18M entries,
`rrc04` in 2010 about 2M. Updates are two to three orders of magnitude fewer
over a window of a few hours. The replay itself runs at several hundred
thousand elements per second with the `bgpkit` parser; every attached observer
runs inside that loop, so a slow one slows the whole pipeline.

Memory follows the table. A run over four collectors with 18 full-feed peers
and four observers attached peaked at 30 GB; a single large Route Views
collector with one observer stays near 10 GB. Run heavy jobs one at a time.

## Incremental against recomputing

Two observers ship in both forms, which makes the trade measurable.
`examples/update_mechanism.py` attaches all four to one run and attributes wall
time to each. Measured over four collectors (`route-views.wide`,
`route-views.perth`, `rrc04`, `rrc06`), 18 full-feed peers, 15.9M RIB entries,
329k updates and six 5-minute dumps:

| Quantity | Form | Warm-up | Per update | Per dump | Total |
| --- | --- | --- | --- | --- | --- |
| IODA visible /24s | `IODAObserver` | 12.0 s (0.75 µs per entry) | 0.55 µs | 0.35 s | 14 s |
| | `RibIODAObserver` | none | none | 41.6 s | 251 s |
| Link hegemony, deaggregated | `HegemonyObserver` | 259 s (16 µs per entry) | 4.6 µs | 3.5 s | 281 s |
| | `RibHegemonyObserver` | none | none | 70.1 s | 422 s |

The recomputing form pays O(peers x prefixes) at every dump however little
changed; the incremental form pays a warm-up proportional to the table once,
then only what changed. Where the two cross depends on how expensive the
per-entry bookkeeping is:

- IODA's incremental form costs one dictionary update per entry and pays its
  warm-up back within the first dump.
- Hegemony's deaggregating form maintains a prefix trie per vantage point, at
  16 µs per entry, and needs about four dumps (20 minutes of window) to pay
  back. Over a shorter window, recomputing wins.

Both forms produce the same result; `tests/test_ioda.py` and
`tests/test_hegemony.py` pin that.

## Keeping a run cheap

- **Start on or shortly after a dump.** The interval between the last dump and
  `start_time` is replayed before the window begins. Route Views dumps every
  two hours, RIPE RIS every eight.
- **Prefer the smallest collector that answers the question.** A small
  collector runs in a fraction of the time, but with fewer well-connected peers
  it can miss the more specifics an event is made of.
- **Leave `fullfeed_only=True`** unless the analysis needs partial feeds. The
  excluded peers never enter the table, which roughly halves the build.
- **Set `cache_dir`** and reuse it. Archives are downloaded once.
- **Install a native parser** (`bgpkit` or `bgpdump`). The default pure-Python
  parser is several times slower.
- **Keep observer update hooks O(1).** No per-element NumPy, no large
  allocations, no I/O; aggregate in `dump`, which runs once per window.
