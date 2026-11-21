"""National Gas Transmission (GB NTS) operational data.

Primary: the JSON publications API used by the National Gas Data Portal
    POST https://api.nationalgas.com/operationaldata/v1/publications/gasday
    body {"publicationIds": [...], "fromDate": "YYYY-MM-DD", "toDate": "YYYY-MM-DD"}
    -> [{"publicationId": "PUBOBJ570", "publications": [{"applicableFor": "2026-11-09", "value": "73.8", ...}]}]
It answers 403 to non-browser User-Agents, so we send a browser-like one.

Fallback: the documented CSV download (one data item per call)
    GET https://data.nationalgas.com/api/find-gas-data-download?applicableFor=Y&dateFrom=...&dateTo=...
        &dateType=GASDAY&latestFlag=Y&ids=PUBOBJ570&type=CSV
Publication IDs (PUBOB...) are listed on the portal's "Find gas data" page; the ones we use are in
config/datasets.yaml with their names and units.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Iterator

import pandas as pd

from ..http import make_session
from ..registry import DatasetSpec
from . import daily_windows, day_utc

log = logging.getLogger(__name__)

JSON_URL = "https://api.nationalgas.com/operationaldata/v1/publications/gasday"
CSV_URL = "https://data.nationalgas.com/api/find-gas-data-download"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) eu-gas-dash/0.1"


class NationalGasClient:
    def __init__(self, session=None, timeout: float = 60):
        self.session = session or make_session()
        self.session.headers.update({"User-Agent": BROWSER_UA, "Accept": "application/json"})
        self.timeout = timeout
        self._json_ok = True

    @staticmethod
    def parse_json(body, items: dict) -> pd.DataFrame:
        rows = []
        for item in body or []:
            pid = item.get("publicationId")
            if pid not in items:
                continue
            for p in item.get("publications") or []:
                v = p.get("value")
                if v in (None, ""):
                    continue
                rows.append({"gasDay": p.get("applicableFor"), "item": pid, "name": items[pid]["name"],
                             "value": pd.to_numeric(v, errors="coerce"), "unit": items[pid].get("unit"),
                             "generatedAt": p.get("generatedTimeStamp") or p.get("generatedTime") or p.get("createdDate")})
        df = pd.DataFrame(rows)
        if not df.empty:
            df["gasDay"] = day_utc(df["gasDay"])
        return df

    def _json(self, ids: list[str], d_from, d_to, items) -> pd.DataFrame:
        body = {"publicationIds": ids, "fromDate": d_from.isoformat(), "toDate": d_to.isoformat()}
        r = self.session.post(JSON_URL, json=body, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
        return self.parse_json(r.json(), items)

    def _csv(self, ids: list[str], d_from, d_to, items) -> pd.DataFrame:
        frames = []
        for pid in ids:
            params = {"applicableFor": "Y", "dateFrom": f"{d_from.isoformat()}T00:00:00",
                      "dateTo": f"{d_to.isoformat()}T23:59:59", "dateType": "GASDAY", "latestFlag": "Y",
                      "ids": pid, "type": "CSV"}
            r = self.session.get(CSV_URL, params=params, timeout=self.timeout)
            if r.status_code >= 400:
                raise RuntimeError(f"National Gas CSV {pid} -> {r.status_code}: {r.text[:200]}")
            raw = pd.read_csv(io.BytesIO(r.content))
            raw.columns = [c.strip() for c in raw.columns]
            if raw.empty:
                continue
            frames.append(pd.DataFrame({
                "gasDay": day_utc(pd.to_datetime(raw["Applicable For"], dayfirst=True, errors="coerce").dt.strftime("%Y-%m-%d")),
                "item": pid, "name": items[pid]["name"], "value": pd.to_numeric(raw["Value"], errors="coerce"),
                "unit": items[pid].get("unit"), "generatedAt": raw.get("Generated Time"),
            }))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        items: dict = spec.options["items"]
        ids = list(items)
        for d_from, d_to in daily_windows(start, end, spec.chunk_days):
            df = pd.DataFrame()
            if self._json_ok:
                try:
                    df = self._json(ids, d_from, d_to, items)
                except Exception as e:  # fall back to the documented CSV route for the rest of the run
                    log.warning("National Gas JSON API failed (%s); falling back to CSV download", e)
                    self._json_ok = False
            if not self._json_ok:
                df = self._csv(ids, d_from, d_to, items)
            log.info("%s %s..%s: %d rows", spec.name, d_from, d_to, len(df))
            if not df.empty:
                yield df
