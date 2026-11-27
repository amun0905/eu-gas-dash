"""Source clients. Each client exposes `fetch(spec, start, end) -> Iterator[DataFrame]`."""
from __future__ import annotations

import os

import pandas as pd


class SourceNotConfigured(RuntimeError):
    """Raised when a source needs credentials that aren't set (e.g. GIE_API_KEY).
    The CLI treats this as 'skip with a warning', not as a failed run."""


def get_client(source: str):
    if source == "gie":
        from .gie import GieClient
        return GieClient()
    if source == "entsog":
        from .entsog import EntsogClient
        return EntsogClient()
    if source == "nationalgas":
        from .nationalgas import NationalGasClient
        return NationalGasClient()
    if source == "openmeteo":
        from .openmeteo import OpenMeteoClient
        return OpenMeteoClient()
    if source == "ecmwf":
        from .ecmwf import EcmwfClient
        return EcmwfClient()
    raise ValueError(f"No client implemented for source '{source}'")


def is_configured(source: str) -> bool:
    if source == "gie":
        return bool(os.getenv("GIE_API_KEY"))
    return True


# Shown under every chart and in each dataset's _manifest.json. GIE *requires* a visible credit.
ATTRIBUTIONS = {
    "gie": "Source: GIE (Gas Infrastructure Europe), AGSI / ALSI / IIP transparency platforms",
    "entsog": "Source: ENTSOG Transparency Platform",
    "nationalgas": "Source: National Gas Transmission, Data Portal",
    "openmeteo": "Weather data by Open-Meteo.com (CC BY 4.0)",
    "ecmwf": "ECMWF HRES/ENS forecasts and ERA5 (CC BY 4.0), via Open-Meteo.com",
}

SOURCE_LINKS = {
    "gie": "https://www.gie.eu/transparency/",
    "entsog": "https://transparency.entsog.eu/",
    "nationalgas": "https://data.nationalgas.com/",
    "openmeteo": "https://open-meteo.com/",
    "ecmwf": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
}


def attribution(source: str) -> str:
    return ATTRIBUTIONS.get(source, "")


# ---------------------------------------------------------------- shared helpers

GAS_DAY_TZ = "Europe/Brussels"  # European gas day: 06:00-06:00 CET/CEST


def gas_day_from_period_start(values: pd.Series) -> pd.Series:
    """ENTSOG-style period start timestamps (any offset) -> gas day as UTC midnight.
    06:00 CET on 20 Nov is the start of gas day 20 Nov, so shift back 6 h in local time."""
    ts = pd.to_datetime(values, utc=True, errors="coerce")
    local = ts.dt.tz_convert(GAS_DAY_TZ) - pd.Timedelta(hours=6)
    return pd.to_datetime(local.dt.strftime("%Y-%m-%d"), errors="coerce").dt.tz_localize("UTC")


def day_utc(values: pd.Series) -> pd.Series:
    """'2026-11-20' (or a timestamp) -> 2026-11-20 00:00 UTC. Used for gas-day keyed data."""
    d = pd.to_datetime(values.astype(str).str[:10], errors="coerce", format="%Y-%m-%d")
    return d.dt.tz_localize("UTC")


def daily_windows(start, end, chunk_days: int):
    """Split [start, end) into date windows of chunk_days; yields (date_from, date_to_inclusive)."""
    a = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end)
    while a < end:
        b = min(a + pd.Timedelta(days=chunk_days), end.normalize() + pd.Timedelta(days=1))
        yield a.date(), (b - pd.Timedelta(days=1)).date()
        a = b
