# BGPlay

`BGPlayObserver` produces the payload of the RIPEstat
[bgplay](https://stat.ripe.net/docs/02.data-api/bgplay.html) data call, the
feed behind [BGPlay](https://bgplay.massimocandela.com). For one resource it
records the routing state at `start_time` and every update seen afterwards, so
a client can animate how the paths towards that resource evolve.

## Usage

```python
--8<-- "examples/bgplay_payload.py"
```

A resource is a prefix, an IP address, an AS number (`AS1205` or `1205`), or a
comma-separated list of those, following the API's grammar. `to_dict()` returns
the `initial_state`, `events`, `nodes`, `sources` and `targets` sections of the
API response, in the API's own order, so the result can be handed to a BGPlay
client as is. The end-to-end test asserts the payload against the live API.

## Rules the API does not document

- `community` holds standard communities only; extended and large communities,
  which MRT parsers do report, are dropped.
- An AS resource matches the **origin of each path**, not the set of prefixes
  the AS originates, and carries no withdrawals, since a withdrawal has no path
  to attribute. Query the prefix to see them.
- A prefix resource matches exactly; its more specifics are not included.
- Prefixes sort numerically, IPv4 before IPv6.
- Repeated identical announcements are reported each time.

## Differences from the API

- `seq` is a 0-based index rather than RIPE's internal counter.
- Events within the same second come out in archive order rather than in RIS
  ingestion order, which a replay cannot recover. The set of events is
  identical.
- `owner` in `nodes` is empty unless `resolve_owners=True`, which queries the
  RIPEstat `as-names` call.
- A bare IP resource expands to every covering prefix observed, where the API
  resolves it to one.

`diff_bgplay` compares two payloads section by section as multisets and reports
what is only on each side, what differs, and a Jaccard similarity;
`is_equivalent` and `format_diff` reduce and render that report.
