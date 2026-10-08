# Anomaly Detection

The MAD observers score how well the current AS-hegemony graph is explained by
its recent past. They implement the detector of Loye, Bautista and Fontugne,
[When BGP Goes MAD: Multi-Scale Anomaly Detection in Internet
Routing](https://justinloye.github.io/files/loye2026when.pdf) (ACM IMC 2026,
[doi:10.1145/3777912.3809143](https://doi.org/10.1145/3777912.3809143)), which
extends the MAD algorithm for link streams of Bautista, Brisson, Bothorel and
Smits (*MAD: Multi-Scale Anomaly Detection in Link Streams*, WSDM 2024) from
binary to weighted temporal graphs. QuickRIB is the data pipeline of that paper:
the link-hegemony graph produced by a `HegemonyObserver` at every window is the
temporal graph the detector reads.

## Method

Let \(Q\) be the link-hegemony graph of the current window \(t\) and
\(\mathcal{H}\) its history, the graphs of the previous \(N\) windows. The links
are ranked by their weight aggregated over the history,

\[
f(e_i) = \sum_{k=t-N}^{t-1} \mathcal{H}(k, e_i),
\]

and placed in that order at the leaves of a binary tree, padded to a power of
two. Each internal node of the tree compares the two halves of the leaves it
covers: at level \(\ell\), the coefficient

\[
w_k^{(\ell)}(t) = \frac{\sqrt{2^\ell}}{\sqrt{M}} \Big[ \sum_{e_i \in \mathcal{E}^{(\ell+1)}_{2k}} Q(t, e_i) - \sum_{e_j \in \mathcal{E}^{(\ell+1)}_{2k+1}} Q(t, e_j) \Big]
\]

measures how much more hegemony the historically higher-ranked links carry
than the lower-ranked ones. This is a Haar wavelet transform of the link
vector. Together with the total weight \(s(t)\) the coefficients carry the
whole graph, with no loss of information, and they are organised by scale: the
root reacts to a reversal between the two halves of the Internet's links, a
leaf-level coefficient to a swap between two neighbouring links.

Each coefficient is then compared with its own history, by Z-score or, by
default, by the outlier-robust modified Z-score

\[
\mathrm{mZ}\big(w_k^{(\ell)}\big) = 0.6745\,
\frac{\big|\, w_k^{(\ell)}(t) - \mathrm{median}\big(w_k^{(\ell)}[t-N, t-1]\big) \big|}
{\max\big\{\epsilon,\ \mathrm{MD}\big(w_k^{(\ell)}[t-N, t-1]\big)\big\}}
\]

where MD is the median absolute deviation and \(\epsilon\)
(`H_stats_dampening`) keeps a coefficient that never varied from producing an
unbounded score. The anomaly score of the window is the sum over all
coefficients. Because the median and MD are robust, a single anomalous window
in the history does not raise the baseline of the windows that follow it.

## Variants

| Observer | Query \(Q\) | Output |
| --- | --- | --- |
| `MADObserver` | the whole graph (MADglob) | one score per window |
| `MADStaticObserver` | the whole graph on a ranking fixed at the first window (MADstatic) | one score per window, cheaper |
| `MADStarObserver` | the star of links incident to each AS (MADstar) | one score per AS per window |

MADglob detects Internet-level events and MADstar localises an anomaly to the
ASes it touches; an AS that vanishes from the graph is scored on an empty star
rather than skipped. MADstatic freezes the link ranking at the first window so
later graphs are projected onto that basis and links that appear afterwards
are ignored. The paper uses it to make hyper-parameter searches tractable.

## Usage

```python
--8<-- "examples/mad_anomaly.py"
```

`glob.scores` maps a window boundary to its score; `star.node_to_scores` maps a
boundary to `{asn: score}`. No score is produced until `window_size + 1`
windows have accumulated. The paper's window lengths, found by hyper-parameter
optimisation, range from 75 minutes to 15 hours at a 5-minute resolution.

A MAD observer drives its hegemony observer by default. To share one hegemony
observer between several detectors, attach it first and pass
`drives_hegemony=False` to the detectors, as above; its dump is memoised per
window so the shared reads are free.

## Reproducibility

The number of links in the graph sets the size of the tree, so one link
crossing a power of two re-bins every coefficient. A score is therefore
reproducible to about \(10^{-3}\) relative, not to the last bit, which is also
why `min_hegemony` defaults to zero: a positive threshold would let a rounding
difference add or remove a link.
