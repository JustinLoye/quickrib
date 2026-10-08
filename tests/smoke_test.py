"""Smoke test for a built distribution, run by the publish workflow.

Executed with ``uv run --isolated --no-project --with dist/<artifact>``, so only
the package and its declared dependencies are present and nothing is on the
network. It checks that the package imports, that the typing marker and the
observers shipped with it are included, and that a table can be built and
observed from hand-made elements.
"""

import datetime
import sys


def test_smoke():
    import quickrib
    from quickrib import Observer, QuickRIB, QuickRIBConfig, RIBTable
    from quickrib.elements import ParsedElement, WithdrawalElement
    from quickrib.observers import HegemonyObserver, IODAObserver, UpdateTagsCounter

    assert quickrib is not None and Observer is not None and QuickRIB is not None

    # The `py.typed` marker is what makes the type hints usable downstream.
    import importlib.resources

    assert importlib.resources.files("quickrib").joinpath("py.typed").is_file()

    # A configuration validates and normalises its times without any network.
    config = QuickRIBConfig(
        start_time=datetime.datetime(2010, 9, 1, 0, 0),
        end_time=datetime.datetime(2010, 9, 1, 1, 0),
        collectors=["rrc04"],
        dump_res=datetime.timedelta(minutes=5),
    )
    assert config.start_time.tzinfo == datetime.UTC

    # A table driven by hand-made elements, observed by three shipped observers.
    rib = RIBTable()
    counter = UpdateTagsCounter()
    hegemony = HegemonyObserver()
    ioda = IODAObserver(ff_peers={("rrc04", 64500, "10.0.0.1")})
    for observer in (counter, hegemony, ioda):
        rib.attach_observer(observer)

    def route(etype, prefix, path, ts):
        return ParsedElement(
            time=ts, type=etype, collector="rrc04", peer_asn=64500,
            peer_address="10.0.0.1",
            fields={"prefix": prefix, "as-path": [str(a) for a in path], "communities": []},
        )

    rib.update_rib(route("R", "192.0.2.0/24", [64500, 64501, 64502], 0.0))
    rib.update_announcement(route("A", "192.0.2.0/24", [64500, 64503, 64502], 1.0))
    rib.update_withdrawal(
        WithdrawalElement(
            time=2.0, type="W", collector="rrc04", peer_asn=64500,
            peer_address="10.0.0.1", fields={"prefix": "198.51.100.0/24"},
        )
    )

    ts = datetime.datetime(2010, 9, 1, 0, 5, tzinfo=datetime.UTC)
    tags = counter.dump(ts)
    assert tags["TRANSIT_CHANGE"] == 1 and tags["DUPLICATE_WITHDRAWAL"] == 1
    graph = hegemony.dump(ts)
    assert graph.get_weight(("64502", "64503")) == 1.0
    assert ioda.dump(ts) == {"64502": 1}


if __name__ == "__main__":
    test_smoke()
    print("smoke test passed")
    sys.exit(0)
