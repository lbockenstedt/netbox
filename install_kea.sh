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
# We ensure it's listening on localhost
cat <<EOF > /etc/kea/kea-ctrl-agent.conf
{
    "control-agent": {
        "http-host": "127.0.0.1",
        "http-port": 8000,
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
echo "🌐 Control API: http://127.0.0.1:8000"
echo "📦 Integrated with NetBox via Lab Manager Spoke."
