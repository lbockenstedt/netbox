import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netbox_profile import guess_role  # noqa: E402


def test_hostname_tokens_map_to_roles():
    cases = {"U36-AP-02": "access-point", "RSVBE-TOR-4": "switch",
             "MIPBE-AZ4-PDU-B": "pdu", "MIP-PROXMOX-3-ILO": "bmc",
             "MIPBE-AZ13-PXSVR-131": "hypervisor", "NIMBLE-A": "storage",
             "BO-CDG-CRGW01": "gateway", "MACBOOK-GRN-06.corp.local": "workstation",
             "OLTH-UXI-01": "sensor"}
    assert {h: guess_role(h) for h in cases} == cases


def test_unknown_names_and_ips_stay_unclassified():
    assert guess_role("device-aabbccddeeff") == ""
    assert guess_role("10.1.2.3") == ""
    assert guess_role("CAPTAIN") == ""          # tokens match whole, not substrings


def test_specific_profile_beats_hostname_and_generic_server_is_last():
    assert guess_role("lab-sw1", {"sys_descr": "Rack PDU AP8641"}) == "pdu"
    assert guess_role("", {"ssh_banner": "SSH-2.0-OpenSSH_8.9p1 Ubuntu"}) == "server"
    assert guess_role("lab-sw1", {"http_server": "nginx"}) == "switch"


def test_hypervisor_mac_oui_is_a_virtual_machine_only_as_a_last_resort():
    assert guess_role("", None, "BC:24:11:92:89:72") == "virtual-machine"
    assert guess_role("", None, "bc2411928972") == "virtual-machine"
    assert guess_role("", {"ssh_banner": "SSH-2.0-OpenSSH Ubuntu"}, "BC:24:11:00:00:01") == "server"
    assert guess_role("", None, "84:A9:3E:8A:DF:42") == ""
