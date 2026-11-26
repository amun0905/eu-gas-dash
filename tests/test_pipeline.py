import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from gasdash import pipeline
from gasdash.cli import main
from gasdash.registry import load_registry
from gasdash.sources import SourceNotConfigured, gas_day_from_period_start, get_client
from gasdash.sources.entsog import EntsogClient, _records, load_points
from gasdash.sources.gie import GieClient
from gasdash.storage import Store

REG = load_registry()
UTC = timezone.utc


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data"))


def test_registry_valid():
    assert {"agsi_storage", "alsi_lng", "entsog_flows", "entsog_umm", "entsog_cmp_auctions", "gb_nts",
            "weather_temp", "gie_umm"} <= set(REG)


def test_gas_day_conversion():
    # 06:00 CET start of gas day 20 Nov, expressed in UTC; summer: 04:00 UTC
    s = pd.Series(["2026-11-20T05:00:00+00:00", "2026-07-01T04:00:00+00:00", "2026-11-20T06:00:00+01:00"])
    out = gas_day_from_period_start(s)
    assert [d.date().isoformat() for d in out] == ["2026-11-20", "2026-07-01", "2026-11-20"]


def test_entsog_records_wrappers():
    assert _records({"message": "No Data Available"}) == []
    assert _records({"meta": {"count": 1}, "operationaldatas": [{"a": 1}]}) == [{"a": 1}]


def test_gie_requires_key(monkeypatch):
    monkeypatch.delenv("GIE_API_KEY", raising=False)
    with pytest.raises(SourceNotConfigured):
        GieClient()


def test_cli_skips_gie_without_key(api, store, monkeypatch):
    monkeypatch.delenv("GIE_API_KEY", raising=False)
    assert main(["--store", store.url, "update", "agsi_storage"]) == 0


def test_points_resolution(api):
    res = EntsogClient().resolve(load_points())
    assert (res.status != "unknown_key").all()
    lng = res[res.series == "lng_entsog"]
    assert set(lng.country) == {"NL", "PL", "IT", "GB"}   # picked up by the match rule, UK -> GB
    assert len(lng) == 4                                     # one reading per terminal, TSO side, no virtual TVB
    assert not lng.key.str.contains("LSO|VTP").any()
    tarifa = res[res.series == "im_tarifa"].set_index("key")["sign"].to_dict()
    assert sorted(tarifa.values()) == [-1, 1]          # net flow = entry - exit


def test_unknown_key_flagged(api):
    cfg = {"series": [{"id": "x", "group": "G", "label": "L", "keys": ["XX-TSO-9999ITP-99999entry"]}]}
    res = EntsogClient().resolve(cfg)
    assert res.status.tolist() == ["unknown_key"]


def test_full_update_then_idempotent(api, gie_key, store):
    assert main(["--store", store.url, "update"]) == 0
    for name, spec in REG.items():
        if spec.options.get("refresh_when_due"):
            continue  # refreshed once a day; checked in its own test
        assert store.partitions(name), f"{name} wrote nothing"
    flows = store.read(REG["entsog_flows"])
    assert set(flows.indicator) == {"Physical Flow", "Nomination"}
    assert flows.groupby(["gasDay", "pointDirection", "indicator"]).size().max() == 1
    agsi = store.read(REG["agsi_storage"])
    assert {"EU", "DE", "UA"} <= set(agsi.entity)
    assert agsi["full"].between(0, 110).all()
    main(["--store", store.url, "update"])   # second run reaches further back (lookback > bootstrap)
    n_before = {name: len(store.read(spec)) for name, spec in REG.items()}
    assert main(["--store", store.url, "update"]) == 0
    n_after = {name: len(store.read(spec)) for name, spec in REG.items()}
    assert n_before == n_after                          # re-running adds no duplicates
    assert store.fs.exists(f"{store.root}/reference/entsog_points_resolved.json")
    assert store.fs.exists(f"{store.root}/catalog.json")


def test_umm_keeps_versions_and_export_takes_latest(api, gie_key, store):
    main(["--store", store.url, "update", "entsog_umm", "gie_umm"])
    umm = store.read(REG["entsog_umm"])
    assert set(umm.versionNumber.dropna()) == {1, 2}
    from gasdash.export import build_events
    ev = build_events(store, REG)
    ids = [e["id"] for e in ev["events"] if e["source"] == "ENTSOG"]
    assert len(ids) == len(set(ids))
    assert {e["source"] for e in ev["events"]} == {"ENTSOG", "GIE IIP"}
    one = next(e for e in ev["events"] if e["id"] == "UMM-1")
    assert one["gwh_d"] == 200.0                         # version 2: 2e8 kWh/d


def test_exports(api, gie_key, store):
    main(["--store", store.url, "backfill", "agsi_storage", "alsi_lng", "weather_temp",
          "--start", (datetime.now(UTC) - timedelta(days=800)).date().isoformat()])
    main(["--store", store.url, "update"])
    assert main(["--store", store.url, "export"]) == 0
    for name in ("storage", "lng", "flows", "events", "auctions", "sources"):
        path = f"{store.root}/web/{name}.json"
        assert store.fs.exists(path), name
        with store.fs.open(path, "rb") as f:
            data = json.loads(f.read())
        assert data["sources"] if name != "sources" else data["datasets"]
    with store.fs.open(f"{store.root}/web/flows.json", "rb") as f:
        flows = json.loads(f.read())
    ids = {s["id"] for s in flows["series"]}
    assert {"no_emden", "ru_turkstream", "no_langeled_gb", "ic_nl_de"} <= ids
    emden = next(s for s in flows["series"] if s["id"] == "no_emden")
    last = max(i for i, v in enumerate(emden["values"]) if v is not None)
    assert emden["nominated"][last] == 1 and emden["nominated"][last - 4] == 0   # newest days = nominations
    assert sum(emden["nominated"]) <= 3
    with store.fs.open(f"{store.root}/web/storage.json", "rb") as f:
        st = json.loads(f.read())
    eu = st["series"]["EU"]["full_seasonal"]
    assert len(eu["md"]) == 366 and eu["band_years"]
    assert st["weather"]["hdd"]


def test_flow_net_sign():
    from gasdash.export import flow_matrix
    idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    flows = pd.DataFrame({"gasDay": list(idx) * 2, "pointDirection": ["Aentry"] * 3 + ["Aexit"] * 3,
                          "indicator": "Physical Flow", "value": [10e6] * 3 + [4e6] * 3, "unit": "kWh/d"})
    resolved = pd.DataFrame({"series": ["s", "s"], "group": "G", "label": "L", "key": ["Aentry", "Aexit"],
                             "sign": [1, -1], "country": "XX", "status": "ok"})
    values, nominated, _ = flow_matrix(flows, resolved, idx)
    assert values["s"].tolist() == [6.0, 6.0, 6.0]


def test_gie_paging_survives_capped_pages(api, gie_key):
    from gasdash.sources.gie import GieClient
    rows = [r for page in GieClient().pages("iip", {"published": "2026-11"}) for r in page]
    assert len(rows) == 120          # 3 pages of 50/50/20 although last_page said 1


def test_capacity_units():
    from gasdash.export import _gwh_d
    out = _gwh_d(pd.Series([1e6, 1e6, 10.0, 5.0]), pd.Series(["kWh/h", "kWh/d", "GWh/d", "kWh"]))
    assert out.tolist()[:3] == [24.0, 1.0, 10.0] and pd.isna(out.iloc[3])


def test_terminals(api, gie_key, store):
    from gasdash.export import build_lng
    main(["--store", store.url, "backfill", "alsi_terminals", "alsi_lng",
          "--start", (datetime.now(UTC) - timedelta(days=120)).date().isoformat()])
    t = store.read(REG["alsi_terminals"])
    assert set(t.entity) == {"21W0000000001245", "37W0000000000001", "21W000000000SH01"}  # closed FSRU skipped
    assert set(t.country) == {"NL", "DE", "GB"}
    lng = build_lng(store, REG)
    terms = lng["terminals"]["list"]
    assert len(terms) == 3 and all(len(x["send_out_series"]) == 365 for x in terms)
    assert all(x["days_cover"] and x["days_cover"] > 0 for x in terms)
    assert all(1 <= x["unloads_30d"] <= 5 for x in terms)  # one cargo every 9 days, not every day
    assert {x["name"] for x in terms} == {"Gate Terminal (I)", "Wilhelmshaven 1", "South Hook"}


def test_leap_day_does_not_spike_band():
    from gasdash.export import _seasonal
    idx = pd.date_range("2021-01-01", "2026-06-30", freq="D", tz="UTC")
    s = pd.Series(50.0, index=idx)
    s[s.index == pd.Timestamp("2024-02-29", tz="UTC")] = 90.0   # only the leap year has this day
    out = _seasonal(s, 2026)
    i = out["md"].index("02-29")
    assert out["avg"][i] < 60 and out["current"][i] == 50.0     # filled from neighbours, no gap
    assert set(out["years"]) == {"2021", "2022", "2023", "2024", "2025"}


def test_gie_throttle_body_is_retried_not_treated_as_empty(api, gie_key):
    import fakes
    from gasdash.sources.gie import GieClient
    fakes.THROTTLE_NEXT[0] = 2                       # two 'too many requests' answers, then data
    rows = [r for page in GieClient().pages("agsi", {"country": "de", "from": "2025-03-01", "to": "2025-03-31"}) for r in page]
    assert len(rows) == 31


def test_gie_persistent_error_fails_loudly(api, gie_key):
    import fakes
    from gasdash.sources.gie import GieClient
    fakes.THROTTLE_NEXT[0] = 99
    with pytest.raises(RuntimeError, match="still failing"):
        list(GieClient().pages("agsi", {"country": "de", "from": "2025-03-01", "to": "2025-03-31"}))


def test_gaps_found_and_healed(api, gie_key, store):
    import fakes
    from gasdash.pipeline import find_gaps, heal
    spec = REG["agsi_storage"]
    fakes.HOLES.add(("DE", "2025-03"))
    main(["--store", store.url, "backfill", "agsi_storage", "--start", "2025-01-01", "--end", "2025-06-01"])
    g = find_gaps(spec, store)
    assert list(g) == ["2025-03"] and len(g["2025-03"]) == 31
    # the source still has no data -> re-fetched once, then remembered and not retried
    assert heal(spec, store)["refetched"] == ["2025-03"]
    assert heal(spec, store)["refetched"] == []
    # the data turns up -> `repair` re-fetches regardless of what was tried before
    fakes.HOLES.clear()
    assert main(["--store", store.url, "repair", "agsi_storage"]) == 0
    assert find_gaps(spec, store) == {}


def test_storage_sites(api, gie_key, store):
    from gasdash.export import build_storage_sites
    main(["--store", store.url, "backfill", "agsi_facilities",
          "--start", (datetime.now(UTC) - timedelta(days=400)).date().isoformat()])
    sites = store.read(REG["agsi_facilities"])
    assert set(sites.name) == {"UGS Rehden", "UGS Epe L", "Bergermeer"}
    out = build_storage_sites(store, REG)
    idx = out["index"]
    assert idx["countries"] == ["DE", "NL"] and len(idx["sites"]) == 3
    assert all(len(x["spark"]) == 90 and x["vs_last_year_pp"] is not None for x in idx["sites"])
    de = out["countries"]["DE"]
    assert len(de["t"]) == 365 and len(de["sites"]) == 2 and len(de["sites"][0]["full_last_year"]) == 365
    assert main(["--store", store.url, "export"]) == 0
    assert store.fs.exists(f"{store.root}/web/storage_sites_DE.json")


def test_gie_refresh_due():
    from gasdash.cli import gie_refresh_due
    ts = lambda d: pd.Timestamp(d, tz="UTC")
    # 25 Nov 14:30 UTC: GIE has published up to gas day 23 Nov
    assert gie_refresh_due(ts("2026-11-23"), datetime(2026, 11, 25, 14, 30, tzinfo=UTC))[0] is False
    assert gie_refresh_due(ts("2026-11-22"), datetime(2026, 11, 25, 14, 30, tzinfo=UTC))[0] is True
    # after the evening publication, 24 Nov is due; a late run next morning still catches up
    assert gie_refresh_due(ts("2026-11-23"), datetime(2026, 11, 25, 20, 17, tzinfo=UTC))[0] is True
    assert gie_refresh_due(ts("2026-11-23"), datetime(2026, 11, 26, 2, 34, tzinfo=UTC))[0] is True
    assert gie_refresh_due(None, datetime(2026, 11, 25, 9, 0, tzinfo=UTC))[0] is True


def test_storage_sites_skipped_when_up_to_date(api, gie_key, store, caplog):
    import logging
    main(["--store", store.url, "update", "agsi_facilities"])     # named -> always runs
    assert store.partitions("agsi_facilities")
    caplog.set_level(logging.INFO)
    main(["--store", store.url, "update"])                         # scheduled-style run
    assert "the newest GIE has published; skipping" in caplog.text


def test_entity_that_stopped_reporting_is_left_out(api, gie_key, store):
    import fakes
    from gasdash.export import build_storage
    fakes.HOLES.update({("DE", m) for m in pd.period_range("2025-03", pd.Timestamp.now().strftime("%Y-%m"), freq="M").strftime("%Y-%m")})
    main(["--store", store.url, "backfill", "agsi_storage", "--start", "2025-01-01"])
    codes = {r["code"] for r in build_storage(store, REG)["latest"]}
    assert "DE" not in codes and "EU" in codes          # DE's last day is Feb 2025 -> dropped
