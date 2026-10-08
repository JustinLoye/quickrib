"""Shared fixtures for the QuickRIB test suite.

The suite has two tiers (see AGENTS.md "Testing policy"):

* **standalone**: no marker, driven by hand-built fake ``BGPElement`` / ``data``
  dicts. Fast, offline, deterministic; run by default.
* **end-to-end**: marked ``e2e``, replays a small, fixed window of real BGP
  archives through the full :class:`QuickRIB` pipeline and asserts on the
  result. Slow and network-bound, so deselected by default. Run with
  ``uv run pytest -m e2e`` (only e2e) or ``uv run pytest -m ""`` (everything).

Every e2e test in the suite shares one pinned window, the same two-hour,
two-collector window ``test_quickrib.py`` asserts exact counts on, so the MRT
archives are downloaded once and reused from ``cache/`` on later runs.

``test_radix.py`` is exempt from the two-tier policy: it covers
:mod:`quickrib.radix_utils` in isolation (prefix-tree measurement, no BGP and
no pipeline) for every observer that builds on it.
"""

import datetime
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

import pytest
import requests_cache
from pybgpflux import BGPElement

from quickrib import QuickRIB, QuickRIBConfig
from quickrib.rib_table import RIBTable

# Pinned e2e window. Keep in sync with the counts asserted in the e2e tests.
#
# `start_time` is 00:00 because that is where route-views.wide dumps, and rrc04
# dumped a minute earlier at 23:59. The reconstruction therefore starts from
# those two dumps with almost no catch-up. Asking for an earlier instant is
# legitimate but expensive: the last dump before 23:58 is rrc04's at 15:59, so
# the run would replay eight hours of updates before reaching the window.
E2E_START = datetime.datetime(2010, 9, 1, 0, 0, tzinfo=datetime.UTC)
E2E_END = datetime.datetime(2010, 9, 1, 1, 59, tzinfo=datetime.UTC)
E2E_COLLECTORS = ["route-views.wide", "rrc04"]
E2E_DUMP_RES = datetime.timedelta(minutes=5)
E2E_PARSER = "bgpkit"

# Second pinned window, for the e2e tests that check QuickRIB against a live
# RIPE API. It cannot share the 2010 window above: RIPEstat only indexes BGP
# data from January 2024 onwards, and its RIS-only endpoints know nothing about
# route-views. It starts exactly on an rrc04 RIB dump (RIS dumps at 00:00, 08:00
# and 16:00) so the reconstructed table at ``start_time`` is that dump, which is
# also what the API reports as the state at that instant.
RIPE_START = datetime.datetime(2025, 3, 3, 0, 0, tzinfo=datetime.UTC)
RIPE_END = datetime.datetime(2025, 3, 3, 2, 0, tzinfo=datetime.UTC)
RIPE_COLLECTORS = ["rrc04"]

# Third pinned window, for the e2e tests compared against the IODA API. IODA's
# BGP series only goes back to 2022-01-19, so this is as far back as the archives
# can be pushed to keep the replay cheap. The window brackets a clean, isolated
# BGP outage: AS43160 (ES-MDC-DATACENTER) loses roughly a quarter of its
# visible /24s at 04:20 UTC and recovers at 05:45. It starts exactly on a
# route-views RIB dump (those land on even hours, where RIS only dumps at
# 00:00/08:00/16:00 and so cannot bracket an outage this precisely).
IODA_START = datetime.datetime(2022, 1, 26, 4, 0, tzinfo=datetime.UTC)
IODA_END = datetime.datetime(2022, 1, 26, 6, 30, tzinfo=datetime.UTC)
IODA_COLLECTORS = ["route-views.eqix"]
IODA_DUMP_RES = datetime.timedelta(minutes=5)


@pytest.fixture(scope="session")
def e2e_config() -> QuickRIBConfig:
    """The pinned :class:`QuickRIBConfig` shared by every e2e test."""
    return QuickRIBConfig(
        start_time=E2E_START,
        end_time=E2E_END,
        collectors=list(E2E_COLLECTORS),
        dump_res=E2E_DUMP_RES,
        cache_dir=Path("cache"),
        parser=E2E_PARSER,
    )


@pytest.fixture(scope="session")
def ripe_config() -> QuickRIBConfig:
    """Pinned config for e2e tests compared against a live RIPEstat data call.

    ``fullfeed_only`` is off because the RIPE APIs report every RIS peer, partial
    feeds included; filtering to full-feed peers would drop rows the API has.
    """
    return QuickRIBConfig(
        start_time=RIPE_START,
        end_time=RIPE_END,
        collectors=list(RIPE_COLLECTORS),
        dump_res=E2E_DUMP_RES,
        cache_dir=Path("cache"),
        parser=E2E_PARSER,
        fullfeed_only=False,
    )


@pytest.fixture(scope="session")
def ioda_config() -> QuickRIBConfig:
    """Pinned config for the e2e tests compared against the live IODA API.

    ``dump_res`` is IODA's native 5-minute step so the dumps land on its bins.
    ``fullfeed_only`` is left on: the observer's visibility quorum is defined
    over QuickRIB's own full-feed selection, so the peers it would ignore need
    never enter the RIB at all, which is most of them and most of the cost.
    """
    return QuickRIBConfig(
        start_time=IODA_START,
        end_time=IODA_END,
        collectors=list(IODA_COLLECTORS),
        dump_res=IODA_DUMP_RES,
        cache_dir=Path("cache"),
        parser=E2E_PARSER,
    )


@pytest.fixture(scope="session", autouse=True)
def http_cache():
    """Cache the broker/archive HTTP traffic for the whole test session.

    ``install_cache`` patches ``requests`` process-wide, so it is autouse and
    session-scoped rather than a side effect of whichever fixture happened to run
    first. It lands in ``cache/`` beside the MRT archives it belongs with, not in
    the working directory.

    It does **not** cache the live-API comparisons: ``test_bgplay`` and
    ``test_ioda`` reach RIPEstat and IODA through ``urllib``, which
    ``requests_cache`` does not intercept. Those stay live, which is the point
    of them.
    """
    cache_dir = Path("cache")
    cache_dir.mkdir(exist_ok=True)
    requests_cache.install_cache(str(cache_dir / "http_cache"))
    yield
    requests_cache.uninstall_cache()


@pytest.fixture(scope="session")
def make_quickrib():
    """Return a factory that builds a :class:`QuickRIB` for a given config.

    ``rib_cls`` is passed here rather than through the config so an e2e test can
    pick a RIB flavor such as ``RIBTablePathHistory`` without a config per
    flavor. ``QuickRIB.from_config`` forwards ``config.rib_cls`` too.
    """

    def _make(config: QuickRIBConfig, *, rib_cls: type[RIBTable] = RIBTable) -> QuickRIB:
        return QuickRIB(
            start_time=config.start_time,
            end_time=config.end_time,
            dump_res=config.dump_res,
            collectors=list(config.collectors),
            filters=config.filters,
            cache_dir=config.cache_dir,
            parser=config.parser,
            fullfeed_only=config.fullfeed_only,
            rib_cls=rib_cls,
        )

    return _make


def parser_element(
    etype: Literal["R", "A", "W"],
    prefix: str,
    as_path: Sequence[str] = (),
    *,
    collector: str = "rrc04",
    peer_asn: int = 64500,
    peer_ip: str = "10.0.0.1",
    communities: Sequence[str] = (),
    ts: float = 1000.0,
) -> Any:
    """A raw ``pybgpflux.BGPElement``, shaped the way a parser really builds one.

    The standalone tests otherwise construct the narrowed ``quickrib.elements``
    types, which is what the *static* contract says an observer receives. The
    pipeline hands it something else: a different class, whose withdrawal
    ``fields`` carry the prefix alone: no ``as-path``, no ``communities``
    (check ``pybgpflux.parsers.bgpkit`` and ``.bgpdump``). An observer that
    discriminates on the Python class, or reads a path off a withdrawal, passes
    every hand-built test and fails on the first real message.

    Use this wherever an observer's behaviour could depend on either. The return
    type is deliberately ``Any``: the mismatch is the point, and the pipeline
    papers over the same seam with two documented suppressions.
    """
    fields: dict[str, object]
    if etype == "W":
        fields = {"prefix": prefix}
    else:
        # `as-path` is already a list of ASNs: `process_path` rewrites it in
        # place before the element reaches an observer.
        fields = {
            "prefix": prefix,
            "as-path": [str(asn) for asn in as_path],
            "communities": list(communities),
        }
    # The cast is the seam itself: these `fields` are what a parser produces,
    # which is deliberately not what the narrowed `ElementFields` describes.
    return BGPElement(
        time=ts, type=etype, collector=collector, peer_asn=peer_asn,
        peer_address=peer_ip, fields=cast(Any, fields),
    )
