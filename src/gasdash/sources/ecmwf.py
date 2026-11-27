"""ECMWF temperature forecasts, averaged per country (via Open-Meteo, free, no key, CC BY 4.0).

Each country is a basket of its largest cities weighted by population (configured in
datasets.yaml), so the average leans towards where people live and gas is burnt for heat,
the same idea as the "population-weighted" temperatures commercial weather vendors sell.

Per day and country we store:
  tmean     best estimate: ERA5 actual for the past, ECMWF HRES (9 km) for the future
  hres      ECMWF HRES deterministic forecast
  ens_mean  mean of the 51 ECMWF ENS members (0.25 deg)
  ens_p10 / ens_p90   10th / 90th percentile of the members' country averages
History (ERA5, ~5 days behind) is what the export uses to build the 10-year normal.

With `mode: runs` (dataset weather_runs) the same forecast is stored again, labelled with the ECMWF
run it came from (Open-Meteo's model metadata), and never overwritten by later runs. That is what the
"vs previous run" view compares. Both datasets share one forecast download per update.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterator

import numpy as np
import pandas as pd

from ..http import make_session
from ..registry import DatasetSpec
from . import day_utc

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
META_URL = "https://api.open-meteo.com/data/{model}/static/meta.json"
HRES_MODEL, ENS_MODEL = "ecmwf_ifs", "ecmwf_ifs025"
ENS_META_MODEL = "ecmwf_ifs025_ensemble"
ARCHIVE_LAG_DAYS = 6
FORECAST_DAYS = 16          # Open-Meteo counts today as day 1; ECMWF reaches ~15 days ahead
PAST_DAYS = ARCHIVE_LAG_DAYS
HOURLY_BUDGET = 4000        # Open-Meteo free tier: 5,000 calls/hour; stay under it during backfills
CACHE_SECONDS = 1800        # weather_country and weather_runs reuse one forecast download per run

_forecast_cache: dict = {}


class OpenMeteoRateLimited(RuntimeError):
    pass


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def points_from_options(options: dict) -> list[dict]:
    """datasets.yaml 'countries' -> flat list of {country, name, lat, lon, w} with weights summing to 1 per country."""
    out = []
    for iso, cities in options["countries"].items():
        total = float(sum(c["pop"] for c in cities))
        for c in cities:
            out.append({"country": iso, "name": c["name"], "lat": c["lat"], "lon": c["lon"], "w": c["pop"] / total})
    return out


def weighted_by_country(values: pd.DataFrame, points: list[dict]) -> pd.DataFrame:
    """values: index = day, one column per point (same order as `points`) -> index = day, one column per country.
    A country's value is null on a day when any of its points is missing, rather than a skewed partial average."""
    if len(values.columns) != len(points):
        raise RuntimeError(f"Open-Meteo returned {len(values.columns)} locations, expected {len(points)}")
    w = pd.Series([p["w"] for p in points], index=values.columns)
    by = pd.Series([p["country"] for p in points], index=values.columns)
    out = {}
    for iso in dict.fromkeys(by):           # keep config order
        cols = by.index[by == iso]
        v = values[cols]
        out[iso] = (v * w[cols]).sum(axis=1, min_count=len(cols)).where(v.notna().all(axis=1))
    return pd.DataFrame(out, index=values.index)


class EcmwfClient:
    def __init__(self, session=None, timeout: float = 90, sleep=time.sleep):
        self.session = session or make_session()
        self.timeout = timeout
        self.sleep = sleep

    # ------------------------------------------------------------ http

    def _call(self, url: str, params: dict, pace_weight: float = 0.0) -> list[dict]:
        for attempt in range(4):
            r = self.session.get(url, params=params, timeout=self.timeout)
            if r.status_code == 429:
                reason = r.text[:200]
                if "Daily" in reason or "daily" in reason or attempt == 3:
                    raise OpenMeteoRateLimited(
                        f"Open-Meteo rate limit ({reason}). Backfills resume where they stopped: run it again later.")
                log.warning("Open-Meteo rate limited (%s); waiting 65 s", reason)
                self.sleep(65)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Open-Meteo {r.url} -> {r.status_code}: {r.text[:200]}")
            # Pace month-long archive requests so a 10-year backfill stays inside the hourly free-tier budget
            # (Open-Meteo counts each location, and each extra two weeks of data, as another call).
            pace = float(os.getenv("OPENMETEO_PACE", "1"))   # tests set 0
            if pace_weight > 60 and pace > 0:
                self.sleep(pace * pace_weight * 3600 / HOURLY_BUDGET)
            body = r.json()
            return body if isinstance(body, list) else [body]
        raise RuntimeError("unreachable")

    @staticmethod
    def _coords(points: list[dict]) -> dict:
        return {"latitude": ",".join(f"{p['lat']:.3f}" for p in points),
                "longitude": ",".join(f"{p['lon']:.3f}" for p in points),
                "timezone": "UTC"}

    @staticmethod
    def _daily_frame(bodies: list[dict], key: str) -> pd.DataFrame:
        """One column per location holding daily[key]; index = day (UTC midnight)."""
        cols = {i: pd.Series(dtype="float64") for i in range(len(bodies))}   # keep one column per location
        index = None
        for i, b in enumerate(bodies):
            daily = b.get("daily") or {}
            if not daily.get("time") or daily.get(key) is None:
                continue
            idx = pd.DatetimeIndex(day_utc(pd.Series(daily["time"])))
            s = pd.Series(pd.to_numeric(pd.Series(daily.get(key)), errors="coerce").values, index=idx)
            cols[i] = s
            index = idx if index is None else index.union(idx)
        if index is None:
            return pd.DataFrame()
        return pd.DataFrame({i: c.reindex(index) for i, c in cols.items()}, index=index).sort_index()

    # ------------------------------------------------------------ pieces

    def archive(self, points: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        days = (end - start).days + 1
        bodies = self._call(ARCHIVE_URL, {**self._coords(points), "daily": "temperature_2m_mean", "models": "era5",
                                          "start_date": start.date().isoformat(), "end_date": end.date().isoformat()},
                            pace_weight=len(points) * max(1.0, days / 14))
        v = self._daily_frame(bodies, "temperature_2m_mean")
        if v.empty:
            return pd.DataFrame()
        t = weighted_by_country(v, points)
        df = t.stack(future_stack=True).rename("tmean").reset_index()
        df.columns = ["day", "country", "tmean"]
        df["is_forecast"] = 0
        for c in ("hres", "ens_mean", "ens_p10", "ens_p90"):   # forecast-only columns
            df[c] = np.nan
        return df.dropna(subset=["tmean"])

    def forecast(self, points: list[dict], today: pd.Timestamp) -> pd.DataFrame:
        coords = self._coords(points)
        hres = self._call(FORECAST_URL, {**coords, "daily": "temperature_2m_mean", "models": HRES_MODEL,
                                         "forecast_days": FORECAST_DAYS, "past_days": PAST_DAYS})
        hv = self._daily_frame(hres, "temperature_2m_mean")
        if hv.empty:
            raise RuntimeError("Open-Meteo returned no ECMWF HRES forecast")
        h = weighted_by_country(hv, points)

        ens = self._call(ENSEMBLE_URL, {**coords, "daily": "temperature_2m_mean", "models": ENS_MODEL,
                                        "forecast_days": FORECAST_DAYS, "past_days": 1})
        # Control run is "temperature_2m_mean", perturbed members "..._member01".."_member50".
        member_keys = sorted({k for b in ens for k in (b.get("daily") or {}) if k.startswith("temperature_2m_mean")})
        members = []
        for k in member_keys:
            m = self._daily_frame(ens, k)
            if not m.empty:
                members.append(weighted_by_country(m, points))
        frames = []
        for iso in h.columns:
            d = pd.DataFrame({"hres": h[iso]})
            if members:
                stack = pd.concat([m[iso] for m in members], axis=1)
                ok = stack.notna().sum(axis=1) >= max(10, len(members) // 2)
                d["ens_mean"] = stack.mean(axis=1).where(ok)
                d["ens_p10"] = stack.quantile(0.1, axis=1).where(ok)
                d["ens_p90"] = stack.quantile(0.9, axis=1).where(ok)
            d["country"] = iso
            frames.append(d.rename_axis("day").reset_index())
        df = pd.concat(frames, ignore_index=True)
        for c in ("ens_mean", "ens_p10", "ens_p90"):
            if c not in df:
                df[c] = np.nan
        df["tmean"] = df["hres"].fillna(df["ens_mean"])
        df["is_forecast"] = (df["day"] >= today).astype(int)
        return df.dropna(subset=["tmean"])

    def cached_forecast(self, points: list[dict], today: pd.Timestamp) -> pd.DataFrame:
        key = (tuple((p["country"], p["lat"], p["lon"], round(p["w"], 6)) for p in points), today)
        hit = _forecast_cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1].copy()
        df = self.forecast(points, today)
        _forecast_cache.clear()
        _forecast_cache[key] = (time.monotonic(), df)
        return df.copy()

    def run_init(self, model: str) -> pd.Timestamp:
        """Initialisation time of the model run Open-Meteo is currently serving."""
        r = self.session.get(META_URL.format(model=model), timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Open-Meteo metadata for {model} -> {r.status_code}")
        return pd.Timestamp(int(r.json()["last_run_initialisation_time"]), unit="s", tz="UTC")

    # ------------------------------------------------------------ skip updates when ECMWF has no new run

    @staticmethod
    def _state_path(spec: DatasetSpec, store) -> str:
        return f"{store.root}/reference/ecmwf_last_runs_{spec.name}.json"

    def _current_runs(self) -> dict:
        return {"hres": self.run_init(HRES_MODEL).isoformat(), "ens": self.run_init(ENS_META_MODEL).isoformat(),
                "day": datetime.now(timezone.utc).date().isoformat()}

    def needs_update(self, spec: DatasetSpec, store) -> bool:
        """False when neither the HRES nor the ENS run has changed since this dataset's last update (same UTC day,
        so ERA5 history still moves on daily). Fails open: any problem means 'update'."""
        try:
            path = self._state_path(spec, store)
            if not store.fs.exists(path):
                return True
            with store.fs.open(path, "rb") as f:
                last = json.loads(f.read())
            self._pending = self._current_runs()
            return self._pending != last
        except Exception as e:  # noqa: BLE001
            log.warning("%s: couldn't check for a new ECMWF run (%s); updating anyway", spec.name, e)
            return True

    def mark_updated(self, spec: DatasetSpec, store) -> None:
        try:
            state = getattr(self, "_pending", None) or self._current_runs()
            store.write_json(self._state_path(spec, store), state)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: couldn't record the ECMWF run (%s)", spec.name, e)
        self._pending = None

    def runs(self, points: list[dict], today: pd.Timestamp) -> pd.DataFrame:
        hres_run, ens_run = self.run_init(HRES_MODEL), self.run_init(ENS_META_MODEL)
        fc = self.cached_forecast(points, today)
        fc = fc[fc["day"] >= today]
        h = fc[["day", "country", "hres"]].rename(columns={"hres": "t"}).assign(model="hres", run=hres_run)
        e = fc[["day", "country", "ens_mean", "ens_p10", "ens_p90"]].rename(
            columns={"ens_mean": "t", "ens_p10": "p10", "ens_p90": "p90"}).assign(model="ens", run=ens_run)
        df = pd.concat([h, e], ignore_index=True)
        return df.dropna(subset=["t"])

    # ------------------------------------------------------------ pipeline entry point

    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        points = points_from_options(spec.options)
        today = pd.Timestamp(datetime.now(timezone.utc).date(), tz="UTC")
        if spec.options.get("mode") == "runs":     # snapshot of the current runs; start/end don't apply
            df = self.runs(points, today)
            log.info("%s: HRES run %s, ENS run %s, %d rows", spec.name, df.loc[df.model == "hres", "run"].max(),
                     df.loc[df.model == "ens", "run"].max(), len(df))
            if not df.empty:
                yield df.assign(points_id=spec.options.get("points_id"))
            return
        start, end = _utc(start), _utc(end)
        archive_end = min(end.normalize() - pd.Timedelta(days=1), today - pd.Timedelta(days=ARCHIVE_LAG_DAYS))
        a = start.normalize()
        run = datetime.now(timezone.utc).isoformat(timespec="minutes")
        while a <= archive_end:          # month at a time, like the backfill
            b = min((a + pd.DateOffset(months=1)).replace(day=1) - pd.Timedelta(days=1), archive_end)
            df = self.archive(points, a.tz_localize(None), b.tz_localize(None))
            log.info("%s ERA5 %s..%s: %d rows", spec.name, a.date(), b.date(), len(df))
            if not df.empty:
                df["fetched_at"] = run
                yield df.assign(points_id=spec.options.get("points_id"))
            a = b + pd.Timedelta(days=1)
        if end > today - pd.Timedelta(days=ARCHIVE_LAG_DAYS):
            df = self.cached_forecast(points, today)
            # never let a forecast row overwrite days ERA5 already covers
            df = df[df["day"] > today - pd.Timedelta(days=ARCHIVE_LAG_DAYS)]
            log.info("%s ECMWF forecast: %d rows", spec.name, len(df))
            if not df.empty:
                df["fetched_at"] = run
                yield df.assign(points_id=spec.options.get("points_id"))
