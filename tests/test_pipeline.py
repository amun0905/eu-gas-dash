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
    for name in REG:
        assert store.partitions(name), f"{name} wrote nothing"
    flows = store.read(REG["entsog_flows"])
    assert set(flows.indicator) == {"Physical Flow", "Nomination"}
    assert flows.groupby(["gasDay", "pointDirection", "indicator"]).size().max() == 1
    agsi = store.read(REG["agsi_storage"])
    assert {"EU", "DE", "GB"} <= set(agsi.entity)
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
