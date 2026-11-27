"""Build config/weather_points.yaml: where we sample ECMWF temperatures in each country, and how much each point counts.

Fully reproducible from open data:
  * Population: GHS-POP R2023A, epoch 2025, 30 arc-second (~1 km) grid, EU Joint Research Centre, CC BY 4.0
    https://human-settlement.emergency.copernicus.eu/ghs_pop2023.php
  * Borders and place names: Natural Earth 1:10m admin-0 countries and populated places (public domain)

Method (same for every country):
  1. Add up GHS-POP population on ECMWF's 0.25-degree grid (each ~1 km cell goes to its nearest grid point),
     using only cells inside the country's border.
  2. Pick N sample points per country, greedily: the most populated grid point first, then the next most
     populated one that is at least D km from every point already picked, until N are chosen.
     D = 0.6 x sqrt(country land area / N), which spreads points across the country rather than piling them
     into its biggest city.
  3. Every populated grid point in the country is assigned to its nearest sample point. A sample point's
     weight is the population it represents divided by the country's population, so the weights cover
     100% of residents, not just city centres.
  4. Each point is labelled with the largest Natural Earth place within 30 km (just a label, not used in maths).

Usage (needs: pip install rasterio shapely numpy pyyaml):
    python scripts/build_weather_points.py --data-dir ./ghs-data        # downloads what's missing into ./ghs-data
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import math
import os
import sys
import urllib.request
import zipfile
from datetime import date

import numpy as np

GHS_BASE = ("https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/"
            "GHS_POP_E2025_GLOBE_R2023A_4326_30ss/V1-0/tiles/")
GHS_TILE = "GHS_POP_E2025_GLOBE_R2023A_4326_30ss_V1_0_{}"
# 10x10-degree tiles covering 29-69N, 20W-30E (all 13 countries, incl. northern Scotland and southern Spain/Italy)
GHS_TILES = ["R3_C17", "R3_C18", "R3_C19", "R4_C17", "R4_C18", "R4_C19", "R4_C20", "R4_C21",
             "R5_C18", "R5_C19", "R5_C20", "R5_C21", "R6_C18", "R6_C19", "R6_C20", "R6_C21"]
NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
NE_COUNTRIES = "ne_10m_admin_0_countries.geojson"
NE_PLACES = "ne_10m_populated_places_simple.geojson"

# Sample points per country. Total kept equal to what Open-Meteo's free tier comfortably allows for a
# 10-year history backfill; more points = more API calls.
POINTS = {"GB": 5, "IE": 3, "FR": 6, "BE": 3, "NL": 3, "DE": 6, "DK": 3, "PL": 4, "CZ": 3, "AT": 3,
          "HU": 3, "IT": 5, "ES": 4}
GRID = 0.25            # ECMWF open-data grid spacing, degrees
SPACING_FACTOR = 0.6
LABEL_RADIUS_KM = 30
BOUNDS = (-11.0, 35.0, 30.0, 61.0)   # lon/lat window read from the mosaic (all 13 countries' mainland)


def fetch(url: str, path: str) -> None:
    if os.path.exists(path):
        return
    print(f"downloading {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=300) as r:
        data = r.read()
    with open(path, "wb") as f:
        f.write(data)


def ensure_inputs(data_dir: str) -> tuple[list[str], str, str]:
    os.makedirs(data_dir, exist_ok=True)
    tifs = []
    for t in GHS_TILES:
        name = GHS_TILE.format(t)
        tif = os.path.join(data_dir, name + ".tif")
        if not os.path.exists(tif):
            z = os.path.join(data_dir, name + ".zip")
            fetch(GHS_BASE + name + ".zip", z)
            with zipfile.ZipFile(z) as zf:
                zf.extract(name + ".tif", data_dir)
        tifs.append(tif)
    for f in (NE_COUNTRIES, NE_PLACES):
        fetch(NE_BASE + f, os.path.join(data_dir, f))
    return tifs, os.path.join(data_dir, NE_COUNTRIES), os.path.join(data_dir, NE_PLACES)


def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def build(tifs: list[str], countries_path: str, places_path: str) -> dict:
    import rasterio
    from rasterio.features import rasterize
    from rasterio.merge import merge
    from shapely.geometry import shape

    pop, transform = merge([rasterio.open(t) for t in tifs], bounds=BOUNDS)
    pop = np.nan_to_num(pop[0].astype("float64"))
    pop[pop < 0] = 0                                    # no-data / sea
    h, w = pop.shape
    rows, cols = np.arange(h), np.arange(w)
    lats = transform.f + (rows + 0.5) * transform.e     # cell centres
    lons = transform.c + (cols + 0.5) * transform.a
    cell_km2 = (abs(transform.a) * 111.32) * (abs(transform.e) * 111.32) * np.cos(np.radians(lats))

    feats = json.load(open(countries_path, encoding="utf-8"))["features"]
    iso_of = lambda p: p.get("ISO_A2_EH") if p.get("ISO_A2_EH") not in (None, "-99") else p.get("ISO_A2")
    geoms = {}
    for f in feats:   # a code can appear on several features (e.g. small territories): merge them
        iso = iso_of(f["properties"])
        if iso in POINTS:
            g = shape(f["geometry"])
            geoms[iso] = geoms[iso].union(g) if iso in geoms else g
    missing = set(POINTS) - set(geoms)
    if missing:
        raise SystemExit(f"countries missing from Natural Earth file: {missing}")
    codes = {iso: i + 1 for i, iso in enumerate(POINTS)}
    mask = rasterize(((geoms[iso], codes[iso]) for iso in POINTS), out_shape=(h, w), transform=transform,
                     fill=0, dtype="int16")

    places = [f["properties"] for f in json.load(open(places_path, encoding="utf-8"))["features"]]

    out, summary = {}, {}
    gi = np.round(lats * (1 / GRID)).astype(int)            # grid index per row / column
    gj = np.round(lons * (1 / GRID)).astype(int)
    for iso, n in POINTS.items():
        m = mask == codes[iso]
        r, c = np.nonzero(m)
        v = pop[r, c]
        area = float(cell_km2[r].sum())
        # population per ECMWF grid point
        keys = gi[r].astype(np.int64) * 10000 + (gj[c] + 1000)     # +1000 keeps western longitudes positive
        uk, inv = np.unique(keys, return_inverse=True)
        gpop = np.bincount(inv, weights=v)
        glat = (uk // 10000) * GRID
        glon = (uk % 10000 - 1000) * GRID
        keep = gpop > 0
        gpop, glat, glon = gpop[keep], glat[keep], glon[keep]
        total = float(gpop.sum())

        spacing = SPACING_FACTOR * math.sqrt(area / n)
        order = np.argsort(-gpop)
        picked = []
        for k in order:
            if all(haversine_km(glat[k], glon[k], glat[p], glon[p]) >= spacing for p in picked):
                picked.append(k)
                if len(picked) == n:
                    break
        plat, plon = glat[picked], glon[picked]
        d = np.stack([haversine_km(glat, glon, a, b) for a, b in zip(plat, plon)], axis=1)
        owner = d.argmin(axis=1)
        catch = np.bincount(owner, weights=gpop, minlength=len(picked))

        pts = []
        for j, (a, b) in enumerate(zip(plat, plon)):
            near = [(p["pop_max"], p["name"]) for p in places
                    if p.get("iso_a2") == iso and p.get("pop_max", 0) > 0
                    and haversine_km(a, b, p["latitude"], p["longitude"]) <= LABEL_RADIUS_KM]
            if not near:  # fall back to the closest place in the country
                cands = [(float(haversine_km(a, b, p["latitude"], p["longitude"])), p["name"]) for p in places
                         if p.get("iso_a2") == iso]
                label = f"near {min(cands)[1]}" if cands else f"{a:.2f}N {b:.2f}E"
            else:
                label = max(near)[1]
            pts.append({"name": label, "lat": round(float(a), 2), "lon": round(float(b), 2),
                        "pop": round(float(catch[j]) / 1e6, 3), "weight": round(float(catch[j] / total), 4)})
        pts.sort(key=lambda p: -p["pop"])
        out[iso] = pts
        summary[iso] = {"population_m": round(total / 1e6, 2), "area_km2": round(area), "spacing_km": round(spacing)}
    return {"countries": out, "summary": summary}


def write_yaml(result: dict, path: str) -> None:
    lines = [
        "# Where the Weather tab samples ECMWF temperatures, and how much each point counts.",
        "# GENERATED by scripts/build_weather_points.py - edit that script, not this file.",
        "#",
        "# Population: GHS-POP R2023A, epoch 2025, 30 arc-second grid (EU Joint Research Centre, CC BY 4.0)",
        "# Borders/labels: Natural Earth 1:10m admin-0 countries and populated places (public domain)",
        "# pop    = residents (millions) whose nearest sample point this is, i.e. the population it represents",
        "# weight = pop / country population (sums to 1 per country). lat/lon are ECMWF 0.25-degree grid points.",
        f"# built: {date.today().isoformat()}",
        "",
        "method:",
        "  population: GHS-POP R2023A, epoch 2025, 30 arc-second (~1 km) grid, EU JRC, CC BY 4.0",
        "  population_url: https://human-settlement.emergency.copernicus.eu/ghs_pop2023.php",
        "  borders: Natural Earth 1:10m admin-0 countries (public domain)",
        "  grid_degrees: 0.25",
        f"  spacing: {SPACING_FACTOR} x sqrt(country area / points)",
        f"  built: '{date.today().isoformat()}'",
        "summary:",
    ]
    for iso, s in result["summary"].items():
        lines.append(f"  {iso}: {{population_m: {s['population_m']}, area_km2: {s['area_km2']}, spacing_km: {s['spacing_km']}}}")
    lines.append("countries:")
    for iso, pts in result["countries"].items():
        lines.append(f"  {iso}:")
        for p in pts:
            name = json.dumps(p["name"], ensure_ascii=False)
            lines.append(f"    - {{name: {name}, lat: {p['lat']}, lon: {p['lon']}, pop: {p['pop']}, weight: {p['weight']}}}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="ghs-data", help="where inputs are cached / downloaded")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "config", "weather_points.yaml"))
    a = ap.parse_args(argv)
    tifs, countries, places = ensure_inputs(a.data_dir)
    result = build(tifs, countries, places)
    write_yaml(result, a.out)
    for iso, pts in result["countries"].items():
        s = result["summary"][iso]
        print(f"{iso}: {s['population_m']}M people, points every >= {s['spacing_km']} km -> "
              + ", ".join(f"{p['name']} {p['weight']:.0%}" for p in pts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
