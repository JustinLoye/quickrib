# AS Hegemony

AS hegemony measures how much of the Internet's address space depends on a
given AS, or on a given AS link, to be reached. It was introduced by Fontugne,
Shah and Aben in [The (thin) Bridges of AS Connectivity: Measuring Dependency
using AS Hegemony](https://arxiv.org/abs/1711.02805) (PAM 2018) and is the
metric behind the [Internet Health Report](https://www.ihr.live).

## Definition

Consider one vantage point \(v\), a collector peer carrying a full table. Every
prefix \(p\) it routes comes with an AS path, and every AS on that path forwards
traffic towards \(p\). The betweenness centrality of an AS \(X\) seen from \(v\)
is the share of the address space routed by \(v\) whose path traverses \(X\):

\[
BC_v(X) = \frac{\sum_{p \,:\, X \in \mathrm{path}_v(p)} w(p)}{\sum_{p} w(p)}
\]

where \(w(p)\) is the number of addresses in \(p\). A transit provider scores
high because most paths traverse it; a stub network scores close to zero
because only its own prefixes do.

A single vantage point is a biased observer: its own upstream providers appear
on every one of its paths and would score near one. Hegemony removes that bias
by computing the centrality independently from each vantage point and taking a
**trimmed mean** across them:

\[
H(X) = \mathrm{TM}_\alpha\big(\{BC_v(X)\}_v\big)
\]

With \(n\) vantage points, the \(\lfloor \alpha n \rfloor\) lowest and highest
values are discarded before averaging. QuickRIB discards at least one value
from each end as soon as there are more than two vantage points, so a lone
vantage point can never carry an AS into the result. What survives is the
dependency that many independent vantage points agree on.

**Edge hegemony** applies the same construction to AS links, the consecutive
pairs on a path. Links are undirected, so \((A, B)\) and \((B, A)\) are the same
link, and the repeats introduced by path prepending are collapsed. Edge hegemony
is what the [anomaly detectors](mad.md) consume, because a link appearing,
disappearing or changing weight localises an event better than a node does.

## Address-space weighting

Prefixes overlap. A peer routing `10.0.0.0/16` together with `10.0.1.0/24`
would count the addresses of the /24 twice if both were taken at their nominal
size. **Deaggregation** attributes each address to the most specific prefix
covering it, by subtracting a prefix's direct more specifics from it.

- `deaggregate=True` applies this and matches the post-hoc computation exactly.
- `deaggregate=False` (the default for the incremental observer) weights each
  prefix by its nominal size. It is cheaper, rankings stay close, but absolute
  values differ.

## Usage

Over a reconstructed table:

```python
--8<-- "examples/hegemony_quickstart.py"
```

`node_hegemony` returns `{asn: hegemony}` and `edge_hegemony` returns
`{(asn, asn): hegemony}` with the smaller ASN first.

For a time series, `HegemonyObserver` maintains the per-vantage-point counts in
the update path and produces the link graph at every dump:

```python
from quickrib.observers import HegemonyObserver

observer = HegemonyObserver(deaggregate=True, alpha=0.2)
quickrib.rib.attach_observer(observer)
quickrib.run()
graph = observer.dump(config.end_time)   # Graph; graph.to_weightfn() is {(u, v): hegemony}
```

The graph holds both orientations of every link. `alpha` is the trimming
fraction; `min_hegemony` drops links at or below a threshold and defaults to
zero, so only weightless links are dropped. A positive threshold decides graph
membership by comparing floating-point values, which the anomaly detectors are
sensitive to; see [Anomaly Detection](mad.md).

## Validation

`examples/hegemony.py` is a marimo notebook that computes hegemony on the
synthetic topology of the paper, checks the result against the published
figure, then applies the same code to a reconstructed table, where the top ASes
are Tier-1 providers and known single-homed networks show a dependency near
one on their provider.

```bash
uv run examples/hegemony.py
```
