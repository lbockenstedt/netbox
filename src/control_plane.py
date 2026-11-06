"""Process entry point for the NetBox (IPAM) spoke.

Self-heals missing Python dependencies, configures logging, then builds a
NetboxControlPlane that connects to the Lab Manager Hub, registers the
NetboxSpoke module, runs the NetBox -> KEA DHCP sync and (optionally) hosts
the NetBox-host Agent listener.
"""
# Dependency self-heal — MUST run before the third-party imports below. A skewed
# auto-update / partial install can leave the venv missing a declared dep, which
# would hard-crash at import and crash-loop the unit under Restart=always.
# dep_guard is stdlib-only; it find_spec-checks requirements.txt and pip-installs
# any missing. Best-effort — an unavailable dep_guard is skipped, never fatal.
# Stdlib os under a private alias, used only by the dependency self-heal below.
import os as _os
# Wrap the whole self-heal in a try so any failure in it can never stop startup.
try:
    # Prefer the package-style import (repo checkout); fall back to a flat import
    # when the spoke is installed with core/src copied next to it.
    try:
        from core.src.dep_guard import ensure_requirements as _ensure_requirements
    except ImportError:
        from dep_guard import ensure_requirements as _ensure_requirements
    # Point the guard at this spoke's requirements.txt (one directory above src/).
    _ensure_requirements(_os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "requirements.txt"))
# Self-heal is best-effort: if it fails the real imports below will report the problem.
except Exception:
    pass

# asyncio runs the control plane's event loop.
import asyncio
# Standard-library logging for status and error messages.
import logging
# argparse reads the --id/--secret/--hub command-line options.
import argparse
# os reads configuration from environment variables.
import os
# Base class that provides the Hub connection and the /ws/agent listener; try the
# repo layout first, then the flat installed layout.
try:
    from core.src.messaging.agent_hosting import AgentHostingControlPlane
except ImportError:
    from messaging.agent_hosting import AgentHostingControlPlane
# The NetBox module that does the actual IPAM/DCIM work.
from netbox_spoke import NetboxSpoke
# load_dotenv pulls NETBOX_URL / tokens from the spoke's .env file into os.environ.
from dotenv import load_dotenv

# Shared logging configuration: try the flat install path, then the repo path.
try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        # Neither copy of logging_setup is available: define a minimal stand-in so the
        # spoke still logs with the same format.
        import logging as _logging
        # Same line format / date format as the shared logging_setup module.
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            """Fallback logging setup: console (and optional file) output at the given level."""
            # Log to file + console when a log file is given; None lets basicConfig use stderr.
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            # force=True replaces any handlers a library may already have installed.
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
# Configure logging once at import so every later message is formatted consistently.
configure_logging()
# Named logger so control-plane messages can be filtered separately.
logger = logging.getLogger("NetboxControlPlane")


class NetboxControlPlane(AgentHostingControlPlane):
    """Control plane for the NetBox documentation spoke.

    Loads NetBox/KEA connection config from .env, registers the NetboxSpoke module,
    starts the background NetBox -> KEA DHCP sync, and runs the Hub control loop.

    Also an **agent host** (like the pxmx spoke): it serves a ``/ws/agent``
    listener so the NetBox-host Agent dials THIS spoke (never the hub). The spoke
    holds the cert-install logic and drives the dumb Agent with WRITE_FILE +
    RUN_COMMAND. See NetboxSpoke (cert custodian) + _on_agent_registered.
    """

    # NetBox-unique agent-host knobs (must NOT collide with pxmx's 8443/8766 or its
    # env/config path on a co-located box). Opt-in: the listener only runs when
    # LM_NETBOX_AGENT_LISTENER=1, so existing API-only ipam spokes are unaffected.
    MODULE_TYPE = "ipam"
    AGENT_PORT_ENV = "LM_NETBOX_AGENT_PORT"
    AGENT_LOOPBACK_ENV = "LM_NETBOX_AGENT_LOOPBACK"
    AGENT_LISTENER_ENV = "LM_NETBOX_AGENT_LISTENER"
    AGENT_CONFIG_PATH = "/etc/lm-netbox-agent/config.json"
    AGENT_LISTENER_OPT_IN = True
    AGENT_LOOPBACK_PORT = 8444
    AGENT_WSS_PORT = 8444
    AGENT_FALLBACK_PORT = 8767

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        """Store Hub identity and load NetBox/KEA connection settings from .env."""
        # Let the base class set up identity, secrets and the Hub URL.
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        # Tell the Hub this spoke provides the "ipam" module type.
        self.module_type = "ipam"
        self._netbox_spoke = None  # set in run(); used by _on_agent_registered
        # Read the spoke's .env so the os.getenv calls below see its settings.
        load_dotenv()
        # Connection settings for NetBox and the KEA control agent, with local defaults.
        self.config = {
            "netbox_url": os.getenv("NETBOX_URL", "http://localhost:8000"),
            "api_token": os.getenv("NETBOX_API_TOKEN", ""),
            "kea_ctrl_url": os.getenv("KEA_CTRL_URL", "http://localhost:8000"),
        }
        # Without a token every NetBox API call is rejected, so warn loudly at startup.
        if not self.config["api_token"]:
            logger.warning(
                "NETBOX_API_TOKEN is not set. NetBox API calls will fail until it is "
                "configured in .env (must match a valid NetBox API token)."
            )

    def get_service_name(self) -> str:
        """Systemd service name the Hub restarts on self-update."""
        return "lm-netbox"

    async def run(self):
        """Native LM Spoke behavior: register the NetBox module and start KEA sync.
        Also starts the ``/ws/agent`` listener when enabled so the NetBox-host
        Agent can dial this spoke."""
        # Announce which Hub this spoke is connecting to.
        logger.info(f"Starting NetBox Module in HUB MODE -> {self.hub_url}")
        # Build the NetBox module and keep a reference for the agent-registration hook.
        netbox_spoke = NetboxSpoke(self.spoke_id, self.config, control_plane=self)
        self._netbox_spoke = netbox_spoke
        # Register it so the Hub can route "netbox" commands to it.
        self.register_module("netbox", netbox_spoke)
        # Start the background NetBox -> KEA DHCP reservation sync.
        await netbox_spoke.start_kea_sync()
        # Scheduled NW inventory -> NetBox apply (inventory is held on disk).
        await netbox_spoke.start_nw_flush()
        # Agent host: serve /ws/agent so the NetBox-host Agent dials us (Style 1
        # remote wss / Style 2 loopback). Gated by AGENT_LISTENER_OPT_IN + the env.
        if self._agent_listener_enabled():
            self._start_agent_server_task()
            logger.info("NetBox agent listener started (spoke hosts the NetBox-host Agent).")
        # Run the Hub control loop until it exits; always stop the KEA sync afterwards.
        try:
            await super().run()
        finally:
            await netbox_spoke.stop_kea_sync()
            await netbox_spoke.stop_nw_flush()

    async def _on_agent_registered(self, agent_id: str):
        """New-device-connect trigger: when the NetBox-host Agent connects+approves,
        deploy the spoke's cached cert to it immediately (cert-custodian model)."""
        # Only possible once run() has created the NetBox module.
        if self._netbox_spoke is not None:
            try:
                await self._netbox_spoke.deploy_cached_cert_to_agent(agent_id)
            # Cert push failure must not block the agent from registering.
            except Exception as e:  # noqa: BLE001 - never break registration
                logger.debug("cert auto-deploy on agent %s connect skipped: %s", agent_id, e)


# Script entry point: parse the command line and run the control plane.
if __name__ == "__main__":
    # Command-line options: spoke identity, auth secrets and the Hub URL.
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", default=os.getenv("SPOKE_SECRET", ""),
                        help="Authentication secret (omit for zero-touch provisioning)")
    parser.add_argument("--hub-secret", nargs='?', default="", const="", help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", required=True, help="Hub WebSocket URL")
    # Parse argv into an args namespace.
    args = parser.parse_args()

    # Build the control plane and run it until it stops.
    cp = NetboxControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    asyncio.run(cp.run())