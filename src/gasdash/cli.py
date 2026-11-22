"""Command line entry point.

    gasdash list
    gasdash update [DATASET ...]
    gasdash backfill DATASET [...] [--start 2021-01-01] [--end ...] [--no-resume]
    gasdash status [--fail-on-stale]
    gasdash export                 write web/*.json for the website
    gasdash points [--csv out.csv] check config/entsog_points.yaml against ENTSOG's live point list
    gasdash probe [DATASET ...]    fetch 3 recent days per dataset and print what came back (no writes)
    gasdash gaps [DATASET ...]     list missing days in daily datasets (no writes)
    gasdash repair [DATASET ...]   re-fetch every month that has missing days

Storage location: --store, or env GASDASH_STORE (default ./data).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import pipeline
from .registry import load_registry
from .sources import SourceNotConfigured, get_client, is_configured
from .storage import Store

log = logging.getLogger("gasdash")

RESOLVED_POINTS = "reference/entsog_points_resolved.json"


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _pick(registry, names):
    if not names:
        return list(registry.values())
    unknown = [n for n in names if n not in registry]
    if unknown:
        sys.exit(f"Unknown dataset(s): {unknown}. Known: {sorted(registry)}")
    return [registry[n] for n in names]


def save_resolved_points(store: Store, client) -> None:
    """Keep the ENTSOG key -> series mapping next to the data, so the export never needs the API."""
    from .sources.entsog import load_points
    resolved = client.resolve(load_points())
    store.write_json(f"{store.root}/{RESOLVED_POINTS}", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": json.loads(resolved.to_json(orient="records")),
    })


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gasdash")
    p.add_argument("--store", default=os.getenv("GASDASH_STORE", "./data"))
    p.add_argument("--config", default=os.getenv("GASDASH_CONFIG"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")
    u = sub.add_parser("update"); u.add_argument("datasets", nargs="*")
    b = sub.add_parser("backfill")
    b.add_argument("datasets", nargs="+")
    b.add_argument("--start", type=_dt, help="default: dataset's configured start")
    b.add_argument("--end", type=_dt)
    b.add_argument("--no-resume", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--fail-on-stale", action="store_true")
    sub.add_parser("export", help="write web/*.json for the website")
    pt = sub.add_parser("points", help="check ENTSOG point config against the live point list")
    pt.add_argument("--csv", help="also write the full resolution table to this CSV file")
    pr = sub.add_parser("probe", help="fetch a few recent days per dataset and print a summary (no writes)")
    pr.add_argument("datasets", nargs="*")
    pr.add_argument("--days", type=int, default=3)
    gp = sub.add_parser("gaps", help="list missing days in daily datasets"); gp.add_argument("datasets", nargs="*")
    rp = sub.add_parser("repair", help="re-fetch months with missing days"); rp.add_argument("datasets", nargs="*")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    registry = load_registry(a.config)

    if a.cmd == "list":
        for spec in registry.values():
            flag = "" if is_configured(spec.source) else "  (not configured)"
            print(f"{spec.name:22s} {spec.source:12s} {spec.description}{flag}")
        return 0

    if a.cmd == "points":
        from .sources.entsog import EntsogClient, load_points
        import pandas as pd
        res = EntsogClient().resolve(load_points())
        with pd.option_context("display.width", 220, "display.max_rows", 500, "display.max_colwidth", 60):
            print(res[["series", "key", "status", "pointLabel", "operatorLabel", "country"]].to_string(index=False))
        print(f"\n{len(res)} point-directions, {int((res.status == 'unknown_key').sum())} unknown keys")
        if a.csv:
            res.to_csv(a.csv, index=False)
        return 1 if (res.status == "unknown_key").any() else 0

    if a.cmd == "probe":
        return probe(registry, a.datasets, a.days)

    store = Store(a.store)
    failures = []

    def run_one(spec, fn):
        client = get_client(spec.source)
        out = fn(client)
        if spec.source == "entsog" and spec.options.get("kind") == "flows":
            save_resolved_points(store, client)
        return out

    if a.cmd == "gaps":
        for spec in _pick(registry, a.datasets):
            g = pipeline.find_gaps(spec, store)
            if spec.options.get("gap_key") is None:
                continue
            n = sum(len(v) for v in g.values())
            print(f"{spec.name}: {n} missing entity-days in {len(g)} months")
            for m, holes in g.items():
                ents = sorted({e for e, _ in holes})
                print(f"  {m}: {len(holes):4d} missing  ({', '.join(ents[:8])}{' ...' if len(ents) > 8 else ''})")
        return 0

    if a.cmd == "repair":
        for spec in _pick(registry, a.datasets):
            if spec.options.get("gap_key") is None:
                continue
            try:
                print(spec.name, run_one(spec, lambda c, spec=spec: pipeline.heal(spec, store, client=c, force=True)))
            except SourceNotConfigured as e:
                log.warning("%s: %s", spec.name, e)
            except Exception:
                log.exception("repair failed for %s", spec.name)
                failures.append(spec.name)

    if a.cmd == "update":
        hour = datetime.now(timezone.utc).hour
        for spec in _pick(registry, a.datasets):
            hours = spec.options.get("update_hours_utc")
            if hours and not a.datasets and hour not in hours:
                log.info("%s: refreshed only at %s UTC; skipping this run", spec.name, hours)
                continue
            try:
                run_one(spec, lambda c, spec=spec: pipeline.update(spec, store, client=c))
                if spec.options.get("gap_key"):  # self-healing: a couple of holey months per run
                    h = run_one(spec, lambda c, spec=spec: pipeline.heal(spec, store, client=c, max_months=2))
                    if h["refetched"]:
                        log.info("%s healed: %s", spec.name, h)
            except SourceNotConfigured as e:
                log.warning("%s: %s", spec.name, e)
            except Exception:  # keep going: one broken feed shouldn't stop the others
                log.exception("update failed for %s", spec.name)
                failures.append(spec.name)

    elif a.cmd == "backfill":
        for spec in _pick(registry, a.datasets):
            start = a.start or datetime.combine(spec.start, datetime.min.time(), tzinfo=timezone.utc)
            try:
                print(spec.name, run_one(spec, lambda c, spec=spec, start=start: pipeline.backfill(
                    spec, store, start, a.end, client=c, resume=not a.no_resume)))
            except SourceNotConfigured as e:
                log.warning("%s: %s", spec.name, e)
            except Exception:
                log.exception("backfill failed for %s", spec.name)
                failures.append(spec.name)

    elif a.cmd == "status":
        report = [pipeline.freshness(spec, store) if is_configured(spec.source)
                  else {"dataset": spec.name, "skipped": "source not configured", "stale": False}
                  for spec in registry.values()]
        print(json.dumps(report, indent=2))
        if a.fail_on_stale:
            failures = [r["dataset"] for r in report if r["stale"]]

    elif a.cmd == "export":
        from .export import EXPORTS
        for name, fn in EXPORTS.items():
            try:
                fn(store, registry)
            except Exception:
                log.exception("export %s failed", name)
                failures.append(f"export:{name}")

    if a.cmd in ("update", "backfill", "repair"):
        manifests = [m for spec in registry.values() if (m := store.manifest(spec))]
        store.write_catalog(manifests)

    if failures:
        log.error("Failed/stale: %s", failures)
        return 1
    return 0


def probe(registry, names, days: int) -> int:
    """First-run diagnostics: show columns, row counts and a sample for each dataset."""
    from .transform import normalise
    now = pipeline.utcnow()
    bad = []
    for spec in _pick(registry, names):
        print(f"\n=== {spec.name} ({spec.source}) ===")
        try:
            client = get_client(spec.source)
            frames = list(client.fetch(spec, now - timedelta(days=days), now + timedelta(days=spec.horizon_days)))
            if not frames:
                print("  no rows returned")
                continue
            import pandas as pd
            raw = pd.concat(frames, ignore_index=True)
            print(f"  raw rows: {len(raw)}  columns: {sorted(raw.columns)}")
            clean = normalise(raw, spec)
            empty_cols = [c for c in spec.columns if clean[c].isna().all()]
            print(f"  clean rows: {len(clean)}  {spec.time_column}: {clean[spec.time_column].min()} .. {clean[spec.time_column].max()}")
            if empty_cols:
                print(f"  ! columns entirely empty: {empty_cols}")
            print(clean.tail(3).to_string(max_colwidth=40))
        except SourceNotConfigured as e:
            print(f"  skipped: {e}")
        except Exception as e:
            log.exception("probe failed")
            print(f"  FAILED: {e}")
            bad.append(spec.name)
    return 1 if bad else 0


def run() -> None:
    """Console entry point. Exits without interpreter teardown: the S3 client's background thread
    and pyarrow's pool occasionally abort at shutdown after all work is done."""
    rc = 1
    try:
        rc = main()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            rc = 1
        else:
            rc = exc.code or 0
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        logging.shutdown()
        os._exit(rc if isinstance(rc, int) else 1)


if __name__ == "__main__":
    run()
