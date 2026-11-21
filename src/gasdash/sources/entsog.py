"""ENTSOG Transparency Platform client (free, no key).

API manual v3.0: https://transparency.entsog.eu/api/archiveDirectories/8/api-manual/ENTSOG_TP_API_UserManual_v3.0.pdf
  * base https://transparency.entsog.eu/api/v1/<endpoint>
  * every query is killed after 60 s, so ask for small windows / few points per call
  * limit defaults to 100: always send limit=-1
  * a point-direction key is operatorKey + pointKey + directionKey, e.g. DE-TSO-0005ITP-00081entry
  * "no data" comes back as 404 (sometimes 500) with a JSON {"message": ...}; treated as empty

Which points we follow is configured in config/entsog_points.yaml. Keys there are checked against
/operatorPointDirections on every run (`gasdash points` prints the check), and `match:` rules pick
up points by label/type so new LNG terminals appear without code changes.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pandas as pd
import yaml

from ..http import make_session
from ..registry import DatasetSpec
from . import daily_windows, gas_day_from_period_start

log = logging.getLogger(__name__)

BASE_URL = "https://transparency.entsog.eu/api/v1"
POINTS_RELPATH = Path("config") / "entsog_points.yaml"
KEY_BATCH = 20  # point-directions per request (keeps queries well under the 60 s limit)


class EntsogNoData(Exception):
    pass


def find_points_file() -> Path:
    for c in (Path.cwd() / POINTS_RELPATH, Path(__file__).resolve().parents[3] / POINTS_RELPATH):
        if c.is_file():
            return c
    raise FileNotFoundError(f"config/entsog_points.yaml not found (looked in cwd and repo root)")


def load_points(path: str | Path | None = None) -> dict:
    return yaml.safe_load(Path(path or find_points_file()).read_text())


def pd_key(operator_key: str, point_key: str, direction: str) -> str:
    return f"{operator_key}{point_key}{direction.lower()}"


def _records(body: dict) -> list[dict]:
    """ENTSOG wraps data as {"meta": {...}, "<endpointName>": [...]}; "message" means no data."""
    if not isinstance(body, dict):
        return body if isinstance(body, list) else []
    if "message" in body and len(body) <= 2 and not any(isinstance(v, list) for v in body.values()):
        return []
    for k, v in body.items():
        if k != "meta" and isinstance(v, list):
            return v
    return []


class EntsogClient:
    def __init__(self, session=None, timeout: float = 90):
        self.session = session or make_session(total_retries=4, backoff=3)
        self.timeout = timeout
        self._opd: pd.DataFrame | None = None

    # ------------------------------------------------------------------ http
    def get(self, endpoint: str, params: dict) -> list[dict]:
        q = {"limit": -1, "timeZone": "UCT", **params}
        r = self.session.get(f"{BASE_URL}/{endpoint}", params=q, timeout=self.timeout)
        if r.status_code == 404 or (r.status_code == 500 and "no data" in r.text.lower()):
            return []
        if r.status_code >= 400:
            raise RuntimeError(f"ENTSOG {r.url} -> {r.status_code}: {r.text[:300]}")
        try:
            return _records(r.json())
        except ValueError:
            raise RuntimeError(f"ENTSOG {r.url} returned non-JSON: {r.text[:300]}")

    # ------------------------------------------------------------------ reference data
    def operator_point_directions(self) -> pd.DataFrame:
        if self._opd is None:
            rows = self.get("operatorPointDirections", {})
            df = pd.DataFrame(rows)
            if df.empty:
                raise RuntimeError("ENTSOG /operatorPointDirections returned nothing; cannot resolve points")
            df["key"] = [pd_key(o, p, d) for o, p, d in zip(df["operatorKey"], df["pointKey"], df["directionKey"])]
            log.info("ENTSOG reference: %d operator point directions", len(df))
            self._opd = df
        return self._opd

    def resolve(self, points_cfg: dict) -> pd.DataFrame:
        """Expand config/entsog_points.yaml into one row per (series, point-direction key).

        Columns: series, group, label, key, sign, pointLabel, operatorLabel, country, status
        status = ok | unknown_key (explicit key not in ENTSOG's reference list) | matched"""
        opd = self.operator_point_directions()
        by_key = opd.drop_duplicates("key").set_index("key")
        rows = []
        for s in points_cfg.get("series", []):
            base = {"series": s["id"], "group": s["group"], "label": s["label"]}
            for spec in s.get("keys", []):
                key, sign = (spec["key"], spec.get("sign", 1)) if isinstance(spec, dict) else (spec, 1)
                hit = by_key.loc[key] if key in by_key.index else None
                rows.append({**base, "key": key, "sign": sign,
                             "pointLabel": None if hit is None else hit.get("pointLabel"),
                             "operatorLabel": None if hit is None else hit.get("operatorLabel"),
                             "country": None if hit is None else hit.get("tSOCountry"),
                             "status": "ok" if hit is not None else "unknown_key"})
            if "match" in s:
                m = s["match"]
                sel = opd
                for col, field in (("point_label", "pointLabel"), ("point_type", "pointType"),
                                   ("operator", "operatorLabel"), ("connected", "connectedOperators")):
                    if col in m:
                        sel = sel[sel[field].fillna("").str.contains(m[col], flags=re.I, regex=True)]
                if "direction" in m:
                    sel = sel[sel["directionKey"].str.lower() == m["direction"].lower()]
                if "country" in m:
                    sel = sel[sel["tSOCountry"].fillna("").str.upper().isin([c.upper() for c in _as_list(m["country"])])]
                if "exclude_label" in m:
                    sel = sel[~sel["pointLabel"].fillna("").str.contains(m["exclude_label"], flags=re.I, regex=True)]
                if "isInvalid" in sel:
                    sel = sel[~sel["isInvalid"].fillna(False).astype(bool)]
                sel = sel.drop_duplicates("key")
                for _, hit in sel.iterrows():
                    rows.append({**base, "key": hit["key"], "sign": m.get("sign", 1),
                                 "pointLabel": hit.get("pointLabel"), "operatorLabel": hit.get("operatorLabel"),
                                 "country": hit.get("tSOCountry"), "status": "matched"})
                if sel.empty:
                    log.warning("ENTSOG series %s: match rule %s found no points", s["id"], m)
        out = pd.DataFrame(rows, columns=["series", "group", "label", "key", "sign", "pointLabel",
                                          "operatorLabel", "country", "status"])
        bad = out[out.status == "unknown_key"]
        for _, r in bad.iterrows():
            log.warning("ENTSOG series %s: key %s is not in ENTSOG's point list (renamed or retired?)", r.series, r.key)
        return out

    # ------------------------------------------------------------------ fetch
    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        kind = spec.options.get("kind")
        if kind == "flows":
            yield from self._fetch_flows(spec, start, end)
        elif kind in ("umm", "cmp_auctions"):
            yield from self._fetch_simple(spec, start, end)
        else:
            raise ValueError(f"{spec.name}: options.kind must be flows, umm or cmp_auctions")

    def _fetch_flows(self, spec, start, end):
        resolved = self.resolve(load_points(spec.options.get("points_file")))
        keys = sorted(set(resolved.loc[resolved.status != "unknown_key", "key"]))
        if not keys:
            raise RuntimeError("No ENTSOG point-directions resolved; check config/entsog_points.yaml")
        indicators = ",".join(spec.options.get("indicators", ["Physical Flow"]))
        for d_from, d_to in daily_windows(start, end, spec.chunk_days):
            frames = []
            for i in range(0, len(keys), KEY_BATCH):
                batch = keys[i:i + KEY_BATCH]
                rows = self.get("operationalDatas", {
                    "pointDirection": ",".join(batch), "indicator": indicators, "periodType": "day",
                    "from": d_from.isoformat(), "to": (pd.Timestamp(d_to) + pd.Timedelta(days=1)).date().isoformat(),
                })
                if rows:
                    frames.append(self.parse_operational(rows))
            df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            log.info("%s %s..%s: %d rows for %d point-directions", spec.name, d_from, d_to, len(df), len(keys))
            if not df.empty:
                yield df

    @staticmethod
    def parse_operational(rows: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["pointDirection"] = [pd_key(o, p, d) for o, p, d in zip(df["operatorKey"], df["pointKey"], df["directionKey"])]
        df["gasDay"] = gas_day_from_period_start(df["periodFrom"])
        return df

    def _fetch_simple(self, spec, start, end):
        endpoint = spec.options["endpoint"]
        for d_from, d_to in daily_windows(start, end, spec.chunk_days):
            params = {"from": d_from.isoformat(), "to": (pd.Timestamp(d_to) + pd.Timedelta(days=1)).date().isoformat(),
                      **spec.options.get("params", {})}
            rows = self.get(endpoint, params)
            df = pd.DataFrame(rows)
            log.info("%s %s..%s: %d rows", spec.name, d_from, d_to, len(df))
            if not df.empty:
                yield df


def _as_list(v):
    return v if isinstance(v, list) else [v]
