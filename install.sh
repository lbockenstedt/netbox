#!/bin/bash
set -euo pipefail

# ============================================================
# Lab Manager — NetBox IPAM Installer
#
# Installs NetBox (PostgreSQL, Redis, gunicorn, nginx) and the
# LM NetBox spoke in one shot. Safe to re-run: updates code,
# runs migrations, restarts services — never overwrites
# existing credentials or database content.
#
# Quick start:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/netbox/main/install.sh \
#     | sudo bash -s -- --hub ws://LM_HUB_IP:8765
#
# With an existing NetBox instance (skip app install):
#   curl -sSL ... | sudo bash -s -- \
#     --hub ws://LM_HUB_IP:8765 \
#     --netbox-url http://existing-netbox --netbox-token NETBOX_API_TOKEN
# ============================================================

# ── Defaults ─────────────────────────────────────────────────
HUB_URL="ws://localhost:8765"
SPOKE_ID="netbox-spoke-1"
SPOKE_SECRET=""
HUB_SECRET=""
ADMIN_TOKEN=""
NETBOX_URL=""          # Set to skip local NetBox install
NETBOX_TOKEN=""        # Pre-existing token; auto-generated if empty
SPOKE_ONLY=false       # --spoke-only: skip app install, just wire up the LM spoke
NB_VERSION="stable"    # "stable" → latest GitHub release tag
DB_NAME="netbox"
DB_USER="netbox"
DB_PASS=""             # Auto-generated if empty
NB_SUPERUSER="admin"
NB_SUPERPASS=""        # Auto-generated if empty
NB_SUPERMAIL="admin@localhost"
SVC_USER="svc_lm"
NB_APP_DIR="/opt/netbox-app"   # NetBox application checkout
LM_DIR="/opt/lm"               # LM installation root
NB_PORT=8001                   # gunicorn bind port; nginx proxies :80 → this

# ── Argument parsing ─────────────────────────────────────────
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub)             HUB_URL="$2";       shift ;;
        --id|--name)       SPOKE_ID="$2";      shift ;;
        --secret)          SPOKE_SECRET="$2";  shift ;;
        --hub-secret)      HUB_SECRET="$2";    shift ;;
        --admin-token)     ;; # deprecated — zero-touch provisioning, no longer used
        --netbox-url)      NETBOX_URL="$2";    shift ;;
        --netbox-token)    NETBOX_TOKEN="$2";  shift ;;
        --db-pass)         DB_PASS="$2";       shift ;;
        --superuser)       NB_SUPERUSER="$2";  shift ;;
        --superpass)       NB_SUPERPASS="$2";  shift ;;
        --supermail)       NB_SUPERMAIL="$2";  shift ;;
        --netbox-version)  NB_VERSION="$2";    shift ;;
        --spoke-only)      SPOKE_ONLY=true ;;
        --all-prereqs) ;;  # no-op; accepted for LM hub compat
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# ── Guards ───────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || { echo "❌ Must be run as root (sudo)."; exit 1; }

# ── Helpers ──────────────────────────────────────────────────
GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GRN}✅  $*${NC}"; }
warn() { echo -e "${YLW}⚠️   $*${NC}"; }
die()  { echo -e "${RED}❌  $*${NC}"; exit 1; }
step() { echo -e "\n${GRN}━━  $*  ━━${NC}"; }

gen_secret() { python3 -c "import secrets; print(secrets.token_urlsafe(50))" 2>/dev/null \
               || openssl rand -base64 40 | tr -d '=+/\n'; }

step "Lab Manager — NetBox IPAM Installer"

# ── Determine if we install the NetBox application ───────────
INSTALL_APP=true
if [ "$SPOKE_ONLY" = true ]; then
    INSTALL_APP=false
    ok "Spoke-only mode — skipping local NetBox application install"
elif [ -n "$NETBOX_URL" ] && [ -n "$NETBOX_TOKEN" ]; then
    INSTALL_APP=false
    ok "External NetBox supplied — skipping local application install"
fi

# ── Generate passwords/keys for fresh installs ───────────────
[ -n "$DB_PASS" ]      || DB_PASS="$(gen_secret)"
[ -n "$NB_SUPERPASS" ] || NB_SUPERPASS="$(gen_secret)"

# ── Service user (shared with rest of LM) ────────────────────
if ! id "$SVC_USER" &>/dev/null; then
    useradd -r -s /bin/false -M "$SVC_USER"
    ok "Created service user $SVC_USER"
fi

# ============================================================
# A0. SELF-HEAL — verify/repair a LOCAL NetBox application
# ============================================================
# install_all.sh always runs this script with --spoke-only, which skips the
# full app install below. That left the NetBox app (gunicorn on 127.0.0.1:8001
# behind nginx on :80) unmanaged: once gunicorn died (OOM, postgres restart,
# stale venv, reboot without the unit enabled), nginx kept returning 502 and
# no production re-run ever repaired it. This block heals a LOCAL app even in
# spoke-only mode. An EXTERNAL NetBox (NETBOX_URL set to a real host) is left
# alone — we don't manage something we didn't install.
_is_local_netbox_url() {
    case "${NETBOX_URL:-}" in
        ""|"http://localhost"|"http://localhost/"|"http://127.0.0.1"|"http://127.0.0.1/")
            return 0 ;;
        *) return 1 ;;
    esac
}

_nb_health_code() {
    # Root '/' needs no token (LOGIN_REQUIRED is off). 200/302 = gunicorn up,
    # 502 = nginx up but gunicorn dead, 000 = nothing listening / curl missing.
    command -v curl >/dev/null 2>&1 || { echo "000"; return; }
    curl -sS -o /dev/null -w "%{http_code}" --max-time 10 http://localhost/ 2>/dev/null || echo "000"
}

if _is_local_netbox_url; then
    if [ -d "$NB_APP_DIR/netbox" ]; then
        step "Verifying local NetBox application health"
        _HC=$(_nb_health_code)
        if [ "$_HC" = "200" ] || [ "$_HC" = "302" ]; then
            ok "NetBox app healthy (http://localhost/ → $_HC)"
            # Ensure the app units are enabled so they survive reboots.
            systemctl enable netbox netbox-rq 2>/dev/null || true
        else
            warn "NetBox app unhealthy (http://localhost/ → $_HC). Attempting repair..."

            # Repair 1: restart gunicorn + rq workers (handles OOM/crash/reboot).
            systemctl daemon-reload 2>/dev/null || true
            systemctl enable netbox netbox-rq 2>/dev/null || true
            systemctl restart netbox netbox-rq 2>/dev/null || true
            sleep 4
            _HC=$(_nb_health_code)

            # Repair 2: still bad → re-run migrations + collectstatic, fix perms, restart.
            if [ "$_HC" != "200" ] && [ "$_HC" != "302" ]; then
                warn "Restart did not restore NetBox (→ $_HC). Re-running migrations..."
                set +e
                "$NB_APP_DIR/venv/bin/python3" "$NB_APP_DIR/netbox/manage.py" migrate --no-input -v 0 2>&1 | tail -5
                "$NB_APP_DIR/venv/bin/python3" "$NB_APP_DIR/netbox/manage.py" collectstatic --no-input -v 0 2>/dev/null
                set -e
                chown -R "$SVC_USER:$SVC_USER" "$NB_APP_DIR" 2>/dev/null || true
                systemctl restart netbox netbox-rq 2>/dev/null || true
                sleep 4
                _HC=$(_nb_health_code)
            fi

            if [ "$_HC" = "200" ] || [ "$_HC" = "302" ]; then
                ok "NetBox app repaired (http://localhost/ → $_HC)"
            else
                # Repair 3: escalate to a full application reinstall below.
                warn "NetBox app still unhealthy (→ $_HC). Escalating to full reinstall."
                INSTALL_APP=true
                SPOKE_ONLY=false
            fi
        fi
    else
        # Local app expected but never installed (fresh host, or app dir wiped).
        warn "Local NetBox expected but $NB_APP_DIR not found. Installing application."
        INSTALL_APP=true
        SPOKE_ONLY=false
    fi
fi

# ============================================================
# A. SYSTEM PACKAGES
# ============================================================
if [ "$INSTALL_APP" = true ]; then
    step "Installing system packages"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
        postgresql postgresql-contrib redis-server \
        python3 python3-venv python3-pip python3-dev \
        build-essential libxml2-dev libxslt1-dev libffi-dev \
        libpq-dev libssl-dev zlib1g-dev \
        nginx git curl jq
    ok "System packages ready"
fi

# ============================================================
# B. POSTGRESQL (idempotent)
# ============================================================
if [ "$INSTALL_APP" = true ]; then
    # ── Self-healing: detect and fix common existing-install issues ──────────
    step "Self-healing check"
    NB_CFG_EARLY="$NB_APP_DIR/netbox/netbox/configuration.py"

    # Fix 1: API_TOKEN_PEPPERS missing from an existing configuration.py
    if [ -f "$NB_CFG_EARLY" ] && ! grep -q "^API_TOKEN_PEPPERS" "$NB_CFG_EARLY"; then
        _PEPPER="$(gen_secret)"
        printf '\n# Required by NetBox v4 for v2 API token creation.\nAPI_TOKEN_PEPPERS = {\n    0: '"'"'%s'"'"',\n}\n' "$_PEPPER" >> "$NB_CFG_EARLY"
        ok "Self-heal: added API_TOKEN_PEPPERS to configuration.py"
        systemctl restart netbox netbox-rq 2>/dev/null || true
    fi

    # Fix 2: DB exists with wrong encoding (SQL_ASCII) — drop and recreate
    if systemctl is-active --quiet postgresql 2>/dev/null; then
        _DB_ENC=$(sudo -u postgres psql -Atc "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null || echo "")
        if [ -n "$_DB_ENC" ] && [ "$_DB_ENC" != "UTF8" ]; then
            warn "Self-heal: database '$DB_NAME' has encoding '$_DB_ENC' — recreating as UTF-8"
            sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid <> pg_backend_pid();" 2>/dev/null || true
            sudo -u postgres psql -c "DROP DATABASE $DB_NAME;" 2>/dev/null || true
        fi
    fi

    step "Configuring PostgreSQL"

    systemctl enable --now postgresql

    # Preserve existing DB password across re-runs
    EXISTING_DB_PASS=""
    NB_CFG="$NB_APP_DIR/netbox/netbox/configuration.py"
    if [ -f "$NB_CFG" ]; then
        EXISTING_DB_PASS=$(grep -oP "(?<='PASSWORD': ')[^']*" "$NB_CFG" 2>/dev/null || true)
    fi
    [ -n "$EXISTING_DB_PASS" ] && DB_PASS="$EXISTING_DB_PASS"

    sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 \
        || sudo -u postgres psql -c "CREATE ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS';"

    # Determine best available locale for the DB. en_US.UTF-8 is preferred; fall back
    # to C.UTF-8 or C. NetBox v4 ICU collations are per-column, not database-level,
    # so C locale works fine as long as the encoding is UTF8.
    _pick_db_locale() {
        apt-get install -y -q locales 2>/dev/null || true
        if locale-gen en_US.UTF-8 2>/dev/null && locale -a 2>/dev/null | grep -q 'en_US.UTF-8\|en_US.utf8'; then
            echo "en_US.UTF-8"
        elif locale -a 2>/dev/null | grep -q 'C.UTF-8\|C.utf8'; then
            echo "C.UTF-8"
        else
            echo "C"
        fi
    }
    DB_LOCALE=$(_pick_db_locale)
    _create_db() {
        sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER ENCODING 'UTF8' LC_COLLATE '$DB_LOCALE' LC_CTYPE '$DB_LOCALE' TEMPLATE template0;"
    }

    # Create database with UTF-8 encoding from template0 — required for NetBox v4 ICU
    # collations. SQL_ASCII (common in minimal LXC containers) causes migrate to fail.
    # If the DB already exists with wrong encoding, drop and recreate it.
    if sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
        DB_ENC=$(sudo -u postgres psql -Atc "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null || echo "UNKNOWN")
        if [ "$DB_ENC" != "UTF8" ]; then
            warn "Database '$DB_NAME' has encoding '$DB_ENC' — dropping and recreating as UTF-8 (locale: $DB_LOCALE)"
            sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid <> pg_backend_pid();" 2>/dev/null || true
            sudo -u postgres psql -c "DROP DATABASE $DB_NAME;"
            _create_db
        fi
    else
        _create_db
    fi

    sudo -u postgres psql -c "ALTER ROLE $DB_USER PASSWORD '$DB_PASS';" 2>/dev/null || true
    ok "PostgreSQL: database '$DB_NAME' ready (UTF-8)"
fi

# ============================================================
# C. REDIS
# ============================================================
if [ "$INSTALL_APP" = true ]; then
    step "Configuring Redis"
    systemctl enable --now redis-server
    ok "Redis running"
fi

# ============================================================
# D. NETBOX APPLICATION  (install or update)
# ============================================================
if [ "$INSTALL_APP" = true ]; then
    step "Installing / updating NetBox application"

    # Resolve latest stable tag
    if [ "$NB_VERSION" = "stable" ]; then
        NB_VERSION=$(curl -sf https://api.github.com/repos/netbox-community/netbox/releases/latest \
            | jq -r '.tag_name' 2>/dev/null || echo "v4.2.4")
        ok "Resolved NetBox version: $NB_VERSION"
    fi

    if [ -d "$NB_APP_DIR/.git" ]; then
        echo "   Existing install found — pulling $NB_VERSION"
        git -C "$NB_APP_DIR" fetch --tags -q
        git -C "$NB_APP_DIR" checkout "$NB_VERSION" -q 2>/dev/null \
            || git -C "$NB_APP_DIR" pull --rebase --autostash -q
    else
        echo "   Cloning netbox-community/netbox $NB_VERSION"
        git clone -q --depth 1 --branch "$NB_VERSION" \
            https://github.com/netbox-community/netbox.git "$NB_APP_DIR" \
            || git clone -q https://github.com/netbox-community/netbox.git "$NB_APP_DIR"
        git -C "$NB_APP_DIR" fetch --tags -q
        git -C "$NB_APP_DIR" checkout "$NB_VERSION" -q 2>/dev/null || true
    fi

    # Python venv — preserve on update (avoid full reinstall of large deps)
    if [ ! -f "$NB_APP_DIR/venv/bin/python3" ]; then
        python3 -m venv "$NB_APP_DIR/venv"
    fi
    # Install build tools first so wheel builds don't silently stall
    apt-get install -y -qq build-essential python3-dev libpq-dev libxml2-dev libxslt1-dev \
        libjpeg-dev zlib1g-dev libffi-dev libssl-dev 2>/dev/null || true
    echo "   Installing NetBox Python requirements (this takes a few minutes)..."
    "$NB_APP_DIR/venv/bin/pip" install --upgrade pip wheel --no-cache-dir
    "$NB_APP_DIR/venv/bin/pip" install -r "$NB_APP_DIR/requirements.txt" --no-cache-dir
    ok "NetBox Python requirements installed"

    # ── configuration.py — create on first run, preserve on update ──
    NB_CFG="$NB_APP_DIR/netbox/netbox/configuration.py"
    # Generate API_TOKEN_PEPPERS entry if not present regardless of whether
    # configuration.py exists — NetBox v4 requires this for v2 API tokens.
    NB_TOKEN_PEPPER="$(gen_secret)"

    if [ ! -f "$NB_CFG" ]; then
        NB_SECRET_KEY="$(gen_secret)"
        cat > "$NB_CFG" <<NBCFG
# Auto-generated by Lab Manager NetBox installer.
# Edit this file to customise your NetBox installation.
ALLOWED_HOSTS = ['*']

DATABASE = {
    'NAME': '$DB_NAME',
    'USER': '$DB_USER',
    'PASSWORD': '$DB_PASS',
    'HOST': 'localhost',
    'PORT': '',
    'CONN_MAX_AGE': 300,
}

REDIS = {
    'tasks':   {'HOST': 'localhost', 'PORT': 6379, 'DB': 0, 'SSL': False},
    'caching': {'HOST': 'localhost', 'PORT': 6379, 'DB': 1, 'SSL': False},
}

SECRET_KEY = '$NB_SECRET_KEY'

# Required by NetBox v4 for v2 API token creation.
API_TOKEN_PEPPERS = {
    0: '$NB_TOKEN_PEPPER',
}

# Uncomment to enable additional features:
# PLUGINS = []
# LOGIN_REQUIRED = True
NBCFG
        ok "configuration.py created"
    else
        ok "configuration.py already exists — preserving existing settings"
        # Ensure API_TOKEN_PEPPERS is present — required by NetBox v4 for v2 API tokens.
        # Append if the key is absent; never overwrite an existing entry.
        if ! grep -q "^API_TOKEN_PEPPERS" "$NB_CFG"; then
            printf '\n# Required by NetBox v4 for v2 API token creation.\nAPI_TOKEN_PEPPERS = {\n    0: '"'"'%s'"'"',\n}\n' "$NB_TOKEN_PEPPER" >> "$NB_CFG"
            ok "API_TOKEN_PEPPERS added to configuration.py"
            # Restart services immediately so the running NetBox picks up the new setting
            systemctl restart netbox netbox-rq 2>/dev/null || true
        fi
    fi

    # ── Migrations + static files ────────────────────────────
    step "Running database migrations"
    cd "$NB_APP_DIR"
    # Temporarily disable pipefail so a migration failure (common in LXC
    # containers with locale issues) falls back to spoke-only mode rather
    # than aborting the entire install before the LM connector is written.
    set +e
    "$NB_APP_DIR/venv/bin/python3" netbox/manage.py migrate --no-input -v 0
    MIGRATE_RC=$?
    set -e
    if [ $MIGRATE_RC -ne 0 ]; then
        warn "Database migration failed (exit $MIGRATE_RC) — this is common in LXC containers."
        warn "Falling back to spoke-only mode. You can complete the NetBox install later."
        INSTALL_APP=false
    else
        "$NB_APP_DIR/venv/bin/python3" netbox/manage.py collectstatic --no-input -v 0 2>/dev/null || true
        ok "Migrations and static files complete"
    fi

if [ "$INSTALL_APP" = false ]; then : ; else  # guard: skip if migrations failed

    # ── Superuser (skip if exists) ───────────────────────────
    SUPERUSER_EXISTS=$("$NB_APP_DIR/venv/bin/python3" netbox/manage.py shell -c \
        "from django.contrib.auth import get_user_model; U=get_user_model(); print(U.objects.filter(username='$NB_SUPERUSER').exists())" \
        2>/dev/null | tail -1 || echo "False")

    if [ "$SUPERUSER_EXISTS" != "True" ]; then
        DJANGO_SUPERUSER_PASSWORD="$NB_SUPERPASS" \
            "$NB_APP_DIR/venv/bin/python3" netbox/manage.py createsuperuser \
            --username "$NB_SUPERUSER" --email "$NB_SUPERMAIL" --noinput
        ok "Superuser '$NB_SUPERUSER' created"
    else
        ok "Superuser '$NB_SUPERUSER' already exists"
    fi

    # ── API token — retrieve existing or create ──────────────
    # Supports NetBox v4 (users.models.Token) and v3 (extras.models.Token).
    # Uses a sentinel line so stdout noise from Django startup doesn't corrupt the result.
    if [ -z "$NETBOX_TOKEN" ]; then
        TOKEN_OUTPUT=$("$NB_APP_DIR/venv/bin/python3" netbox/manage.py shell -c "
import sys, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
from django.contrib.auth import get_user_model
try:
    from users.models import Token
except ImportError:
    from extras.models import Token
import secrets
U = get_user_model()
try:
    user = U.objects.get(username='$NB_SUPERUSER')
    existing = Token.objects.filter(user=user).first()
    if existing:
        sys.stdout.write('LM_TOKEN:' + str(existing.key) + '\n')
    else:
        key = secrets.token_hex(20)
        t = Token(user=user, description='Lab Manager auto-generated')
        t.key = key
        t.save()
        sys.stdout.write('LM_TOKEN:' + str(t.key) + '\n')
except Exception as e:
    sys.stderr.write('Token creation failed: ' + str(e) + '\n')
" 2>&1)
        NETBOX_TOKEN=$(echo "$TOKEN_OUTPUT" | grep '^LM_TOKEN:' | cut -d: -f2-)
        if [ -n "$NETBOX_TOKEN" ]; then
            ok "API token retrieved/created: ${NETBOX_TOKEN:0:8}..."
        else
            warn "Could not create API token. Output was:"
            echo "$TOKEN_OUTPUT" | grep -v '^LM_TOKEN:' | head -10 >&2
            warn "Set NETBOX_API_TOKEN manually in $LM_DIR/netbox/.env after install"
        fi
    fi

    # ── Lab Manager Proxmox VMID-range custom fields + validators ──────────
    # Idempotent: safe on a fresh install and on a re-run. Adds integer custom
    # fields vmid_start/vmid_end to tenancy.tenant, ships a custom-validator
    # module (range start<=end + no overlap between tenants; a VM's
    # proxmox_vmid must fall inside its tenant's range), and injects
    # CUSTOM_VALIDATORS into configuration.py. Both validators are LENIENT when
    # a range is unset so the Proxmox→NetBox sync keeps working before/without
    # ranges — enforcement strengthens as tenants get ranges.
    step "Ensuring Lab Manager VMID-range custom fields + validators"
    NB_PROJECT_DIR="$NB_APP_DIR/netbox"   # manage.py lives here (on sys.path)

    # 1) Validator module — written to the project root so configuration.py can
    #    `import lm_custom_validators`. Overwritten each run (it is ours).
    cat > "$NB_PROJECT_DIR/lm_custom_validators.py" <<'LMCV'
"""Lab Manager custom validators for NetBox (loaded via CUSTOM_VALIDATORS in
configuration.py by the Lab Manager NetBox installer).

Enforces per-tenant Proxmox VMID allocation ranges:
  * ProxmoxRangeValidator (tenancy.tenant) — vmid_start <= vmid_end, and a
    tenant's [vmid_start, vmid_end] range must not overlap another tenant's.
  * ProxmoxVmidInRangeValidator (virtualization.virtualmachine) — a VM's
    proxmox_vmid custom field must fall inside its assigned tenant's range.

Both are LENIENT when a range is unset: a tenant with no vmid_start/vmid_end is
unconstrained (operators adopt ranges incrementally), and a VM whose tenant has
no range (or which has no proxmox_vmid) is skipped. This keeps the Lab Manager
Proxmox→NetBox sync working before/without ranges.

Imported by configuration.py at Django startup, so NetBox-internal model
imports are deferred into validate() (apps are loaded by then). Only
extras.validators (which itself imports no NetBox models) is imported at
module load.
"""
from extras.validators import CustomValidator


class ProxmoxRangeValidator(CustomValidator):
    """Validate a tenancy.tenant's Proxmox VMID range on create/save."""

    def validate(self, instance, request):
        cf = getattr(instance, "custom_field_data", {}) or {}
        start = cf.get("vmid_start")
        end = cf.get("vmid_end")
        # Lenient: no range set -> nothing to enforce.
        if start in (None, "") or end in (None, ""):
            return
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            self.fail("vmid_start/vmid_end must be integers", field="custom_fields")
            return
        if start > end:
            self.fail("vmid_start (%d) must be <= vmid_end (%d)" % (start, end),
                      field="custom_fields")
            return
        # No overlap with another tenant's range.
        from tenancy.models import Tenant
        qs = Tenant.objects.filter(
            custom_field_data__vmid_start__lte=end,
            custom_field_data__vmid_end__gte=start,
        )
        if instance.pk:
            qs = qs.exclude(pk=instance.pk)
        for other in qs:
            ocf = getattr(other, "custom_field_data", {}) or {}
            os_, oe = ocf.get("vmid_start"), ocf.get("vmid_end")
            if os_ in (None, "") or oe in (None, ""):
                continue  # JSON lookup matches missing keys as null; skip empties
            try:
                os_, oe = int(os_), int(oe)
            except (TypeError, ValueError):
                continue
            if os_ <= end and oe >= start:
                self.fail("VMID range [%d-%d] overlaps tenant '%s' [%d-%d]"
                          % (start, end, other.name, os_, oe),
                          field="custom_fields")
                return


class ProxmoxVmidInRangeValidator(CustomValidator):
    """Validate a virtualization.virtualmachine's proxmox_vmid is inside its
    assigned tenant's [vmid_start, vmid_end] range."""

    def validate(self, instance, request):
        cf = getattr(instance, "custom_field_data", {}) or {}
        vmid = cf.get("proxmox_vmid")
        # Lenient: not a Proxmox-sourced VM -> skip.
        if vmid in (None, ""):
            return
        try:
            vmid = int(vmid)
        except (TypeError, ValueError):
            return  # non-numeric proxmox_vmid — leave to other validation
        tenant_pk = getattr(instance, "tenant_id", None)
        if not tenant_pk:
            # Untagged/global VM — no tenant range to enforce. Lenient.
            return
        from tenancy.models import Tenant
        try:
            tenant = Tenant.objects.get(pk=tenant_pk)
        except Tenant.DoesNotExist:
            return
        tcf = getattr(tenant, "custom_field_data", {}) or {}
        start = tcf.get("vmid_start")
        end = tcf.get("vmid_end")
        # Lenient: tenant has no range -> skip.
        if start in (None, "") or end in (None, ""):
            return
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            return
        if not (start <= vmid <= end):
            self.fail("proxmox_vmid %d is outside tenant '%s' VMID range [%d-%d]"
                      % (vmid, tenant.name, start, end),
                      field="custom_fields")
LMCV
    ok "lm_custom_validators.py written"

    # 2) Inject CUSTOM_VALIDATORS into configuration.py (guarded, like
    #    API_TOKEN_PEPPERS) — append only if absent, never overwrite.
    if ! grep -q "^CUSTOM_VALIDATORS" "$NB_CFG"; then
        cat >> "$NB_CFG" <<'NBCUST'

# Lab Manager Proxmox VMID-range custom validators (added by the LM installer).
from lm_custom_validators import ProxmoxRangeValidator, ProxmoxVmidInRangeValidator
CUSTOM_VALIDATORS = {
    'tenancy.tenant': [ProxmoxRangeValidator()],
    'virtualization.virtualmachine': [ProxmoxVmidInRangeValidator()],
}
NBCUST
        ok "CUSTOM_VALIDATORS added to configuration.py"
        # Restart so the running NetBox loads the validators (also restarted
        # below after gunicorn setup, but this covers the re-run case where the
        # services are already up).
        systemctl restart netbox netbox-rq 2>/dev/null || true
    else
        ok "CUSTOM_VALIDATORS already present in configuration.py"
    fi

    # 3) Ensure vmid_start/vmid_end integer custom fields on tenancy.tenant
    #    (idempotent via get_or_create; content_types set each run).
    LM_CF_OUT=$("$NB_APP_DIR/venv/bin/python3" netbox/manage.py shell -c "
from extras.models import CustomField
from extras.choices import CustomFieldTypeChoices
from django.contrib.contenttypes.models import ContentType
from tenancy.models import Tenant
tct = ContentType.objects.get_for_model(Tenant)
for name, label in (('vmid_start', 'Proxmox VMID range start'), ('vmid_end', 'Proxmox VMID range end')):
    cf, created = CustomField.objects.get_or_create(name=name, defaults={'type': CustomFieldTypeChoices.TYPE_INTEGER, 'label': label, 'description': 'Proxmox VMID allocation range (Lab Manager)'})
    cf.content_types.set([tct])
print('LM_CF_OK')
" 2>/dev/null | tail -1 || echo "LM_CF_FAIL")
    if [ "$LM_CF_OUT" = "LM_CF_OK" ]; then
        ok "Proxmox VMID-range custom fields ensured on tenancy.tenant"
    else
        warn "VMID-range custom field creation did not report OK (continuing)"
    fi

    chown -R "$SVC_USER:$SVC_USER" "$NB_APP_DIR"

    # ── gunicorn service ─────────────────────────────────────
    step "Configuring NetBox services"
    cat > /etc/systemd/system/netbox.service <<SYSD
[Unit]
Description=NetBox WSGI Service
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$NB_APP_DIR/netbox
# svc_lm is created without a home dir; point HOME at the app dir (owned by
# svc_lm) so gunicorn/Django don't hit "Permission denied: '/home/svc_lm'".
Environment="HOME=$NB_APP_DIR"
ExecStart=$NB_APP_DIR/venv/bin/gunicorn --bind 127.0.0.1:$NB_PORT --workers 3 --timeout 120 --worker-tmp-dir /tmp netbox.wsgi
StandardOutput=append:/var/log/lm/lm-netbox.log
StandardError=append:/var/log/lm/lm-netbox.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSD

    cat > /etc/systemd/system/netbox-rq.service <<SYSD
[Unit]
Description=NetBox Request Queue Worker
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$NB_APP_DIR/netbox
Environment="HOME=$NB_APP_DIR"
ExecStart=$NB_APP_DIR/venv/bin/python3 manage.py rqworker high default low
StandardOutput=append:/var/log/lm/lm-netbox-worker.log
StandardError=append:/var/log/lm/lm-netbox-worker.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSD

    systemctl daemon-reload
    systemctl enable netbox netbox-rq
    systemctl restart netbox netbox-rq
    ok "NetBox services started (gunicorn on 127.0.0.1:$NB_PORT)"

    # ── nginx ────────────────────────────────────────────────
    step "Configuring nginx reverse proxy"
    cat > /etc/nginx/sites-available/netbox <<NGINX
server {
    listen 80;
    server_name _;

    client_max_body_size 25m;

    location /static/ {
        alias $NB_APP_DIR/netbox/static/;
    }

    location / {
        proxy_pass         http://127.0.0.1:$NB_PORT;
        proxy_set_header   Host              \$http_host;
        proxy_set_header   X-Forwarded-Host  \$http_host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
        proxy_buffer_size       32k;
        proxy_buffers           8 32k;
        proxy_busy_buffers_size 64k;
    }
}
NGINX

    ln -sf /etc/nginx/sites-available/netbox /etc/nginx/sites-enabled/netbox
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
    nginx -t && systemctl enable --now nginx && systemctl reload nginx
    ok "nginx configured — NetBox accessible on port 80"

    NETBOX_URL="http://localhost"
fi  # end migration-succeeded guard
fi  # end INSTALL_APP

# ============================================================
# E. LM NETBOX SPOKE  (install or update)
# ============================================================
step "Installing LM NetBox Spoke"

# Clone/update the spoke repo and build the venv FIRST — before any credential
# checks that could die early and leave the service file pointing at a missing venv.
mkdir -p "$LM_DIR"
if [ -d "$LM_DIR/netbox/.git" ]; then
    echo "   Existing spoke found — updating"
    git -C "$LM_DIR/netbox" pull --rebase --autostash origin main -q
else
    echo "   Cloning LM NetBox spoke"
    git clone -q https://github.com/lbockenstedt/netbox.git "$LM_DIR/netbox"
fi

# Spoke venv — always rebuild (small dep set, ensures clean state)
rm -rf "$LM_DIR/netbox/venv"
python3 -m venv "$LM_DIR/netbox/venv"
"$LM_DIR/netbox/venv/bin/pip" install --upgrade pip -q
[ -f "$LM_DIR/netbox/requirements.txt" ] && \
    "$LM_DIR/netbox/venv/bin/pip" install -r "$LM_DIR/netbox/requirements.txt" -q
ok "LM spoke dependencies installed"

# Preserve existing spoke secret across re-runs
EXISTING_SPOKE_SECRET=""
[ -f "$LM_DIR/netbox/.env" ] && \
    EXISTING_SPOKE_SECRET=$(grep "^SPOKE_SECRET=" "$LM_DIR/netbox/.env" | cut -d= -f2-)

if [ -z "$SPOKE_SECRET" ]; then
    if [ -n "$EXISTING_SPOKE_SECRET" ]; then
        SPOKE_SECRET="$EXISTING_SPOKE_SECRET"
        ok "Reusing existing spoke secret from .env"
    else
        warn "No pre-shared secret — spoke will connect unauthenticated and await admin approval in the LM WebUI."
    fi
fi

# Preserve existing NetBox API token across re-runs
EXISTING_NB_TOKEN=""
[ -f "$LM_DIR/netbox/.env" ] && \
    EXISTING_NB_TOKEN=$(grep "^NETBOX_API_TOKEN=" "$LM_DIR/netbox/.env" | cut -d= -f2-)
[ -n "$NETBOX_TOKEN" ] || NETBOX_TOKEN="$EXISTING_NB_TOKEN"

# Write .env — overwrite connection info but preserve existing token if none supplied
cat > "$LM_DIR/netbox/.env" <<DOTENV
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=${HUB_SECRET:-}
NETBOX_URL=${NETBOX_URL:-http://localhost}
NETBOX_API_TOKEN=${NETBOX_TOKEN:-}
KEA_CTRL_URL=http://localhost:8000
DOTENV
chmod 600 "$LM_DIR/netbox/.env"

# ── Provision Lab Manager custom fields via the REST API ──────────────
# Idempotent get-or-create for the custom fields the Proxmox/Hypervisor→IPAM
# and Firewall→IPAM syncs write to. Runs whenever we have a NETBOX_URL +
# NETBOX_TOKEN (local OR external NetBox), so it works in --spoke-only mode
# against the external NetBox — unlike the Django manage.py shell step above,
# which only runs on the full-app install path and is skipped when
# install_all.sh invokes us with --spoke-only. The spoke also self-heals these
# at startup (_ensure_custom_fields); this installer step is the durable
# provision-on-install the user asked for. Best-effort: a transient API
# failure warns and continues — it must never abort the spoke install.
if [ -n "${NETBOX_URL:-}" ] && [ -n "${NETBOX_TOKEN:-}" ] && command -v python3 >/dev/null 2>&1; then
    step "Ensuring Lab Manager custom fields via NetBox REST API"
    set +e
    "$LM_DIR/netbox/venv/bin/python3" - "$NETBOX_URL" "$NETBOX_TOKEN" <<'LMCF' | sed 's/^/   /'
import json, sys, urllib.request, urllib.error
url, token = sys.argv[1].rstrip("/"), sys.argv[2]
FIELDS = [
    ("proxmox_unique_id", "text", "Proxmox unique id", "virtualization.virtualmachine"),
    ("proxmox_vmid",      "text", "Proxmox VMID",       "virtualization.virtualmachine"),
    ("proxmox_node",      "text", "Proxmox node",       "virtualization.virtualmachine"),
    ("proxmox_type",      "text", "Proxmox type",       "virtualization.virtualmachine"),
    ("discovered_from",   "text", "Discovered from",    "dcim.device"),
    ("mac_address",       "text", "MAC address",        "ipam.ipaddress"),
    # mac_address also attaches to dcim.device (the access-tracker sync keys
    # existing-device matching off the device's MAC custom field). The GET-skip
    # below leaves an already-existing field as-is; the spoke self-heals the
    # extra content-type at startup (_ensure_custom_fields).
    ("mac_address",       "text", "MAC address",        "dcim.device"),
    ("switch_ip",         "text", "Switch IP",          "dcim.device"),
    ("switch_port",       "text", "Switch port",        "dcim.device"),
    ("last_seen",         "text", "Last seen",          "dcim.device"),
    ("vmid_start",        "integer", "Proxmox VMID range start", "tenancy.tenant"),
    ("vmid_end",          "integer", "Proxmox VMID range end",   "tenancy.tenant"),
]
hdr = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
def _req(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{url}{path}", data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read().decode() or "{}")
ok = miss = 0
for name, ftype, label, ct in FIELDS:
    try:
        st, resp = _req("GET", f"/api/extras/custom-fields/?name={name}")
        if resp.get("count", 0) > 0:
            ok += 1
            continue
        st, resp = _req("POST", "/api/extras/custom-fields/",
                        {"name": name, "type": ftype, "label": label, "content_types": [ct]})
        print(f"created {name} on {ct}")
        miss += 1
    except urllib.error.HTTPError as e:
        print(f"SKIP {name}: HTTP {e.code} {e.reason}")
    except Exception as e:
        print(f"SKIP {name}: {e}")
print(f"custom fields: {ok} present, {miss} created, {len(FIELDS)-ok-miss} skipped")
LMCF
    set -e
    ok "Lab Manager custom fields ensured (see lines above)"
else
    ok "Skipping REST custom-field provisioning (no NETBOX_URL/TOKEN or no python3) — spoke will self-heal at startup"
fi

# systemd unit
HUB_SECRET_ARG=""
[ -n "${HUB_SECRET:-}" ] && HUB_SECRET_ARG="--hub-secret ${HUB_SECRET}"
# Only pass --secret when a value is present. Passing it empty makes argparse
# abort with "argument --secret: expected one argument" and crash-loop the
# service. Zero-touch omits it (control_plane.py falls back to SPOKE_SECRET
# from the .env, then awaits admin approval in the WebUI).
SECRET_ARG=""
[ -n "$SPOKE_SECRET" ] && SECRET_ARG="--secret $SPOKE_SECRET"
cat > /etc/systemd/system/lm-netbox.service <<SYSD
[Unit]
Description=Lab Manager Spoke - NetBox IPAM
After=network.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$LM_DIR/netbox
EnvironmentFile=$LM_DIR/netbox/.env
Environment="PYTHONPATH=$LM_DIR:$LM_DIR/core/src:$LM_DIR/netbox/src"
Environment="HOME=$LM_DIR/netbox"
ExecStart=$LM_DIR/netbox/venv/bin/python3 -m src.control_plane --id $SPOKE_ID --hub $HUB_URL $SECRET_ARG $HUB_SECRET_ARG
StandardOutput=append:/var/log/lm/lm-netbox-spoke.log
StandardError=append:/var/log/lm/lm-netbox-spoke.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSD

systemctl daemon-reload
systemctl enable lm-netbox
systemctl restart lm-netbox
ok "LM NetBox spoke started"

# ── Register NetBox API token with the LM Hub ────────────────
if [ -n "$NETBOX_TOKEN" ] && [ -n "$HUB_URL" ]; then
    API_HOST=$(echo "$HUB_URL" | sed 's|wss\?://||' | cut -d: -f1)
    curl -sf -X POST "http://$API_HOST:8000/setup/netbox-config" \
        -H "Content-Type: application/json" \
        -d "{\"config\": {\"url\": \"http://localhost\", \"api_token\": \"$NETBOX_TOKEN\"}}" \
        2>/dev/null && ok "NetBox API token registered with LM Hub" \
        || warn "Could not auto-register token with Hub — set it manually in Setup → NetBox"
fi

chown -R "$SVC_USER:$SVC_USER" "$LM_DIR/netbox" 2>/dev/null || true

# ============================================================
# SUMMARY
# ============================================================
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "this-host")

echo ""
echo "══════════════════════════════════════════════════════"
ok "Installation complete!"
echo "══════════════════════════════════════════════════════"

if [ "$INSTALL_APP" = true ]; then
echo "  NetBox UI:        http://$LOCAL_IP/"
echo "  Admin user:       $NB_SUPERUSER"
[ "$SUPERUSER_EXISTS" != "True" ] && echo "  Admin password:   $NB_SUPERPASS"
echo "  API token:        ${NETBOX_TOKEN:-<see warning below>}"
echo ""
fi

echo "  LM Hub:           $HUB_URL"
echo "  Spoke ID:         $SPOKE_ID"
echo "  LM Spoke version: $(cat $LM_DIR/netbox/VERSION 2>/dev/null || echo unknown)"
echo ""

if [ -z "$NETBOX_TOKEN" ]; then
    warn "NETBOX_API_TOKEN is not set."
    echo "   Edit $LM_DIR/netbox/.env and add the token, then:"
    echo "   sudo systemctl restart lm-netbox"
fi

echo "Next steps:"
echo "  1. Open http://$LOCAL_IP/ → log in → Tenancy → Tenants → create your lab tenants"
echo "  2. In LM WebUI: Setup → NetBox Config → set URL + token if not auto-configured"
echo "  3. Setup → Tenant Config → link each LM tenant to a NetBox tenant slug"
echo ""
echo "  Approve the spoke in LM WebUI → Setup → Spoke Approvals"
echo "  Check spoke status: sudo systemctl status lm-netbox"
