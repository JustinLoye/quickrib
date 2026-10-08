# QuickRIB

[![Docs](https://img.shields.io/badge/docs-justinloye.github.io-blue)](https://justinloye.github.io/quickrib/)
[![PyPI - Version](https://img.shields.io/pypi/v/quickrib.svg)](https://pypi.org/project/quickrib)
[![CI](https://github.com/JustinLoye/quickrib/actions/workflows/ci.yml/badge.svg)](https://github.com/JustinLoye/quickrib/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/JustinLoye/quickrib.svg)](https://github.com/JustinLoye/quickrib/blob/main/LICENSE)

BGP routing table reconstruction and analysis, on top of
[pybgpflux](https://github.com/JustinLoye/pybgpflux).

A route collector publishes a full dump of its tables every two or eight hours
and a stream of update messages in between, so the table at an arbitrary
instant has to be reconstructed: the last dump before it, plus every update
since. QuickRIB performs that reconstruction once, per collector peer, and
exposes the result as a live object. Analysis modules ("observers") attach to
it and are notified of every change, so a quantity can be maintained message by
message instead of being recomputed from a million-prefix table every few
minutes.

- Reconstructs the table each collector peer held at a given instant, from RIB
  dumps and updates, and replays the updates that follow
- Identifies the peers carrying a full table before the build, and can keep
  only those
- Drives any number of observers from a single replay, with a result per
  fixed-length window
- Built-in observers: AS hegemony, IODA's visible-/24s outage signal, the
  RIPEstat BGPlay payload, BLT update classification, multi-scale anomaly
  detection over the hegemony graph
- Observers reproducing a published signal are tested against the live API
- Typed throughout, Pydantic configuration

## Quick start

```sh
pip install quickrib
```

```python
import datetime
from quickrib import QuickRIB, QuickRIBConfig
from quickrib.observers import UpdateTagsCounter

config = QuickRIBConfig(
    start_time=datetime.datetime(2010, 9, 1, 0, 0),
    end_time=datetime.datetime(2010, 9, 1, 1, 59),
    collectors=["route-views.wide"],
    dump_res=datetime.timedelta(minutes=15),
)

quickrib = QuickRIB.from_config(config)
quickrib.rib.attach_observer(UpdateTagsCounter())
quickrib.run()
```

The [documentation](https://justinloye.github.io/quickrib/) covers the
reconstruction, the observer interface and each built-in analysis.

QuickRIB is the data pipeline of [When BGP Goes MAD: Multi-Scale Anomaly
Detection in Internet Routing](https://justinloye.github.io/files/loye2026when.pdf)
(Loye, Bautista and Fontugne, ACM IMC 2026), which introduces the hegemony
temporal graphs and the MAD observers it ships with.

## Examples

Run with `uv run examples/<name>.py`.

- [quickstart.py](examples/quickstart.py): reconstruct a table and count the updates applied to it
- [custom_observer.py](examples/custom_observer.py): write an observer
- [hegemony_quickstart.py](examples/hegemony_quickstart.py): AS and link hegemony over a reconstructed table
- [hegemony.py](examples/hegemony.py): the hegemony algorithm validated on the paper's topology, then applied to a real table (marimo notebook)
- [ioda_signal.py](examples/ioda_signal.py): IODA's visible-/24s signal and outage detection
- [bgplay_payload.py](examples/bgplay_payload.py): the RIPEstat bgplay payload, diffed against the live API
- [mad_anomaly.py](examples/mad_anomaly.py): anomaly scores per window and per AS
- [update_classifier.py](examples/update_classifier.py): BLT update classification
- [origin_hijack.py](examples/origin_hijack.py): a hijack detector built on BLT's `ORIGIN_CHANGE`
- [update_mechanism.py](examples/update_mechanism.py): incremental observers against recomputing per window

## Development

```sh
uv sync --group dev
uv run pytest              # fast, offline suite
uv run pytest -m e2e       # replays real BGP archives (slow, network-bound)
uvx ruff check             # lint
uvx ty check               # type check
uv run mkdocs serve        # docs
```

See [AGENTS.md](AGENTS.md) for the architecture and contributor notes.
