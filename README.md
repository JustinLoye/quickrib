# QuickRIB
Python tool for efficient (re-)construction and analysis of BGP "routing tables", inspired by [BGPView](https://github.com/CAIDA/bgpview) from [CAIDA](https://www.caida.org/).

## Background

QuickRIB background is a subset of BGPView [background](https://github.com/CAIDA/bgpview?tab=readme-ov-file#background):

> At a high level, the goal of BGPView is to facilitate the inference of "Global" routing tables at a finer granularity than the RIB dumps provided by the RouteViews and RIPE RIS projects. For example, currently RouteViews collectors export a snapshot of their RIB (a "RIB dump") every 2 hours -- for RIPE RIS this is once every 8 hours. For applications [...] interested in observing short-duration events, they cannot rely on these RIB dumps alone (i.e., using RouteViews data, they would only be able to observe events that last at least 2 hours, and with up to 2 hours of latency).  
The normal approach to solving this problem is to write some code that starts with a RIB dump, and then incrementally applies update information to approximate the state of each peer's routing table at any point in time. Then, depending on the application, one can either react to specific events (e.g., a prefix is announced, withdrawn, etc.) or, periodically walk through these routing tables and perform analysis on the entire "global" routing table (or, "inferred" RIB). BGPView is designed to make it simple to write analysis code in the latter model, with all of the details of obtaining the raw BGP data, processing it, and inferring the routing table for each peer are abstracted away from the user. The user is instead able to focus on writing an analysis kernel (a "BGPView Consumer") that is invoked every time a new inferred RIB (a "BGPView") is available.

In short, this project provide the following functionalities to reconstruct and analyze BGP routing tables:
- Downloading and caching RIPE RIS and RouteViews MRT archives
- Building an agglomerated RIB table from different collectors
- Handling the updates in order to have a complete RIB at a higher frequency
- Predefined analysis kernels called *observers* (e.g. topology graphs, AS paths count),
- Easy implementation of observers


The main differences between QuickRIB and BGPView are the following:
- No real-time functionalities
- Written in Python and easily extendable
- Analysis output time must be aligned to update files resolution (no user custom timestamp)
- Probably less performant but still efficient. `bgpdump` does efficiently most of the heavy lifting. The rest of the program is simply collecting the output to efficient data structures (Python dicts-like)

## Quick start

QuickRIB is run within a docker container:

```bash
docker build -t quickrib .
docker run --rm --name quickrib -v /path/to/user_config.py:/app/configs/config.py -v /path/to/output_folder:/app/data quickrib
```

Where `user_config.py` is a file that defines the data collection parameters, for example: 
```python
# Time format processing arguments and naming files
time_fmt = '%Y%m%d.%H%M'

# Data collection data range with inclusive borders
date_range = "20100901.0000,20100901.0200"

# Most of files will use this prefix
# Will make a folder in output_dir
output_filename = "default_conf"

# Time between observers dump in seconds
interval = 900

# Collector names
collectors = ["route-views.sydney", "route-views.wide"]

# Routing table for only ASNs in this list
peer_asns = ["2497"]

# Routing table for only peers in this list
peer_ips = ["127.0.0.0", "192.168.0.0"]
```

By default (without user config files) the program will run during 2 minutes with [config.py](/configs/config.py), corresponding to two hours of data collection from two small collectors in 2010:
```bash
docker run --rm --name quickrib -v /path/to/output_folder:/app/data quickrib
```
It will output the observers:
- [update_count](/observers/update_count.py) to count the number of updates,
- [graph](/observers/graph.py) and [multigraph](/observers/graph.py) to keep track of the complete topology,
- [path](/observers/path.py) to keep track of the AS paths.

## Adding your own data analysis observer
In the same spirit as BGPView *consumers*, it is easy to add a new data analysis module called *observer*. Observers are decoupled from the RIB updating process, allowing to focus on the analysis.  
  
First, you'll need to be able to make changes to the project by mounting its root to a development container.

```bash
docker run --name quickrib_dev -it -v $(pwd):/app --entrypoint bash quickrib
```

### General case
All observers must implement the abstract base class defined in [observer.py](/observers/observer.py). You should provide the building and updating mechanism, as well as how to dump the data to file.   
  
Let's take the example of the [update count observer](/observers/update_count.py).
1. First define your data structure:
    ```python
    class UpdateCountObserver(Observer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.n_updates = defaultdict(int)
            self.n_withdrawals_ipv4 = defaultdict(int)
            self.n_withdrawals_ipv6 = defaultdict(int)
            self.n_announcements_ipv4 = defaultdict(int)
            self.n_announcements_ipv6 = defaultdict(int)
            self.n_updates_per_peer = defaultdict(lambda: defaultdict(int))
    ```
2. Provide the building mechanism:
    ```python
    def add_path_ipv4(self, rc, peer_ip, pfx, path):
        # Only interested in updates so nothing for RIB construction
        pass

    def add_path_ipv6(self, rc, peer_ip, pfx, path):
        # Only interested in updates so nothing for RIB construction
        pass
    ```
3. Provide the update mechanism (note that here we don't make use of all the data contained in the update):
    ```python
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
    ```
4. Define how to dump the data:
    ```python
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
                                ts.strftime(f"{self.name}.{self.time_fmt}.json"))
        
        with open(filepath, 'w') as update_count_file:
            json.dump(serializable_dict, update_count_file)
    ```
5. Attach the observer to the RIB table observable in `BGPDownloader.warm_update_process()` in [main.py](main.py)
    ```python
    update_count_observer = UpdateCountObserver(name="update_count", output_dir=observers_output_dir)

    rib_table.attach_observer(update_count_observer)
    ```

### Graph observer case
Implementing a graph observer is shorter. Add yours to [/observers/graph.py](/observers/graph.py). Let's take the example of the multigraph observer, that keep track of ASes engaged in peering session (adjacent in AS paths) and which peer reported it at which route collector:

1. A proper subclassing and `self._graph_ipv4` and `self._graph_ipv6` **must** be provided:
    ```python
    class ASMultiGraphObserver(GraphPropertiesMixin, BaseGraphObserver):
        """
        recommended kwargs:
        `name` (default observer)
        `output_dir` (default ./)        
        """
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._graph_ipv4 = nx.MultiGraph()
            self._graph_ipv6 = nx.MultiGraph()
    ```
2. `self._remove_path()` and `self._add_path()` must be implemented:
    ```python
    def _remove_path(self, as_graph, path, **kwargs):
        key = f"{kwargs['rc']}_{kwargs['peer_ip']}"
        for l in range(0, len(path)-1):
            u, v = path[l], path[l+1]
            try:
                as_graph[u][v][key]['paths_count'] -= 1
                if as_graph[u][v][key]['paths_count'] == 0:
                    as_graph.remove_edge(u, v, key=key)
            except KeyError:
                pass

    def _add_path(self, as_graph, path, **kwargs):
        key = f"{kwargs['rc']}_{kwargs['peer_ip']}"
        for l in range(0, len(path)-1):
            u, v = path[l], path[l+1]
            try:
                as_graph[u][v][key]['paths_count'] += 1
            except KeyError:
                as_graph.add_edge(u, v, key=key, paths_count=1)
    ```
3. Define how to dump the data
4. Attach it in `main.py`
    ```python
    as_multigraph_observer = ASMultiGraphObserver(name="multigraph", output_dir=observers_output_dir)

    rib_table.attach_observer(as_multigraph_observer)
    ```