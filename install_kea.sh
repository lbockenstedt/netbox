#!/bin/bash
set -e

echo "🚀 Installing KEA DHCP Server for NetBox Integration..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

# 1. Update and Install KEA
apt-get update
apt-get install -y kea-dhcp4-server kea-ctrl-agent jq

echo "⚙️ Configuring KEA Control Agent..."
# The control agent allows updating the DHCP server config via API without restarts
# We ensure it's listening on localhost. Port 8760 (NOT 8000): the LM hub owns
# 0.0.0.0:8000 (admin WebUI/API) on any box where a spoke runs in HUB MODE
# (notably the netbox spoke, which is hub-colocated). KEA CA's default 8000
# collides with the hub → kea-ctrl-agent fails to bind and the netbox spoke's
# KEA sync loop POSTs the hub instead, which 405s ("KEA rejected scope: None").
KEA_CA_PORT=8760
cat <<EOF > /etc/kea/kea-ctrl-agent.conf
{
    "control-agent": {
        "http-host": "127.0.0.1",
        "http-port": ${KEA_CA_PORT},
        "logging": {
            "severity": "INFO",
            "facility": "local7"
        }
    }
}
EOF

echo "⚙️ Configuring KEA DHCP4 Server..."
# Basic config that uses the control agent and starts empty
cat <<EOF > /etc/kea/kea-dhcp4.conf
{
    "Dhcp4": {
        "interfaces-config": {
            "interfaces": [
                {
                    "subnet-interface": "eth0"
                }
            ]
        },
        "logs": [
            {
                "type": "syslog",
                "severity": "INFO",
                "facility": "local7"
            }
        ],
        "subnet-points": []
    }
}
EOF

# Note: 'subnet-points' is not a real KEA key, I'll use 'subnets'
# Let's use a valid minimal config
cat <<EOF > /etc/kea/kea-dhcp4.conf
{
    "Dhcp4": {
        "interfaces-config": {
            "interfaces": [
                {
                    "subnet-interface": "eth0"
                }
            ]
        },
        "logs": [
            {
                "type": "syslog",
                "severity": "INFO",
                "facility": "local7"
            }
        ],
        "subnets": []
    }
}
EOF

# 2. Enable and Start Services
systemctl daemon-reload
systemctl enable kea-ctrl-agent
systemctl restart kea-ctrl-agent
systemctl enable kea-dhcp4-server
systemctl restart kea-dhcp4-server

echo "✅ KEA DHCP Server and Control Agent installed and running."
echo "🌐 Control API: http://127.0.0.1:${KEA_CA_PORT}"
echo "📦 Integrated with NetBox via Lab Manager Spoke."
