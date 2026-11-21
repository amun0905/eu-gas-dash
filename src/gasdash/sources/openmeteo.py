"""Open-Meteo daily mean temperature for a few demand-weighted cities (free, no key, CC BY 4.0).

History comes from the archive API (ERA5-based, ~5 days behind); the last days and the 7-day
forecast come from the forecast API. Heating degree days are derived in the export, not stored.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator

import pandas as pd

from ..http import make_session
from ..registry import DatasetSpec
from . import day_utc

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_LAG_DAYS = 6


class OpenMeteoClient:
    def __init__(self, session=None, timeout: float = 60):
        self.session = session or make_session()
        self.timeout = timeout

    def _call(self, url: str, params: dict) -> list[dict]:
        r = self.session.get(url, params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Open-Meteo {r.url} -> {r.status_code}: {r.text[:200]}")
        body = r.json()
        bodies = body if isinstance(body, list) else [body]  # several locations -> list
        return bodies

    @staticmethod
    def parse(bodies: list[dict], cities: list[dict], forecast: bool) -> pd.DataFrame:
        frames = []
        for city, b in zip(cities, bodies):
            daily = b.get("daily") or {}
            if not daily.get("time"):
                continue
            frames.append(pd.DataFrame({
                "day": day_utc(pd.Series(daily["time"])),
                "city": city["name"],
                "country": city["country"],
                "tmean": pd.to_numeric(pd.Series(daily.get("temperature_2m_mean")), errors="coerce"),
                "is_forecast": int(forecast),
            }))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        cities = spec.options["cities"]
        common = {"latitude": ",".join(str(c["lat"]) for c in cities),
                  "longitude": ",".join(str(c["lon"]) for c in cities),
                  "daily": "temperature_2m_mean", "timezone": "UTC"}
        today = pd.Timestamp(datetime.now(timezone.utc).date())
        archive_end = min(pd.Timestamp(end).tz_localize(None).normalize(), today - pd.Timedelta(days=ARCHIVE_LAG_DAYS))
        a = pd.Timestamp(start).tz_localize(None).normalize()
        # archive: one request per year keeps responses small
        while a <= archive_end:
            b = min(a + pd.DateOffset(years=1) - pd.Timedelta(days=1), archive_end)
            df = self.parse(self._call(ARCHIVE_URL, {**common, "start_date": a.date().isoformat(),
                                                     "end_date": b.date().isoformat()}), cities, False)
            log.info("%s archive %s..%s: %d rows", spec.name, a.date(), b.date(), len(df))
            if not df.empty:
                yield df
            a = b + pd.Timedelta(days=1)
        # recent days + forecast (later archive runs overwrite these rows with re-analysis values)
        if pd.Timestamp(end).tz_localize(None) > archive_end:
            df = self.parse(self._call(FORECAST_URL, {**common, "past_days": 10, "forecast_days": 10}), cities, True)
            df["is_forecast"] = (df["day"] > pd.Timestamp(today, tz="UTC")).astype(int)
            log.info("%s forecast: %d rows", spec.name, len(df))
            if not df.empty:
                yield df
