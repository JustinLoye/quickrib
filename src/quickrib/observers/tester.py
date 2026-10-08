import datetime
from collections import defaultdict
from typing import TYPE_CHECKING

from quickrib.elements import AnyElement, ParsedElement, WithdrawalElement
from quickrib.observers.observer import Observer

if TYPE_CHECKING:  # pragma: no cover
    from quickrib import QuickRIBConfig

# How far before `start_time` a collector's RIB dump can legitimately sit: the
# dump interval of each project. An unknown collector gets the widest of them
# rather than being rejected, since this is a reconstruction check, not a
# registry of collectors.
RIB_INTERVALS = {"rrc": datetime.timedelta(hours=8), "route-views": datetime.timedelta(hours=2)}
DEFAULT_RIB_INTERVAL = max(RIB_INTERVALS.values())


def rib_interval(collector: str) -> datetime.timedelta:
    """How often ``collector``'s project dumps its table."""
    for name, interval in RIB_INTERVALS.items():
        if collector.startswith(name) or name in collector:
            return interval
    return DEFAULT_RIB_INTERVAL


class InvalidElement(AssertionError):
    """An element the pipeline should not have produced for this config."""


def _check(condition: bool, message: str) -> None:
    # A plain `assert` would vanish under `python -O`, and this is the only
    # thing standing between a broken replay and a green run.
    if not condition:
        raise InvalidElement(message)


def is_valid_bgpelement(bgpelem: AnyElement, config: "QuickRIBConfig"):
    """Check that a BGP element is one the configuration asked for."""
    earliest = config.start_time - rib_interval(bgpelem.collector)
    _check(
        bgpelem.time > earliest.timestamp(),
        f"{bgpelem.collector} element at {bgpelem.time} predates {earliest.isoformat()}",
    )
    _check(
        bgpelem.time <= config.end_time.timestamp(),
        f"{bgpelem.collector} element at {bgpelem.time} is past end_time",
    )
    _check(
        bgpelem.collector in config.collectors,
        f"element from {bgpelem.collector}, which is not in {config.collectors}",
    )

    if config.filters:
        if config.filters.peer_asn:
            _check(
                bgpelem.peer_asn == config.filters.peer_asn,
                f"peer {bgpelem.peer_asn} despite a peer_asn filter",
            )
        if config.filters.ip_version:
            version = 6 if ":" in bgpelem.fields["prefix"] else 4
            _check(
                config.filters.ip_version == version,
                f"IPv{version} prefix despite an ip_version={config.filters.ip_version} filter",
            )


class TesterObserver(Observer):
    __test__ = False  # its name starts with "Test"; keep pytest from collecting it

    def __init__(self, config: "QuickRIBConfig", name: str = "tester"):
        super().__init__(name)
        self.config = config
        
        self.n_rib_entries = defaultdict(int)
        self.n_updates = defaultdict(int)
        self.n_withdrawals = defaultdict(int)
        self.n_announcements = defaultdict(int)
        
        self.n_rib_entries_per_peer = defaultdict(lambda: defaultdict(int))
        self.n_withdrawals_per_peer = defaultdict(lambda: defaultdict(int))
        self.n_announcements_per_peer = defaultdict(lambda: defaultdict(int))
        
        # New: Track [first_timestamp, last_timestamp] per peer
        self.peer_rib_times = defaultdict(lambda: defaultdict(lambda: [float('inf'), float('-inf')]))
        self.peer_update_times = defaultdict(lambda: defaultdict(lambda: [float('inf'), float('-inf')]))

    def update_rib(self, bgpelem: ParsedElement):
        is_valid_bgpelement(bgpelem, self.config)
        assert bgpelem.type == "R"
        
        peer_key = (bgpelem.peer_asn, bgpelem.peer_address)
        self.n_rib_entries[bgpelem.collector] += 1
        self.n_rib_entries_per_peer[bgpelem.collector][peer_key] += 1
        
        # Track RIB times
        ts = bgpelem.time
        times = self.peer_rib_times[bgpelem.collector][peer_key]
        times[0] = min(times[0], ts)
        times[1] = max(times[1], ts)

    def update_withdrawal(self, bgpelem: WithdrawalElement, data):
        is_valid_bgpelement(bgpelem, self.config)
        assert bgpelem.type == "W"
        
        peer_key = (bgpelem.peer_asn, bgpelem.peer_address)
        self.n_updates[bgpelem.collector] += 1
        self.n_withdrawals[bgpelem.collector] += 1
        self.n_withdrawals_per_peer[bgpelem.collector][peer_key] += 1
        
        # Track Update times
        ts = bgpelem.time
        times = self.peer_update_times[bgpelem.collector][peer_key]
        times[0] = min(times[0], ts)
        times[1] = max(times[1], ts)

    def update_announcement(self, bgpelem: ParsedElement, data, old_data):
        is_valid_bgpelement(bgpelem, self.config)
        assert bgpelem.type == "A"
        
        peer_key = (bgpelem.peer_asn, bgpelem.peer_address)
        self.n_updates[bgpelem.collector] += 1
        self.n_announcements[bgpelem.collector] += 1
        self.n_announcements_per_peer[bgpelem.collector][peer_key] += 1
        
        # Track Update times
        ts = bgpelem.time
        times = self.peer_update_times[bgpelem.collector][peer_key]
        times[0] = min(times[0], ts)
        times[1] = max(times[1], ts)

    def dump(self, ts: datetime.datetime):
        pass

    def compare(self, other):
        # not concerned with checks
        pass
    
    def __str__(self) -> str:
        collectors = set()
        for d in (self.n_rib_entries, self.n_updates, self.n_withdrawals, self.n_announcements,
                  self.n_rib_entries_per_peer, self.n_withdrawals_per_peer, self.n_announcements_per_peer,
                  self.peer_rib_times, self.peer_update_times):
            collectors.update(d.keys())

        if not collectors:
            return "<TesterObserver State: Empty (No data collected)>"

        # Helper to format timestamps gracefully
        def fmt_ts(ts):
            if ts in (float('inf'), float('-inf')):
                return "N/A"
            return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).strftime('%Y-%m-%d %H:%M:%S')

        lines = ["<TesterObserver Internal State>"]
        
        for coll in sorted(collectors):
            lines.append(f"\n[Collector: {coll}]")
            lines.append("  Totals:")
            lines.append(f"    RIB Entries:   {self.n_rib_entries.get(coll, 0)}")
            lines.append(f"    Updates:       {self.n_updates.get(coll, 0)}")
            lines.append(f"    Withdrawals:   {self.n_withdrawals.get(coll, 0)}")
            lines.append(f"    Announcements: {self.n_announcements.get(coll, 0)}")
            
            peers = set()
            for peer_dict in (self.n_rib_entries_per_peer.get(coll, {}),
                              self.n_withdrawals_per_peer.get(coll, {}),
                              self.n_announcements_per_peer.get(coll, {}),
                              self.peer_rib_times.get(coll, {}),
                              self.peer_update_times.get(coll, {})):
                peers.update(peer_dict.keys())
                
            if peers:
                lines.append("  Per-Peer Breakdown:")
                for peer in sorted(peers):
                    asn, addr = peer
                    rib = self.n_rib_entries_per_peer.get(coll, {}).get(peer, 0)
                    withd = self.n_withdrawals_per_peer.get(coll, {}).get(peer, 0)
                    ann = self.n_announcements_per_peer.get(coll, {}).get(peer, 0)
                    
                    rib_times = self.peer_rib_times.get(coll, {}).get(peer, [float('inf'), float('-inf')])
                    upd_times = self.peer_update_times.get(coll, {}).get(peer, [float('inf'), float('-inf')])
                    
                    lines.append(f"    - Peer ASN: {asn:<6} IP: {addr}")
                    lines.append(f"        Counts -> RIB: {rib:<5} | Announce: {ann:<5} | Withdraw: {withd}")
                    lines.append(f"        RIB    -> First: {fmt_ts(rib_times[0]):<19} | Last: {fmt_ts(rib_times[1])}")
                    lines.append(f"        Update -> First: {fmt_ts(upd_times[0]):<19} | Last: {fmt_ts(upd_times[1])}")
                    
        lines.append("\n</TesterObserver>")
        return "\n".join(lines)