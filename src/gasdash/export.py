"""Build the compact JSON files the website reads (web/*.json).

The website never touches Parquet. After each update we pre-aggregate into small, chart-ready
arrays, one file per page. Every file carries its sources so the page can credit them.

Units on the site: storage in TWh and %, flows and send-out in GWh/d.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd

from .registry import DatasetSpec
from .sources import ATTRIBUTIONS, SOURCE_LINKS
from .storage import Store

log = logging.getLogger(__name__)

GWH_PER_MCM_GB = 10.97      # GB NTS volumes: ~39.5 MJ/m3 typical calorific value (actual CV moves ~1%)
KWH_PER_GWH = 1e6

COUNTRY_NAMES = {
    "EU": "EU", "DE": "Germany", "IT": "Italy", "FR": "France", "NL": "Netherlands", "AT": "Austria",
    "HU": "Hungary", "CZ": "Czechia", "SK": "Slovakia", "PL": "Poland", "ES": "Spain", "BE": "Belgium",
    "RO": "Romania", "BG": "Bulgaria", "HR": "Croatia", "DK": "Denmark", "LV": "Latvia", "PT": "Portugal",
    "SE": "Sweden", "GB": "United Kingdom", "UA": "Ukraine", "FI": "Finland", "GR": "Greece", "LT": "Lithuania",
    "EE": "Estonia", "IE": "Ireland", "SI": "Slovenia", "LU": "Luxembourg", "RS": "Serbia", "MT": "Malta",
}


# ---------------------------------------------------------------- helpers

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _r(v, nd=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, nd)


def _arr(values, nd=2) -> list:
    return [_r(v, nd) for v in values]


def _ms(index) -> list[int]:
    return [int(pd.Timestamp(t).timestamp() * 1000) for t in index]


def _day_index(start, end) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D", tz="UTC")


def _sources(*names: str) -> list[dict]:
    return [{"source": n, "text": ATTRIBUTIONS[n], "url": SOURCE_LINKS[n]} for n in names]


def _write(store: Store, name: str, obj: dict) -> None:
    obj = {"generated_at": now_utc().isoformat(timespec="seconds"), **obj}
    store.write_json(f"{store.root}/web/{name}.json", obj, compact=True)
    log.info("wrote web/%s.json", name)


def _md_template() -> pd.DatetimeIndex:
    """366 calendar days on a leap year; used to line up years for seasonal charts."""
    return pd.date_range("2024-01-01", "2024-12-31", freq="D")


def _by_calendar_day(s: pd.Series) -> pd.Series:
    """Daily series (UTC midnight index) -> values indexed by 'MM-DD' over 366 slots."""
    keys = s.index.strftime("%m-%d")
    out = pd.Series(s.values, index=keys)
    out = out[~out.index.duplicated(keep="last")]
    out = out.reindex(_md_template().strftime("%m-%d"))
    # Non-leap years have no 29 Feb. Fill that slot with the midpoint of 28 Feb and 1 Mar, so a single
    # leap year doesn't become the whole 5-year average (and min/max) for that day.
    if pd.isna(out.get("02-29")) and pd.notna(out.get("02-28")) and pd.notna(out.get("03-01")):
        out["02-29"] = (out["02-28"] + out["03-01"]) / 2
    return out


def _seasonal(s: pd.Series, year: int, n_years: int = 5) -> dict:
    """Current year, previous year and the min/avg/max band of the n_years before `year`."""
    s = s.dropna()
    years = {}
    for y in range(year - n_years, year + 1):
        part = s[s.index.year == y]
        if not part.empty:
            years[y] = _by_calendar_day(part)
    band_years = [y for y in range(year - n_years, year) if y in years]
    band = pd.concat([years[y] for y in band_years], axis=1) if band_years else pd.DataFrame(index=_md_template().strftime("%m-%d"))
    return {
        "md": list(_md_template().strftime("%m-%d")),
        "current_year": year,
        "current": _arr(years.get(year, pd.Series(dtype=float)).reindex(_md_template().strftime("%m-%d"))),
        "previous": _arr(years.get(year - 1, pd.Series(dtype=float)).reindex(_md_template().strftime("%m-%d"))),
        "band_years": band_years,
        "min": _arr(band.min(axis=1)) if band_years else [],
        "max": _arr(band.max(axis=1)) if band_years else [],
        "avg": _arr(band.mean(axis=1)) if band_years else [],
        # each year behind the band, so the table can show which year drives a kink
        "years": {str(y): _arr(years[y]) for y in band_years},
    }


def _five_year_avg_on(s: pd.Series, day: pd.Timestamp, n_years: int = 5):
    vals = []
    for k in range(1, n_years + 1):
        try:
            d = day - pd.DateOffset(years=k)
        except Exception:
            continue
        if d in s.index and not pd.isna(s[d]):
            vals.append(float(s[d]))
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------- storage (AGSI + weather)

def build_storage(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or now_utc())
    spec = registry["agsi_storage"]
    df = store.read(spec, now - pd.DateOffset(years=6, months=1), now + timedelta(days=1))
    if df.empty:
        return {"sources": _sources("gie"), "entities": [], "latest": [], "series": {}, "weather": build_weather(store, registry, now)}
    df = df.sort_values("gasDay")
    year = int(df["gasDay"].max().year)
    entities, latest_rows, series = [], [], {}
    order = ["EU"] + [e.upper() for e in spec.options.get("entities", []) if e.upper() != "EU"]
    for ent in [e for e in order if e in set(df["entity"])]:
        d = df[df["entity"] == ent].set_index("gasDay")
        d = d[~d.index.duplicated(keep="last")]
        full, twh = d["full"].astype(float), d["gasInStorage"].astype(float)
        net_inj = (d["injection"].astype(float) - d["withdrawal"].astype(float))
        valid = full.dropna()
        if valid.empty:
            continue
        last_day = valid.index[-1]
        if last_day < df["gasDay"].max() - pd.Timedelta(days=30):
            log.info("storage: %s stopped reporting on %s; left out", ent, last_day.date())
            continue  # e.g. GB stopped reporting to AGSI+ after Brexit (last day 30 Dec 2020)
        def ago(n):
            k = last_day - pd.Timedelta(days=n)
            return float(full[k]) if k in full.index and not pd.isna(full[k]) else None
        avg5 = _five_year_avg_on(full, last_day)
        last_year = full.get(last_day - pd.DateOffset(years=1))
        entities.append({"code": ent, "name": COUNTRY_NAMES.get(ent, d["name"].dropna().iloc[-1] if d["name"].notna().any() else ent)})
        latest_rows.append({
            "code": ent, "name": entities[-1]["name"], "gas_day": last_day.date().isoformat(),
            "full": _r(valid.iloc[-1]), "twh": _r(twh.get(last_day), 1), "wgv_twh": _r(d["workingGasVolume"].get(last_day), 1),
            "d1_pp": _r(valid.iloc[-1] - ago(1)) if ago(1) is not None else None,
            "d7_pp": _r(valid.iloc[-1] - ago(7)) if ago(7) is not None else None,
            "net_injection_gwh": _r(net_inj.get(last_day), 0),
            "vs_5y_pp": _r(valid.iloc[-1] - avg5) if avg5 is not None else None,
            "vs_last_year_pp": _r(valid.iloc[-1] - last_year) if last_year is not None and not pd.isna(last_year) else None,
            "status": d["status"].get(last_day),
        })
        recent = _day_index(now - timedelta(days=400), last_day)
        series[ent] = {
            "full_seasonal": _seasonal(full, year),
            "t": _ms(recent),
            "twh": _arr(twh.reindex(recent), 1),
            "net_injection_gwh": _arr(net_inj.reindex(recent), 0),
            "withdrawal_capacity_gwh": _arr(d["withdrawalCapacity"].astype(float).reindex(recent), 0),
        }
    return {
        "sources": _sources("gie", "openmeteo", "nationalgas"),
        "units": {"full": "% of working gas volume", "twh": "TWh", "flows": "GWh/d"},
        "entities": entities,
        "latest": latest_rows,
        "series": series,
        "weather": build_weather(store, registry, now),
        "gb_stock": build_gb_stock(store, registry, now),
        "notes": [
            "Gas in storage at the end of each gas day, as reported by storage operators to GIE AGSI+. "
            "Status E = estimated, C = confirmed.",
            "5-year range = min / average / max of the five previous calendar years on the same date.",
        ],
    }


def build_gb_stock(store: Store, registry: dict[str, DatasetSpec], now) -> dict:
    """GB storage stock from National Gas (GB no longer reports to AGSI+): this year vs last year, GWh."""
    spec = registry.get("gb_nts")
    if spec is None:
        return {}
    df = store.read(spec, pd.Timestamp(now) - timedelta(days=760), pd.Timestamp(now) + timedelta(days=1))
    if df.empty:
        return {}
    st = df[df["item"] == "PUBOBJ330"].set_index("gasDay")["value"].astype(float) / KWH_PER_GWH
    st = st[~st.index.duplicated(keep="last")].dropna()
    if st.empty:
        return {}
    end = st.index.max()
    window = _day_index(end - timedelta(days=364), end)
    ly = st.reindex(window - pd.DateOffset(years=1))
    return {"t": _ms(window), "gwh": _arr(st.reindex(window), 0), "gwh_last_year": _arr(ly.values, 0),
            "latest": {"gas_day": end.date().isoformat(), "gwh": _r(st[end], 0),
                       "d7_gwh": _r(st[end] - st.get(end - pd.Timedelta(days=7)), 0)
                       if pd.notna(st.get(end - pd.Timedelta(days=7))) else None},
            "note": "Total gas in GB storage sites as published by National Gas (energy, so no fill %: National Gas "
                    "doesn't publish the capacity alongside it). GB stopped reporting to GIE AGSI+ after 2020."}


def hdd(t: pd.Series) -> pd.Series:
    """Eurostat heating degree days: 18 - T when the daily mean T is at or below 15 C, else 0."""
    return (18.0 - t).where(t <= 15.0, 0.0)


def build_weather(store: Store, registry: dict[str, DatasetSpec], now) -> dict:
    spec = registry.get("weather_temp")
    if spec is None:
        return {}
    df = store.read(spec, now - pd.DateOffset(years=11), now + timedelta(days=15))
    if df.empty:
        return {}
    df = df.assign(hdd=hdd(df["tmean"].astype(float)))
    daily = df.pivot_table(index="day", columns="city", values="hdd", aggfunc="last")
    avg = daily.mean(axis=1)  # simple average across the cities
    fc = df.groupby("day")["is_forecast"].max()
    hist = avg[avg.index < pd.Timestamp(now).normalize() - pd.Timedelta(days=0)]
    md = hist.groupby(hist.index.strftime("%m-%d")).mean()
    md = md.reindex(_md_template().strftime("%m-%d")).interpolate(limit_direction="both")
    md = md.rolling(7, center=True, min_periods=1).mean()  # smooth the 10-year normal
    window = _day_index(pd.Timestamp(now) - timedelta(days=120), avg.index.max())
    normal = [md.get(t.strftime("%m-%d")) for t in window]
    return {
        "cities": sorted(daily.columns.tolist()),
        "t": _ms(window),
        "hdd": _arr(avg.reindex(window), 1),
        "normal": _arr(normal, 1),
        "is_forecast": [int(fc.get(t, 0) or 0) for t in window],
        "note": "Heating degree days (Eurostat method, 18 C base, 15 C threshold), simple average of "
                + ", ".join(sorted(daily.columns.tolist()))
                + ". Normal = 10-year average for the date, 7-day smoothed. Shaded days are forecasts.",
    }


# ---------------------------------------------------------------- LNG (ALSI + ENTSOG LNG points)

def build_lng(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or now_utc())
    spec = registry["alsi_lng"]
    df = store.read(spec, now - pd.DateOffset(years=6, months=1), now + timedelta(days=1))
    out = {"sources": _sources("gie", "entsog"), "units": {"send_out": "GWh/d", "inventory": "GWh"},
           "notes": ["Send-out = regasified LNG delivered into the grid, as reported by LNG system operators to GIE ALSI.",
                     "DTMI = declared total maximum inventory; DTRS = declared total reference send-out (technical capacity)."]}
    if df.empty:
        return {**out, "latest": [], "eu": {}, "countries": {}}
    df = df.sort_values("gasDay")
    year = int(df["gasDay"].max().year)
    eu = df[df["entity"] == "EU"].set_index("gasDay")
    eu = eu[~eu.index.duplicated(keep="last")]
    last_day = eu["sendOut"].dropna().index.max() if not eu.empty else df["gasDay"].max()
    window = _day_index(now - timedelta(days=730), last_day)
    eu_send = eu["sendOut"].astype(float)
    out["eu"] = {
        "t": _ms(window),
        "send_out": _arr(eu_send.reindex(window), 0),
        "send_out_7d": _arr(eu_send.rolling(7, min_periods=4).mean().reindex(window), 0),
        "inventory_gwh": _arr(eu["inventory_gwh"].astype(float).reindex(window), 0),
        "inventory_pct": _arr((eu["inventory_gwh"] / eu["dtmi_gwh"] * 100).astype(float).reindex(window), 1),
        "send_out_seasonal_7d": _seasonal(eu_send.rolling(7, min_periods=4).mean(), year),
    }
    countries = [c for c in df["entity"].unique() if c != "EU"]
    win_c = _day_index(now - timedelta(days=365), last_day)
    by_country, latest = {}, []
    for c in countries:
        d = df[df["entity"] == c].set_index("gasDay")
        d = d[~d.index.duplicated(keep="last")]
        s = d["sendOut"].astype(float)
        if s.dropna().empty:
            continue
        by_country[c] = _arr(s.reindex(win_c), 0)
        ld = s.dropna().index.max()
        latest.append({
            "code": c, "name": COUNTRY_NAMES.get(c, c), "gas_day": ld.date().isoformat(),
            "send_out": _r(s[ld], 0), "avg_7d": _r(s.loc[ld - pd.Timedelta(days=6): ld].mean(), 0),
            "avg_30d": _r(s.loc[ld - pd.Timedelta(days=29): ld].mean(), 0),
            "utilisation_pct": _r(s[ld] / d["dtrs"][ld] * 100, 0) if pd.notna(d["dtrs"].get(ld)) and d["dtrs"].get(ld) else None,
            "inventory_gwh": _r(d["inventory_gwh"].get(ld), 0),
            "inventory_pct": _r(d["inventory_gwh"].get(ld) / d["dtmi_gwh"].get(ld) * 100, 0)
            if pd.notna(d["dtmi_gwh"].get(ld)) and d["dtmi_gwh"].get(ld) else None,
        })
    latest.sort(key=lambda r: -(r["avg_30d"] or 0))
    out["countries"] = {"t": _ms(win_c), "order": [r["code"] for r in latest], "send_out": by_country,
                        "names": {c: COUNTRY_NAMES.get(c, c) for c in by_country}}
    if not eu.empty:
        ld = eu_send.dropna().index.max()
        latest.insert(0, {"code": "EU", "name": "EU", "gas_day": ld.date().isoformat(), "send_out": _r(eu_send[ld], 0),
                          "avg_7d": _r(eu_send.loc[ld - pd.Timedelta(days=6): ld].mean(), 0),
                          "avg_30d": _r(eu_send.loc[ld - pd.Timedelta(days=29): ld].mean(), 0),
                          "utilisation_pct": _r(eu_send[ld] / eu["dtrs"][ld] * 100, 0) if eu["dtrs"].get(ld) else None,
                          "inventory_gwh": _r(eu["inventory_gwh"].get(ld), 0),
                          "inventory_pct": _r(eu["inventory_gwh"].get(ld) / eu["dtmi_gwh"].get(ld) * 100, 0) if eu["dtmi_gwh"].get(ld) else None})
    out["latest"] = latest
    if "alsi_terminals" in registry:
        out["terminals"] = build_terminals(store, registry["alsi_terminals"], now)
    return out


def build_terminals(store: Store, spec: DatasetSpec, now) -> dict:
    """One row per LNG terminal: latest figures, a 90-day sparkline, a year of send-out and inventory,
    and days where a jump in inventory suggests a cargo was unloaded."""
    df = store.read(spec, now - timedelta(days=400), now + timedelta(days=1))
    if df.empty:
        return {}
    df = df.sort_values("gasDay")
    end = df["gasDay"].max()
    window = _day_index(end - timedelta(days=364), end)
    terms = []
    for eic, d in df.groupby("entity", sort=False):
        d = d.set_index("gasDay")
        d = d[~d.index.duplicated(keep="last")]
        so, inv = d["sendOut"].astype(float), d["inventory_gwh"].astype(float)
        dtmi, dtrs = d["dtmi_gwh"].astype(float), d["dtrs"].astype(float)
        if so.dropna().empty and inv.dropna().empty:
            continue
        ld = so.dropna().index.max() if so.notna().any() else inv.dropna().index.max()
        a7 = so.loc[ld - pd.Timedelta(days=6): ld].mean()
        a30 = so.loc[ld - pd.Timedelta(days=29): ld].mean()
        # cargo received ~ change in tank inventory + what was sent out that day
        received = inv.diff() + so.fillna(0)
        cap = dtmi.get(ld)
        thresh = max(150.0, 0.15 * cap) if pd.notna(cap) and cap else 150.0
        unl = received[(received >= thresh) & (received.index >= window[0])]
        last = d.iloc[-1]
        terms.append({
            "eic": eic, "name": last.get("name"), "country": last.get("country"), "company": last.get("company"),
            "type": last.get("facilityType"), "gas_day": ld.date().isoformat(), "status": d["status"].get(ld),
            "send_out": _r(so.get(ld), 0), "avg_7d": _r(a7, 0), "avg_30d": _r(a30, 0),
            "utilisation_pct": _r(so.get(ld) / dtrs.get(ld) * 100, 0) if pd.notna(dtrs.get(ld)) and dtrs.get(ld) else None,
            "inventory_gwh": _r(inv.get(ld), 0),
            "inventory_pct": _r(inv.get(ld) / cap * 100, 0) if pd.notna(cap) and cap else None,
            "days_cover": _r(inv.get(ld) / a7, 1) if pd.notna(inv.get(ld)) and a7 and a7 > 0 else None,
            "unloads_30d": int((unl.index > ld - pd.Timedelta(days=30)).sum()),
            "send_out_series": _arr(so.reindex(window), 0),
            "inventory_series": _arr(inv.reindex(window), 0),
            "unloads": [{"t": int(t.timestamp() * 1000), "gwh": _r(v, 0)} for t, v in unl.items()],
        })
    terms.sort(key=lambda r: -(r["avg_30d"] or 0))
    return {"t": _ms(window), "list": terms,
            "note": "Likely unloading = a day when tank inventory rose by more than send-out explains "
                    "(at least 150 GWh or 15% of tank capacity). Inferred from ALSI data, not from ship tracking."}


# ---------------------------------------------------------------- flows (ENTSOG + National Gas)

def load_resolved_points(store: Store) -> pd.DataFrame:
    path = f"{store.root}/reference/entsog_points_resolved.json"
    if not store.fs.exists(path):
        return pd.DataFrame(columns=["series", "group", "label", "key", "sign", "country"])
    with store.fs.open(path, "rb") as f:
        return pd.DataFrame(json.loads(f.read())["rows"])


def to_gwh_per_day(value: pd.Series, unit: pd.Series) -> pd.Series:
    """ENTSOG daily values are normally kWh/d; anything else is converted where we safely can."""
    u = unit.fillna("").str.lower().str.replace(" ", "")
    out = pd.Series(float("nan"), index=value.index)
    kwh = u.str.startswith("kwh")
    out[kwh] = value[kwh] / KWH_PER_GWH
    mwh = u.str.startswith("mwh")
    out[mwh] = value[mwh] / 1e3
    gwh = u.str.startswith("gwh")
    out[gwh] = value[gwh]
    other = ~(kwh | mwh | gwh) & value.notna()
    if other.any():
        log.warning("ENTSOG: %d rows in units we don't convert (%s); left out", int(other.sum()), sorted(u[other].unique()))
    return out


def flow_matrix(flows: pd.DataFrame, resolved: pd.DataFrame, index: pd.DatetimeIndex):
    """-> (values per series [GWh/d], nomination-only mask per series, per-key LNG by country)."""
    if flows.empty or resolved.empty:
        return {}, {}, {}
    f = flows.assign(gwh=to_gwh_per_day(flows["value"].astype(float), flows["unit"]))
    pf = f[f["indicator"].str.lower() == "physical flow"].pivot_table(index="gasDay", columns="pointDirection", values="gwh", aggfunc="last")
    nom = f[f["indicator"].str.lower() == "nomination"].pivot_table(index="gasDay", columns="pointDirection", values="gwh", aggfunc="last")
    pf, nom = pf.reindex(index), nom.reindex(index)
    values, nominated = {}, {}
    for sid, grp in resolved[resolved["status"] != "unknown_key"].groupby("series", sort=False):
        total = pd.Series(0.0, index=index)
        any_pf = pd.Series(False, index=index)
        any_val = pd.Series(False, index=index)
        used_nom = pd.Series(False, index=index)
        for _, r in grp.iterrows():
            k, sign = r["key"], float(r.get("sign", 1) or 1)
            p = pf[k] if k in pf else pd.Series(float("nan"), index=index)
            n = nom[k] if k in nom else pd.Series(float("nan"), index=index)
            # Physical flow when published; nomination fills the most recent day(s) before it is.
            v = p.copy()
            fill = p.isna() & n.notna() & (index >= (p.last_valid_index() or index[0]))
            v[fill] = n[fill]
            used_nom |= fill
            any_pf |= p.notna()
            any_val |= v.notna()
            total = total.add(v.fillna(0) * sign)
        total[~any_val] = float("nan")
        values[sid] = total
        nominated[sid] = used_nom & ~any_pf
    # LNG send-out by country from the matched LNG entry points (cross-check for ALSI)
    lng = {}
    lng_keys = resolved[resolved["group"] == "LNG"]
    for country, grp in lng_keys.groupby("country"):
        cols = [k for k in grp["key"] if k in pf]
        if cols:
            s = pf[cols].sum(axis=1, min_count=1)
            lng[country] = s
    return values, nominated, lng


def build_flows(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or now_utc())
    days = 400
    index = _day_index(now - timedelta(days=days), now + timedelta(days=1))
    out = {"sources": _sources("entsog", "nationalgas"), "units": "GWh/d", "t": _ms(index),
           "notes": [
               "ENTSOG daily physical flows, read on the EU side of each border. When a gas day has no physical "
               "flow yet, the latest nomination is shown instead (dotted).",
               "GB figures come from National Gas and are converted from mcm at 10.97 GWh per mcm "
               "(typical NTS calorific value; the actual value varies by about 1%).",
               "Net interconnector flows = exit minus entry at one operator; negative values mean flow in the opposite direction.",
           ]}
    resolved = load_resolved_points(store)
    flows = store.read(registry["entsog_flows"], index[0], index[-1] + timedelta(days=1))
    values, nominated, lng = flow_matrix(flows, resolved, index)

    series = []
    meta = {}
    if not resolved.empty:
        for sid, grp in resolved.groupby("series", sort=False):
            meta[sid] = {"group": grp["group"].iloc[0], "label": grp["label"].iloc[0],
                         "points": sorted({f"{r.pointLabel} ({r.operatorLabel})" for r in grp.itertuples()
                                           if r.pointLabel}), "unknown_keys": sorted(grp.loc[grp.status == "unknown_key", "key"])}
    for sid, s in values.items():
        if meta.get(sid, {}).get("group") == "LNG":
            continue
        series.append({"id": sid, **meta.get(sid, {}), "values": _arr(s, 1),
                       "nominated": [int(x) for x in nominated[sid].astype(int)]})

    # GB (National Gas): Langeled joins the Norway group; full GB supply stack + demand
    gb = store.read(registry["gb_nts"], index[0], index[-1] + timedelta(days=1)) if "gb_nts" in registry else pd.DataFrame()
    gb_out = {}
    if not gb.empty:
        p = gb.pivot_table(index="gasDay", columns="item", values="value", aggfunc="last").reindex(index)
        def g(item, scale=GWH_PER_MCM_GB):
            return p[item] * scale if item in p else pd.Series(float("nan"), index=index)
        langeled = g("PUBOBJ570")
        series.append({"id": "no_langeled_gb", "group": "Norway", "label": "Easington - Langeled (GB)",
                       "points": ["National Gas: Easington Langeled"], "unknown_keys": [], "values": _arr(langeled, 1),
                       "nominated": [0] * len(index), "source": "nationalgas"})
        gb_out = {
            "supply": {
                "UK Continental Shelf & other beach": _arr(g("PUBOBJ627") - langeled, 0),
                "Norway (Langeled)": _arr(langeled, 0),
                "LNG": _arr(g("PUBOBJ629"), 0),
                "Storage withdrawal": _arr(g("PUBOBJ630"), 0),
                "Interconnector imports": _arr(g("PUBOBJ628"), 0),
            },
            "demand": _arr(g("PUBOB637"), 0),
            "demand_normal": _arr(g("PUBOB2535"), 0),
            "storage_stock_gwh": _arr(g("PUBOBJ330", 1 / KWH_PER_GWH), 0),
            "note": "GB NTS physical inputs by source and NTS demand (D+1). Norwegian gas landing at St Fergus via "
                    "Vesterled is metered together with UK gas, so it sits in the beach-terminal figure.",
        }
    out["series"] = series
    out["gb"] = gb_out
    out["lng_entsog"] = {c: _arr(s, 0) for c, s in lng.items()}

    # latest table: last day with physical flow for each series
    table = []
    for s in series:
        v = pd.Series(s["values"], index=index, dtype=float)
        nm = pd.Series(s["nominated"], index=index).astype(bool)
        actual = v[~nm].dropna()
        if actual.empty:
            continue
        ld = actual.index[-1]
        table.append({"id": s["id"], "group": s["group"], "label": s["label"], "gas_day": ld.date().isoformat(),
                      "value": _r(actual.iloc[-1], 1),
                      "avg_7d": _r(actual.loc[ld - pd.Timedelta(days=6): ld].mean(), 1),
                      "avg_30d": _r(actual.loc[ld - pd.Timedelta(days=29): ld].mean(), 1),
                      "year_ago_30d": _r(actual.loc[ld - pd.Timedelta(days=365 + 29): ld - pd.Timedelta(days=365)].mean(), 1),
                      "nomination_next": _r(v[nm].dropna().iloc[-1], 1) if v[nm].notna().any() else None})
    out["latest"] = table
    return out


# ---------------------------------------------------------------- events (ENTSOG UMMs + GIE IIP)

def _gwh_d(capacity: pd.Series, unit: pd.Series) -> pd.Series:
    """Capacity in GWh/d from energy-rate units (kWh/d, MWh/h, GWh/d, ...). Volumes are left out."""
    u = unit.fillna("").str.lower().str.replace(" ", "")
    out = pd.Series(float("nan"), index=capacity.index)
    for prefix, div in (("kwh", 1e6), ("mwh", 1e3), ("gwh", 1.0)):
        daily = u.str.startswith(prefix) & (u.str.contains("/d") | u.str.contains("day") | u.str.endswith("d"))
        hourly = u.str.startswith(prefix) & u.str.contains("/h")
        out[daily] = capacity[daily] / div
        out[hourly & ~daily] = capacity[hourly & ~daily] * 24 / div
    m = u.str.startswith("twh")
    out[m] = capacity[m] * 1e3
    return out


def build_events(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or now_utc())
    frames = []
    if "entsog_umm" in registry:
        u = store.read(registry["entsog_umm"], now - timedelta(days=400), now + timedelta(days=2))
        if not u.empty:
            # a thread = one event; each update is a new version (and usually a new messageId)
            thread = u["threadId"].fillna(u["messageId"]) if "threadId" in u else u["messageId"]
            u = u.assign(_thread=thread).sort_values(["_thread", "versionNumber", "publicationDateTime"])
            u = u.drop_duplicates("_thread", keep="last")
            if "isLatestVersion" in u:  # ENTSOG marks superseded versions explicitly
                u = u[~u["isLatestVersion"].fillna("Yes").str.lower().isin(["no", "false", "0"])]
            frames.append(pd.DataFrame({
                "source": "ENTSOG", "published": u["publicationDateTime"], "id": u["messageId"],
                "message_type": u["messageType"],
                "participant": u["marketParticipantName"], "asset": u["affectedAssetName"],
                "zone": u["balancingZoneName"], "type": u["unavailabilityType"], "event": u["eventType"],
                "status": u["eventStatus"], "start": u["eventStart"], "stop": u["eventStop"],
                "direction": u["direction"], "unavailable": u["unavailableCapacity"], "unit": u["unitMeasure"],
                "reason": u["unavailabilityReason"], "remarks": u["remarks"],
            }))
    if "gie_umm" in registry:
        g = store.read(registry["gie_umm"], now - timedelta(days=400), now + timedelta(days=2))
        if not g.empty:
            frames.append(pd.DataFrame({
                "source": "GIE IIP", "published": g["published"], "id": g["messageId"],
                "message_type": g["messageType"],
                "participant": g["reportingEntity"], "asset": g["asset"], "zone": g["balancingZone"],
                "type": g["unavailabilityType"], "event": g["messageType"], "status": g["eventStatus"],
                "start": g["eventStart"], "stop": g["eventStop"], "direction": g["direction"],
                "unavailable": g["unavailableCapacity"], "unit": g["unit"], "reason": g["reason"], "remarks": g["remarks"],
            }))
    out = {"sources": _sources("entsog", "gie"),
           "notes": ["Urgent market messages (REMIT inside information) about unavailability of transmission, storage "
                     "and LNG capacity. Shown: the latest version of each message that is active, upcoming, or was "
                     "published in the last 14 days.",
                     "The timeline adds up unavailable capacity reported in energy per day (GWh/d). Messages in "
                     "other units are listed but not added."]}
    if not frames:
        return {**out, "events": [], "timeline": {}}
    ev = pd.concat(frames, ignore_index=True)
    ev = ev[~ev["status"].fillna("").str.lower().isin(["dismissed", "cancelled", "withdrawn"])]
    recent = ev["published"] >= now - timedelta(days=14)
    live = ev["stop"].isna() | (ev["stop"] >= now - timedelta(days=1))
    ev = ev[recent | live].copy()
    ev["gwh_d"] = _gwh_d(ev["unavailable"].astype(float), ev["unit"].astype("string"))
    ev = ev.sort_values(["start", "published"], ascending=[True, False])

    horizon = _day_index(now - timedelta(days=7), now + timedelta(days=90))
    timeline = {}
    for src, grp in ev.groupby("source"):
        tot = pd.Series(0.0, index=horizon)
        for r in grp.dropna(subset=["gwh_d", "start"]).itertuples():
            stop = r.stop if pd.notna(r.stop) else horizon[-1]
            mask = (horizon + pd.Timedelta(days=1) > r.start) & (horizon < stop)
            tot[mask] += r.gwh_d
        timeline[src] = _arr(tot, 0)

    def iso(x):
        return None if pd.isna(x) else pd.Timestamp(x).isoformat()
    rows = [{
        "source": r.source, "published": iso(r.published), "id": r.id, "message_type": r.message_type,
        "capacity_notice": bool(pd.notna(r.unavailable) or (isinstance(r.type, str) and r.type.strip())),
        "participant": r.participant, "asset": r.asset,
        "zone": r.zone, "type": r.type, "event": r.event, "status": r.status, "start": iso(r.start), "stop": iso(r.stop),
        "direction": r.direction, "unavailable": _r(r.unavailable, 1), "unit": r.unit, "gwh_d": _r(r.gwh_d, 1),
        "reason": r.reason, "remarks": (r.remarks or "")[:400] if isinstance(r.remarks, str) else None,
    } for r in ev.itertuples()]
    return {**out, "events": rows, "timeline": {"t": _ms(horizon), **timeline}}


# ---------------------------------------------------------------- auctions (ENTSOG CMP)

def build_auctions(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or now_utc())
    out = {"sources": _sources("entsog"),
           "notes": ["CMP auction data on the ENTSOG Transparency Platform: capacity auctions (mostly on PRISMA) that "
                     "cleared above the reserve price. Auctions that cleared at the reserve price are not reported here."]}
    df = store.read(registry["entsog_cmp_auctions"], now - timedelta(days=365), now + timedelta(days=2))
    if df.empty:
        return {**out, "auctions": [], "by_point": []}
    df = df.sort_values("auctionFrom", ascending=False)

    def iso(x):
        return None if pd.isna(x) else pd.Timestamp(x).isoformat()
    rows = [{"auction": iso(r.auctionFrom), "capacity_from": iso(r.capacityFrom), "capacity_to": iso(r.capacityTo),
             "point": r.pointLabel, "operator": r.operatorLabel, "direction": r.directionKey,
             "premium": _r(r.auctionPremium, 4), "cleared": _r(r.clearedPrice, 4), "reserve": _r(r.reservePrice, 4),
             "unit": r.unit, "platform": r.bookingPlatformLabel} for r in df.head(500).itertuples()]
    by_point = (df.groupby(["pointLabel", "directionKey"]).agg(n=("auctionFrom", "size"), last=("auctionFrom", "max"),
                                                               max_premium=("auctionPremium", "max"))
                .reset_index().sort_values("n", ascending=False).head(30))
    return {**out, "auctions": rows,
            "by_point": [{"point": r.pointLabel, "direction": r.directionKey, "n": int(r.n), "last": iso(r.last),
                          "max_premium": _r(r.max_premium, 4)} for r in by_point.itertuples()]}


# ---------------------------------------------------------------- storage sites (AGSI facility level)

def build_storage_sites(store: Store, registry: dict[str, DatasetSpec], now=None) -> dict:
    """Index (latest figures + 90-day sparkline per site) and one detail file per country, so the
    browser only downloads the year of daily data for the country being looked at."""
    now = pd.Timestamp(now or now_utc())
    spec = registry.get("agsi_facilities")
    if spec is None:
        return {"index": {}, "countries": {}}
    df = store.read(spec, now - timedelta(days=760), now + timedelta(days=1))
    if df.empty:
        return {"index": {"sites": []}, "countries": {}}
    df = df.sort_values("gasDay")
    end = df["gasDay"].max()
    window = _day_index(end - timedelta(days=364), end)
    sites, countries = [], {}
    for eic, d in df.groupby("entity", sort=False):
        d = d.set_index("gasDay")
        d = d[~d.index.duplicated(keep="last")]
        full, twh = d["full"].astype(float), d["gasInStorage"].astype(float)
        net = d["injection"].astype(float) - d["withdrawal"].astype(float)
        valid = full.dropna()
        if valid.empty:
            continue
        ld = valid.index[-1]
        if ld < end - pd.Timedelta(days=30):
            continue  # site no longer reporting
        k7 = ld - pd.Timedelta(days=7)
        last = d.iloc[-1]
        ctry = str(last.get("country") or "")
        site = {
            "eic": eic, "name": last.get("name"), "country": ctry, "company": last.get("company"),
            "type": last.get("facilityType"), "gas_day": ld.date().isoformat(), "status": d["status"].get(ld),
            "full": _r(valid.iloc[-1], 1), "twh": _r(twh.get(ld), 2), "wgv_twh": _r(d["workingGasVolume"].get(ld), 2),
            "net_injection_gwh": _r(net.get(ld), 0),
            "d7_pp": _r(valid.iloc[-1] - full[k7], 1) if k7 in full.index and pd.notna(full[k7]) else None,
            "vs_last_year_pp": None, "spark": _arr(full.reindex(_day_index(ld - timedelta(days=89), ld)), 1),
        }
        ly = full.get(ld - pd.DateOffset(years=1))
        if ly is not None and pd.notna(ly):
            site["vs_last_year_pp"] = _r(valid.iloc[-1] - ly, 1)
        sites.append(site)
        countries.setdefault(ctry, []).append({
            "eic": eic, "name": site["name"],
            "full": _arr(full.reindex(window), 1),
            "full_last_year": _arr(full.reindex(window - pd.DateOffset(years=1)), 1),
            "twh": _arr(twh.reindex(window), 2),
            "net_injection_gwh": _arr(net.reindex(window), 0),
        })
    sites.sort(key=lambda r: -(r["wgv_twh"] or 0))
    for c in countries.values():
        c.sort(key=lambda x: -max([v for v in x["twh"] if v is not None] or [0]))
    return {
        "index": {"sources": _sources("gie"), "sites": sites,
                  "countries": sorted(c for c in countries if c),
                  "note": "Individual storage sites as reported to GIE AGSI+, refreshed once each evening after "
                          "GIE's publication. Last year = the same date one year earlier."},
        "countries": {c: {"t": _ms(window), "sites": v} for c, v in countries.items() if c},
    }


def export_storage_sites(store: Store, registry, now=None):
    data = build_storage_sites(store, registry, now)
    _write(store, "storage_sites", data["index"])
    for c, obj in data["countries"].items():
        _write(store, f"storage_sites_{c}", obj)
    return data


# ---------------------------------------------------------------- sources & freshness

def build_sources(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    from .pipeline import freshness
    from .sources import is_configured
    rows = []
    for spec in registry.values():
        m = store.manifest(spec) or {}
        f = freshness(spec, store) if is_configured(spec.source) else {"stale": None, "latest": None, "age_hours": None}
        rows.append({"dataset": spec.name, "description": spec.description, "source": spec.source,
                     "attribution": ATTRIBUTIONS.get(spec.source), "url": SOURCE_LINKS.get(spec.source),
                     "rows": m.get("rows"), "from": m.get("min_time"), "to": m.get("max_time"),
                     "updated_at": m.get("updated_at"), "stale": f.get("stale"),
                     "max_staleness_hours": spec.max_staleness_hours,
                     "configured": is_configured(spec.source)})
    return {"datasets": rows, "sources": _sources("gie", "entsog", "nationalgas", "openmeteo", "ecmwf"),
            "weather_points": _weather_points(registry)}


def _weather_points(registry: dict[str, DatasetSpec]) -> dict | None:
    """The Weather tab's sample points and weights, straight from the config the pipeline uses (Sources page)."""
    from .export_weather import NAMES
    spec = registry.get("weather_country")
    if spec is None or "countries" not in spec.options:
        return None
    meta = spec.options.get("points_meta", {})
    countries = []
    for iso, pts in spec.options["countries"].items():
        total = float(sum(p["pop"] for p in pts))
        countries.append({"iso": iso, "name": NAMES.get(iso, iso),
                          "population_m": (meta.get("summary", {}).get(iso) or {}).get("population_m", round(total, 2)),
                          "points": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"], "pop_m": p["pop"],
                                      "weight": round(p["pop"] / total, 4)} for p in pts]})
    return {"points_id": spec.options.get("points_id"), "method": meta.get("method", {}), "countries": countries}


def _writer(name, builder):
    def fn(store, registry, now=None):
        data = builder(store, registry, now)
        _write(store, name, data)
        return data
    return fn


EXPORTS = {
    "storage": _writer("storage", build_storage),
    "lng": _writer("lng", build_lng),
    "flows": _writer("flows", build_flows),
    "events": _writer("events", build_events),
    "auctions": _writer("auctions", build_auctions),
    "storage_sites": export_storage_sites,
    "sources": _writer("sources", build_sources),
}


def _weather_map(store, registry, now=None):
    from .export_weather import build_weather_map
    data = build_weather_map(store, registry, now)
    _write(store, "weather_map", data)
    return data


EXPORTS["weather_map"] = _weather_map
