import os
import sys
from pathlib import Path

import pytest
import responses

sys.path.insert(0, str(Path(__file__).parent))
os.environ["GIE_MIN_INTERVAL_S"] = "0"
os.environ["GIE_THROTTLE_WAIT_S"] = "0"
os.environ["OPENMETEO_PACE"] = "0"

import fakes  # noqa: E402


@pytest.fixture
def api():
    """All upstream APIs replaced by deterministic fakes."""
    fakes.HOLES.clear(); fakes.THROTTLE_NEXT[0] = 0
    fakes.FAKE_RUN.update(hours=-12, shift=0.0)
    from gasdash.sources import ecmwf
    ecmwf._forecast_cache.clear()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        fakes.install(rsps)
        yield rsps


@pytest.fixture
def gie_key(monkeypatch):
    monkeypatch.setenv("GIE_API_KEY", "test-key")
