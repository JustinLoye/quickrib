import marimo

__generated_with = "0.23.9"
app = marimo.App()


@app.cell
def _():
    import marimo as mo
    import os

    import os

    if os.path.basename(os.getcwd()) == "examples":
        os.chdir("..")
    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
 
    """)
    return


@app.cell
def _():
    """Recreate AS hegemony example of Romain's seminal paper figure 1"""

    import networkx as nx
    import matplotlib.pyplot as plt
    from collections import defaultdict
    import numpy as np

    G = nx.Graph()
    G.add_edge("Stub1", "Regional1")
    G.add_edge("Stub2", "Regional1")
    G.add_edge("Stub3", "Regional2")
    G.add_edge("Stub4", "Regional2")
    G.add_edge("Stub5", "Regional3")
    G.add_edge("Stub6", "Regional3")
    G.add_edge("Stub7", "Regional4")
    G.add_edge("Stub8", "Regional4")
    G.add_edge("Regional1", "Transit")
    G.add_edge("Regional2", "Transit")
    G.add_edge("Regional3", "Transit")
    G.add_edge("Regional4", "Transit")

    # hardcoded hierarchical layout instead of spring_layout
    pos = {
        "Transit": (4.0, 2),
        "Regional1": (1.0, 1), "Regional2": (3.0, 1),
        "Regional3": (5.2, 1), "Regional4": (7.2, 1),
        "Stub1": (0.4, 0), "Stub2": (1.6, 0),
        "Stub3": (2.4, 0), "Stub4": (3.6, 0),
        "Stub5": (4.6, 0), "Stub6": (5.8, 0),
        "Stub7": (6.9, 0), "Stub8": (8.0, 0),
    }

    # biaised centrality from the PAM paper
    centrality = {
        "Transit": 0.58,
        "Regional1": 0.50, "Regional2": 0.50, "Regional3": 0.50, "Regional4": 0.16,
        "Stub1": 0.38, "Stub2": 0.08, "Stub3": 0.38, "Stub4": 0.08,
        "Stub5": 0.38, "Stub6": 0.08, "Stub7": 0.08, "Stub8": 0.08,
    }

    def plot_topology(title: str | None = None, centrality: dict[str, float] | None = None, edge_centrality: dict[str, float] | None = None):

        if centrality:
            node_labels = {n: f"{n}\n{centrality[n]:.2f}" for n in G.nodes()}
        else:
            node_labels = node_labels = {n: n for n in G.nodes()}


        node_size = [2200 if pos[n][1] == 2 else 1600 if pos[n][1] == 1 else 1200 for n in G.nodes()]

        colors = [1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0]

        fig = plt.figure()
        ax = fig.add_subplot(111)
        nx.draw(G, pos, ax=ax, labels=node_labels, with_labels=bool(node_labels),
                node_color=colors, edgecolors="black", node_size=node_size,
                font_size=10, font_weight="bold", cmap=plt.cm.winter)
        if title:
            ax.set_title(title)

        if edge_centrality:
            edge_labels = {e: f"{edge_centrality[e]:.2f}" for e in G.edges()}
            # print(edge_labels)
            nx.draw_networkx_edge_labels(G, pos, edge_labels)

        plt.show()

    plot_topology("PAM paper topology")
    return G, defaultdict, nx, plot_topology


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Vanilla betweenness centrality (aka Expected BC):
    """)
    return


@app.cell
def _(G, nx, plot_topology):
    bc = nx.betweenness_centrality(G, endpoints=True)
    edge_bc = nx.edge_betweenness_centrality(G)
    # print(edge_bc)
    plot_topology("Expected BC",bc, edge_bc)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Biaised betweenness centrality (aka Sampled BC)
    """)
    return


@app.cell
def _(G, defaultdict, nx, plot_topology):
    from itertools import pairwise

    stub1_paths = list(nx.all_simple_paths(G, source="Stub1",
                                      target=[n for n in G.nodes()
                                              if n != "Stub1"]))
    stub3_paths = list(nx.all_simple_paths(G, source="Stub3",
                                      target=[n for n in G.nodes()
                                              if n != "Stub3"]))
    stub5_paths = list(nx.all_simple_paths(G, source="Stub5",
                                      target=[n for n in G.nodes()
                                              if n != "Stub5"]))

    biaised_paths = stub1_paths + stub3_paths + stub5_paths


    def node_betweenness(paths: list[list[str]]) -> dict[str, float]:
        """Version with a list of paths"""
        node_to_betweenness: defaultdict[str, float] = defaultdict(float)
        score_increment = 1.0/len(paths)

        for path in paths:
            for node in path:
                node_to_betweenness[node] += score_increment

        return node_to_betweenness

    def edge_betweenness(paths: list[list[str]]) -> dict[str, float]:
        """Version with a list of paths"""
        edge_to_betweenness: defaultdict[tuple[str, str], float] = defaultdict(float)
        score_increment = 1.0/len(paths)

        for path in paths:
            for u, v in pairwise(path):
                edge_to_betweenness[(u,v)] += score_increment
                edge_to_betweenness[(v,u)] += score_increment

        return edge_to_betweenness

    def get_paths_count(paths: list[list[str]]):
        paths_count = defaultdict(int)
        for path in paths:
            for node in path:
                paths_count[node] += 1
        return paths_count

    def betweenness(paths_count: dict[str, float], norm: float) -> dict[str, float]:
        """Compute betweenness based on paths_count of asn"""
        # weights_sum = sum(paths_count.values())
        return {node: weight / norm for node, weight in paths_count.items()}

    biaised_bc1 = node_betweenness(biaised_paths)
    biaised_bc1 = {key: round(val, 3) for key, val in biaised_bc1.items()}


    paths_count = get_paths_count(biaised_paths)
    biaised_bc2 = betweenness(paths_count, len(biaised_paths))
    biaised_bc2 = {key: round(val, 3) for key, val in biaised_bc2.items()}

    assert biaised_bc1 == biaised_bc2, "2 different ways to compute node BC does not match"

    biaised_edge_bc = edge_betweenness(biaised_paths)

    plot_topology("Sampled BC",biaised_bc1, biaised_edge_bc)
    return (
        edge_betweenness,
        node_betweenness,
        stub1_paths,
        stub3_paths,
        stub5_paths,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    AS Hegemony
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    by hand
    """)
    return


@app.cell
def _():
    # from sortedcontainers import SortedList
    # from math import floor

    # # Betweenness centrality of all nodes, for eeach vp
    # vp_bcs = [node_betweenness(stub1_paths),
    #             node_betweenness(stub3_paths),
    #             node_betweenness(stub5_paths)]

    # # Agglomerate scores from all vps
    # node_to_bcs = defaultdict(SortedList)
    # for stub_bc in stub_bcs:
    #     for node in stub_bc:
    #         node_to_bcs[node].add(stub_bc[node])

    # def hegemony(node_to_bcs: dict[SortedList], alpha=0.4):
    #     hegemony_nodes_score = defaultdict(float)

    #     for node in node_to_bcs:

    #         # Slice index to filter biaised (extremal scores)
    #         filter_lower_threshold = floor(alpha * len(node_to_bcs[node]))
    #         filter_upper_threshold = len(node_to_bcs[node]) - floor(alpha * len(node_to_bcs[node]))

    #         bc_unbiaised_scores = node_to_bcs[node][filter_lower_threshold:filter_upper_threshold]
    #         if len(bc_unbiaised_scores) == 0:
    #             hegemony_nodes_score[node] = 0.0
    #         else:
    #             hegemony_nodes_score[node] = np.mean(bc_unbiaised_scores)

    #     return hegemony_nodes_score

    # hegemony_bc = hegemony(node_to_bcs)
    # hegemony_bc = {key: round(val, 3) for key, val in hegemony_bc.items()}

    # plot_topology("Sampled BC",hegemony_bc)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    with scipy
    """)
    return


@app.cell
def _(
    G,
    edge_betweenness,
    node_betweenness,
    plot_topology,
    stub1_paths,
    stub3_paths,
    stub5_paths,
):
    from scipy import stats

    # Betweenness centrality of all nodes, for each vp
    vp_bcs = [node_betweenness(stub1_paths),
                node_betweenness(stub3_paths),
                node_betweenness(stub5_paths)]

    vp_edge_bcs = [edge_betweenness(stub1_paths),
                edge_betweenness(stub3_paths),
                edge_betweenness(stub5_paths)]

    node_bcs = {n: [vp_bcs[0][n], vp_bcs[1][n], vp_bcs[2][n]] for n in G.nodes()}
    edge_bcs = {e: [vp_edge_bcs[0][e], vp_edge_bcs[1][e], vp_edge_bcs[2][e]] for e in G.edges()}

    node_hege = {n: stats.trim_mean(bcs, proportiontocut=0.4) for n, bcs in node_bcs.items()}
    edge_hege = {e: stats.trim_mean(bcs, proportiontocut=0.4) for e, bcs in edge_bcs.items()}

    plot_topology("Sampled BC",node_hege, edge_hege)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Apply to real world BGP data
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    I recommend inspecting the logs to make sure:
    - the expected BGP archives got parsed
    - the expected fullfeed peers were detected with the expected number of prefixes
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ### Global
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Setup experiment and get the RIB table
    """)
    return


@app.cell
def _():
    from pathlib import Path
    from pybgpflux import FilterOptions
    import datetime
    from quickrib import QuickRIBConfig, QuickRIB
    import logging
    import sys

    Path("cache").mkdir(exist_ok=True)

    FORMAT = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        format=FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            # Beside the MRT archives, like the other examples, not in the
            # working directory.
            logging.FileHandler(Path("cache") / "example_hegemony.log"),
        ],
        # INFO, not DEBUG: at DEBUG the pipeline counts every peer's prefixes
        # at each dump, which walks the whole table.
        level=logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = QuickRIBConfig(
        # Starting on the Route Views dump: the table at start_time is the dump
        # itself, with no catch-up interval to replay.
        start_time=datetime.datetime(2010,9,2,0,0),
        end_time=datetime.datetime(2010,9,2,0,5),
        dump_res=datetime.timedelta(minutes=5), # useless here
        collectors=["route-views.wide", "rrc04", "rrc00", "route-views.linx"],
        cache_dir=Path("cache"),
        parser="bgpkit",
        filters=FilterOptions(ip_version=4),
    )

    rib = QuickRIB.from_config(config)

    rib.initialize_processing()
    rib.build_rib()
    return FilterOptions, Path, QuickRIB, QuickRIBConfig, datetime, rib


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Apply node hegemony to RIB table
    Visual sanity checks: top nodes are T1
    """)
    return


@app.cell
def _(rib):
    import polars as pl
    from quickrib.observers.hegemony import node_hegemony

    bgp_node_hege = node_hegemony(rib.rib.data, alpha=0.2)
    bgp_node_hege_df = pl.DataFrame({"asn": bgp_node_hege.keys(), "hege": bgp_node_hege.values()})
    bgp_node_hege_df.sort(by="hege", descending=True)
    return node_hegemony, pl


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Apply edge hegemony to RIB rable
    Visual sanity checks: top edges are T1
    """)
    return


@app.cell
def _(pl, rib):
    from quickrib.observers.hegemony import edge_hegemony

    bgp_edge_hege = edge_hegemony(rib.rib.data, alpha=0.2)
    bgp_edge_hege_df = pl.DataFrame({"asn": bgp_edge_hege.keys(), "hege": bgp_edge_hege.values()})
    bgp_edge_hege_df.sort(by="hege", descending=True)
    return (edge_hegemony,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ### Local

    Local AS hegemony is useful to find AS dependency, i.e. how dependent an origin ASN is to its upstream ASes.
    For now the local version simply consists of applying the global version to BGP data filtered to a specific origin ASN.
    The current implementation is not optimized because for each origin ASN we need to build a RIB, ideally we'd like to build the RIB once and then compute the local hegemony for all ASes
    """)
    return


@app.cell
def _(FilterOptions, Path, QuickRIB, QuickRIBConfig, datetime):
    _config = QuickRIBConfig(
        start_time=datetime.datetime(2020,3,3,0,0),
        end_time=datetime.datetime(2020,3,3,0,5),
        dump_res=datetime.timedelta(minutes=5),
        collectors=["rrc00", "rrc10","route-views2", "route-views.linx"],
        cache_dir=Path("cache"),
        parser="bgpkit",
        filters=FilterOptions(ip_version=4, origin_asn=2497),
    )

    rib_iij = QuickRIB.from_config(_config)

    rib_iij.initialize_processing()
    rib_iij.build_rib()
    return (rib_iij,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Sanity check: data matches (might be few differences because of VP choice) https://www.ihr.live/en/documentation#AS-dependency
    """)
    return


@app.cell
def _(node_hegemony, pl, rib_iij):
    _bgp_node_hege = node_hegemony(rib_iij.rib.data, alpha=0.1)
    _bgp_node_hege_df = pl.DataFrame({"asn": _bgp_node_hege.keys(), "hege": _bgp_node_hege.values()})
    _bgp_node_hege_df.sort(by="hege", descending=True)
    return


@app.cell
def _(edge_hegemony, pl, rib_iij):
    _bgp_edge_hege = edge_hegemony(rib_iij.rib.data, alpha=0.1)
    _bgp_edge_hege_df = pl.DataFrame({"asn": _bgp_edge_hege.keys(), "hege": _bgp_edge_hege.values()})
    _bgp_edge_hege_df.sort(by="hege", descending=True)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Other sanity check: confirm that TIM has a 100 percent dependency to Telecom Italia
    """)
    return


@app.cell
def _(FilterOptions, Path, QuickRIB, QuickRIBConfig, datetime):
    _config = QuickRIBConfig(
        start_time=datetime.datetime(2020,3,3,0,0),
        end_time=datetime.datetime(2020,3,3,0,5),
        dump_res=datetime.timedelta(minutes=5),
        collectors=["rrc00", "rrc10","route-views2", "route-views.linx"],
        cache_dir=Path("cache"),
        parser="bgpkit",
        filters=FilterOptions(ip_version=4, origin_asn=3269),
    )

    rib_tim = QuickRIB.from_config(_config)

    rib_tim.initialize_processing()
    rib_tim.build_rib()
    return (rib_tim,)


@app.cell
def _(node_hegemony, pl, rib_tim):
    _bgp_node_hege = node_hegemony(rib_tim.rib.data, alpha=0.1)
    _bgp_node_hege_df = pl.DataFrame({"asn": _bgp_node_hege.keys(), "hege": _bgp_node_hege.values()})
    _bgp_node_hege_df.sort(by="hege", descending=True)
    return


if __name__ == "__main__":
    app.run()
