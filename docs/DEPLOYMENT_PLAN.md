# Deployment plan: sharing Waypoint with named people only

Goal: the reengineered platform runs somewhere reachable on the internet, but only people you
explicitly name can see the code or use the app. Nothing is public.

There are two things to share, and each has its own gate:

| What | Who gets it | Gate |
|---|---|---|
| Source code | Developers / reviewers you name | Private GitHub repository, invited collaborators |
| Running app | Business users you name | Private deployment + app login by invitation (+ optional network gate) |

---

## 1. Code: private GitHub repository

1. Create a **private** repo, e.g. `ttschaffler/waypoint` (Settings → Visibility: Private).
2. Push the contents of `app/` plus the docs.
3. Invite each named person: Settings → Collaborators → *Add people*.
   Use **Read** for reviewers, **Write** for contributors, **Admin** only for yourself.
4. Protect `main`: require a pull request and passing CI before merge.
5. Add a GitHub Actions workflow that runs `pytest` on every PR and builds the Docker image.
6. Secrets (`WAYPOINT_SECRET_KEY`, `WAYPOINT_BOOTSTRAP_TOKEN`, deploy tokens) live only in
   GitHub → Settings → Secrets, never in the repo.

Removing someone = remove them as collaborator. Access stops immediately.

## 2. App: private hosted deployment

### Recommended: one container on a managed host (Fly.io, Render or Railway)

Why: cheapest path to a real HTTPS URL, automatic TLS, persistent volume for SQLite, deploy from
GitHub. Steps (Fly.io shown; Render/Railway are equivalent):

1. `fly launch --no-deploy` in `app/` (uses the included Dockerfile), pick a region close to users.
2. `fly volumes create waypoint_data --size 1` and mount it at `/data`
   (`WAYPOINT_DB=/data/waypoint.db`).
3. `fly secrets set WAYPOINT_SECRET_KEY=$(openssl rand -hex 32) WAYPOINT_BOOTSTRAP_TOKEN=$(openssl rand -hex 24)`
4. `fly deploy`. The app is now at `https://<name>.fly.dev`, HTTPS only.
5. Create the first admin once:
   `curl -X POST https://<name>.fly.dev/api/auth/bootstrap -H 'Content-Type: application/json' -d '{"token":"<bootstrap token>","email":"you@example.com","password":"<strong password>"}'`
   Then remove or rotate `WAYPOINT_BOOTSTRAP_TOKEN` (bootstrap is refused once an admin exists anyway).
6. Invite named users from the **Admin** tab in the dashboard (or `POST /api/users/invite`).
   Each person gets a one-time link valid for 72 hours, sets their own password, and gets a role:
   `viewer` (read-only), `member` (can ingest/edit), `admin`.
7. Revoke a person from the Admin tab; their sessions and keys stop working at once.

### Optional second gate (recommended for confidential data)

Put an identity-aware proxy in front so strangers cannot even reach the login page:

- **Cloudflare Access (Zero Trust, free up to 50 users)**: point a domain at the app through
  Cloudflare, create an Access application, policy "Allow → Emails → alice@…, bob@…".
  People log in with a one-time email code or Google/Microsoft SSO before the app loads.
- **Tailscale**: run the container on a VM in your tailnet and share the machine with named
  people (Tailscale "Share node"). No public URL at all.

With either, access is two-factor in practice: the proxy list of named emails, then the app invite.

### Alternative: self-hosted VM

Any small VM (Hetzner, DigitalOcean, Azure, AWS Lightsail): install Docker, copy the repo,
`docker compose up -d`, put Caddy in front for automatic HTTPS (Caddyfile included as an example
in the README), then add Cloudflare Access or Tailscale as above.

## 3. Operations checklist

- **Backups**: daily copy of the SQLite file (`fly ssh console -C "sqlite3 /data/waypoint.db .backup /data/backup.db"`,
  or a volume snapshot). Move to managed Postgres once more than a handful of people write data.
- **Updates**: merge to `main` → CI green → `fly deploy` (or a GitHub Action that deploys on tag).
- **Audit**: `GET /api/audit` (admin) shows logins, invites, revocations and key creation.
- **Secrets rotation**: rotating `WAYPOINT_SECRET_KEY` signs everyone out; do it after anyone with
  admin access leaves.
- **Data**: only load data you are allowed to share with every invited person. The MVP is
  single-tenant: all users in one deployment see the same shipments.

## 4. Who needs to do what

| Step | Who |
|---|---|
| Decide the list of named people and their roles | You |
| Create / pick the private GitHub repo | You (or Claude, once you say which repo) |
| Push code, add CI | Claude |
| Create the hosting account and run the first deploy | You (needs your billing account), Claude can script it |
| Send invites from the Admin tab | You |
