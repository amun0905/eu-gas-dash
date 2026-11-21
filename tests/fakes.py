"""Fake upstream APIs for offline tests and the local demo (scripts/demo_data.py).

Each fake answers the way the real API does (paging, wrappers, string numbers), with
deterministic synthetic values, so the whole pipeline can run without network access.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pandas as pd
import responses
import yaml

from gasdash.sources import entsog, gie, nationalgas, openmeteo
from gasdash.sources.entsog import find_points_file

TODAY = datetime.now(timezone.utc).date()


def _q(request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(request.url).query).items()}


def _days(a: str, b: str, ahead: int = -1):
    d0, d1 = date.fromisoformat(a), min(date.fromisoformat(b), TODAY + timedelta(days=ahead))
    while d0 <= d1:
        yield d0
        d0 += timedelta(days=1)


def _season(d: date, lo=20.0, hi=95.0, phase=0.0) -> float:
    """Storage-like curve: low in March/April, high in October/November."""
    x = (d.timetuple().tm_yday - 100) / 365 * 2 * math.pi
    return lo + (hi - lo) * (0.5 - 0.5 * math.cos(x + phase)) + 3 * math.sin(d.toordinal() / 9)


# ---------------------------------------------------------------- GIE
def gie_callback(platform):
    def cb(request):
        q = _q(request)
        if request.headers.get("x-key") != "test-key":
            return 401, {}, json.dumps({"error": "invalid key"})
        size, page = int(q.get("size", 30)), int(q.get("page", 1))
        if platform == "iip":
            m = q.get("published", "2026-01")
            rows = [{"published": f"{m}-0{i}T08:00:00Z", "status": "active",
                     "from": f"{m}-0{i}T06:00:00Z", "to": f"{m}-2{i}T06:00:00Z", "direction": "withdrawal",
                     "unavailabilityReason": "Maintenance", "remarks": "Planned works",
                     "reportingEntity": {"name": f"Storage Co {i}", "code": "21X", "type": "SSO"},
                     "message": {"messageId": f"IIP-{m}-{i}", "messageType": "1", "unavailabilityType": "Planned"},
                     "asset": {"name": f"UGS {i}", "code": "21W"}, "balancingZone": {"name": "THE", "code": "THE"},
                     "unavailable": {"capacity": 50.0 * i, "unit": "GWh/d"}} for i in range(1, 4)]
            rows += [{"published": f"{m}-15T08:00:00Z", "status": "inactive", "from": f"{m}-01T06:00:00Z",
                      "to": f"{m}-02T06:00:00Z", "message": {"messageId": f"IIP-{m}-old-{k}", "unavailabilityType": "Planned"},
                      "unavailable": {"capacity": 1000.0, "unit": "kWh/h"}} for k in range(117)]
            size = min(size, 50)
        else:
            ent = (q.get("country") or q.get("type") or "eu").upper()
            days = list(_days(q["from"], q["to"]))
            rows = []
            for d in reversed(days):  # GIE returns newest first
                if platform == "agsi":
                    full = _season(d, phase=hash(ent) % 7 / 10)
                    wgv = 1100.0 if ent == "EU" else 50.0
                    rows.append({"name": ent, "code": ent.lower(), "url": ent.lower(), "updatedAt": f"{d} 18:31:05",
                                 "gasDayStart": d.isoformat(), "gasInStorage": f"{wgv * full / 100:.4f}",
                                 "consumption": "-", "consumptionFull": "-", "injection": f"{max(0, 40 * math.sin(d.toordinal() / 30)):.2f}",
                                 "withdrawal": f"{max(0, -40 * math.sin(d.toordinal() / 30)):.2f}", "netWithdrawal": "0",
                                 "workingGasVolume": f"{wgv}", "injectionCapacity": "500", "withdrawalCapacity": "900",
                                 "status": "C" if d < TODAY - timedelta(days=3) else "E", "trend": "0.1", "full": f"{full:.2f}",
                                 "info": [], "children": []})
                else:
                    so = 3000 + 800 * math.sin(d.toordinal() / 20) if ent == "EU" else 200 + 50 * math.sin(d.toordinal() / 15)
                    rows.append({"name": ent, "code": ent.lower(), "url": ent.lower(), "updatedAt": f"{d} 18:31:05",
                                 "gasDayStart": d.isoformat(), "inventory": {"lng": "1500.5", "gwh": f"{so * 3:.1f}"},
                                 "sendOut": f"{so:.1f}", "dtmi": {"lng": "3000", "gwh": f"{so * 6:.1f}"},
                                 "dtrs": f"{so * 1.6:.1f}", "status": "C", "info": [], "children": []})
        last = 1 if platform == "iip" else max(1, math.ceil(len(rows) / size))
        body = {"last_page": last, "total": len(rows), "dataset": platform, "gas_day": str(TODAY),
                "data": rows[(page - 1) * size: page * size]}
        return 200, {"Content-Type": "application/json"}, json.dumps(body)
    return cb


# ---------------------------------------------------------------- ENTSOG
def opd_rows() -> list[dict]:
    """A reference list that contains every key in the config (and two LNG terminals for the match rule)."""
    cfg = yaml.safe_load(find_points_file().read_text())
    rows = []
    for s in cfg["series"]:
        for k in s.get("keys", []):
            key = k["key"] if isinstance(k, dict) else k
            m = re.match(r"^([A-Z]{2}-TSO-\d{4})([A-Z]{3}-\d{5})(entry|exit)$", key)
            if not m:
                continue
            rows.append({"operatorKey": m[1], "pointKey": m[2], "directionKey": m[3], "pointLabel": s["label"],
                         "operatorLabel": m[1], "tSOCountry": m[1][:2], "pointType": "Cross-Border Transmission IP",
                         "connectedOperators": "", "isInvalid": False})
    # Gate is reported by the terminal (LSO) and the TSO; Spain's TVB is a virtual point; Isle of Grain is "UK"
    for op, pk, lab in (("NL-TSO-0001", "LNG-00027", "Gate Terminal (I)"), ("NL-LSO-0001", "LNG-00027", "Gate Terminal (I)"),
                        ("PL-TSO-0002", "LNG-00006", "Swinoujscie"), ("ES-TSO-0006", "VTP-00043", "TVB"),
                        ("IT-TSO-0001", "LNG-00015", "Cavarzere"), ("IT-TSO-0004", "LNG-00015", "Cavarzere"),
                        ("UK-TSO-0001", "LNG-00008", "Isle of Grain")):
        rows.append({"operatorKey": op, "pointKey": pk, "directionKey": "entry", "pointLabel": lab, "operatorLabel": op,
                     "tSOCountry": op[:2], "pointType": "LNG Entry point", "connectedOperators": "", "isInvalid": False})
    return rows


def entsog_callback(request):
    path = urlparse(request.url).path.rsplit("/", 1)[-1]
    q = _q(request)
    if path == "operatorPointDirections":
        return 200, {}, json.dumps({"meta": {"count": 1}, "operatorpointdirections": opd_rows()})
    if path == "operationalDatas":
        keys = q["pointDirection"].split(",")
        rows = []
        for d in _days(q["from"], q["to"], ahead=1):
            for i, key in enumerate(keys):
                m = re.match(r"^([A-Z]{2}-TSO-\d{4})([A-Z]{3}-\d{5})(entry|exit)$", key)
                start = pd.Timestamp(d, tz="Europe/Brussels") + pd.Timedelta(hours=6)
                for ind in q["indicator"].split(","):
                    if ind == "Physical Flow" and d >= TODAY - timedelta(days=1):
                        continue  # physical flow is published D+1
                    rows.append({"indicator": ind, "periodType": "day",
                                 "periodFrom": start.tz_convert("UTC").isoformat(),
                                 "periodTo": (start + pd.Timedelta(days=1)).tz_convert("UTC").isoformat(),
                                 "operatorKey": m[1], "operatorLabel": m[1], "pointKey": m[2], "pointLabel": key,
                                 "directionKey": m[3], "unit": "kWh/d",
                                 "value": 1e6 * (50 + 10 * i + 5 * math.sin(d.toordinal() / 5)) * (0.5 if m[3] == "exit" else 1),
                                 "lastUpdateDateTime": f"{d}T12:00:00+01:00", "flowStatus": "Confirmed"})
        if not rows:
            return 404, {}, json.dumps({"message": "No Data Available"})
        return 200, {}, json.dumps({"meta": {"count": len(rows)}, "operationaldatas": rows})
    if path == "urgentMarketMessages":
        rows = [{"messageId": f"UMM-{i}", "versionNumber": v, "isLatestVersion": v == 2, "marketParticipantName": "Gasunie",
                 "messageType": "UnavailabilitiesOfGasFacilities", "publicationDateTime": f"{TODAY - timedelta(days=3)}T09:0{v}:00+01:00",
                 "eventStatus": "Active", "eventType": "Transmission unavailability",
                 "eventStart": f"{TODAY}T06:00:00+01:00", "eventStop": f"{TODAY + timedelta(days=10 * i)}T06:00:00+01:00",
                 "unavailabilityType": "Planned", "unavailabilityReason": "Maintenance", "unitMeasure": "kWh/d",
                 "balancingZoneName": "TTF", "affectedAssetName": f"Point {i}", "direction": "Entry",
                 "unavailableCapacity": 1e8 * i * v, "remarks": "Works", "lastUpdateDateTime": f"{q['from']}T10:00:00+01:00"}
                for i in (1, 2) for v in (1, 2)]
        return 200, {}, json.dumps({"meta": {}, "urgentmarketmessages": rows})
    if path == "cmpAuctions":
        rows = [{"auctionFrom": f"{q['from']}T08:00:00+01:00", "auctionTo": f"{q['from']}T08:30:00+01:00",
                 "capacityFrom": f"{q['from']}T06:00:00+01:00", "capacityTo": f"{q['to']}T06:00:00+01:00",
                 "operatorKey": "DE-TSO-0009", "operatorLabel": "Open Grid Europe", "pointKey": "ITP-00538",
                 "pointLabel": "VIP Waidhaus", "directionKey": "exit", "unit": "EUR/kWh/h/d",
                 "auctionPremium": 0.0123, "clearedPrice": 0.05, "reservePrice": 0.0377,
                 "bookingPlatformLabel": "PRISMA", "lastUpdateDateTime": f"{q['from']}T09:00:00+01:00"}]
        return 200, {}, json.dumps({"meta": {}, "cmpauctions": rows})
    return 404, {}, json.dumps({"message": "No Data Available"})


# ---------------------------------------------------------------- National Gas
def nationalgas_callback(request):
    if "Mozilla" not in request.headers.get("User-Agent", ""):
        return 403, {}, "Forbidden"
    body = json.loads(request.body)
    base = {"PUBOBJ635": 300, "PUBOBJ627": 180, "PUBOBJ570": 70, "PUBOBJ629": 60, "PUBOBJ630": 20, "PUBOBJ628": 5,
            "PUBOB637": 290, "PUBOB2535": 270, "PUBOBJ330": 1.0e10}
    out = []
    for pid in body["publicationIds"]:
        pubs = [{"applicableFor": d.isoformat(), "value": str(base[pid] * (1 + 0.1 * math.sin(d.toordinal() / 7)))}
                for d in _days(body["fromDate"], body["toDate"])]
        out.append({"publicationId": pid, "publications": pubs})
    return 200, {}, json.dumps(out)


# ---------------------------------------------------------------- Open-Meteo
def openmeteo_callback(request):
    q = _q(request)
    n = len(q["latitude"].split(","))
    if "start_date" in q:
        days = list(_days(q["start_date"], q["end_date"]))
    else:
        days = [TODAY + timedelta(days=k) for k in range(-int(q["past_days"]), int(q["forecast_days"]))]
    bodies = [{"daily": {"time": [d.isoformat() for d in days],
                         "temperature_2m_mean": [8 - 10 * math.cos((d.timetuple().tm_yday - 15) / 365 * 2 * math.pi) + j
                                                 for d in days]}} for j in range(n)]
    return 200, {}, json.dumps(bodies if n > 1 else bodies[0])


def install(rsps: responses.RequestsMock) -> None:
    for platform, url in gie.BASE.items():
        rsps.add_callback(responses.GET, url, callback=gie_callback(platform))
    rsps.add_callback(responses.GET, re.compile(re.escape(entsog.BASE_URL) + r"/.*"), callback=entsog_callback)
    rsps.add_callback(responses.POST, nationalgas.JSON_URL, callback=nationalgas_callback)
    rsps.add_callback(responses.GET, openmeteo.ARCHIVE_URL, callback=openmeteo_callback)
    rsps.add_callback(responses.GET, openmeteo.FORECAST_URL, callback=openmeteo_callback)
