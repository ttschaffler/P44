# Architecture

```
carriers / TMS / CSV ──► POST /api/events ──► normalize ──► dedupe ──► apply event ──► ETA engine ──► exception rules
                                                │                         │                              │
                                           quarantine              shipments, stops,            exceptions table
                                                                     events tables                       │
dashboard / API clients ◄── shipments, orders (stitched), analytics ◄──┘            webhooks (HMAC) ◄────┘
                                 periodic sweep re-runs ETA + rules every N seconds
```

| Module | Responsibility |
|---|---|
| `normalize.py` | Canonical status set, alias vocabulary, record validation, fingerprints for dedupe |
| `eta.py` | Mode profiles (speed, dwell, tolerances) and the baseline ETA model behind an `EtaModel` protocol |
| `rules.py` | `LATE`, `STALE_TRACKING`, `LONG_DWELL`, `MISSED_PICKUP`; opens, updates and auto-resolves |
| `tracking.py` | Shipment lifecycle, event application (no rewinding on late data), order stitching, analytics |
| `auth.py` / `security.py` | Bootstrap admin, invitations, scrypt passwords, keyed token hashes, roles, audit |
| `webhooks.py` | Signed outbound notifications; failures are logged and never block ingestion |
| `main.py` | FastAPI routes, background sweep, dashboard, security headers |
| `seed.py` | Deterministic demo data across all six modes, including two multimodal orders |

Key decisions:

- **Canonical statuses** (`PLANNED → PICKED_UP → IN_TRANSIT ⇄ AT_STOP → OUT_FOR_DELIVERY → DELIVERED`, plus `CANCELLED`).
  Status never moves backwards; late-arriving older events go into the timeline without changing current state.
- **Order status** follows the current leg (first undelivered leg in planned order); order ETA cascades, pushing upstream delay onto legs that have not started.
- **Tokens** (sessions, API keys, invites) are random and stored only as HMAC hashes keyed by `WAYPOINT_SECRET_KEY`,
  so a database leak does not leak usable tokens and rotating the key signs everyone out.

## Path to production

1. **Postgres** instead of SQLite (all SQL lives in `db.py`, `tracking.py`, `rules.py`, `auth.py`); add migrations (Alembic).
2. **Workers**: move ingestion, sweep and webhooks onto a queue (e.g. Redis/RQ or a managed queue) with retries.
3. **Carrier connectors**: adapters that pull from carrier APIs, ELD/telematics, AIS (ocean) and flight data and post into the same ingestion path.
4. **ML ETAs**: implement `EtaModel.predict` with a model trained on historical events; keep the baseline as fallback.
5. **Multi-tenancy**: `org_id` already sits on users, carriers, orders and shipments (always 1 today); add an orgs table, scope every query by it, and add per-org SSO (OIDC/SAML).
6. **Observability**: structured logs, metrics on ingestion lag and ETA error, alerting.

## Out of scope for the MVP

Live carrier integrations, freight procurement and settlement, port-congestion intelligence,
emissions reporting, ML ETAs, multi-tenant billing, SSO.
