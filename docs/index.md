# QuickRIB

BGP routing table reconstruction and analysis.

QuickRIB rebuilds the routing table that each peer of a route collector held at
a chosen instant, replays the BGP updates that followed, and drives any number
of analysis modules from that single replay. The data comes from the RIPE RIS
and Route Views archives through
[PyBGPFlux](https://github.com/JustinLoye/pybgpflux).

## The problem

A route collector publishes a complete dump of its tables every two hours
(Route Views) or every eight hours (RIPE RIS), and a continuous stream of the
update messages received in between. The table at an arbitrary instant is
therefore not stored anywhere: it is the last dump before that instant with
every subsequent update applied to it. Most quantities computed from BGP data,
whether a centrality metric, a reachability signal or a classification of the
updates themselves, are functions of that table or of how it changes, so the
reconstruction is a prerequisite that every study has to repeat.

QuickRIB performs it once and exposes the result as a live object. Analysis code
attaches to the table as an *observer* and is notified of every change, so a
quantity can be maintained message by message instead of being recomputed from
a million-prefix table every few minutes. Where an analysis is cheaper to
recompute than to maintain, the same replay serves it too.

## Built-in observers

| Observer | Quantity | Reference |
| --- | --- | --- |
| `HegemonyObserver` | AS and link hegemony, a centrality robust to the collector's partial view | Fontugne, Shah and Aben, PAM 2018 |
| `IODAObserver` | Visible /24 blocks per origin AS, the BGP signal of the IODA outage detector | IODA, Georgia Tech / CAIDA |
| `BGPlayObserver` | The RIPEstat `bgplay` payload for a prefix or an AS | RIPE NCC |
| `UpdateTagger` | BLT classification of every update message | Kitabatake, Fontugne and Esaki, 2018 |
| `MADObserver` | Multi-scale anomaly scores over the hegemony temporal graph | Loye, Bautista and Fontugne, IMC 2026 |

Observers that reproduce a published signal are tested against the live API
that publishes it.

## Installation

```bash
pip install quickrib
```

## Quick start

```python
--8<-- "examples/quickstart.py"
```

## Reading on

- [Getting Started](getting_started.md): installation, parsers and configuration.
- [Reconstruction](guide/reconstruction.md): what the pipeline computes and the
  assumptions it makes about the archives.
- [Observers](guide/observers.md): the analysis interface and how to write one.
- [Performance](performance.md): what a run costs and how to keep it cheap.
