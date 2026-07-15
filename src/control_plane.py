# Dependency self-heal — MUST run before the third-party imports below. A skewed
# auto-update / partial install can leave the venv missing a declared dep, which
# would hard-crash at import and crash-loop the unit under Restart=always.
# dep_guard is stdlib-only; it find_spec-checks requirements.txt and pip-installs
# any missing. Best-effort — an unavailable dep_guard is skipped, never fatal.
import os as _os
try:
    try:
        from core.src.dep_guard import ensure_requirements as _ensure_requirements
    except ImportError:
        from dep_guard import ensure_requirements as _ensure_requirements
    _ensure_requirements(_os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "requirements.txt"))
except Exception:
    pass

import asyncio
import logging
import argparse
import os
try:
    from core.src.messaging.control_plane import BaseControlPlane
except ImportError:
    from messaging.control_plane import BaseControlPlane
from netbox_spoke import NetboxSpoke
from dotenv import load_dotenv

try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        import logging as _logging
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
configure_logging()
logger = logging.getLogger("NetboxControlPlane")


class NetboxControlPlane(BaseControlPlane):
    """Control plane for the NetBox documentation spoke.

    Loads NetBox/KEA connection config from .env, registers the NetboxSpoke module,
    starts the background NetBox -> KEA DHCP sync, and runs the Hub control loop.
    """

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "ipam"
        load_dotenv()
        self.config = {
            "netbox_url": os.getenv("NETBOX_URL", "http://localhost:8000"),
            "api_token": os.getenv("NETBOX_API_TOKEN", ""),
            "kea_ctrl_url": os.getenv("KEA_CTRL_URL", "http://localhost:8000"),
        }
        if not self.config["api_token"]:
            logger.warning(
                "NETBOX_API_TOKEN is not set. NetBox API calls will fail until it is "
                "configured in .env (must match a valid NetBox API token)."
            )

    def get_service_name(self) -> str:
        """Systemd service name the Hub restarts on self-update."""
        return "lm-netbox"

    async def run(self):
        """Native LM Spoke behavior: register the NetBox module and start KEA sync."""
        logger.info(f"Starting NetBox Module in HUB MODE -> {self.hub_url}")
        netbox_spoke = NetboxSpoke(self.spoke_id, self.config, control_plane=self)
        self.register_module("netbox", netbox_spoke)
        await netbox_spoke.start_kea_sync()
        try:
            await super().run()
        finally:
            await netbox_spoke.stop_kea_sync()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", default=os.getenv("SPOKE_SECRET", ""),
                        help="Authentication secret (omit for zero-touch provisioning)")
    parser.add_argument("--hub-secret", nargs='?', default="", const="", help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", required=True, help="Hub WebSocket URL")
    args = parser.parse_args()

    cp = NetboxControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    asyncio.run(cp.run())