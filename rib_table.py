from collections import defaultdict
from datetime import datetime, timedelta
from itertools import groupby
import logging
import subprocess as sp
from requests_cache import CachedSession
import tempfile
import os
from observers.observer import Observer


class RIBTable:
    """
    RIBTable is a class designed for handling Routing Information Base (RIB) data.
    It includes functionalities for attaching and detaching observers (processing units),
    notifying them of BGP updates and outputs dump, and comparing RIB tables.

    Attributes
    ----------
    _observers : list
        List of `Observer`, used for example to count paths or build a topology.
    data : dict
        Main RIB data storage. 
        Stored as a nested dict `route_collector -> peer_ip -> prefix -> as_path`.
    stop_updating : dict
        A dictionary indicating whether to stop updates for each route collector.
        Set to true if an update entry is later than `self.ts_end`.
     
    Methods
    -------
    attach_observer(observer: Observer) -> None
        Attach observer to RIBtable.
    detach_observer(observer: Observer) -> None
        Detach observer to RIBtable.
    build(rc_to_url: dict) -> None
        Build RIBTable and observers from `rc_to_url`, RIB urls keyed by route collector name.
    update(rc_to_url: dict) -> None
        Update RIBTable and observers from `rc_to_url`, updates urls keyed by route collector name.
    dump(ts: datetime) -> None
        Dump RIB and/or observers to file.
    compare(other: RIBTable) -> None
        Compare RIB and observers to another RIB and observer.
        Typically used at the end of processing to see if any reconstruction error has been accumulated.
    
    Examples
    --------
    >>> # Define data processing
    >>> rib_table = RIBTable(
            session=session,
            peer_ip_filter=peer_ip_filter,
            ts_start=ts_start,
            ts_end=ts_end,
        )
    >>> as_multigraph_observer = ASMultiGraphObserver(name="multigraph")
    >>> rib_table.attach_observer(as_multigraph_observer)
    ... 
    >>> # Process initial RIB
    >>> rc_to_url = get_start_rib(ts_start)
    >>> rib_table.build(rc_to_url)
    >>> rib_table.dump(ts_start)
    ...
    >>> # Apply the updates and dump to file
    >>> for ts in timestamp_list:
    ...     rc_to_url = get_update_rib(ts)
    ...     rib_table.update(rc_to_url)
    ...     rib_table.dump(ts)
    ...
    >>> # Compare reconstructed RIB to ground truth
    >>> end_rib_table = RIBTable(
            session=session,
            peer_ip_filter=peer_ip_filter,
            ts_start=ts_start,
            ts_end=ts_end,
        )
    >>> rc_to_url = get_end_rib(ts_end)
    >>> end_rib_table.build(rc_to_url)
    >>> rib_table.compare(end_rib_table)
    """
    
    def __init__(self, session: CachedSession, peer_ip_filter: list, ts_start: datetime, ts_end: datetime):
        """
        Initialize RIB table

        Parameters
        ----------
        session : CachedSession
            requests-cache session giving access to input RIB and updates files.
        peer_ip_filter : list[str]
            List of peer_ip we only want to process data from.
        ts_start : datetime
            Data processing start time
        ts_end : datetime
            Data processing end time
        """
        self._observers = []
        self.data = {}  # RC -> peer_ip -> pfx -> path
        self.stop_updating = {} # RC -> bool
        self.peer_ip_filter = peer_ip_filter
        self.ts_start = ts_start
        self.ts_end = ts_end
        self.session = session

    def attach_observer(self, observer: Observer):
        self._observers.append(observer)

    def detach_observer(self, observer: Observer):
        self._observers.remove(observer)

    def _notify_add_path_ipv4(self, rc, peer_ip, pfx, path):
        for observer in self._observers:
            observer.add_path_ipv4(rc, peer_ip, pfx, path)
            
    def _notify_add_path_ipv6(self, rc, peer_ip, pfx, path):
        for observer in self._observers:
            observer.add_path_ipv6(rc, peer_ip, pfx, path)

    def _notify_update_announcement_ipv4(self, rc, peer_ip, pfx, new_path, old_path=None):
        for observer in self._observers:
            observer.update_announcement_ipv4(
                rc, peer_ip, pfx, new_path, old_path)

    def _notify_update_announcement_ipv6(self, rc, peer_ip, pfx, new_path, old_path=None):
        for observer in self._observers:
            observer.update_announcement_ipv6(
                rc, peer_ip, pfx, new_path, old_path)
    
    def _notify_update_withdrawal_ipv4(self, rc, peer_ip, pfx, path):
        for observer in self._observers:
            observer.update_withdrawal_ipv4(rc, peer_ip, pfx, path)
            
    def _notify_update_withdrawal_ipv6(self, rc, peer_ip, pfx, path):
        for observer in self._observers:
            observer.update_withdrawal_ipv6(rc, peer_ip, pfx, path)
            
    def _notify_dump(self, ts: datetime):
        for observer in self._observers:
            observer.dump(ts)
    
    def compare(self, other: 'RIBTable'):
        """Compare this RIB table and its observers to another one"""
        # Comparing RIB table
        for rc in self.data:
            for peer_ip in self.data[rc]:
                if len(other.data[rc][peer_ip]) == 0:
                    continue
                logging.info(
                    f"Performing RIB check for peer {peer_ip} at {rc}")
                try:
                    added, removed, modified = dict_diff(
                        self.data[rc][peer_ip], other.data[rc][peer_ip])
                    
                    if len(added) == len(removed) == len(modified) == 0:
                        logging.info(f"No RIB reconstruction error")
                    else:
                        logging.info(
                            f"{len(added)} ({100*len(added)/len(other.data[rc][peer_ip]):.2f} %) pfx present only in ground truth")
                        logging.info(
                            f"{len(removed)} ({100*len(removed)/len(other.data[rc][peer_ip]):.2f} %) pfx present only in my processed version")
                        logging.info(
                            f"{len(modified)} ({100*len(modified)/len(other.data[rc][peer_ip]):.2f} %) pfx present in both but with different as-paths")
                except KeyError:
                    logging.error(f"peer not present in ground truth")
        
        # Compare observers (names must match)
        for other_observer in other._observers:
            for own_observer in self._observers:
                if other_observer.name == own_observer.name:
                    own_observer.compare(other_observer)
                     

    def build(self, rc_to_url: dict) -> None:
            
        self.data = {rc: self._build_rib_from_url(url, rc)
                     for rc, url in rc_to_url.items()}
        
        self.stop_updating = {rc: False for rc in rc_to_url}
        
    def _build_rib_from_url(self, url:str, rc:str) -> dict:
        """
        Build a RIB table and observers for a given route collector's rib table downloaded from `url` 

        Parameters
        ----------
        url : str
            url to the rib file, for example https://data.ris.ripe.net/rrc00/2024.02/bview.20240201.0000.gz
        rc : str
            Route collector name.

        Returns
        -------
        peer_to_pfx_to_path
            A nested dict peer_ip -> prefix -> as_path
        """
        
        db_retrieve_start = datetime.now().timestamp()
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix=".bz2") as tmp:
            tmp.write(self.session.get(url).content)
            tmp.flush()  # Always flush
            tmp_path = tmp.name
        
        logging.info(
            f'Wrote {url} RIB to temporary file in'
            f'{datetime.now().timestamp() - db_retrieve_start:.2f}s')
        
        build_rib_start = datetime.now().timestamp()
                
        # Create a subprocess to feed the content to bgpdump
        p = sp.Popen(['bgpdump', '-m', '-v', tmp_path], stdout=sp.PIPE, text=True, bufsize=1)
        
        peer_to_pfx_to_path = defaultdict(dict)
        n_invalid = 0
        n_entries = 0
        for line in p.stdout:
            n_entries += 1
            res = line.strip().split('|')
            peer_ip, peer_asn, pfx, as_path = res[3:7]
            peer_asn = int(peer_asn)
            
            # Optional prefix filtering
            if len(self.peer_ip_filter) == 0 or peer_ip in self.peer_ip_filter:
                try:
                    path = [int(k) for k, g in groupby(as_path.split(" "))]
                    if is_valid_path(path, peer_asn):
                        
                        # Add entry to RIB table
                        peer_to_pfx_to_path[peer_ip][pfx] = path
                        
                        # Add entry to observers
                        if "." in pfx:
                            self._notify_add_path_ipv4(
                                rc, peer_ip, pfx, path)
                        elif ":" in pfx:
                            self._notify_add_path_ipv6(
                                rc, peer_ip, pfx, path)
                            
                    else:
                        n_invalid += 1
                except ValueError:
                    n_invalid += 1
                        
        if n_entries > 0:
            logging.info(f"{n_invalid} invalid entries out of {n_entries} ({100*n_invalid/n_entries:.2f} %)")
        else:
            logging.warning(f"RIB content empty for {url}")
        
        logging.info(f'Built {url} RIB in {datetime.now().timestamp() - build_rib_start:.2f}s')
        
        os.remove(tmp_path)
        
        return peer_to_pfx_to_path

    def update(self, rc_to_url):
        """Note that update notification is delegated to the private method"""
        for rc, url in rc_to_url.items():
            self._update_rib_from_url(self.data[rc], url, rc) 
        
    def _update_rib_from_url(self, rib: dict, url: str, rc) -> None:
        """
        Update a RIB table and observers for a given route collector's update file downloaded from `url` 

        Parameters
        ----------
        url : str
            url to the updatefile, for example https://data.ris.ripe.net/rrc00/2024.02/updates.20240201.0000.gz
        rc : str
            Route collector name.
        """
        
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix=".bz2") as tmp:
            tmp.write(self.session.get(url).content)
            tmp.flush()  # Always flush
            tmp_path = tmp.name
    
        p = sp.Popen(['bgpdump', '-m', '-v', tmp_path],
                    stdout=sp.PIPE, text=True, bufsize=1)
        
        for update in p.stdout:
            update = update.rstrip().split("|")
            update_ts = datetime.utcfromtimestamp(float(update[1]))
            
            # Handling entries timestamp
            if update_ts < self.ts_start:
                continue
            
            # I add one second because of rounding issues in the updates
            # For example update 1675044000.074351 is rounded to 1675044000 in the RIB
            # Since in the end I compare my reconstruction to ground-truth RIB,
            # I want to be as close as possible to the RIB content
            if update_ts > self.ts_end + timedelta(seconds=1):
                self.stop_updating[rc] = True
                return
            
            update_type = update[2]
            peer_ip = update[3]
            pfx = update[5]
            
            # Optional prefix filtering
            if len(self.peer_ip_filter) > 0 and peer_ip not in self.peer_ip_filter:
                continue
            
            # Handling Withdrawal update
            if len(update) == 6 and update_type == 'W':
                if peer_ip in rib:
                    if pfx in rib[peer_ip]:
                        if "." in pfx:
                            self._notify_update_withdrawal_ipv4(
                                rc, peer_ip, pfx, rib[peer_ip][pfx])
                        elif ":" in pfx:
                            self._notify_update_withdrawal_ipv6(
                                rc, peer_ip, pfx, rib[peer_ip][pfx])
                        rib[peer_ip].pop(pfx)
                                    
            # Handling Announcement update
            elif len(update) == 15 and update_type == 'A':
                peer_asn = int(update[4])
                as_path = update[6]

                # Sanitazing
                if peer_ip in rib:
                    try:
                        new_path = [int(k)
                                    for k, g in groupby(as_path.split(" "))]
                    except ValueError:
                        # If path is not valid (likely contains AS set)
                        # I remove also the old information (outdated)
                        if pfx in rib[peer_ip]:
                            if "." in pfx:
                                self._notify_update_withdrawal_ipv4(
                                    rc, peer_ip, pfx, rib[peer_ip][pfx])
                            elif ":" in pfx:
                                self._notify_update_withdrawal_ipv6(
                                    rc, peer_ip, pfx, rib[peer_ip][pfx])
                            rib[peer_ip].pop(pfx)
                        continue

                    if is_valid_path(new_path, peer_asn):
                        if pfx not in rib[peer_ip]:
                            if "." in pfx:
                                self._notify_update_announcement_ipv4(
                                    rc, peer_ip, pfx, new_path)
                            elif ":" in pfx:
                                self._notify_update_announcement_ipv6(
                                    rc, peer_ip, pfx, new_path)
                        else:
                            if "." in pfx:
                                self._notify_update_announcement_ipv4(
                                    rc, peer_ip, pfx, new_path, old_path=rib[peer_ip][pfx])
                            elif ":" in pfx:
                                self._notify_update_announcement_ipv6(
                                    rc, peer_ip, pfx, new_path, old_path=rib[peer_ip][pfx])
                            
                        rib[peer_ip][pfx] = new_path
                        
                    # If path is not valid, I remove also the old information (outdated)
                    else:
                        if pfx in rib[peer_ip]:
                            if "." in pfx:
                                self._notify_update_withdrawal_ipv4(
                                    rc, peer_ip, pfx, rib[peer_ip][pfx])
                            elif ":" in pfx:
                                self._notify_update_withdrawal_ipv6(
                                    rc, peer_ip, pfx, rib[peer_ip][pfx])
                            rib[peer_ip].pop(pfx)
                else:
                    pass
                    # logging.warning(f'{peer_ip} first seen in update message')

            # Handling other updates type and errors
            else:
                pass
                # logging.warning(f"could not parse {update}")
                # logging.warning(len(update))
        os.remove(tmp_path)

    def dump(self, ts):
        # No need to dump the RIB, I just care about dumping observers
        self._notify_dump(ts)
        

def is_valid_path(path, peer_asn):
    return (len(path) > 1 and path[0] == peer_asn)


def dict_diff(dict1, dict2):
    """
    Compare two dicts
    Returns:
    - `added`: Contains key-value pairs that are present in the second dictionary but not in the first.
    - `removed`: Contains key-value pairs that are present in the first dictionary but not in the second.
    - `modified`: Contains key-value pairs present in both dictionaries but with different values.
    """
    keys_diff = set(dict1.keys()) ^ set(dict2.keys())
    shared_keys = set(dict1.keys()) & set(dict2.keys())

    added = {key: dict2[key] for key in keys_diff if key in dict2}
    removed = {key: dict1[key] for key in keys_diff if key in dict1}

    modified = {}
    for key in shared_keys:
        if dict1[key] != dict2[key]:
            if isinstance(dict1[key], list) and isinstance(dict2[key], list):
                modified[key] = (dict1[key], dict2[key])
            else:
                if dict1[key] != dict2[key]:
                    modified[key] = (dict1[key], dict2[key])

    return added, removed, modified
