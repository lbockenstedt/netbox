"""Guess a device role for a discovered device from its hostname and probe profile.

Feeders (ARP, DHCP, LLDP, the nw ping/scan) often know only a hostname, and the
nw scanner adds a ``profile`` of what the host answered with (SNMP sysDescr,
HTTP title/server/realm, SSH banner). A device with nothing better stays on the
generic 'discovered' role; this turns those clues into a real role slug.

Order: specific profile evidence, then hostname tokens, then generic OS/web
server evidence ("server"). Returns "" when nothing matches.
"""
import re
from typing import Any, Dict, List, Tuple

# (role slug, pattern) checked against the lower-cased profile text.
_PROFILE_RULES: List[Tuple[str, str]] = [
    ("bmc", r"integrated lights-out|\bilo ?\d\b|idrac|megarac|\bipmi\b|\bbmc\b"),
    ("pdu", r"rack pdu|\bpdu\b|raritan|servertech|sentry3?\b"),
    ("ups", r"\bups\b|smart-ups|network management card"),
    ("console-server", r"opengear|lantronix|consolepi|avocent|console server"),
    ("access-point", r"access point|instant ap|\bap-?\d{3}\b|unifi ap"),
    ("firewall", r"fortigate|fortinet|pfsense|opnsense|palo alto|sonicwall|\bsrx\d*\b"),
    ("gateway", r"mobility (?:controller|gateway|conductor)|aruba gateway"),
    ("switch", r"aos-cx|arubaos-cx|procurve|\bjunos\b|cisco ios|nx-os|\bswitch\b"),
    ("hypervisor", r"proxmox|pve-manager|esxi|vmware|hyper-v"),
    ("storage", r"truenas|freenas|synology|netapp|nimble|qnap|storage array"),
    ("printer", r"laserjet|jetdirect|officejet|printer|xerox|ricoh|lexmark"),
    ("camera", r"camera|hikvision|dahua|\baxis\b"),
]
_SERVER_RULE = r"ubuntu|debian|centos|red hat|rocky linux|almalinux|windows server|microsoft-iis|nginx|apache"

# (role slug, pattern) matched against each whole hostname token (upper case).
_HOSTNAME_RULES: List[Tuple[str, str]] = [
    ("bmc", r"ILO\w*|IDRAC\w*|BMC\d*|IPMI\d*"),
    ("pdu", r"PDU\w*"),
    ("ups", r"UPS\d*"),
    ("console-server", r"CON\d*|CONSOLE\w*"),
    ("access-point", r"I?AP\d*"),
    ("firewall", r"[A-Z]*FW\d*|FIREWALL\d*|SRX\d*"),
    ("gateway", r"[A-Z]*GW\d*|GATEWAY\d*|VPNC\d*"),
    ("switch", r"[A-Z]*SW\d*|SWITCH\d*|TOR\d*|AGG\d*|CORE\d*|6[234]00[A-Z]?|8[34]60|29[34]0[A-Z]?"),
    ("hypervisor", r"PXSVR\d*|PXMX\d*|PROXMOX\d*|PVE\d*|ESXI?\d*|HYPERV\d*"),
    ("storage", r"NIMBLE\w*|NAS\d*|SAN\d*|TRUENAS\w*|NETAPP\w*|SYNOLOGY\w*|MSA\d*"),
    ("printer", r"PRN\d*|PRINTER\d*|MFP\d*"),
    ("camera", r"CAM\d*|CAMERA\d*"),
    ("sensor", r"UXI\d*"),
    ("phone", r"IPHONE\w*|ANDROID\w*|PIXEL\w*|GALAXY\w*"),
    ("workstation", r"MACBOOK\w*|LAPTOP\w*|DESKTOP\w*|IMAC\w*|WORKSTATION\w*"),
    ("server", r"SVR\d*|SRV\d*|SERVER\d*|DC\d+"),
]

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def profile_text(profile: Any) -> str:
    """All probe strings of a profile as one lower-cased text."""
    if not isinstance(profile, dict):
        return ""
    keys = ("sys_descr", "http_title", "http_server", "http_realm", "ssh_banner")
    return " ".join(str(profile.get(k) or "") for k in keys).lower()


def hostname_tokens(hostname: str) -> List[str]:
    """Upper-cased tokens of the short hostname (domain suffix dropped)."""
    name = str(hostname or "").strip()
    if not name or _IP_RE.match(name):
        return []
    return [t for t in re.split(r"[^A-Za-z0-9]+", name.split(".")[0].upper()) if t]


def guess_role(hostname: str = "", profile: Dict[str, Any] = None) -> str:
    """Role slug for a device from its hostname + probe profile, or ""."""
    text = profile_text(profile)
    for role, pattern in _PROFILE_RULES:
        if text and re.search(pattern, text):
            return role
    tokens = hostname_tokens(hostname)
    for role, pattern in _HOSTNAME_RULES:
        if any(re.fullmatch(pattern, t) for t in tokens):
            return role
    if text and re.search(_SERVER_RULE, text):
        return "server"
    return ""
