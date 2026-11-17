# NetBox certificate delivery: Hub → IPAM spoke → certificate device

The NetBox server keeps its **hub-connected `lm-agent`** for NetBox SSO/admin
operations. Do not reinstall that service in device mode.

## Initial listener setup (IPAM spoke host)

The generic agent's `netbox` role and standalone `lm-netbox` both use the shared
`AgentHostingControlPlane`. Opt in to the NetBox-specific listener:

```bash
sudo bash setup-agent-host.sh --service lm-agent --port 8444 \
  --tls-cert /path/to/listener-fullchain.pem --tls-key /path/to/listener-key.pem
```

For a standalone spoke use `--service lm-netbox`. The script preserves
`/etc/lm-netbox-agent/config.json` and writes only a service drop-in containing
`LM_NETBOX_AGENT_LISTENER`, `LM_NETBOX_AGENT_PORT`, `LM_NETBOX_TLS_CERT` and
`LM_NETBOX_TLS_KEY`. It validates the certificate/key and expiry, then restarts
the chosen service (failure exits nonzero). It no longer creates a CN-only
self-signed certificate or asks an agent to use unverified TLS.

Use an existing trusted listener certificate, or a separately provisioned CA
and SAN certificate. Its SAN must cover the exact DNS name or private IP in
the configured URL. Do not copy a PSK or display a private key. The listener
uses its own approval PSK, never Proxmox's config/PSK.
Changing or clearing the association cancels old replay and serializes with
direct installs. An RPC already dispatched cannot be recalled from a device;
the change is acknowledged only after the in-flight delivery stops or completes.

With the updated shared LM framework, the IPAM listener checks for changed TLS
files every 60 seconds and rebinds only after the new pair validates. Connected
devices briefly reconnect; invalid or missing files are retried while the old
listener remains up. Update shared LM before the standalone spoke; an older
framework logs that the watcher is unavailable and needs a service restart
after renewal. The renewed name and issuer must remain trusted by the device.

## Managed server channel (existing Load Role UI/API)

Deploy the updated NetBox role before enabling its listener. The updated generic
host refuses to expose older, unscoped certificate-broadcasting role code.

On the NetBox server's hub-agent row, Load Role → **NetBox Server** accepts:

```json
{
  "role": "netbox-server",
  "config": {
    "cert_spoke_url": "wss://spoke.example.com:8444/ws/agent",
    "cert_ca_pem": "<trusted CA certificate PEM, not a private key>"
  }
}
```

This is the existing `LOAD_ROLE` payload, relayed by the existing authorized
agent load-role API. A running NetBox install is not reinstalled: the same
operation configures/repairs its certificate channel and leaves SSO intact.
New installs configure the channel only after the app installer succeeds.
Read the deploy status for background installation failures; starting a
deployment is not completion.

The separate `lm-netbox-cert-agent.service` runs the **existing generic
device-mode agent** from `/opt/lm/agent`, via
`control_plane.py --device-config /etc/lm-netbox-cert-agent/channel.json`.
Its ID is `<base-hub-agent-id>-netbox-cert`. The directory is 0700, its runtime
config is 0600, and each destination URL gets independent CA/PSK/install-UUID
state. Changing the URL requires explicit CA input; a previously unseen URL
starts unapproved and never receives the old spoke's PSK. Omitted values preserve saved settings.
Unchanged re-loads start rather than restart the service. Agent/NetBox
installer reruns do not replace this independent unit or its saved channel.

The channel checks the CA and hostname on every connection and never enters
device mode's legacy unverified-TLS recovery. Public and private URLs work;
the deployment must supply reachability and a matching SAN. Private IPs are
intentionally allowed for this device endpoint; it is not a hub web fetch.

## Associate, approve, deploy

1. In Setup → IPAM, edit the intended instance, already bound to its intended
   IPAM spoke. Select the certificate agent by name in **Certificate agent**
   (the dropdown stores its exact `cert_agent_id`). Save. The field is delivered both on save and reconnect;
   the spoke persists it for offline restarts. Blank disables delivery.
2. Approve the separate pending device on that IPAM spoke through the existing
   WebUI approval flow. Pending/unrelated agents receive no certificate.
3. Redeploy the current LE certificate to that exact IPAM target.

`INSTALL_CERT {domain, fullchain, privkey}` validates the pair, persists it
encrypted and sends acknowledged `WRITE_FILE` commands followed by
`RUN_COMMAND sudo -n /usr/local/bin/lm-netbox-install-cert …`. The helper validates,
swaps nginx's cert/key, tests nginx, and reloads. Any failed/unacknowledged write,
bad helper reply, failed validation or reload returns ERROR. An offline/unset
association returns DEFERRED, never cached-only SUCCESS.

On authenticated reconnect, cached replay is scheduled **after registration
returns**, allowing the listener to read RPC replies. It returns/logs its
actual result, not a fire-and-forget success. Old peers lacking WRITE_FILE or
RUN_COMMAND must be updated; their error is not treated as an install.
Unrelated connected agents are never selected as a fallback.

Historical cached-only SUCCESS entries in the hub ledger need an explicit
redeploy after setup. Reconnect replay repairs the host but does not rewrite
that ledger or the existing certificate-monitor telemetry. Verify nginx's
served leaf/expiry and check SSO/login after deployment.

## Trust and rollback caveats

- Trust for this channel is independent of NetBox nginx's expiring leaf. Keep
  the listener certificate/CA valid; an expired listener fails closed and needs
  renewal through its existing management path, not unverified bootstrap.
- This managed channel uses verified server TLS plus the existing PSK approval
  framework. It does not provision/present a client certificate; a listener
  enforcing **strict mTLS** is not supported by this provisioning path.
- A provisioning/service failure is ERROR and restores previous config files.
  Inspect systemd status before retrying; failure is never reported as deployed.
- All live setup, reachability changes, approvals and verification are operator
  work; source tests do not contact live systems.
