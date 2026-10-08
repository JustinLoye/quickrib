# AGENTS.md: QuickRIB

Guidance for AI agents (and humans) working in this repo. `CLAUDE.md` is a symlink
to this file, so both names load the same content.

## What QuickRIB is

QuickRIB reconstructs and analyses BGP routing tables. It is a from-scratch
re-write of the original [quickrib](https://github.com/JustinLoye/quickrib) as a
proper installable Python library.

The pipeline (`QuickRIB.run()` in [src/quickrib/quickrib.py](src/quickrib/quickrib.py)):

1. **initialize_processing**: ask the broker for each collector's most recent
   RIB dump at or before `start_time`, stream those dumps, count unique prefixes
   per collector, detect full-feed (FF) peers (seeing >80% of prefixes), and
   record each collector's RIB start/end times. `start_time` needs no
   relationship to the dump schedule; the interval between the dump and it is
   replayed in step 3. Dump times come from the broker rather than an assumed
   cadence, because cadence varies by project and over time: in 2010 `rrc04`
   dumped at 15:59 and 23:59, not on the 8-hour marks.
2. **build_rib**: replay RIB dumps + updates to reconstruct the routing table at
   `start_time`, keeping only FF peers when `fullfeed_only` is set.

   A collector's RIB dump is a **single instant**: every entry in an MRT table
   dump carries the dump file's own timestamp, so `rib_start == rib_end` for each
   collector (one distinct timestamp across 18M entries at `route-views.eqix`).
   `build_rib` keeps that instant and nothing else, streaming **one collector
   at a time over its own instant**: a single stream from the earliest dump to
   the latest also fetched and parsed every update file in between, and any
   intermediate dump of a collector that dumps more often, only to discard
   them (two hours of four collectors' updates plus a whole extra Route Views
   table, on the hegemony notebook's window). `replay_updates` starts strictly
   after each collector's instant, because collectors dump at different
   instants. Without that filter the later collector's updates were applied
   twice, and the ones predating its own dump were applied on top of a newer
   table: 895 of route-views.wide's 31,956 updates (2.8%) on the pinned 2010
   window, purely because the two collectors dump a minute apart.
3. **replay_updates**: replay updates from `start_time` to `end_time`, calling
   `RIBTable.dump()` every `dump_res`.

The dump schedule is **absolute**: the boundaries are `start_time + k * dump_res`,
and `dump(ts)` is passed the boundary, not the message that happened to cross it.
An arriving element releases *every* boundary it has passed, not just the next
one (`elapsed_dump_boundaries`), and the boundaries the stream ran out before
reaching are flushed once it ends, so the number of windows a run reports
depends on the window, never on how busy the collectors were. Tested offline in
`tests/test_quickrib.py`.

BGP data comes from `pybgpflux` (`BGPStream`, `BGPElement`, `FilterOptions`).
`BGPElement` is a namedtuple `(time, type, collector, peer_asn, peer_address, fields)`
where `fields` is a dict with `prefix`, `as-path`, `communities`. `type` is `"R"`
(RIB entry), `"A"` (announcement) or `"W"` (withdrawal).

## Layout

- [src/quickrib/quickrib.py](src/quickrib/quickrib.py): orchestration + config
- [src/quickrib/rib_table.py](src/quickrib/rib_table.py): the `RIBTable` subject
- [src/quickrib/elements.py](src/quickrib/elements.py): `ParsedElement` /
  `ParsedFields` / `RIBNodeData`: what an observer is actually handed
- `typings/radix.pyi`: stubs for `py-radix`, which ships none
- [src/quickrib/radix_utils.py](src/quickrib/radix_utils.py): every helper that
  *reads* a prefix tree, plus the `RadixNode` typing shim. See "Measuring a
  prefix tree" below; tested in `tests/test_radix.py`
- [src/quickrib/observers/observer.py](src/quickrib/observers/observer.py): the
  `Observer` interface every analysis module inherits
- [src/quickrib/observers/update_tagger.py](src/quickrib/observers/update_tagger.py): BLT update tagger + `RIBTablePathHistory`
- [src/quickrib/observers/hegemony.py](src/quickrib/observers/hegemony.py): incremental
  `HegemonyObserver` (edge hegemony, `deaggregate` toggle), post-hoc `RibHegemonyObserver`,
  and the `node_hegemony` / `edge_hegemony` functions. `deaggregate=True` matches the
  post-hoc path exactly.
- [src/quickrib/observers/mad.py](src/quickrib/observers/mad.py): Multi-Scale Anomaly
  Detection observers (`MADObserver` / `MADStaticObserver` / `MADStarObserver`) over
  hegemony temporal graphs. The method is Loye, Bautista and Fontugne, "When BGP
  Goes MAD" (ACM IMC 2026, doi:10.1145/3777912.3809143), of which QuickRIB is
  the data pipeline; `docs/guide/mad.md` cites it.
- [src/quickrib/observers/_gdecomp.py](src/quickrib/observers/_gdecomp.py): vendored,
  reduced Haar wavelet graph-decomposition subset used by `mad.py`
- [src/quickrib/observers/tester.py](src/quickrib/observers/tester.py): `TesterObserver` reconstruction checks
- [src/quickrib/observers/bgplay.py](src/quickrib/observers/bgplay.py): `BGPlayObserver`,
  which reproduces the RIPEstat `bgplay` data call byte-for-byte (see below), plus the
  `diff_bgplay` / `is_equivalent` / `format_diff` comparison helpers
- [src/quickrib/observers/ioda.py](src/quickrib/observers/ioda.py): IODA's BGP
  signal (visible /24s per origin AS): post-hoc `full_feed_peers` /
  `visible_slash24s`, the incremental `IODAObserver`, the recompute-everything
  `RibIODAObserver`, and the `evaluate_series` / `detect_alerts` / `detect_events`
  outage rule
- `tests/`: one `test_<feature>.py` per module, standalone and e2e side by side
  (see Testing policy)
- `archive/`: gitignored; pre-rewrite code kept out of the way. Nothing in the
  tree imports it, and it is not a reference for current APIs.

## Architecture: subject–observer

`RIBTable` is the **subject**. Analysis modules ("observers") attach to it and are
notified on every RIB mutation. This keeps analysis decoupled from RIB
reconstruction: the same replay drives any number of observers.

### RIB data model

`RIBData = rc: str -> (peer_asn: int, peer_ip: str) -> radix.Radix`

Each radix node's `.data` dict holds `as-path`, `communities`, `time` (the
`RIBTablePathHistory` flavor also appends `as-path-history`, a bounded `deque`).
AS paths are stored as produced by `process_path()`: a list of ASN strings whose
first element equals the peer ASN, with AS-set paths dropped entirely. A
**single-ASN path**, a prefix the peer originates itself, is kept. Rare (0.02%
of RIB entries on the pinned windows) but real, and dropping it hid a full-feed
peer's own address space from every observer. Such a path carries no AS *link*,
so `_canon_edges` yields nothing for it and hegemony credits no edge while still
counting the space; its origin (`path[-1]`) is the peer.

### Observer interface

Inherit [`Observer`](src/quickrib/observers/observer.py) and override only the
hooks you need. Every method has a no-op default, so an observer that ignores
withdrawals simply does not define one. It is a `Protocol`, so structural typing
works too, but inheriting documents the intent and gives you the defaults.
[examples/custom_observer.py](examples/custom_observer.py) is the smallest
complete example.

| method | when | args |
| --- | --- | --- |
| `update_rib(bgpelem)` | RIB-dump entry during build | none |
| `update_announcement(bgpelem, data, old_data)` | `A` message | `data` = new node `.data`; `old_data` = previous `.data` or `None` |
| `update_withdrawal(bgpelem, data)` | `W` message | `data` = withdrawn node `.data`, or `None` if prefix was absent |
| `dump(ts)` | every `dump_res`, on the window boundary | compute and **return** the window's result |
| `compare(other)` | end of run | report accumulated reconstruction error |
| `set_rib(rib)` | end of `build_rib` | back-reference to the subject, for observers that must read it |
| `set_full_feed_peers(vps)` | after `initialize_processing`, before any replay | the pipeline's full-feed selection, as `(collector, peer_asn, peer_ip)` |

Attach with `rib.attach_observer(obs)`. `RIBTable` keeps separate
announcement/withdraw observer lists so update ordering can be customised. Those
lists order the two *update* hooks only: `update_rib`, `dump`, `set_rib` and
`set_full_feed_peers` go to every attached observer, in attachment order,
however it was registered. (`update_rib` used to go to the announcement list
only, which contradicted this paragraph; `tests/test_rib_table.py` now pins it.)

The dependency runs one way only: observers import `RIBTable`, and `rib_table.py`
imports `Observer` under `TYPE_CHECKING` alone: the subject knows the interface
by shape, not by import. Importing it for real is a cycle (`observers.observer`
runs the observers package `__init__`, which imports the observers that import
`rib_table`), and it stays broken only as long as that import is a type-only one.

A withdrawal deletes the node **before** notifying, so an observer that raises
cannot leave a withdrawn prefix in the table; the node's `.data` is captured
first and outlives it. The pipeline no longer swallows what an observer raises.

**Observers do not do IO.** `dump` returns its result and the calling program
decides where it goes. An observer carrying an `output_dir` and writing its own
files is an abstraction leak. That was removed along with the old `Observer`
ABC, `add_path`, and the unused `metadata` argument on `dump`.

### Observers should not reach into the subject

Ideally an observer only consumes the `bgpelem` / `data` / `old_data` it is
handed and never reads or mutates the `RIBTable`. `set_rib()` is the escape
hatch for the cases where that genuinely isn't possible; use it sparingly and
treat the reference as read-only.

### Reproducing an external API: `BGPlayObserver`

[bgplay.py](src/quickrib/observers/bgplay.py) is the worked example of holding an
observer to an external contract. For one *resource* (a prefix, an IP, or an
origin AS) it emits the exact payload of the RIPEstat `bgplay` data call
(`initial_state` / `events` / `nodes` / `sources` / `targets`), and
`tests/test_bgplay.py` asserts that against a **live call to the public API**.
Getting there pinned down several rules that are not in the API documentation
and that only a diff against real responses reveals:

- `community` carries **standard** communities only; extended (`0:2:20965:…`)
  and large (`13335:28000:16276`) ones, which the MRT parser does report, are
  dropped.
- An **AS resource matches the origin of each path**, not merely the set of
  prefixes that AS originates, and it carries **no withdrawals at all**, since
  a withdrawal has no path to attribute. Query the prefix to see those.
- Prefixes sort **numerically** (IPv4 before IPv6), not lexicographically, both
  in `targets` and as the secondary key of `initial_state`.
- Duplicate announcements are real and are reported twice. Nothing is
  collapsed: since `replay_updates` starts strictly after each collector's dump
  instant, an element reaches an observer exactly once.

Two things are deliberately not reproduced: `seq` (RIPE's opaque counter) and
the order of events *within a single second* (RIS ingestion order, which a
replay of the collector archives cannot recover). `diff_bgplay` ignores the
former; the e2e test compares same-second events as sets.

### Approximating an external signal: `IODAObserver`

Where `BGPlayObserver` reproduces an API exactly, [ioda.py](src/quickrib/observers/ioda.py)
is the case where it **cannot**, and the job becomes bounding the error instead.
IODA computes its BGP signal (the number of /24 blocks an AS has visible to
more than half the full-feed peers, every 5 minutes) over *all* Route Views and
RIPE RIS collectors. A QuickRIB run reads a handful, so the quorum is taken over
a much smaller peer population and the answer can only be close, never equal.
`tests/test_ioda.py` therefore asserts a **deviation budget** against the live
API and prints the measured deviation, rather than asserting equality. On the
pinned window one collector gets within **0.35% mean relative error** of IODA
across 28 ASes, with most large ASes matching to the block in every bin, and it
reconstructs the window's outage on the API's own numbers (122 visible /24s
falling to 90 and back). Transitions land up to a bin late by construction, and
a recovery can be a few bins late because routes come back to one collector over
several minutes while IODA sees whichever of its collectors relearns first.

Two things fall out of the method that are worth knowing:

- **Full-feed selection is QuickRIB's, not IODA's.** IODA's own rule is an
  absolute prefix count (>400k IPv4, >10k IPv6); QuickRIB's is relative (>0.8 ×
  the collector's unique prefixes) and `initialize_processing()` has already
  applied it *before* the build. `RIBTable.notify_full_feed_peers()` hands that
  selection to any observer defining `set_full_feed_peers`, so `IODAObserver`
  never counts prefixes itself, never needs a `rib=` reference, and accumulates
  straight out of `update_rib`. Run it with `fullfeed_only=True`: the peers it
  would ignore then never enter the RIB, which roughly halves the pipeline.
  The catch is that QuickRIB's denominator mixes address families, so at a
  dual-stack collector the IPv6 prefixes inflate it and IPv4-only sessions
  carrying a full table can fall just under the bar. At `route-views.eqix` the
  rule keeps 9 peers where IODA's would keep 18. For the same reason an IPv6
  signal needs an explicit `ff_peers` set there.
- The signal is a **union**, not a sum: a more specific nested inside a visible
  less specific is already counted (`radix_block_count`, the block-unit sibling
  of hegemony's `radix_size`).

`IODAObserver` and `visible_slash24s` agree **exactly**. The e2e test checks
that on the real 72k-AS table, and a randomised standalone test hammers the
hot-path bookkeeping against the reference after every batch of updates. That is
the equivalence to protect when touching the incremental path.

It is also what makes the incremental version worth its complexity. Measured on
the pinned window (`route-views.eqix`, 9 full-feed peers, identical output in
every bin) over the 29 dumps it produced then; it emits 30 now that the final
window is flushed, at the same cost per dump. `IODAObserver` costs **6.2s**, 4.2s spread over 8.7M
elements at ~0.4µs each plus ~7ms per dump, where `RibIODAObserver` costs
**524s**, about 14s of rescan per dump regardless of how little changed. That is
**85x** end to end, and the two break even before the first dump, because the
rescan is O(peers x prefixes) every time while the incremental dump only touches
the ASes whose visible set moved.

The alert rule (`< 99%` of the 24-hour median) is reproduced exactly on clean
outages: alert times, values, `historyValue` and IODA's event `score`,
`500 * sum(1 - value / historyValue)`, which is not documented anywhere and was
recovered by fitting three of its events. One detail only a diff against the API
reveals: **an alerting bin does not feed the median**, otherwise a long outage
erodes its own baseline and declares itself over while still down. On heavily
flapping ASes IODA's baseline still moves differently from ours, so the rule
over-reports there; the e2e test pins the agreement it does achieve.

### A MAD score is not reproducible to the last bit

`tree_leaves` pads the edge vector to a **power of two**, so the number of edges
in a hegemony graph sets the length of the coefficient vector. One edge crossing
that boundary re-bins every coefficient. Meanwhile `HegemonyObserver` decides
membership with a hard `min_hegemony` cut (1e-15), so an edge whose hegemony
lands on the threshold can enter or leave the graph on a last-bit difference.

Put together: a change that is mathematically a no-op (reassociating a sum,
replacing `scipy.stats.trim_mean` with the same computation) could move a MAD
score by ~1e-4 relative in the windows whose edge count is near a power of two,
and not at all in the others. Measured, not assumed: `tests/test_mad.py`'s
`TestScoreStability` pins both halves, and it is why the e2e expectations are
frozen at `rel=1e-3` rather than the `1e-9` they used to carry. A tolerance
tighter than that does not test the detector, it tests the floating-point
arithmetic of whatever produced the graph.

If that fragility is ever worth removing, the lever is the padding (a fixed
basis size, so membership stops changing the vector length), not the tolerance.

### RIB flavors (direction of travel)

Different observers want different views of the RIB (path history, block sizes,
per-origin filtering, …). Rather than bloating one `RIBTable`, the plan is to
treat **the RIB itself as an observer** and offer several flavors that subclass
`RIBTable` and enrich node `.data` as needed.
[`RIBTablePathHistory`](src/quickrib/observers/update_tagger.py) is the first
example: it maintains `as-path-history` for route-flap detection. New flavors
follow that pattern by overriding **`RIBTable._enrich_announcement(data, old_data)`**,
the hook that runs between the write and the notification. Overriding the whole
of `update_announcement` (as it used to) means restating the write and the
notify, and having to restate them again whenever the base changes.

## Performance is a first-class constraint

Update ingestion is a hot loop: **~300k `BGPElement`/s**. Every attached observer
runs synchronously inside that loop, so a slow observer slows the whole pipeline.

When writing or reviewing observer `update_*` code:

- Keep it O(1) per element; no per-element `pandas`/`numpy`, no per-element
  allocation of large objects, no I/O.
- Precompute lookup tables once (see `PRECOMPUTED_BLOCK_SIZES_*`,
  `UpdateTagsCounter._tag_list`).
- Prefer plain `dict` / `collections.Counter` / `set` and local-variable hoisting.
- Push expensive aggregation (trimmed means, graph metrics, serialisation) into
  `dump()`, which runs once per window, or into a post-hoc pass over `rib.data`.
- If you change anything on the hot path, profile before and after (the old
  `cProfile` + `gprof2dot` recipe is sketched in `test.py`).
- The same applies to `dump()`, which is not the hot path but is not free
  either. Two things that were: `scipy.stats.trim_mean` called once per edge
  (~35k calls a dump, now one sorted array, 155x faster), and a log line counting
  every peer's prefixes with `len(peer_table.prefixes())`, which walks the
  whole table and is now behind `logger.isEnabledFor(logging.DEBUG)` and
  `radix_utils.tree_size`. Measured, not assumed: `nodes()` builds its list in
  C and is four times faster than iterating the tree from Python, so
  `tree_size` uses it, and `radix_utils._union` reads `rnode.parent is None`
  instead of a `search_worst` lookup per node (same answer; `parent` skips
  py-radix's glue nodes). Neither moves the post-hoc functions much:
  `edge_hegemony` on the 2010 two-collector table spends its 14s in
  `radix_prefix_size` (one `search_covered` per node) and in `_canon_edges`
  (an `int()` comparison per link), in roughly equal parts; a MADstar dump on
  the same table costs ~2.7s for 35k stars. These are the reference paths,
  and the incremental observers exist so they need not run per window.

## Linting

`uvx ruff check` is the third check, next to `ty` and `pyright`: neither type
checker reports an unused import, a mutable default, or a loop that only reads a
dict's values. Configuration is in [pyproject.toml](pyproject.toml).

Two rule families are deliberately **off**, for the reason
`missing-override-decorator` is: PEP 695's `def f[T]()` and `type X = ...`
(`UP047`, `UP040`) are standard but not yet the common idiom, and neither type
checker asks for them, and rewriting the public type surface buys nothing
checkable. `UP007`/`UP045` (`Optional[X]` to `X | None`) are off for the same
reason: the codebase uses both about equally and churning one into the other is
not a fix.

## Types

**Pylance/pyright is the source of truth**: it is what the IDE runs, so the code
has to be clean under it whatever else we do. `uvx pyright` and `uvx ty check` are
both green and should stay that way; ty is the fast pre-commit check, pyright is
the one that decides. Configuration for both is in [pyproject.toml](pyproject.toml).

They disagree, and it is worth knowing how. ty has no equivalent of
`reportTypedDictNotRequiredAccess`, so it accepted 50 reads of keys that a
`total=False` TypedDict says may be absent; pyright rejected all of them. Where a
rule exists in both, ty is sometimes the sharper one: it proves `Flag.name` is a
non-empty literal here where pyright only sees `str | None`. Neither is a superset. Configuration lives in `[tool.ty]` in
[pyproject.toml](pyproject.toml); `ty` finds `.venv` and `typings/` from there,
so the bare command is enough.

Two narrowings make this possible, both in
[elements.py](src/quickrib/elements.py):

- **`ParsedElement`**: `pybgpflux` types `fields["as-path"]` as the wire
  *string*, because that is what it is on the wire. `process_path` rewrites it in
  place to a `list[str]` before any observer sees it, so from the pipeline
  boundary down the element is a `ParsedElement`, which says so. Two documented
  suppressions in `quickrib.py` mark that boundary; everything downstream is
  honestly typed and costs nothing at runtime.
- **`RIBNodeData`**: what a radix node's `.data` holds, and therefore what
  `data` / `old_data` are. `data["as-path"]` is a `list[str]`, not `Any`.
- **`ParsedElement` vs `WithdrawalElement`**: a route carries a prefix, a path
  and communities; a withdrawal carries a prefix and nothing else. The hooks
  already discriminate (`update_withdrawal` only ever receives a `W`), so the
  types say so and neither hook needs a guard. `AnyElement` is for helpers that
  only touch what both carry.

Nothing is `total=False` out of habit. A key is optional only where it genuinely
can be absent: `next-hop`, which nothing reads, and `as-path-history`, which
only `RIBTablePathHistory` writes. Getting that wrong is what produced 50 of the
first 126 pyright errors, and tightening it caught a real omission:
`RIBTable.get_bgpelem` was silently dropping `communities`.

`ElementFields` also marks `prefix` as possibly absent. That is vestigial:
`pybgpflux` types its element as `Literal["R", "A", "W"]` and its `bgpdump`
parser drops BGP *state-change* records, so no element QuickRIB can receive
lacks a prefix. Checking for it would be dead code on the hot path, so don't.
If state changes are ever wanted (they mark peer session resets, which is when a
collector silently loses a peer's whole table), they belong in a separate stream
and hook, not in `BGPElement`.

`quickrib.py` carries a file-level `# pyright: reportTypedDictNotRequiredAccess=false`
because it is the one module that touches raw `pybgpflux` elements; below that
seam the narrowed types apply and the rule is back on. The handful of
`# pyright: ignore` comments there mark the same seam: reinterpreting an element
after `process_path` costs nothing at runtime, and a cast would.

Exemptions are configured and each says why: `_gdecomp.py` is vendored,
`examples/hegemony.py` is a marimo notebook whose imports live inside cell
functions, `archive/` is pre-rewrite code, and `py-radix` needs the local stub.

`missing-override-decorator` is deliberately **off**. `typing.override` is
standard (PEP 698) but opt-in, and pyright, mypy and ty all ship the rule off by
default, and requiring it would put a decorator on ~60 methods here. The check that
matters, `invalid-method-override`, is on by default and needs no decorator: an
override with the wrong signature is caught either way. The only thing
`@override` would additionally catch is a *misspelled* hook name, which, since
`Observer`'s hooks are no-op defaults, would be a method that is never called.
That is a real hazard, but a thin one to pay for on every method; tests are the
better net for it.

## Development

Python **3.12+**, managed with **uv**.

```bash
uv sync                        # install (add --group dev for tests/docs/examples)
uvx ty check                   # type check (must be clean)
uvx ruff check                 # lint (must be clean)
uv run pytest                  # fast suite only; e2e tests are deselected by default
uv run pytest -m e2e           # only the end-to-end tests (download real archives, slow)
uv run pytest -m ""            # everything, standalone + e2e
uv run pytest tests/test_rib_table.py      # a single fast module
uv run examples/hegemony.py    # run an example
uv run mkdocs serve            # docs, live-reloaded at localhost:8000
uv run mkdocs build --strict   # docs, failing on any warning
uv run mkdocs gh-deploy        # publish to GitHub Pages
```

### Documentation

The site is built with mkdocs-material from [docs/](docs), configured in
[mkdocs.yml](mkdocs.yml). API pages are generated by mkdocstrings from the
docstrings, so the reference follows the code; prose pages are written by hand.

Docstrings are **numpydoc** style (`Parameters` / `----------`), which is what
mkdocstrings is configured for. Keep them that way.

Examples shown in the docs are **runnable scripts in [examples/](examples)**,
included with a snippet directive rather than pasted:

````markdown
```python
--8<-- "examples/quickstart.py"
```
````

That way an example in the docs is one that has actually been run. Add the
script first, check it runs, then include it.

### Writing style

Prose in this repo (docstrings, comments, docs, this file) avoids the em dash.
Use a colon, a semicolon, parentheses, or two sentences. Headings and docstring
summaries state what the thing is, not what it does for the reader: "Incremental
observers versus recomputing from the RIB", not "What the update mechanism buys
you".

### Dependencies

Add a dependency only when it is genuinely necessary. This is a library and the
hot loop favours the stdlib. If you do add one, put it in the right group in
[pyproject.toml](pyproject.toml) (`test` / `docs` / `examples` vs. runtime
`dependencies`), run `uv sync`, and make sure the suite still passes.

### Testing policy for observers

Every observer needs tests at **two levels**:

1. **Standalone**: drive the observer (or a small `RIBTable` + observer) with
   hand-built fake `BGPElement`s / `data` dicts. Fast, offline, deterministic.
   Exhaustively cover the classification / computation logic. **No marker**, so these
   run by default. See `tests/test_update_tagger.py` and `tests/test_radix.py`.
2. **End-to-end**: run the real `QuickRIB` pipeline over a small, fixed time
   window of real archives and assert on the observer's output (see
   `tests/test_quickrib.py` with `TesterObserver`). Pin the config so counts are
   reproducible; expect it to be slow and network-bound. **Mark every such test,
   or the whole module, with `@pytest.mark.e2e`**. The pipeline should run on small config (few collectors, interval of few hours in 2010) like in `tests/test_quickrib.py`.

Co-locate both levels in one `test_<feature>.py`, mirroring how
[examples/hegemony.py](examples/hegemony.py) keeps synthetic and real side by
side; the marker keeps them separately runnable.

#### e2e marker mechanics (the convention from now on)

e2e tests are slow and network-bound, so they are **deselected by default**. The
[pyproject.toml](pyproject.toml) config:

```toml
[tool.pytest.ini_options]
log_cli = true
log_cli_level = "INFO"
addopts = '-q -rs --strict-markers -m "not e2e"'
markers = [
    "e2e: end-to-end test; downloads real BGP archives and runs the full QuickRIB pipeline (slow, network-bound; deselected by default)",
]
```

- `uv run pytest` → standalone tests only.
- `uv run pytest -m e2e` → only the e2e tests (a CLI `-m` overrides the one baked
  into `addopts`); `uv run pytest -m ""` → run everything.
- `--strict-markers` turns a mistyped / unregistered marker into an error instead
  of a silent always-collected test, so keep the `markers` list in sync.
- Mark a whole e2e module with `pytestmark = pytest.mark.e2e` rather than
  decorating each function, but only when the module really is all e2e; a
  module with both tiers marks the e2e tests individually.
- Gotcha: a module whose tests are *all* e2e reports "no tests ran" on its own, so
  add `-m e2e` (or `-m ""`). Prefer keeping both tiers in the module, as
  `test_quickrib.py` now does, so the fast tier runs by default.
- CI: run the fast suite on every push, `-m e2e` on a schedule / pre-release.

#### Two pinned e2e windows

[tests/conftest.py](tests/conftest.py) pins two windows, and a new e2e test
should reuse one rather than invent a third (the MRT archives are cached in
`cache/` and shared across the suite):

- `e2e_config`: the 2010 two-collector window, for anything checked against
  QuickRIB's own reconstruction.
- `ripe_config`: a 2025 `rrc04` window, for anything checked against a live
  RIPE API. RIPEstat only indexes BGP data from January 2024 onwards and its
  RIS-only endpoints know nothing about route-views, so the 2010 window cannot
  serve. It starts exactly on an RIS RIB dump (00:00/08:00/16:00) so the
  reconstructed table at `start_time` *is* that dump, and it sets
  `fullfeed_only=False` because the RIPE APIs report partial-feed peers too.
- `ioda_config`: a 2022-01-26 `route-views.eqix` window bracketing a real BGP
  outage, for anything checked against the IODA API. IODA's BGP series starts on
  2022-01-19, so this is as far back as the archives can be pushed; a
  route-views collector is required because RIS dumps only every 8 hours and
  cannot start close to the outage. `dump_res` is IODA's native 5 minutes, and
  `fullfeed_only` is left on (see the IODA section).

Each window costs what its collector costs. A collector's RIB dump dominates the
runtime. `route-views.eqix` is ~18M entries and a couple of minutes, so prefer
the smallest collector that still answers the question. For IODA that trades off
against the full-feed count: `route-views.perth` runs in half the time but its 8
Australian peers miss the more specifics a Spanish AS's outage is made of, while
`route-views.eqix`'s 18 well-connected ones see them.

[examples/hegemony.py](examples/hegemony.py) is the **golden standard** to
emulate: it first validates the algorithm on a synthetic topology with fake
paths (the PAM-paper figure), then applies the exact same code to a real
reconstructed RIB with sanity checks (top ASes are Tier-1s, known single-homed
ASes show ~100% dependency). New features should ship with tests at both levels.

Examples currently live as `marimo` notebooks runnable as plain scripts
(`uv run examples/<name>.py`). The exception is
[examples/update_mechanism.py](examples/update_mechanism.py), a plain script that
benchmarks both observers that ship in an incremental *and* a recompute-per-dump
flavour (IODA, hegemony) over four collectors, splitting the cost into warm-up,
per-update and per-dump so the crossover is explicit. It is the demo for why the
subject-observer replay exists, and it is honest about the trade: IODA's
incremental form pays back its warm-up in under a dump, hegemony's
`deaggregate=True` form takes ~4 dumps (~20 minutes of window) because
maintaining its per-VP deaggregation tries costs ~16µs per RIB entry (measured
2026-10-04: 15.9M entries, 18 full-feed peers, 6 dumps; recomputing costs 42s
per IODA dump and 70s per hegemony dump). That run peaks at **30 GB** of
memory and the hegemony notebook at 17 GB, so run the heavy examples and the
e2e tier one at a time: four of them in parallel exhausted a 62 GB machine and
took the terminal down with them. Sequentially, the e2e tier takes ~13 minutes
with a warm cache and peaks at 11 GB.

## Known rough edges (don't be surprised)

- `Observer`'s hooks carry an explicit `return None`. ty 0.0.84 reads a
  docstring-only body in a `Protocol` as abstract, which made every observer
  non-instantiable under the type checker (82 diagnostics); the explicit
  return is what marks the default as a real implementation.
- `quickrib.py` suppresses the type checkers on the four lines where a raw
  `pybgpflux` element is handed to the `RIBTable` as a `ParsedElement` /
  `WithdrawalElement`, for both pyright and ty. That is the seam described
  under "Types"; a cast would cost a function call per element.
