"""Pins the reported bug: a DHCP reservation added in the LM WebUI silently
disappeared a few minutes later ("I had a reservation for 172.17.1.199").

The hub already does the right thing — after writing the reservation to Kea it
mirrors the MAC onto the matching NetBox IP object via ``NETBOX_UPDATE_IP_ADDR``
with ``{"custom_fields": {"mac_address": ...}}``. That mirroring is what makes
the reservation durable, because ``build_dhcp_payload`` mints a reservation only
for an IP carrying ``custom_fields.mac_address`` and ``core.dns_dhcp_sync``
rebuilds Kea's entire ``subnet4`` from NetBox alone and ``config-set``s it.

Two compounding bugs on this side of the wire threw that payload away:

1. ``NetboxSpoke.handle_command`` forwarded only ``dns_name``/``description``/
   ``status`` to the engine and dropped ``custom_fields`` on the floor.
2. ``update_ip_address()`` had no ``custom_fields`` parameter at all, so it
   could not have written it even if the spoke had passed it through.

Net effect: the MAC never reached NetBox, the reservation stayed Kea-only, and
the next NetBox->Kea sync deleted it. The loss was invisible — the add returned
SUCCESS and the row showed up in the reservation list before vanishing.

This is the exact bug already fixed for prefixes in
test_prefix_dhcp_custom_fields.py; the IP-address path was missed. Uses a
pynetbox stand-in (no live NetBox), mirroring that file.
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_engine import NetboxEngine  # noqa: E402
from netbox_spoke import NetboxSpoke  # noqa: E402


class _Rec:
    """pynetbox Record stand-in: arbitrary attrs + an auto id."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        if not getattr(self, "id", None):
            self.id = id(self) % 100000


def _engine():
    eng = NetboxEngine("http://localhost", "tok")
    eng.nb = MagicMock()
    return eng


def _ip(**kw):
    eng = _engine()
    rec = _Rec(**kw)
    rec.save = MagicMock()
    eng.nb.ipam.ip_addresses.get = MagicMock(return_value=rec)
    return eng, rec


# --------------------------------------------------------------- engine layer

def test_update_ip_address_writes_mac_address():
    """The reservation write-back itself: the MAC must land in NetBox."""
    eng, rec = _ip(id=7, address="172.17.1.199/24", custom_fields={})

    r = eng.update_ip_address(7, custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff"})

    assert r["status"] == "SUCCESS"
    assert rec.custom_fields == {"mac_address": "aa:bb:cc:dd:ee:ff"}
    rec.save.assert_called_once()


def test_update_ip_address_merges_onto_existing_custom_fields():
    """A partial update must not blank out unrelated custom fields."""
    eng, rec = _ip(id=7, address="172.17.1.199/24",
                   custom_fields={"mac_address": "00:00:00:00:00:00",
                                  "owner": "lab-team"})

    eng.update_ip_address(7, custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff"})

    assert rec.custom_fields == {"mac_address": "aa:bb:cc:dd:ee:ff",
                                 "owner": "lab-team"}


def test_update_ip_address_sets_custom_fields_when_none_existed():
    eng, rec = _ip(id=7, address="172.17.1.199/24", custom_fields=None)

    r = eng.update_ip_address(7, custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff"})

    assert r["status"] == "SUCCESS"
    assert rec.custom_fields == {"mac_address": "aa:bb:cc:dd:ee:ff"}


def test_update_ip_address_clears_mac_on_reservation_delete():
    """Deleting a reservation clears the MAC (hub sends an empty string).
    Without this the next sync would recreate the reservation just deleted."""
    eng, rec = _ip(id=7, address="172.17.1.199/24",
                   custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff",
                                  "owner": "lab-team"})

    eng.update_ip_address(7, custom_fields={"mac_address": ""})

    assert rec.custom_fields == {"mac_address": "", "owner": "lab-team"}


def test_update_ip_address_no_op_when_custom_fields_not_given():
    """Unrelated edits (rename/status) must leave the MAC alone."""
    eng, rec = _ip(id=7, address="172.17.1.199/24",
                   custom_fields={"mac_address": "aa:bb:cc:dd:ee:ff"})

    eng.update_ip_address(7, description="renamed")

    assert rec.custom_fields == {"mac_address": "aa:bb:cc:dd:ee:ff"}
    assert rec.description == "renamed"


def test_update_ip_address_still_updates_plain_attributes():
    """Backward compatibility: the pre-existing fields keep working."""
    eng, rec = _ip(id=7, address="172.17.1.199/24", custom_fields={})

    eng.update_ip_address(7, dns_name="host.lab", description="d",
                          status="reserved")

    assert (rec.dns_name, rec.description, rec.status) == ("host.lab", "d",
                                                           "reserved")


def test_update_ip_address_missing_ip_is_an_error():
    eng = _engine()
    eng.nb.ipam.ip_addresses.get = MagicMock(return_value=None)

    r = eng.update_ip_address(7, custom_fields={"mac_address": "aa:bb"})

    assert r["status"] == "ERROR"


# ---------------------------------------------------------------- wire layer

def _dispatch(data):
    """Drive NetboxSpoke.handle_command far enough to reach the IP handler,
    without standing up a real spoke (no config/transport needed)."""
    spoke = object.__new__(NetboxSpoke)  # keeps class attrs, skips __init__
    spoke.engine = MagicMock()
    spoke.engine.update_ip_address = MagicMock(
        return_value={"status": "SUCCESS"})
    spoke._picklist_invalidate = MagicMock()

    async def _run_sync(fn, *a, **kw):
        return fn(*a, **kw)

    spoke._run_sync = _run_sync
    asyncio.run(spoke.handle_command("NETBOX_UPDATE_IP_ADDR", data))
    return spoke.engine.update_ip_address


def test_spoke_forwards_custom_fields_to_engine():
    """The dropped-on-the-floor bug: the hub's payload must reach the engine."""
    called = _dispatch({"ip_id": 7,
                        "custom_fields": {"mac_address": "aa:bb:cc:dd:ee:ff"}})

    called.assert_called_once()
    assert called.call_args.kwargs["custom_fields"] == {
        "mac_address": "aa:bb:cc:dd:ee:ff"}
    assert called.call_args.args[0] == 7


def test_spoke_passes_none_when_no_custom_fields():
    """Absent custom_fields must stay absent, not become {} — the engine
    treats a falsy value as 'leave NetBox's custom fields untouched'."""
    called = _dispatch({"ip_id": 7, "description": "d"})

    assert called.call_args.kwargs["custom_fields"] is None
