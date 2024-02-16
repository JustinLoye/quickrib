"""
Counting various stuff about the updates
"""

from datetime import datetime
import json
from observers.observer import Observer
from collections import defaultdict
import os

class UpdateCountObserver(Observer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_updates = defaultdict(int)
        self.n_withdrawals_ipv4 = defaultdict(int)
        self.n_withdrawals_ipv6 = defaultdict(int)
        self.n_announcements_ipv4 = defaultdict(int)
        self.n_announcements_ipv6 = defaultdict(int)
        self.n_updates_per_peer = defaultdict(lambda: defaultdict(int))
    
    def add_path_ipv4(self, rc, peer_ip, pfx, path):
        # Only interested in updates so nothing for RIB construction
        pass

    def add_path_ipv6(self, rc, peer_ip, pfx, path):
        # Only interested in updates so nothing for RIB construction
        pass

    def update_withdrawal_ipv4(self, rc, peer_ip, pfx, path):
        # Update graph based on BGP withdraw message
        self.n_updates[rc] += 1
        self.n_withdrawals_ipv4[rc] += 1
        self.n_updates_per_peer[rc][peer_ip] += 1

    def update_withdrawal_ipv6(self, rc, peer_ip, pfx, path):
        # Update graph based on BGP withdraw message
        self.n_updates[rc] += 1
        self.n_withdrawals_ipv6[rc] += 1
        self.n_updates_per_peer[rc][peer_ip] += 1

    def update_announcement_ipv4(self, rc, peer_ip, pfx, new_path, old_path):
        self.n_updates[rc] += 1
        self.n_announcements_ipv4[rc] += 1
        self.n_updates_per_peer[rc][peer_ip] += 1

    def update_announcement_ipv6(self, rc, peer_ip, pfx, new_path, old_path):
        self.n_updates[rc] += 1
        self.n_announcements_ipv6[rc] += 1
        self.n_updates_per_peer[rc][peer_ip] += 1

    def dump(self, ts: datetime):
        
        serializable_dict = {
            "n_updates": dict(self.n_updates),
            "n_withdrawals_ipv4": dict(self.n_withdrawals_ipv4),
            "n_withdrawals_ipv6": dict(self.n_withdrawals_ipv6),
            "n_announcements_ipv4": dict(self.n_announcements_ipv4),
            "n_announcements_ipv6": dict(self.n_announcements_ipv6),
            "n_updates_per_peer": {k: dict(v) for k, v in self.n_updates_per_peer.items()}
        }
        
        filepath = os.path.join(self.output_dir,
                                ts.strftime(f'{self.name}.{self.time_fmt}.json'))
        
        with open(filepath, 'w') as update_count_file:
            json.dump(serializable_dict, update_count_file)
    
    def compare(self, other):
        # not concerned with checks
        pass
