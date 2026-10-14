"""SQLite storage. Kept behind plain SQL in one module so a Postgres swap stays local."""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL DEFAULT 1,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','member','viewer')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('member','viewer')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS carriers (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL DEFAULT 1,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    modes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL DEFAULT 1,
    ref TEXT UNIQUE NOT NULL,
    customer TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL DEFAULT 1,
    ref TEXT UNIQUE NOT NULL,
    mode TEXT NOT NULL,
    carrier_id INTEGER REFERENCES carriers(id),
    order_id INTEGER REFERENCES orders(id),
    planned_pickup TEXT NOT NULL,
    planned_delivery TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    cur_lat REAL,
    cur_lon REAL,
    last_event_at TEXT,
    status_since TEXT,
    eta TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stops (
    id INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    planned_arrival TEXT,
    actual_arrival TEXT,
    actual_departure TEXT,
    UNIQUE (shipment_id, seq)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_status TEXT NOT NULL,
    lat REAL,
    lon REAL,
    stop_seq INTEGER,
    source TEXT,
    fingerprint TEXT UNIQUE NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_shipment ON events(shipment_id, ts);
CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_exc_open ON exceptions(shipment_id, type, resolved_at);
CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    events TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id),
    event TEXT NOT NULL,
    status_code INTEGER,
    error TEXT,
    ts TEXT NOT NULL
);
"""


class Database:
    """One shared connection guarded by a lock: plenty for an MVP behind a single process."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def tx(self):
        return _Tx(self)

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params=()) -> int:
        with self.lock:
            return self.conn.execute(sql, params).lastrowid


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
        return False
