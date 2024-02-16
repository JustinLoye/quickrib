"""
Graph observers
"""

from datetime import datetime
import logging
from observers.observer import Observer
import networkx as nx
import os
from typing import Optional

from abc import ABC, abstractmethod

class BaseGraphObserver(Observer, ABC):
    """
    Graph Observers base abstract class definition.
    All observers must provide the attributes:
    - _graph_ipv4
    - _graph_ipv6
    And implement the methods
    - _add_path(self, graph, path, **kwargs)
    - _remove_path(self, graph, path, **kwargs)

    Attributes
    ----------
    name : str
        Observer name. Default is 'observer'.
    output_dir : str
        Directory where observer is dumped to file.
    stop_updating : dict
        time_fmt for formatting the dump file name. Default is '%Y%m%d.%H%M'
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    @property
    @abstractmethod
    def graph_ipv4(self):
        """Return the IPv4 graph. Subclasses must override this property."""
        pass

    @property
    @abstractmethod
    def graph_ipv6(self):
        """Return the IPv6 graph. Subclasses must override this property."""
        pass

    @abstractmethod
    def _add_path(self, graph, path, **kwargs):
        """Add a path in a graph. Subclasses must override this property"""
        pass

    @abstractmethod
    def _remove_path(self, graph, path, **kwargs):
        """Remove a path in a graph. Subclasses must override this property"""
        pass
    
    def add_path_ipv4(self, rc, peer_ip, pfx, path):
        self._add_path(self.graph_ipv4, path, rc=rc, peer_ip=peer_ip, pfx=pfx)
    
    def add_path_ipv6(self, rc, peer_ip, pfx, path):
        self._add_path(self.graph_ipv6, path, rc=rc, peer_ip=peer_ip, pfx=pfx)
    
    def update_withdrawal_ipv4(self, rc, peer_ip, pfx, path):
        # Update graph based on BGP withdraw message
        self._remove_path(self.graph_ipv4, path, rc=rc, peer_ip=peer_ip, pfx=pfx)

    def update_withdrawal_ipv6(self, rc, peer_ip, pfx, path):
        # Update graph based on BGP withdraw message
        self._remove_path(self.graph_ipv6, path, rc=rc, peer_ip=peer_ip, pfx=pfx)

    def update_announcement_ipv4(self, rc, peer_ip, pfx, new_path, old_path):
        if old_path:
            self._remove_path(self.graph_ipv4, old_path,
                              rc=rc, peer_ip=peer_ip, pfx=pfx)
            self._add_path(self.graph_ipv4, new_path,
                           rc=rc, peer_ip=peer_ip, pfx=pfx)
        else:
            self._add_path(self.graph_ipv4, new_path,
                           rc=rc, peer_ip=peer_ip, pfx=pfx)

    def update_announcement_ipv6(self, rc, peer_ip, pfx, new_path, old_path):
        if old_path:
            self._remove_path(self.graph_ipv6, old_path,
                              rc=rc, peer_ip=peer_ip, pfx=pfx)
            self._add_path(self.graph_ipv6, new_path,
                           rc=rc, peer_ip=peer_ip, pfx=pfx)
        else:
            self._add_path(self.graph_ipv6, new_path,
                           rc=rc, peer_ip=peer_ip, pfx=pfx)
            

class GraphPropertiesMixin:
    @property
    def graph_ipv4(self):
        return self._graph_ipv4

    @property
    def graph_ipv6(self):
        return self._graph_ipv6


class ASGraphObserver(GraphPropertiesMixin, BaseGraphObserver):
    def __init__(self, *args, multigraph_observer=Optional['ASMultiGraphObserver'], **kwargs):
        """
        recommended kwargs:
        `name` (default Observer)
        `output_dir` (default ./)
        
        optional kwargs:
        a multigraph observer in order to get the peer count.
        """
        super().__init__(*args, **kwargs)
        self._graph_ipv4 = nx.Graph()
        self._graph_ipv6 = nx.Graph()
        self.multigraph_observer = multigraph_observer
        
    def _remove_path(self, as_graph, path, **kwargs):
        for l in range(0, len(path)-1):
            u, v = path[l], path[l+1]
            try:
                as_graph[u][v]['paths_count'] -= 1
                if as_graph[u][v]['paths_count'] == 0:
                    as_graph.remove_edge(u, v)
            except KeyError:
                # logging.debug(
                #     f"trying to remove a non-existing link between {u} and {v}")
                pass

    def _add_path(self, as_graph, path, **kwargs):
        for l in range(0, len(path)-1):
            u, v = path[l], path[l+1]
            try:
                as_graph[u][v]['paths_count'] += 1
            except KeyError:
                as_graph.add_edge(u, v, paths_count=1)

    def dump(self, ts: datetime, metadata=None):
        """Dump graph data to file"""
        
        # Define output filepath
        filepath_ipv4 = os.path.join(self.output_dir,
                                     ts.strftime(f'{self.name}_ipv4.%Y%m%d.%H%M.csv'))
        filepath_ipv6 = os.path.join(self.output_dir,
                                     ts.strftime(f'{self.name}_ipv6.%Y%m%d.%H%M.csv'))
        
        with open(filepath_ipv4, 'w') as edgelist_ipv4, open(filepath_ipv6, 'w') as edgelist_ipv6:
            
            if metadata:
                print(f"#{metadata}\n", file=edgelist_ipv4)
                print(f"#{metadata}\n", file=edgelist_ipv6)
            
            # Straightforward dump...
            if self.multigraph_observer is None:
                print("#origin,destination,paths_count",
                      file=edgelist_ipv4)
                print("#origin,destination,paths_count",
                        file=edgelist_ipv6)
                
                for u, v, d in self.as_graph_ipv4.edges(data=True):
                    print(u, v, d['paths_count'], sep=",", file=edgelist_ipv4)
                    
                for u, v, d in self.as_graph_ipv4.edges(data=True):
                    print(u, v, d['paths_count'], sep=",", file=edgelist_ipv6)
            
            # ... or get the peers_count from an ASMultiGraph object
            else:
                print("#origin,destination,paths_count,peers_count",
                      file=edgelist_ipv4)
                print("#origin,destination,paths_count,peers_count",
                        file=edgelist_ipv6)
                
                for u, v, d in self.graph_ipv4.edges(data=True):
                    try:
                        peers_count = len(
                            self.multigraph_observer.graph_ipv4[u][v])
                    except KeyError:
                        # logging.error(
                        #     f"Dump: graph link {u}-{v} not present in multigraph")
                        continue
                    print(u, v, d['paths_count']/peers_count,
                            peers_count, sep=",", file=edgelist_ipv4)
                    
                for u, v, d in self.graph_ipv6.edges(data=True):
                    try:
                        peers_count = len(
                            self.multigraph_observer.graph_ipv6[u][v])
                    except KeyError:
                        # logging.error(
                        #     f"Dump: graph link {u}-{v} not present in multigraph")
                        continue
                    print(u, v, d['paths_count']/peers_count,
                          peers_count, sep=",", file=edgelist_ipv6)
        
        logging.info(f"wrote graph of {len(self.graph_ipv4.edges())} edges to {filepath_ipv4}")            
                    
    def compare(self, other: 'ASGraphObserver'):
        logging.info(f"Performing {self.name} check for as_graph_ipv4")
        
        # Computing checks
        self.graph_ipv4.remove_nodes_from(list(nx.isolates(self.graph_ipv4)))
        comparison = compare_weighted_graphs(
            self.graph_ipv4, other.graph_ipv4, weight_key="paths_count")
        
        # logging checks
        has_passed_checks = True
        for key, value in comparison.items():
            if len(value) > 0:
                logging.debug(f"{key}: {value}")
                has_passed_checks = False
        if has_passed_checks: 
            logging.info(f"No reconstruction errors")
        else:
            logging.info(f"Reconstruction errors")
                

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

    def _remove_path(self, as_graph, path, **kwargs):
        key = f"{kwargs['rc']}_{kwargs['peer_ip']}"
        for l in range(0, len(path)-1):
            u, v = path[l], path[l+1]
            try:
                as_graph[u][v][key]['paths_count'] -= 1
                if as_graph[u][v][key]['paths_count'] == 0:
                    as_graph.remove_edge(u, v, key=key)
            except KeyError:
                # logging.debug(
                #     f"trying to remove a non-existing link between {u} and {v} with a key {key}")
                pass

    def _add_path(self, as_graph, path, **kwargs):
        key = f"{kwargs['rc']}_{kwargs['peer_ip']}"
        for l in range(0, len(path)-1):
            u, v = path[l], path[l+1]
            try:
                as_graph[u][v][key]['paths_count'] += 1
            except KeyError:
                as_graph.add_edge(u, v, key=key, paths_count=1)

    def dump(self, ts: datetime):
        # Dump graph data to file or database
        pass
    

def compare_weighted_graphs(graph1, graph2, weight_key='weight'):
    """
    Returns:
    - nodes in graph2 but not in graph1
    - nodes in graph1 but not in graph2
    - edges in graph2 but not in graph1 (considering undirected edges)
    - edges in graph1 but not in graph2 (considering undirected edges)
    - edges in both but with different weights
    """
    added_nodes = set(graph2.nodes()) - set(graph1.nodes())
    removed_nodes = set(graph1.nodes()) - set(graph2.nodes())

    added_edges = set(graph2.edges()) - set(graph1.edges()) - \
        {(v, u) for u, v in set(graph1.edges())}
    removed_edges = set(graph1.edges()) - set(graph2.edges()) - \
        {(v, u) for u, v in set(graph2.edges())}

    modified_edges = {}
    for edge in set(graph1.edges()) & set(graph2.edges()):
        weight1 = graph1.get_edge_data(*edge).get(weight_key, 1)
        weight2 = graph2.get_edge_data(*edge).get(weight_key, 1) if graph2.has_edge(
            *edge) else graph2.get_edge_data(edge[1], edge[0]).get(weight_key, 1)

        if weight1 != weight2:
            modified_edges[edge] = (weight1, weight2)

    return {
        'added_nodes': list(added_nodes),
        'removed_nodes': list(removed_nodes),
        'added_edges': list(added_edges),
        'removed_edges': list(removed_edges),
        'modified_edges': modified_edges
    }
    

def compare_multigraphs(graph1, graph2):
    # Check if the sets of nodes and edges are equal
    if set(graph1.nodes()) != set(graph2.nodes()):
        print("nodes are different")
        return False
    # Check the degree of each node
    for node in graph1.nodes():
        if graph1.degree(node) != graph2.degree(node):
            print(
                f"degree of node not matching: {graph1.degree(node)}{graph2.degree(node)}")
            return False
    return True
