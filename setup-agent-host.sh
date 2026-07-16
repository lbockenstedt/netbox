#!/usr/bin/env bash
# setup-agent-host.sh — provision the ipam/NetBox SPOKE to host the NetBox-host
# Agent's /ws/agent listener (tiered Hub→Spoke→Agent cert delivery).
#
# Run this ON THE SPOKE BOX (the one running the lm-netbox systemd service).
# It is idempotent and non-destructive:
#   1) generates a shared agent_secret → /etc/lm-netbox-agent/config.json (0600)
#   2) ensures a TLS cert for the /ws/agent listener (reuses the NetBox nginx cert
#      if present on this box; else self-signs one — the Agent connects
#      unverified until mTLS is armed)
#   3) drops a systemd override that enables the listener on the lm-netbox unit
#      (LM_NETBOX_AGENT_LISTENER=1, LM_NETBOX_AGENT_PORT=8444, LM_TLS_CERT/KEY)
#   4) daemon-reload + restart lm-netbox
#
# Then install the Agent on the NetBox host in device mode (see DEPLOY.md):
#   Style 1 (split):     install_agent.sh --spoke-url wss://<this-spoke>:8444/ws/agent
#   Style 2 (all-in-one) install_agent.sh --spoke-url ws://127.0.0.1:8444/ws/agent
#
# Usage:  sudo bash setup-agent-host.sh [--port 8444] [--service lm-netbox]
set -euo pipefail

PORT=8444
SERVICE="lm-netbox"
CONFIG_DIR="/etc/lm-netbox-agent"
CONFIG_JSON="$CONFIG_DIR/config.json"
TLS_DIR="/etc/lm/netbox/agent-tls"
NGINX_CRT="/etc/lm/netbox/tls/netbox.crt"
NGINX_KEY="/etc/lm/netbox/tls/netbox.key"

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --port)    PORT="$2";    shift ;;
        --service) SERVICE="$2"; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ "$(id -u)" -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

# 1) Shared agent onboarding secret (used as PSK + frame-signing key).
mkdir -p "$CONFIG_DIR"; chmod 0700 "$CONFIG_DIR"
if [[ ! -s "$CONFIG_JSON" ]]; then
    SECRET="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-40)"
    printf '{"agent_secret": "%s"}\n' "$SECRET" > "$CONFIG_JSON"
    chmod 0600 "$CONFIG_JSON"
    echo "generated agent_secret → $CONFIG_JSON"
else
    echo "agent_secret already present → $CONFIG_JSON (unchanged)"
fi

# 2) TLS cert for the /ws/agent listener.
if [[ -s "$NGINX_CRT" && -s "$NGINX_KEY" ]]; then
    CRT="$NGINX_CRT"; KEY="$NGINX_KEY"
    echo "using existing NetBox cert for /ws/agent: $CRT"
else
    mkdir -p "$TLS_DIR"; chmod 0700 "$TLS_DIR"
    CRT="$TLS_DIR/agent.crt"; KEY="$TLS_DIR/agent.key"
    if [[ ! -s "$CRT" || ! -s "$KEY" ]]; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -subj "/CN=lm-netbox-agent-listener" \
            -keyout "$KEY" -out "$CRT" >/dev/null 2>&1
        chmod 0600 "$KEY" "$CRT"
        echo "self-signed /ws/agent cert → $CRT (replace with the LE wildcard for mTLS)"
    fi
fi

# 3) systemd override enabling the listener on the spoke unit.
DROPIN_DIR="/etc/systemd/system/${SERVICE}.service.d"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/agent-listener.conf" <<EOF
[Service]
Environment=LM_NETBOX_AGENT_LISTENER=1
Environment=LM_NETBOX_AGENT_PORT=$PORT
Environment=LM_TLS_CERT=$CRT
Environment=LM_TLS_KEY=$KEY
EOF
echo "wrote $DROPIN_DIR/agent-listener.conf (listener on :$PORT)"

# 4) apply.
systemctl daemon-reload
systemctl restart "$SERVICE" 2>/dev/null || echo "note: could not restart $SERVICE (start it manually)"

cat <<DONE

ipam spoke is now an agent host on :$PORT.
Copy the agent_secret to the NetBox-host Agent install:
    sudo cat $CONFIG_JSON
Then on the NetBox host:
    curl -fsSL <lm>/agent/install_agent.sh | sudo bash -s -- \\
        --spoke-url wss://<this-spoke>:$PORT/ws/agent --secret <agent_secret>
Approve the Agent in the WebUI (Setup → Spokes & Agents), then deploy the cert
from the LE module (target: ipam) — the spoke installs it via the Agent.
DONE
