"""GIE transparency platforms: AGSI+ (storage), ALSI (LNG terminals), IIP (inside information / UMMs).

API reference: GIE API documentation v013 (https://www.gie.eu/transparency-platform/GIE_API_documentation_v013.pdf)
  * base URLs https://agsi.gie.eu/api, https://alsi.gie.eu/api, https://iip.gie.eu/api
  * header  x-key: <personal API key>   (env GIE_API_KEY; register at agsi.gie.eu/account)
  * paging  page=1.., size<=300; response has last_page, total, data[]
  * limit   60 calls/minute per IP -> we pace requests at ~1/s
  * publication: daily ~19:30 CET, second run 23:00 CET; values are for the previous gas day
  * credit: "GIE AGSI/ALSI" must be shown wherever the data is used
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Iterator

import pandas as pd

from ..http import make_session
from ..registry import DatasetSpec
from . import SourceNotConfigured, day_utc

log = logging.getLogger(__name__)

BASE = {
    "agsi": "https://agsi.gie.eu/api",
    "alsi": "https://alsi.gie.eu/api",
    "iip": "https://iip.gie.eu/api",
}
PAGE_SIZE = 300
MIN_INTERVAL_S = float(os.getenv("GIE_MIN_INTERVAL_S", "1.1"))  # stay under 60 calls/minute


def api_key() -> str:
    return os.getenv("GIE_API_KEY", "").strip()


def _num(v):
    """GIE sends numbers as strings, '-' or '' for missing."""
    if v is None or (isinstance(v, str) and v.strip() in ("", "-", "n/a")):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _entity_params(entity: str) -> dict:
    """'eu' / 'ne' -> aggregate (type=...), otherwise a 2-letter country code."""
    e = entity.strip().lower()
    return {"type": e} if e in ("eu", "ne", "ai") else {"country": e}


class GieClient:
    def __init__(self, session=None, timeout: float = 60, key: str | None = None, pace: bool = True):
        self.key = key if key is not None else api_key()
        if not self.key:
            raise SourceNotConfigured("GIE_API_KEY is not set (register at https://agsi.gie.eu/account); skipping GIE datasets")
        self.session = session or make_session()
        self.session.headers.update({"x-key": self.key})
        self.timeout = timeout
        self.pace = pace
        self._last_call = 0.0

    # ------------------------------------------------------------------ http
    def _get(self, platform: str, params: dict) -> dict:
        if self.pace:
            wait = float(os.getenv("GIE_MIN_INTERVAL_S", MIN_INTERVAL_S)) - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
        self._last_call = time.monotonic()
        url = BASE[platform]
        r = self.session.get(url, params=params, timeout=self.timeout)
        if r.status_code in (401, 403):
            raise RuntimeError(f"GIE {platform} rejected the API key ({r.status_code}). Check the GIE_API_KEY secret. Body: {r.text[:200]}")
        if r.status_code >= 400:
            raise RuntimeError(f"GIE {platform} {r.url} -> {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"GIE {platform} returned non-JSON ({r.headers.get('content-type')}): {r.text[:300]}")

    def pages(self, platform: str, params: dict) -> Iterator[list[dict]]:
        page, last = 1, 1
        while page <= last:
            body = self._get(platform, {**params, "page": page, "size": PAGE_SIZE})
            if isinstance(body, list):  # defensive: some endpoints return a bare list
                yield body
                return
            last = int(body.get("last_page") or 1)
            yield body.get("data") or []
            page += 1

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def parse_agsi(rows: list[dict], entity: str) -> pd.DataFrame:
        out = []
        for r in rows:
            out.append({
                "gasDay": r.get("gasDayStart"),
                "entity": entity.upper(),
                "name": r.get("name"),
                "gasInStorage": _num(r.get("gasInStorage")),          # TWh
                "workingGasVolume": _num(r.get("workingGasVolume")),  # TWh
                "full": _num(r.get("full")),                          # %
                "injection": _num(r.get("injection")),                # GWh/d
                "withdrawal": _num(r.get("withdrawal")),              # GWh/d
                "netWithdrawal": _num(r.get("netWithdrawal")),        # GWh/d
                "injectionCapacity": _num(r.get("injectionCapacity")),
                "withdrawalCapacity": _num(r.get("withdrawalCapacity")),
                "consumption": _num(r.get("consumption")),            # TWh (countries)
                "trend": _num(r.get("trend")),
                "status": r.get("status"),
                "updatedAt": r.get("updatedAt"),
            })
        df = pd.DataFrame(out)
        if not df.empty:
            df["gasDay"] = day_utc(df["gasDay"])
        return df

    @staticmethod
    def parse_alsi(rows: list[dict], entity: str) -> pd.DataFrame:
        out = []
        for r in rows:
            inv = r.get("inventory") or {}
            dtmi = r.get("dtmi") or {}
            out.append({
                "gasDay": r.get("gasDayStart"),
                "entity": entity.upper(),
                "name": r.get("name"),
                "inventory_gwh": _num(inv.get("gwh") if isinstance(inv, dict) else inv),
                "inventory_lng": _num(inv.get("lng") if isinstance(inv, dict) else None),   # 10^3 m3 LNG
                "dtmi_gwh": _num(dtmi.get("gwh") if isinstance(dtmi, dict) else dtmi),
                "dtmi_lng": _num(dtmi.get("lng") if isinstance(dtmi, dict) else None),
                "sendOut": _num(r.get("sendOut")),   # GWh/d
                "dtrs": _num(r.get("dtrs")),         # declared total reference send-out, GWh/d
                "status": r.get("status"),
                "updatedAt": r.get("updatedAt"),
            })
        df = pd.DataFrame(out)
        if not df.empty:
            df["gasDay"] = day_utc(df["gasDay"])
        return df

    @staticmethod
    def parse_iip(rows: list[dict]) -> pd.DataFrame:
        def g(d, *path):
            for p in path:
                d = d.get(p) if isinstance(d, dict) else None
            return d
        out = []
        for r in rows:
            msg = r.get("message") or {}
            out.append({
                "published": r.get("published"),
                "messageId": g(msg, "messageId") or r.get("id"),
                "messageType": g(msg, "messageType"),
                "unavailabilityType": g(msg, "unavailabilityType"),
                "eventStatus": r.get("status"),
                "eventStart": r.get("from"),
                "eventStop": r.get("to"),
                "direction": r.get("direction"),
                "reason": r.get("unavailabilityReason"),
                "remarks": r.get("remarks"),
                "reportingEntity": g(r, "reportingEntity", "name") or r.get("reportingEntityString"),
                "reportingEntityType": g(r, "reportingEntity", "type"),
                "asset": g(r, "asset", "name") or r.get("assetString"),
                "balancingZone": g(r, "balancingZone", "name") or r.get("balancingZoneString"),
                "unavailableCapacity": _num(g(r, "unavailable", "capacity")),
                "availableCapacity": _num(g(r, "available", "capacity")),
                "technicalCapacity": _num(g(r, "technical", "capacity")),
                "unit": g(r, "unavailable", "unit") or g(r, "technical", "unit"),
            })
        return pd.DataFrame(out)

    # ------------------------------------------------------------------ fetch
    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        platform = spec.options.get("platform")
        if platform in ("agsi", "alsi"):
            parse = self.parse_agsi if platform == "agsi" else self.parse_alsi
            d_from = pd.Timestamp(start).date().isoformat()
            d_to = (pd.Timestamp(end) - pd.Timedelta(seconds=1)).date().isoformat()
            for entity in spec.options.get("entities", ["eu"]):
                params = {**_entity_params(entity), "from": d_from, "to": d_to}
                frames = [parse(rows, entity) for rows in self.pages(platform, params)]
                df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                log.info("%s %s %s..%s: %d rows", spec.name, entity, d_from, d_to, len(df))
                if not df.empty:
                    yield df
        elif platform == "iip":
            # Walk publication months (the `published` filter accepts YYYY-MM).
            months = pd.period_range(pd.Timestamp(start).tz_localize(None).to_period("M"),
                                     pd.Timestamp(end).tz_localize(None).to_period("M"), freq="M")
            for m in months:
                frames = [self.parse_iip(rows) for rows in self.pages("iip", {"published": str(m)})]
                df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                log.info("%s published %s: %d UMMs", spec.name, m, len(df))
                if not df.empty:
                    yield df
        else:
            raise ValueError(f"{spec.name}: options.platform must be agsi, alsi or iip")
