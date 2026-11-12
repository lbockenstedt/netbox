import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _device_cache_in_tmp(tmp_path, monkeypatch):
    # Each test gets its own device cache file instead of /var/lib/lm.
    import netbox_sync
    monkeypatch.setattr(netbox_sync, "_DEVICE_CACHE_PATH", str(tmp_path / "device_cache.json"))
