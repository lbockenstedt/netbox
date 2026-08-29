# AGENTS.md — `netbox`

**NetBox IPAM/DCIM module.** Installs NetBox itself *and* the LM spoke in one shot.

- **Repo:** `github.com/lbockenstedt/netbox`
- **Module type:** `module_type = "ipam"`
- **Canonical docs:** [`lm/docs/netbox.md`](../lm/docs/netbox.md) *(in the `lm` repo — the master registry)*
- **Fleet map:** [`../AGENTS.md`](../AGENTS.md) *(only present in a side-by-side checkout)*

## Context

This repo is **one of 16** that make up **Lab Manager (LM)** — a hub-and-spoke
"single pane of glass" orchestrator for lab/datacenter infrastructure. One hub (the `lm`
repo) runs the control plane, REST API and WebUI. Every other repo is a **spoke** wrapping
exactly one external system and dialling the hub over a WebSocket on port 443.

Read [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) — a verbatim
copy also lives in this repo's `docs/` — before making structural changes.

## Layout

`src/netbox_spoke.py` + `control_plane.py` drive it. Domain logic splits by concern:
`netbox_ipam.py`, `netbox_dcim.py`, `netbox_tenancy.py`, `netbox_sync.py`, `netbox_vmsync.py`,
`netbox_dedupe.py`, `netbox_staleness.py`, `netbox_changelog.py`, `netbox_xlsx.py`.
`seed_catalog.json` seeds the initial object catalog.

## netbox-specific gotchas

- `install.sh` provisions the **full NetBox stack** (PostgreSQL, Redis, gunicorn, nginx) *and* the spoke. It is safe to re-run: it updates code, migrates and restarts without touching data.
- **NetBox tenant IDs are half of the fleet's multitenancy model** (Proxmox labels are the other half). Changes to tenancy handling ripple fleet-wide.
- `install_kea.sh` and `setup-agent-host.sh` are separate concerns — see `DEPLOY-AGENT-CERT.md`.
- `API_SPEC.md` documents the spoke's own surface.

## Fleet conventions (identical in every LM repo)

- **Python 3.11**, FastAPI + `websockets` + `asyncio`. WebUI is dependency-free vanilla JS — **no npm build step exists anywhere in this project**.
- **`VERSION` is `MAJOR.NN` and branch-owned.** A bot bumps the last segment. **Never bump it by hand.** Promotion carries code only.
- **Branching: `dev -> qa -> main`.** `qa` and `main` need a PR; `ci.yml` is the required check. Direct pushes to `dev` are allowed.
- **CI runs one pytest process per component.** Components share top-level module names (`control_plane.py` exists in most repos) and collide in a single process.
- **Installers are idempotent** — re-running updates code and preserves credentials. Common flags: `--hub` (bare hostname is normalised to `wss://...:443`), `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs`.
- **Transport:** WebSocket on 443, mailbox pattern, **push-ack-retry — no fire-and-forget**. Heartbeat 30s; yellow at >=120s, red at >=300s. Hub queues 24h for offline spokes.
- **TLS:** encrypted but **verify-OFF by default** (self-signed hub cert). Verification is opt-in at install time via `--tls-verify` / `--tls-ca-cert` — never by hand-editing `.env`.
- **Heavy lifting belongs in the spoke, not the hub.** The hub is transport, state, policy and UI. See `lm/docs/architecture-spoke-heavy-lifting.md`.
- **API-first:** every operation exposes an API; the WebUI only ever calls that API.
- **Atomic transactions:** a mid-chain failure rolls back every preceding step and reports a before/after diff. No zombie resources.
- **Multitenancy is not optional:** isolation rides on Proxmox labels + NetBox tenant IDs. New resources carry tenant context.

## Rules

1. **One repo per change.** Cross-repo work is separate PRs, and the wire contract must stay backward-compatible because the two sides deploy independently.
2. **Read the canonical doc first** (linked above) — it is usually more current than this repo's README.
3. **Never hand-edit `VERSION`.**
4. **Check you are editing the live path,** not a preserved legacy one.
5. Match surrounding style. Comment only what needs clarifying.
