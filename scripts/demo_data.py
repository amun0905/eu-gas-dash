"""Build a local store full of synthetic data (fake APIs from tests/fakes.py) for website work.

    python scripts/demo_data.py            # writes ./demo-data, then: python -m http.server -d . 8000
    open http://localhost:8000/site/?data=/demo-data

Nothing here touches the network; the numbers are made up.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import responses

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("GIE_API_KEY", "test-key")
os.environ["GIE_MIN_INTERVAL_S"] = "0"

import fakes  # noqa: E402
from gasdash.cli import main  # noqa: E402

store = str(ROOT / "demo-data")
start = (datetime.now(timezone.utc) - timedelta(days=365 * 7)).date().isoformat()
with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
    fakes.install(rsps)
    main(["--store", store, "backfill", "agsi_storage", "alsi_lng", "weather_temp", "--start", start])
    main(["--store", store, "backfill", "entsog_flows", "gb_nts", "--start",
          (datetime.now(timezone.utc) - timedelta(days=420)).date().isoformat()])
    main(["--store", store, "update"])
    main(["--store", store, "export"])
print("demo data in", store)
