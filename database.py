"""
database.py
Handles SQLite database creation, schema, and seeding for the
Computer Laboratory Management System (Client-Server Edition).
"""

import sqlite3
import os
import hashlib
import binascii
from datetime import datetime

DB_NAME = "lab_system.db"

# Factory password handed to freshly created client (student) accounts.
# The first login must replace it (users.must_change_password); carrying
# this value during auth also always counts as "still default".
DEFAULT_CLIENT_PASSWORD = "password123"


def get_db_path():
    """Keep the DB next to the running app (works both as .py and frozen .exe)."""
    import sys
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, DB_NAME)


def get_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Enable WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def hash_password(password: str, salt: bytes = None) -> str:
    """Return 'salt$hash' hex string using PBKDF2-HMAC-SHA256."""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return binascii.hexlify(salt).decode() + "$" + binascii.hexlify(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$")
        salt = binascii.unhexlify(salt_hex)
        test = hash_password(password, salt)
        return test == stored
    except Exception:
        return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('student','admin','staff','maintenance')),
    course TEXT,
    year_level TEXT,
    email TEXT,
    contact TEXT,
    status TEXT DEFAULT 'Active',
    must_change_password INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS computers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pc_name TEXT UNIQUE NOT NULL,
    specs TEXT,
    location TEXT,
    status TEXT DEFAULT 'Available',
    assigned_to TEXT,
    -- Client-side tracking fields
    ip_address TEXT,
    mac_address TEXT,
    hostname TEXT,
    cpu_percent REAL DEFAULT 0,
    ram_percent REAL DEFAULT 0,
    last_heartbeat TEXT,
    client_version TEXT,
    is_online INTEGER DEFAULT 0,
    admin_state TEXT DEFAULT '',
    connection_type TEXT DEFAULT '',
    group_name TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    full_name TEXT,
    pc_name TEXT,
    date TEXT,
    time_in TEXT,
    time_out TEXT,
    status TEXT DEFAULT 'In Lab'
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name TEXT NOT NULL,
    category TEXT,
    quantity INTEGER DEFAULT 0,
    available_qty INTEGER DEFAULT 0,
    assigned_qty INTEGER DEFAULT 0,
    condition_status TEXT,
    status TEXT DEFAULT 'AVAILABLE',
    location TEXT,
    notes TEXT,
    date_added TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS borrow_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity TEXT,
    borrow_date TEXT,
    return_date TEXT,
    status TEXT DEFAULT 'Pending Approval'
);

CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_name TEXT NOT NULL,
    issue TEXT,
    date_reported TEXT,
    date_resolved TEXT,
    technician TEXT,
    status TEXT DEFAULT 'Pending'
);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    message TEXT,
    posted_by TEXT,
    date_posted TEXT
);

CREATE TABLE IF NOT EXISTS websites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE NOT NULL,
    -- spec item 6: rules split into the two lists; enabled = rule toggle
    list_type TEXT NOT NULL DEFAULT 'blocked',
    enabled INTEGER NOT NULL DEFAULT 1,
    category TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT
);

-- spec items 7/8: desired policy + synchronization state per Client PC
CREATE TABLE IF NOT EXISTS web_pc_policy (
    pc_name TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    sync_status TEXT NOT NULL DEFAULT 'OFFLINE',
    enforced TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT,
    receiver TEXT,
    message TEXT,
    timestamp TEXT,
    is_read TEXT DEFAULT 'No'
);

-- NEW TABLES for Client-Server Architecture
CREATE TABLE IF NOT EXISTS client_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    pc_name TEXT NOT NULL,
    student_id TEXT NOT NULL,
    full_name TEXT,
    login_time TEXT NOT NULL,
    logout_time TEXT,
    duration_seconds INTEGER,
    ip_address TEXT,
    status TEXT DEFAULT 'Active',
    -- redesign (Sessions page): the PC's reported network medium at the
    -- moment the session started (Ethernet / Wi-Fi / LAN, '' = unknown).
    connection_type TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS admin_activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    timestamp TEXT NOT NULL,
    ip_address TEXT
);

CREATE TABLE IF NOT EXISTS client_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT UNIQUE NOT NULL,
    target_pc TEXT NOT NULL,
    command_type TEXT NOT NULL,
    params TEXT,
    admin_user TEXT,
    status TEXT DEFAULT 'Pending',
    created_at TEXT NOT NULL,
    executed_at TEXT,
    result TEXT
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    description TEXT
);

-- spec items 9/15/17: log events forwarded by Client PCs.  event_id is a
-- client-generated UUID and the PRIMARY KEY - the sync endpoint uses
-- INSERT OR IGNORE, so a resent batch can never duplicate a row and the
-- ack can safely mark every id as accepted (idempotent flush).
CREATE TABLE IF NOT EXISTS client_logs (
    event_id TEXT PRIMARY KEY,
    pc_name TEXT,
    user_id TEXT,
    severity TEXT NOT NULL DEFAULT 'INFO',
    category TEXT DEFAULT '',
    message TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at TEXT,
    received_at TEXT
);
"""


def _migrate_legacy_schema(cur):
    """Upgrade databases created by earlier (standalone) versions."""
    # 1) users table: older builds had CHECK(role IN ('student','admin'))
    #    or CHECK(role IN ('student','admin','staff')) - both predate the
    #    maintenance role.  Rebuild once against the current CHECK,
    #    copying every column the old table actually has (including the
    #    first-login flag, so pending password changes are never lost).
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if row and row[0] and "maintenance" not in row[0]:
        old_cols = [r[1] for r in
                    cur.execute("PRAGMA table_info(users)").fetchall()]
        keep = [c for c in ("id", "student_id", "password", "full_name",
                            "role", "course", "year_level", "email",
                            "contact", "status", "must_change_password",
                            "created_at")
                if c in old_cols]
        sel = ", ".join(keep)
        cur.executescript(f"""
            ALTER TABLE users RENAME TO users_old_v1;
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('student','admin','staff','maintenance')),
                course TEXT,
                year_level TEXT,
                email TEXT,
                contact TEXT,
                status TEXT DEFAULT 'Active',
                must_change_password INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users ({sel}) SELECT {sel} FROM users_old_v1;
            DROP TABLE users_old_v1;
        """)
        print("[DB] Migrated users table (added 'maintenance' role support).")

    # 2) users: first-login password flag (fresh client accounts must
    # replace their default password on first login)
    ucols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "must_change_password" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN "
                    "must_change_password INTEGER DEFAULT 0")
        print("[DB] Added users.must_change_password")

    # 2b) OJT system removed (spec item 3): the feature is gone from the
    #     UI and the API, so its two feature-specific tables are dropped
    #     from legacy databases too.  Nothing else references them.
    for tbl in ("ojt_interns", "tasks"):
        cur.execute(f"DROP TABLE IF EXISTS {tbl}")

    # 2c) inventory (spec item 5): the old TEXT quantity/remarks layout is
    #    rebuilt into integer quantity + available/assigned split, a
    #    status column and bookkeeping dates.  Row data (incl. remarks ->
    #    notes) is copied across; SQLite cannot ALTER a column's type.
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='inventory'"
    ).fetchone()
    if row and row[0] and "available_qty" not in row[0]:
        old_cols = [r[1] for r in
                    cur.execute("PRAGMA table_info(inventory)").fetchall()]
        has = lambda c: c in old_cols
        col = lambda c, alt: c if has(c) else alt
        cur.executescript(f"""
            ALTER TABLE inventory RENAME TO inventory_old_v1;
            CREATE TABLE inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                category TEXT,
                quantity INTEGER DEFAULT 0,
                available_qty INTEGER DEFAULT 0,
                assigned_qty INTEGER DEFAULT 0,
                condition_status TEXT,
                status TEXT DEFAULT 'AVAILABLE',
                location TEXT,
                notes TEXT,
                date_added TEXT,
                last_updated TEXT
            );
            INSERT INTO inventory (id, item_name, category, quantity,
                                   available_qty, assigned_qty,
                                   condition_status, status, location,
                                   notes, date_added, last_updated)
            SELECT id,
                   item_name,
                   {col("category", "NULL")},
                   CAST(COALESCE(NULLIF(quantity, ''), '0') AS INTEGER),
                   CAST(COALESCE(NULLIF(quantity, ''), '0') AS INTEGER),
                   0,
                   {col("condition_status", "NULL")},
                   CASE
                     WHEN {col("condition_status", "''")} = 'Damaged' THEN 'DAMAGED'
                     WHEN CAST(COALESCE(NULLIF(quantity, ''), '0') AS INTEGER) <= 0
                       THEN 'OUT OF STOCK'
                     ELSE 'AVAILABLE' END,
                   {col("location", "NULL")},
                   {col("remarks", "''")},
                   date('now'), date('now')
            FROM inventory_old_v1;
            DROP TABLE inventory_old_v1;
        """)
        print("[DB] Migrated inventory table (quantities/status/bookkeeping).")

    # 2d) websites (spec item 6): the flat block list becomes two rule
    #     lists (allowed/blocked) with an enable flag, category and
    #     bookkeeping.  Legacy rows were pure block-list entries.
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='websites'"
    ).fetchone()
    if row and row[0] and "list_type" not in row[0]:
        cur.executescript("""
            ALTER TABLE websites RENAME TO websites_old_v1;
            CREATE TABLE websites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT UNIQUE NOT NULL,
                list_type TEXT NOT NULL DEFAULT 'blocked',
                enabled INTEGER NOT NULL DEFAULT 1,
                category TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT
            );
            INSERT INTO websites (id, domain, list_type, enabled,
                                  category, notes, created_at)
                SELECT id, domain, 'blocked', 1, '', '', date('now')
                FROM websites_old_v1;
            DROP TABLE websites_old_v1;
        """)
        print("[DB] Migrated websites table (allowed/blocked rules).")

    # 3) computers table: LAN columns added in v2.0 / v2.1
    cols = [r[1] for r in cur.execute("PRAGMA table_info(computers)").fetchall()]
    for col, decl in [
        ("ip_address", "TEXT"),
        ("mac_address", "TEXT"),
        ("cpu_percent", "REAL DEFAULT 0"),
        ("ram_percent", "REAL DEFAULT 0"),
        ("last_heartbeat", "TEXT"),
        ("client_version", "TEXT"),
        ("is_online", "INTEGER DEFAULT 0"),
        ("hostname", "TEXT"),
        # JSON of the admin's desired state ({"cmd": "lock"/"pause", ...})
        # so a lock/pause is re-applied when a client reconnects.
        ("admin_state", "TEXT DEFAULT ''"),
        # LAN medium reported by the client heartbeat: Ethernet / Wi-Fi.
        ("connection_type", "TEXT DEFAULT ''"),
        # spec item 7: minimal PC group tag (Website Access scope target)
        ("group_name", "TEXT DEFAULT ''"),
    ]:
        if col not in cols:
            cur.execute(f"ALTER TABLE computers ADD COLUMN {col} {decl}")
            print(f"[DB] Added computers.{col}")

    # 4) sessions redesign: the network medium reported when the session
    #    started (display-only, captured once in server._on_session_start).
    sess_cols = [r[1] for r in
                 cur.execute("PRAGMA table_info(client_sessions)").fetchall()]
    if sess_cols and "connection_type" not in sess_cols:
        cur.execute("ALTER TABLE client_sessions ADD COLUMN "
                    "connection_type TEXT DEFAULT ''")
        print("[DB] Added client_sessions.connection_type")


def init_db():
    first_time = not os.path.exists(get_db_path())
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript(SCHEMA)
    _migrate_legacy_schema(cur)
    conn.commit()

    # Seed a default admin account if none exists
    cur.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO users (student_id, password, full_name, role, status) VALUES (?,?,?,?,?)",
            ("admin", hash_password("admin123"), "System Administrator", "admin", "Active"),
        )

    # Seed a default staff account if none exists
    cur.execute("SELECT COUNT(*) FROM users WHERE role='staff'")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO users (student_id, password, full_name, role, status) VALUES (?,?,?,?,?)",
            ("staff", hash_password("staff123"), "Lab Staff", "staff", "Active"),
        )

    # Seed a demo student account if none exists
    cur.execute("SELECT COUNT(*) FROM users WHERE role='student'")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO users (student_id, password, full_name, role, course, year_level, email, contact, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2023-00001", hash_password("student123"), "Juan Dela Cruz", "student",
             "BS Computer Science", "3rd Year", "juan@example.com", "09171234567", "Active"),
        )

    # NOTE: demo computers are intentionally NOT seeded. The dashboard only
    # ever shows PCs that really connected to this server (status grid filter
    # on last_heartbeat / is_online), so a fresh install starts with an empty
    # PC grid instead of 10 fake "offline" rows.
    #
    # One-time cleanup for databases created by older versions: remove the
    # leftover demo rows (they carry hardware specs but have never
    # connected). Manually added inventory rows have specs NULL and stay.
    try:
        cur.execute("DELETE FROM computers WHERE specs IS NOT NULL "
                    "AND last_heartbeat IS NULL AND is_online=0")
    except Exception:
        pass

    # Insert default system settings
    default_settings = [
        ("server_port", "8443", "TCP port for secure server communication"),
        ("heartbeat_interval", "5", "Client heartbeat interval in seconds"),
        ("command_timeout", "30", "Command execution timeout in seconds"),
        ("screen_observe_interval", "1.0", "Screen observation capture interval (seconds)"),
        ("screenshot_quality", "70", "JPEG quality for screenshots (1-100)"),
        ("screenshot_scale", "0.75", "Screenshot scale factor (0.1-1.0)"),
        ("max_failed_logins", "5", "Max failed login attempts before lockout"),
        ("lockout_duration", "300", "Lockout duration in seconds"),
        ("low_stock_threshold", "3",
         "Warn when an inventory item's available quantity is at or below this"),
        ("web_mode", "allow_all",
         "Default Website Access mode: allow_all | block_list | allow_only"),
        ("web_policy_version", "0",
         "Monotonic Website Access policy version (bumped on every change)"),
        ("upstream_dns", "1.1.1.1",
         "Upstream DNS resolver used by the client-side policy filter"),
        ("max_offline_days", "7",
         "Days a client may offline-login from its last synced auth roster"),
    ]
    for key, value, desc in default_settings:
        # Upsert, not INSERT OR IGNORE: a database whose description was
        # dropped by the old REPLACE form of set_setting gets its field
        # label back here, while a user's saved value is never touched
        # (only the description column is updated, and only when it is
        # NULL).
        cur.execute(
            "INSERT INTO system_settings (key, value, description) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET description = excluded.description "
            "WHERE system_settings.description IS NULL",
            (key, value, desc)
        )

    # Migrate old small-preview screenshot defaults to readable ones
    # (only when they still hold the previous default values).
    cur.execute("UPDATE system_settings SET value='70' "
                "WHERE key='screenshot_quality' AND value='50'")
    cur.execute("UPDATE system_settings SET value='0.75' "
                "WHERE key='screenshot_scale' AND value='0.5'")

    conn.commit()
    conn.close()
    return first_time


def get_setting(key: str, default: str = "") -> str:
    """Get a system setting value."""
    conn = get_connection()
    row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    """Set a system setting value.

    Upsert rather than ``INSERT OR REPLACE``: the REPLACE form deleted the
    matched row and re-inserted it with only (key, value), which silently
    dropped the description.  The settings page renders that column as its
    field label, so the old form turned every saved page into raw keys."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO system_settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()