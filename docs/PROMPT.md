# Prompt: Clean-room reengineering of a multimodal "Movement"-style visibility platform

> Copy everything below the line into a coding agent (Claude Code or similar). It is self-contained.

---

## Role

You are a senior platform engineer. Build **Waypoint**, an independent, clean-room supply-chain
visibility platform whose feature set is inspired by the publicly described capabilities of
project44's *Movement* platform. You must not use, request, decompile, scrape or imitate any
proprietary project44 code, API schemas, documentation behind a login, data, UI, logos or brand
names. Work only from the public capability list below and from general logistics domain knowledge.
Use original names for everything (product, endpoints, status codes, UI).

## Public capability reference (what Movement is described as doing)

From project44's public product pages and press coverage:

1. Multimodal tracking across truckload (TL), less-than-truckload (LTL), parcel, ocean, air and rail.
2. Order-level visibility: one customer order is stitched across several shipments and modes.
3. Predictive, multimodal ETAs from origin to final delivery.
4. Data quality: cleansing, normalization and enrichment of carrier data that arrives in many formats.
5. Exception management: surfacing late, stalled or at-risk shipments so teams act early.
6. Analytics: volume, on-time performance and tracking-coverage dashboards by carrier, lane and mode.
7. API-first integration with TMS / ERP / WMS systems, plus outbound notifications.
8. Collaboration: the right people (shippers, carriers, customers) see the same shipment state.
9. Security: data protected in transit and at rest; access only for authorized people.

## What to build (MVP scope)

A single deployable service with an API and a web dashboard.

**Domain model**
- `Organization` (single-tenant MVP, but keep an `org_id` on rows for later multi-tenancy).
- `User` with role `admin` | `member` | `viewer`, created **only by invitation** from an admin.
- `Carrier` (name, SCAC-like code, supported modes).
- `Order` (external reference, customer, list of shipments).
- `Shipment` (mode, carrier, reference, origin, destination, planned pickup/delivery, status,
  current position, computed ETA, optional order link).
- `Stop` (sequence, location name, lat/lon, planned and actual arrival/departure).
- `TrackingEvent` (shipment, timestamp, normalized status, raw status, lat/lon, source).
- `Exception` (shipment, type, severity, opened/resolved timestamps, message).
- `Webhook` (URL, secret, event types) for outbound notifications.

**Core services**
1. **Ingestion & normalization**: accept tracking updates via API (single + batch) and CSV upload.
   Map heterogeneous carrier status strings (e.g. "PU", "Picked up", "DEPARTED ORIGIN", "Gate out",
   "AE", "Out for delivery") to one canonical status set:
   `PLANNED, PICKED_UP, IN_TRANSIT, AT_STOP, OUT_FOR_DELIVERY, DELIVERED, CANCELLED`.
   Reject or quarantine malformed records with a reason; deduplicate identical events; validate coordinates.
2. **ETA engine**: transparent, testable baseline. Remaining great-circle distance divided by a
   mode-specific average speed plus mode-specific dwell per remaining stop. Update on every event.
   Design the interface so an ML model can replace it later.
3. **Exception engine**: rules evaluated on every event and on a periodic sweep:
   `LATE` (ETA after planned delivery + tolerance), `STALE_TRACKING` (no event for N hours per mode),
   `LONG_DWELL` (AT_STOP longer than threshold), `MISSED_PICKUP`. Auto-resolve when the condition clears.
4. **Order stitching**: order status and ETA derived from its shipments (status = status of the current,
   first undelivered leg; ETA cascades, so a downstream leg cannot start before the upstream leg arrives).
5. **Analytics**: on-time %, average delay, tracking coverage, open exceptions, all filterable
   by carrier, mode and lane (origin to destination).
6. **Notifications**: HMAC-signed webhooks for `shipment.status_changed`, `exception.opened`,
   `exception.resolved`; failures logged and never block ingestion.

**Access control (required, this is how the platform is shared with named people)**
- No public sign-up. The first admin is created from an environment-provided bootstrap token.
- Admins invite named people by email. An invitation produces a one-time, expiring token; the
  invitee sets a password. Admins can revoke users at any time.
- Session tokens for the dashboard, scoped API keys for system integrations.
- Passwords hashed with a slow KDF (scrypt/argon2/PBKDF2 ≥ 200k iterations). Tokens stored hashed.
- Every API route except health, login and invite acceptance requires authentication.
  `viewer` is read-only, `member` can ingest and edit, `admin` manages users, keys and webhooks.
- Audit log of logins, invites, revocations and key creation.

**Dashboard**
- Login page; shipment list with filters (mode, carrier, status, exceptions only); a map with
  current positions; shipment detail with timeline, stops, ETA and exceptions; analytics tiles;
  admin page for inviting and revoking users.
- Plain HTML/JS with a map library from a CDN is fine. No build step required.

**Non-functional**
- Python 3.11+ with FastAPI; SQLite for the MVP behind a thin repository layer so Postgres is a swap.
- OpenAPI docs auto-generated.
- `pytest` suite covering normalization, ETA, exception rules, order stitching, auth and roles.
- Seed script that generates realistic demo data across all six modes.
- Dockerfile and docker-compose; configuration only through environment variables; no secrets in git.
- README with run, test and deploy instructions.

## Deliverables

1. Source code in `app/` as described.
2. Passing tests (`pytest` green) and the command used to run them.
3. A short `ARCHITECTURE.md`: components, data flow, and what to change for production
   (Postgres, background workers, ML ETA, carrier connectors, multi-tenancy).
4. A list of what is intentionally out of scope for the MVP.

## Out of scope for the MVP

Live carrier/ELD/AIS integrations, freight procurement and settlement, port congestion intel,
sustainability/emissions reporting, ML-based ETAs, multi-tenant billing. Leave clean extension points.

## Working rules

- Keep it small, readable and runnable on a laptop with `docker compose up`.
- Prefer the standard library where it is enough.
- After building, run the tests and the seed script, start the server, and smoke-test the main
  API flows end to end. Report what you verified.
