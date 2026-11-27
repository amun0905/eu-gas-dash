"""web/weather_map.json for the Weather tab: country temperatures vs normal, ECMWF forecast to ~15 days.

Kept in its own module and its own JSON file, so nothing here can affect the other pages.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .registry import DatasetSpec
from .storage import Store

NORMAL_YEARS = 10          # same 10-year normal as the HDD chart on the Storage tab
NORMAL_SMOOTH_DAYS = 15    # centred window; 10 years of single days is too noisy for a daily normal
MIN_NORMAL_YEARS = 3       # below this, no normal (and no anomalies) are published
HISTORY_DAYS = 30          # past days shown on the page, for context
MAIN_RUN_HOURS = (0, 12)   # ECMWF's 00z/12z runs go out to 15 days; 06z/18z stop at day 6, so compare main runs

NAMES = {
    "GB": "United Kingdom", "IE": "Ireland", "FR": "France", "BE": "Belgium", "NL": "Netherlands",
    "DE": "Germany", "DK": "Denmark", "PL": "Poland", "CZ": "Czechia", "AT": "Austria", "HU": "Hungary",
    "IT": "Italy", "ES": "Spain", "PT": "Portugal", "LU": "Luxembourg", "CH": "Switzerland", "SK": "Slovakia",
    "SI": "Slovenia", "HR": "Croatia", "RO": "Romania", "BG": "Bulgaria", "GR": "Greece", "SE": "Sweden",
    "NO": "Norway", "FI": "Finland",
}


def _r(v, nd=1):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) or np.isinf(f) else round(f, nd)


def _arr(values, nd=1) -> list:
    return [_r(v, nd) for v in values]


def daily_normal(hist: pd.Series, years: list[int]) -> pd.Series:
    """Daily history (UTC-midnight index) -> normal by day of year, as a Series indexed 'MM-DD' (366 slots).
    Mean over `years` of each calendar day, then a circular centred rolling mean."""
    h = hist[hist.index.year.isin(years)].dropna()
    md_index = pd.date_range("2024-01-01", "2024-12-31", freq="D").strftime("%m-%d")
    if h.empty:
        return pd.Series(np.nan, index=md_index)
    md = h.groupby(h.index.strftime("%m-%d")).mean().reindex(md_index)
    if pd.isna(md.get("02-29")):
        md["02-29"] = np.nanmean([md.get("02-28"), md.get("03-01")])
    k = NORMAL_SMOOTH_DAYS // 2
    wrapped = pd.concat([md.iloc[-k:], md, md.iloc[:k]])
    sm = wrapped.rolling(NORMAL_SMOOTH_DAYS, center=True, min_periods=NORMAL_SMOOTH_DAYS // 2).mean()
    return pd.Series(sm.iloc[k:-k].values, index=md_index)


def run_changes(store: Store, registry: dict[str, DatasetSpec], window: pd.DatetimeIndex, now) -> tuple[dict, dict]:
    """Latest vs previous main ECMWF run, per model and country, aligned to `window`.
    -> ({"hres": {"latest": iso, "previous": iso}, ...}, {iso: {"prev_hres": [...], "chg_hres": [...], ...}})"""
    spec = registry.get("weather_runs")
    if spec is None:
        return {}, {}
    df = store.read(spec, pd.Timestamp(now) - timedelta(days=4), pd.Timestamp(now) + timedelta(days=1))
    if df.empty:
        return {}, {}
    df = df[df["run"].dt.hour.isin(MAIN_RUN_HOURS)]
    if spec.options.get("points_id") and "points_id" in df:     # only compare runs built with today's points
        df = df[df["points_id"] == spec.options["points_id"]]
    meta, out = {}, {}
    for model in ("hres", "ens"):
        m = df[df["model"] == model]
        runs = sorted(m["run"].dropna().unique())
        if len(runs) < 2:
            continue
        latest, prev = pd.Timestamp(runs[-1]), pd.Timestamp(runs[-2])
        meta[model] = {"latest": latest.isoformat(), "previous": prev.isoformat()}
        for iso, g in m.groupby("country"):
            a = g[g["run"] == latest].set_index("day")["t"].reindex(window)
            b = g[g["run"] == prev].set_index("day")["t"].reindex(window)
            out.setdefault(iso, {})[f"prev_{model}"] = _arr(b)
            out[iso][f"chg_{model}"] = _arr(a - b)
    return meta, out


def build_weather_map(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    from .export import _sources, now_utc   # shared helpers; imported here to keep this module standalone
    now = now or now_utc()
    spec = registry.get("weather_country")
    base = {"sources": _sources("ecmwf"), "countries": [], "series": {}}
    if spec is None:
        return base
    today = pd.Timestamp(now).tz_convert("UTC").normalize()
    first_year = today.year - NORMAL_YEARS
    df = store.read(spec, pd.Timestamp(f"{first_year}-01-01", tz="UTC"), today + timedelta(days=20))
    if spec.options.get("points_id") and "points_id" in df:     # ignore rows built with older sample points
        df = df[df["points_id"] == spec.options["points_id"]]
    if df.empty:
        return base
    df = df.sort_values("day")
    years = list(range(first_year, today.year))          # last 10 complete calendar years

    window = pd.date_range(today - timedelta(days=HISTORY_DAYS), df["day"].max(), freq="D", tz="UTC")
    md_keys = window.strftime("%m-%d")
    order = list(spec.options.get("countries", {}).keys()) or sorted(df["country"].unique())
    series, countries, used_years = {}, [], set()
    for iso in order:
        d = df[df["country"] == iso].set_index("day")
        if d.empty:
            continue
        d = d[~d.index.duplicated(keep="last")]
        hist = d.loc[d["is_forecast"] == 0, "tmean"].astype(float)
        have = sorted({y for y in years if hist[hist.index.year == y].notna().sum() >= 300})
        normal = daily_normal(hist, have) if len(have) >= MIN_NORMAL_YEARS else None
        used_years.update(have)
        d = d.reindex(window)
        norm = [normal.get(k) for k in md_keys] if normal is not None else [None] * len(window)
        series[iso] = {
            "t": _arr(d["tmean"]),
            "hres": _arr(d["hres"]),
            "ens_mean": _arr(d["ens_mean"]),
            "ens_p10": _arr(d["ens_p10"]),
            "ens_p90": _arr(d["ens_p90"]),
            "normal": _arr(norm),
            "fc": [int(v) if pd.notna(v) else None for v in d["is_forecast"]],
        }
        countries.append({"iso": iso, "name": NAMES.get(iso, iso), "normal_years": len(have)})

    run_meta, chg = run_changes(store, registry, window, now)
    for iso, extra in chg.items():
        if iso in series:
            series[iso].update(extra)
    fetched = df.loc[df["is_forecast"] == 1, "fetched_at"].dropna()
    yrs = sorted(used_years)
    return {
        **base,
        "countries": countries,
        "series": series,
        "days": [int(t.timestamp() * 1000) for t in window],
        "today": int(today.timestamp() * 1000),
        "forecast_fetched_at": fetched.max() if not fetched.empty else None,
        "runs": run_meta,
        "normal_label": f"{yrs[0]}–{yrs[-1]} average" if yrs else None,
        "note": "Country temperature = population-weighted average of 3–6 sample points per country, weighted by "
                "GHS-POP 2025 population (daily mean, UTC days). "
                "Past days: ERA5 reanalysis. Forecast: ECMWF HRES (9 km); the band is the 10th–90th percentile of the "
                "51-member ECMWF ensemble. Normal = average of the last complete years for the date, "
                f"{NORMAL_SMOOTH_DAYS}-day smoothed.",
    }
