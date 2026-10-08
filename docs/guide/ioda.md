# IODA Signal

[IODA](https://ioda.inetintel.cc.gatech.edu) (Internet Outage Detection and
Analysis) monitors Internet outages from several independent data sources. Its
BGP signal is the number of /24 blocks an AS, country or region has visible in
the global routing system, computed every five minutes from all Route Views
and RIPE RIS collectors. `quickrib.observers.ioda` reproduces that signal for
ASes, and the alert rule IODA applies to it. The method is described on the
[IODA help page](https://ioda.inetintel.cc.gatech.edu/help).

## Definition

Let \(F\) be the set of full-feed peers and \(n(p)\) the number of them that
have a route to prefix \(p\). The prefix is **visible** when a majority of the
full-feed peers see it:

\[
n(p) > \tfrac{1}{2}\,|F|
\]

The signal of an AS is the number of /24 blocks covered by the union of the
visible prefixes it originates. The union matters: a more specific nested in a
visible less specific adds no blocks, and a prefix longer than a /24 adds none
either, since it does not fill a block. When several ASes originate the same
prefix, each is credited with it.

IODA defines a full-feed peer by an absolute table size (more than 400k IPv4 or
10k IPv6 prefixes). QuickRIB uses its own relative rule, more than 80% of the
collector's distinct prefixes, and the observer takes the selection the
pipeline already made. The two rules select different peers at a dual-stack
collector; see [Reconstruction](reconstruction.md#full-feed-peers).

An AS is **alerting** in a bin when its value falls below 99% of the median of
the previous 24 hours. Consecutive alerting bins form an **outage event**,
scored as

\[
\mathrm{score} = 500 \sum_{\text{bins}} \Big(1 - \frac{\mathrm{value}}{\mathrm{median}}\Big)
\]

A bin that is itself alerting is excluded from the median, otherwise a long
outage would lower its own baseline and declare itself over while still down.
Neither the exclusion nor the score formula is documented by the API; both
were recovered by matching its alert and event records.

## Usage

```python
--8<-- "examples/ioda_signal.py"
```

`observer.series` maps the start of each five-minute bin (a Unix timestamp) to
`{origin ASN: visible /24s}`. Pass `entities=[...]` to restrict the output to a
few ASes, and `ip_version=6` for /48 blocks, in which case an explicit
`ff_peers` set is needed at a dual-stack collector.

`IODAObserver` accumulates its counts while the table is built and keeps them
current in the update path; a dump only recounts the ASes whose visible set
changed. `RibIODAObserver` recomputes the signal from the whole table at every
dump and is the reference the incremental observer is tested against. They
agree exactly.

`evaluate_series`, `detect_alerts` and `detect_events` apply the alert rule to
any series, including one fetched from the API. The rule needs 24 hours of
history, so a short replay alone cannot trigger it.

## Accuracy

IODA computes the quorum over every collector; a QuickRIB run reads a few, so
the quorum is taken over a smaller peer population and the two signals can only
be close. On the pinned test window, a single Route Views collector lands within
0.35% mean relative error of the API across 28 ASes and reproduces an outage on
the API's own values, with the drop one bin late at most. Two effects remain:

- QuickRIB reports the table *at* a bin boundary while IODA's bin covers the
  interval after it, so a transition lands up to one bin late.
- Routes return to one collector over several minutes, while IODA sees
  whichever of its collectors relearns them first, so a recovery can be a few
  bins late.
