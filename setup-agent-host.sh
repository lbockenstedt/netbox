#!/usr/bin/env bash
# setup-agent-host.sh — provision the ipam/NetBox SPOKE to host the NetBox-host
# Agent's /ws/agent listener (tiered Hub→Spoke→Agent cert delivery).
#
# Run this ON THE SPOKE BOX (lm-netbox, or lm-agent hosting the NetBox role).
# It is idempotent and non-destructive:
#   1) generates a shared agent_secret → /etc/lm-netbox-agent/config.json (0600)
#   2) ensures a TLS cert for the /ws/agent listener (reuses the NetBox nginx cert
#      if present on this box; else requires explicit trusted cert/key paths)
#   3) drops a systemd override that enables the listener on the lm-netbox unit
#      (LM_NETBOX_AGENT_LISTENER=1, LM_NETBOX_AGENT_PORT=8444, LM_NETBOX_TLS_CERT/KEY)
#   4) daemon-reload + restart lm-netbox
#
# Then use Load Role / netbox-server to configure its separate verified
# certificate channel. Do not replace the main hub agent (DEPLOY-AGENT-CERT.md).
#
# Usage: sudo bash setup-agent-host.sh [--port 8444] [--service lm-netbox] \
#          --tls-cert /absolute/server.crt --tls-key /absolute/server.key
set -euo pipefail

PORT=8444
SERVICE="lm-netbox"
CONFIG_DIR="/etc/lm-netbox-agent"
CONFIG_JSON="$CONFIG_DIR/config.json"
NGINX_CRT="/etc/lm/netbox/tls/netbox.crt"
NGINX_KEY="/etc/lm/netbox/tls/netbox.key"
CRT=""; KEY=""

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --port)    PORT="$2";    shift ;;
        --service) SERVICE="$2"; shift ;;
        --tls-cert) CRT="$2"; shift ;;
        --tls-key) KEY="$2"; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ "$(id -u)" -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
# Validate before writing any config. No insecure bootstrap fallback.
[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -gt 0 && "$PORT" -le 65535 ]] || { echo "invalid port" >&2; exit 2; }
[[ "$SERVICE" =~ ^[a-zA-Z0-9_.-]+$ ]] || { echo "invalid service" >&2; exit 2; }
CRT="${CRT:-$NGINX_CRT}"; KEY="${KEY:-$NGINX_KEY}"
[[ "$CRT" =~ ^/[a-zA-Z0-9_./-]+$ && "$KEY" =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo "invalid TLS paths" >&2; exit 2; }
[[ -s "$CRT" && -s "$KEY" ]] || { echo "supply --tls-cert and --tls-key for this listener" >&2; exit 2; }
openssl x509 -in "$CRT" -checkend 0 -noout || { echo "listener certificate expired" >&2; exit 2; }
python3 - "$CRT" "$KEY" <<'PY'
import ssl
import sys
ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(sys.argv[1], sys.argv[2])
PY

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

# 3) systemd override enabling the listener on the spoke unit.
DROPIN_DIR="/etc/systemd/system/${SERVICE}.service.d"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/netbox-agent-listener.conf" <<EOF
[Service]
Environment=LM_NETBOX_AGENT_LISTENER=1
Environment=LM_NETBOX_AGENT_PORT=$PORT
Environment=LM_NETBOX_TLS_CERT=$CRT
Environment=LM_NETBOX_TLS_KEY=$KEY
EOF
echo "wrote $DROPIN_DIR/netbox-agent-listener.conf (listener on :$PORT)"

# 4) apply.
systemctl daemon-reload
systemctl restart "$SERVICE"

cat <<DONE

ipam spoke is now an agent host on :$PORT.
Keep the NetBox host's hub agent intact. Configure netbox-server's separate
certificate channel with cert_spoke_url + trusted cert_ca_pem in Load Role.
Set that channel's exact cert_agent_id on the matching IPAM instance, approve
the channel in the WebUI, then redeploy the current LE certificate.
See DEPLOY-AGENT-CERT.md. No PSK needs to be copied or displayed.
DONE
