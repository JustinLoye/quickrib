"""Incremental observers versus recomputing from the RIB.

QuickRIB replays every BGP message through its observers, so an observer can
maintain its result message by message instead of recomputing it from the whole
RIB each time one is asked for. Two observers ship in both forms, and this
script measures the difference:

    IODA      visible /24s per origin AS   IODAObserver     vs RibIODAObserver
    hegemony  edge AS hegemony             HegemonyObserver vs RibHegemonyObserver

All four are attached to the same pipeline run, so they see the same RIB and the
same message stream, and a proxy attributes wall time to each. Both forms of
each observer produce the same result, which `tests/test_ioda.py` and
`tests/test_hegemony.py` pin; this script only measures what each costs.

The cost splits in two:

* **warm-up**: replaying the initial RIB dump into the observer's state. The
  recomputing form skips it, since its `update_*` hooks are no-ops and it reads
  the RIB the pipeline built anyway.
* **per dump**: producing one result. Recomputing is O(peers x prefixes) every
  time, however little changed; maintaining is O(what changed).

An incremental observer therefore trades a one-off warm-up for a cheap dump, and
wins once the run is long enough to amortise it. Where that crossover falls is
the interesting number, and it differs sharply between these two observers.

Run it with `uv run examples/update_mechanism.py`. It downloads a few RIB dumps,
cached in `cache/` afterwards, and takes on the order of fifteen minutes, nearly
all of it inside the two recomputing observers. Shrink COLLECTORS or END to make
it quicker.
"""

import datetime
import time
from collections.abc import Callable
from typing import Optional

from quickrib import QuickRIB
from quickrib.elements import ParsedElement, RIBNodeData, WithdrawalElement
from quickrib.observers import (
    HegemonyObserver,
    IODAObserver,
    Observer,
    RibHegemonyObserver,
    RibIODAObserver,
)

START = datetime.datetime(2022, 1, 26, 8, 0, tzinfo=datetime.UTC)
END = datetime.datetime(2022, 1, 26, 8, 30, tzinfo=datetime.UTC)
DUMP_RES = datetime.timedelta(minutes=5)
# Two Route Views and two RIPE RIS collectors. START is 08:00 because RIS dumps
# its table only at 00:00/08:00/16:00, where Route Views dumps every two hours.
COLLECTORS = ["route-views.wide", "route-views.perth", "rrc04", "rrc06"]


class Timed(Observer):
    """Observer proxy that records where the wrapped observer spends its time."""

    def __init__(self, observer: Observer, signal: str, mode: str):
        self.observer, self.signal, self.mode = observer, signal, mode
        self.warmup = self.updates = self.dumps = 0.0
        self.n_warmup = self.n_updates = self.n_dumps = 0

    def _timed(self, method: Callable[..., object], *args: object) -> float:
        start = time.perf_counter()
        method(*args)
        return time.perf_counter() - start

    def update_rib(self, bgpelem: ParsedElement) -> None:
        # RIB-dump entries, replayed by build_rib: this is the warm-up.
        self.warmup += self._timed(self.observer.update_rib, bgpelem)
        self.n_warmup += 1

    def update_announcement(
        self, bgpelem: ParsedElement, data: RIBNodeData, old_data: Optional[RIBNodeData]
    ) -> None:
        self.updates += self._timed(
            self.observer.update_announcement, bgpelem, data, old_data
        )
        self.n_updates += 1

    def update_withdrawal(self, bgpelem: WithdrawalElement, data: Optional[RIBNodeData]) -> None:
        self.updates += self._timed(self.observer.update_withdrawal, bgpelem, data)
        self.n_updates += 1

    def dump(self, ts: datetime.datetime):
        self.dumps += self._timed(self.observer.dump, ts)
        self.n_dumps += 1

    def set_rib(self, rib):
        self.observer.set_rib(rib)

    def set_full_feed_peers(self, ff_peers):
        # Only IODA defines this hook; the pipeline offers it to every observer.
        hook = getattr(self.observer, "set_full_feed_peers", None)
        if hook is not None:
            hook(ff_peers)

    @property
    def per_dump(self):
        return self.dumps / self.n_dumps if self.n_dumps else 0.0

    @property
    def per_update(self):
        return self.updates / self.n_updates if self.n_updates else 0.0

    @property
    def per_warmup_entry(self):
        return self.warmup / self.n_warmup if self.n_warmup else 0.0

    @property
    def total(self):
        return self.warmup + self.updates + self.dumps


def report(observers):
    print(f"\n{'signal':<9} {'mode':<12} {'warm-up':>9} {'per entry':>10} "
          f"{'updates':>8} {'per msg':>9} {'per dump':>10} {'total':>9}")
    print("-" * 80)
    for obs in observers:
        print(f"{obs.signal:<9} {obs.mode:<12} {obs.warmup:>8.1f}s "
              f"{obs.per_warmup_entry * 1e9:>8.0f}ns {obs.updates:>7.1f}s "
              f"{obs.per_update * 1e9:>7.0f}ns {obs.per_dump:>9.3f}s "
              f"{obs.total:>8.1f}s")

    print()
    for signal in dict.fromkeys(obs.signal for obs in observers):
        pair = {obs.mode: obs for obs in observers if obs.signal == signal}
        fast, slow = pair["incremental"], pair["recompute"]
        # Maintaining costs `fixed` once and `fast.per_dump` per result;
        # recomputing costs `slow.per_dump` per result and nothing up front.
        fixed = fast.warmup + fast.updates - slow.warmup - slow.updates
        crossover = fixed / (slow.per_dump - fast.per_dump)
        print(f"{signal:<9} {slow.per_dump / fast.per_dump:>5.0f}x cheaper per dump; "
              f"pays back its {fixed:.0f}s warm-up after {crossover:.1f} dumps "
              f"({crossover * DUMP_RES.total_seconds() / 60:.0f} min of window), "
              f"then saves {slow.per_dump - fast.per_dump:.1f}s per dump")
        verdict = "ahead" if fast.total < slow.total else "still behind"
        print(f"{'':<9} over the {fast.n_dumps} dumps measured here: "
              f"{fast.total:.0f}s vs {slow.total:.0f}s, {verdict}")


def main():
    quickrib = QuickRIB(
        start_time=START,
        end_time=END,
        dump_res=DUMP_RES,
        collectors=list(COLLECTORS),
        cache_dir="cache",
        parser="bgpkit",
    )
    observers = [
        # IODAObserver takes no RIB reference: the pipeline hands it the
        # full-feed peers, and it counts the RIB dump as it streams past.
        Timed(IODAObserver(), "IODA", "incremental"),
        Timed(RibIODAObserver(rib=quickrib.rib), "IODA", "recompute"),
        # deaggregate=True is the mode that reproduces edge_hegemony exactly.
        Timed(HegemonyObserver(deaggregate=True), "hegemony", "incremental"),
        Timed(RibHegemonyObserver(rib=quickrib.rib), "hegemony", "recompute"),
    ]
    for observer in observers:
        quickrib.rib.attach_observer(observer)

    wall = time.perf_counter()
    quickrib.run()
    wall = time.perf_counter() - wall

    peers = sum(len(peers) for peers in quickrib.rib.data.values())
    first = observers[0]
    print(f"\n{len(COLLECTORS)} collectors, {peers} full-feed peers, "
          f"{first.n_warmup} RIB entries, {first.n_updates} updates, "
          f"{first.n_dumps} dumps of {DUMP_RES.total_seconds():.0f}s, {wall:.0f}s wall")
    report(observers)


if __name__ == "__main__":
    main()
