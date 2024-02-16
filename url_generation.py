"""
Generate urls for RouteViews and RIS projects archive download
"""

from datetime import datetime
import os

RV_UPDATE_RES = 15 * 60
RIS_UPDATE_RES = 5 * 60

def RIS_url(rc: str, ts: datetime, bgptype: str):
    if bgptype == "rib":
        bgptype_in_url = "bview"
    elif bgptype == "update":
        bgptype_in_url = "updates"

    return os.path.join("https://data.ris.ripe.net/", rc, ts.strftime("%Y.%m"), f"{bgptype_in_url}.{ts.strftime('%Y%m%d.%H%M')}.gz")

def RV_url(rc: str, ts: datetime, bgptype: str):
    if bgptype == "rib":
        bgptype_in_url = "RIBS"
    elif bgptype == "update":
        bgptype_in_url = "UPDATES"
        bgptype = "updates"

    if rc != "route-views2":
        return os.path.join("https://routeviews.org/", rc, "bgpdata", ts.strftime("%Y.%m"), bgptype_in_url, f"{bgptype}.{ts.strftime('%Y%m%d.%H%M')}.bz2")
    else:
        return os.path.join("https://routeviews.org/", "bgpdata", ts.strftime("%Y.%m"), bgptype_in_url, f"{bgptype}.{ts.strftime('%Y%m%d.%H%M')}.bz2")

def get_url(rc: str, ts: datetime, bgptype: str):
    if rc[:3] == "rrc":
        return RIS_url(rc, ts, bgptype)
    elif "route-views" in rc:
        return RV_url(rc, ts, bgptype)
    raise ValueError("rc value not recognized")

def url_to_filename(url: str):
    "get a filename from a RIS or RV url"

    # Get RC, handling the edge case of route-views2
    if "routeviews.org" in url and "route-views" not in url:
        rc = "route-views2"
    else:
        rc = url.strip().split('/')[3]
    filename = url.split('/')[-1]
    time_str = ".".join(filename.split('.')[1:3])

    type_str = filename.split('.')[0]
    if type_str == "bview":
        type_str = "rib"
    if type_str == "updates":
        type_str = "update"

    return f'{rc}.{type_str}.{time_str}{os.path.splitext(url)[1]}'

def filename_to_rc(filename: str) -> str:
    res = filename.split('.')
    if res[0] == "route-views":
        return f"{res[0]}.{res[1]}"
    else:
        return res[0]