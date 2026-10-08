# Getting Started

## Installation

```bash
pip install quickrib        # or: uv add quickrib
```

QuickRIB reads MRT archives through PyBGPFlux, which ships a pure-Python parser.
It is enough for a short window, but a full table is tens of millions of
records, so install a native parser for anything larger:

```bash
cargo install bgpkit-parser --features cli   # parser="bgpkit"
apt-get install bgpdump                      # parser="bgpdump"
```

## A first run

```python
--8<-- "examples/quickstart.py"
```

`run()` performs three passes over the archives, described in
[Reconstruction](guide/reconstruction.md):

1. find each collector's most recent table dump before `start_time` and
   identify the peers that carry a full table;
2. rebuild the table from those dumps;
3. replay the updates up to `end_time`, notifying every attached observer and
   asking it for a result once per `dump_res`.

## Configuration

`QuickRIBConfig` is a Pydantic model. Times are UTC; a naive `datetime` is read
as UTC.

| Field | Meaning |
| --- | --- |
| `start_time` | Instant the table is reconstructed for. It need not fall on a dump. |
| `end_time` | End of the update replay. |
| `dump_res` | Interval between two results; the dump schedule is `start_time + k * dump_res`. |
| `collectors` | RIPE RIS (`rrcNN`) and Route Views (`route-views.*`) collector names. |
| `fullfeed_only` | Keep only the peers carrying a full table (default `True`). |
| `filters` | Optional `FilterOptions` passed to PyBGPFlux (peer, prefix, address family). |
| `cache_dir` | Where downloaded archives are kept. Reuse it across runs. |
| `parser` | `pybgpkit` (default), `bgpkit`, `bgpdump` or `pybgpstream`. |
| `rib_lookback` | How far before `start_time` the broker is asked for a dump (default 24 hours). QuickRIB queries the BGPKIT broker for the archive files of each collector and takes the most recent dump in that span; a collector with none is dropped with a warning. |
| `rib_cls` | The `RIBTable` class to use; a subclass can store more per prefix. |

!!! note "Choosing `start_time`"
    The reconstruction starts from the last dump before `start_time` and
    replays the interval in between, so a window starting seven hours after a
    RIPE RIS dump costs seven hours of updates before it begins. Starting on or
    shortly after a dump is cheapest; the run logs how far back each collector
    had to reach.

## The reconstructed table

`quickrib.rib.data` maps a collector to its peers, and a peer to a
[py-radix](https://github.com/mjschultz/py-radix) tree of the prefixes it
routes. Each node holds the AS path, the communities and the time of the last
message that set them.

```python
table = quickrib.rib.get_peer_table("route-views.wide", 2497, "202.249.2.169")
node = table.search_exact("8.8.8.0/24")
if node is not None:
    print(node.data["as-path"], node.data["communities"], node.data["time"])

for collector, peer, prefix, data in quickrib.rib:   # one entry per prefix per peer
    ...
```
