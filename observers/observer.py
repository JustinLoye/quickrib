# Observer interface
from datetime import datetime


class Observer:
    """
    Observers base abstract class definition.
    All observers must implement its methods.

    Attributes
    ----------
    name : str
        Observer name. Default is 'observer'.
    output_dir : str
        Directory where observer is dumped to file.
    stop_updating : dict
        time_fmt for formatting the dump file name. Default is '%Y%m%d.%H%M'
        
    Methods
    -------
    add_path_ipv4(self, rc, peer_ip, pfx, path) -> None
        Add a new ipv4 AS path `path` to reach pfx `prefix` announced by peer `peer_ip` in route collector `rc`.
    add_path_ipv6(self, rc, peer_ip, pfx, path) -> None
        Add a new ipv6 AS path `path` to reach pfx `prefix` announced by peer `peer_ip` in route collector `rc`.
    update_withdrawal_ipv4(self, rc, peer_ip, pfx, path) -> None
        Handle ipv4 withdraw update
    update_withdrawal_ipv6(self, rc, peer_ip, pfx, path) -> None
        Handle ipv6 withdraw update
    update_announcement_ipv4(self, rc, peer_ip, pfx, path) -> None
        Handle ipv4 announcement update
    update_announcement_ipv6(self, rc, peer_ip, pfx, path) -> None
        Handle ipv6 announcement update
    dump(ts: datetime) -> None
        Dump observer to file.
    compare(other: Observer) -> None
        Compare to another observer
        Typically used at the end of processing to see if any reconstruction error has been accumulated.
    """
    
    def __init__(self, name: str="observer", output_dir: str = "./", time_fmt='%Y%m%d.%H%M') -> None:
        self.name = name
        self.output_dir = output_dir
        self.time_fmt = time_fmt
    
    def add_path_ipv4(self, rc, peer_ip, pfx, path):
        raise NotImplementedError("Subclass must implement this method")

    def add_path_ipv6(self, rc, peer_ip, pfx, path):
        raise NotImplementedError("Subclass must implement this method")

    def update_withdrawal_ipv4(self, rc, peer_ip, pfx, path):
        raise NotImplementedError("Subclass must implement this method")

    def update_withdrawal_ipv6(self, rc, peer_ip, pfx, path):
        raise NotImplementedError("Subclass must implement this method")
    
    def update_announcement_ipv4(self, rc, peer_ip, pfx, path, old_path=None):
        raise NotImplementedError("Subclass must implement this method")

    def update_announcement_ipv6(self, rc, peer_ip, pfx, path, old_path=None):
        raise NotImplementedError("Subclass must implement this method")
    
    def dump(self, ts: datetime, metadata=None):
        raise NotImplementedError("Subclass must implement this method")
    
    def compare(self, other: 'Observer'):
        raise NotImplementedError("Subclass must implement this method")
