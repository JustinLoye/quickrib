"""
Counting stuff about paths
"""

from datetime import datetime
import json
import logging
from observers.observer import Observer
from collections import defaultdict
import os


class PathObserver(Observer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Main data structure 
        self.paths_count = defaultdict(int)
    
    def _add_path(self, path):
        self.paths_count[tuple(path)] += 1
      
    def _remove_path(self, path):
        path = tuple(path)
        self.paths_count[path] -= 1
        if self.paths_count[path] <= 0:
            self.paths_count.pop(path)
            
    def add_path_ipv4(self, rc, peer_ip, pfx, path):
        self._add_path(path)

    def add_path_ipv6(self, rc, peer_ip, pfx, path):
        self._add_path(path)

    def update_withdrawal_ipv4(self, rc, peer_ip, pfx, path):
        # Update graph based on BGP withdraw message
        self._remove_path(path)

    def update_withdrawal_ipv6(self, rc, peer_ip, pfx, path):
        # Update graph based on BGP withdraw message
        self._remove_path(path)

    def update_announcement_ipv4(self, rc, peer_ip, pfx, new_path, old_path):
        if old_path:
            self._remove_path(old_path)
        self._add_path(new_path)

    def update_announcement_ipv6(self, rc, peer_ip, pfx, new_path, old_path):
        if old_path:
            self._remove_path(old_path)
        self._add_path(new_path)

    def dump(self, ts: datetime):
        
        # Get length count stats
        paths_length_count = defaultdict(int)
        for path in self.paths_count:
            paths_length_count[len(path)] += 1
            
        serializable_dict = {
            "n_unique_paths": len(self.paths_count),
            "paths_count": {str(key): value for key, value in self.paths_count.items()},
            "paths_length_count": dict(paths_length_count)
        }

        filepath = os.path.join(self.output_dir,
                                ts.strftime(f'{self.name}.{self.time_fmt}.json'))
        
        with open(filepath, 'w') as path_file:
            json.dump(serializable_dict, path_file)

    def compare(self, other):
        # not concerned with checks
        pass
