import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _device_cache_in_tmp(tmp_path, monkeypatch):
    # Each test gets its own device cache directory instead of /var/lib/lm.
    import netbox_device_cache
    monkeypatch.setattr(netbox_device_cache, "CACHE_DIR", str(tmp_path / "devices"))
