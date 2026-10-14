# Waypoint

A clean-room, multimodal supply-chain visibility platform: shipments across truckload, LTL, parcel,
ocean, air and rail, with normalized tracking, ETAs, exceptions, multimodal orders, analytics,
webhooks and invitation-only access.

Waypoint was built from publicly described product capabilities only. It contains no third-party
proprietary code, schemas, data or branding.

## Run locally

```bash
pip install -r requirements-dev.txt
export WAYPOINT_BOOTSTRAP_TOKEN=$(openssl rand -hex 24) WAYPOINT_SECRET_KEY=$(openssl rand -hex 32)
python -m waypoint.seed --reset          # demo data: ~40 shipments across all six modes
uvicorn waypoint.main:create_app --factory --reload
```

Create the first admin (only works while no admin exists):

```bash
curl -X POST localhost:8000/api/auth/bootstrap -H 'Content-Type: application/json' \
  -d "{\"token\":\"$WAYPOINT_BOOTSTRAP_TOKEN\",\"email\":\"you@example.com\",\"password\":\"a-long-password\"}"
```

Open http://localhost:8000, sign in, and invite people from the **Admin** tab.
API docs: http://localhost:8000/docs

With Docker: `cp .env.example .env`, fill it in, `docker compose up --build`.

## Test

```bash
pytest -q
```

## Sending tracking data

Any system with an API key (Admin tab, role `member`) can post updates. Status text is
normalized, so carriers can send their own wording or standard codes (X12 214, IATA, ocean milestones):

```bash
curl -X POST localhost:8000/api/events -H "Authorization: Bearer wpk_..." -H 'Content-Type: application/json' \
  -d '[{"shipment_ref":"TL-1000","timestamp":"2026-10-14T08:00:00Z","status":"X6","lat":41.9,"lon":-86.4}]'
```

CSV works too: `POST /api/events/csv` with columns
`shipment_ref,timestamp,status,lat,lon,stop_sequence,source`. Rejected rows are kept in
`GET /api/quarantine` with the reason.

Webhooks (Admin, API): `POST /api/webhooks` with `{"url": "...", "events": ["exception.opened"]}`.
Each delivery carries `X-Waypoint-Signature: sha256=<HMAC of body with the returned secret>`.

## Roles

| Role | Can |
|---|---|
| viewer | read shipments, orders, exceptions, analytics |
| member | + create shipments/orders/carriers, ingest events, cancel |
| admin | + invite and revoke people, API keys, webhooks, audit log |

## Deploy and share with named people

See `docs/DEPLOYMENT_PLAN.md`. Short version: private GitHub repo with invited collaborators for the
code; one container on Fly.io/Render (or a VM with `deploy/Caddyfile`) for the app; people get in only
through admin-issued invitations, optionally behind Cloudflare Access or Tailscale.
