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
5. Attach the observer to the RIB table observable in `BGPDownloader.warm_update_process()` in [main.py](main.py).
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
4. Attach it in [main.py](main.py).
    ```python
    as_multigraph_observer = ASMultiGraphObserver(name="multigraph", output_dir=observers_output_dir)

    rib_table.attach_observer(as_multigraph_observer)
    ```