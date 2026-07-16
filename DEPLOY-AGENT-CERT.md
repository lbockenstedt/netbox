# NetBox cert delivery — deployment (tiered Hub → Spoke → Agent)

The cert is delivered to the **ipam spoke**, which holds all the install logic and
drives a dumb **Agent** on the NetBox host to place the cert + reload nginx. The
Agent only ever talks to the spoke (never the hub) and only runs generic
primitives (`RUN_COMMAND` / `WRITE_FILE`). The `netbox-server` role still
provisions nginx + the `/usr/local/bin/lm-netbox-install-cert` helper.

Two supported topologies:

| | Style 1 — split | Style 2 — all-in-one |
|---|---|---|
| ipam spoke | box A | same box |
| NetBox + Agent | box B | same box |
| Agent → spoke | `wss://A:8444/ws/agent` | `ws://127.0.0.1:8444/ws/agent` |
| Hub → NetBox host | **not required** | n/a |

Both use the same two steps.

## 1) Make the ipam spoke an agent host (on the spoke box)

```bash
sudo bash netbox/setup-agent-host.sh        # port 8444, service lm-netbox
sudo cat /etc/lm-netbox-agent/config.json   # copy the agent_secret
```
This generates the shared `agent_secret`, ensures a TLS cert for `/ws/agent`
(reuses the NetBox nginx cert if present, else self-signs), enables the listener
(`LM_NETBOX_AGENT_LISTENER=1`, `LM_NETBOX_AGENT_PORT=8444`) via a systemd
override, and restarts `lm-netbox`. It's opt-in — existing ipam spokes are
untouched until you run it.

## 2) Install the Agent on the NetBox host (device mode)

Ensure the NetBox host has the `netbox-server` role (provisions nginx + the cert
helper), then install the Agent pointed at the spoke:

**Style 1 (split):**
```bash
curl -fsSL <lm>/agent/install_agent.sh | sudo bash -s -- \
    --spoke-url wss://<spoke-A>:8444/ws/agent --secret <agent_secret>
```

**Style 2 (all-in-one):**
```bash
curl -fsSL <lm>/agent/install_agent.sh | sudo bash -s -- \
    --spoke-url ws://127.0.0.1:8444/ws/agent --secret <agent_secret>
```

Omit `--secret` for zero-touch: the Agent appears **pending** in the WebUI
(Setup → Spokes & Agents); approve it and the spoke pushes the secret.

## 3) Deploy the cert

From the LE module, deploy to target **ipam**. The spoke validates, caches it
(Fernet-encrypted), and installs it on the NetBox host via the Agent. Any Agent
that connects later auto-gets the current cached cert.

## mTLS (optional, later)

Once the LE **wildcard** is distributed to the hub + spokes + this `/ws/agent`
listener (replace the self-signed cert), open **System → Active Sessions →
Mutual TLS**. When the readiness dot is green, flip **Enable mutual TLS**. If a
cert later expires, the Agent self-heals: it falls back to the PSK channel just
long enough for the spoke to re-deploy a fresh cert, then resumes mTLS.

## Notes / troubleshooting

- Spoke listener port is **8444** (distinct from pxmx's 8443). Pass the port in
  the `--spoke-url`; `--spoke-ip <host>` alone assumes `:443`.
- The spoke needs `/etc/lm-netbox-agent/config.json` (step 1) and the NetBox host
  needs the same `agent_secret` (step 2) — or use zero-touch approval.
- Hub → NetBox host reachability is **not** required in Style 1; the Agent dials
  the spoke, and everything relays up through the spoke.
