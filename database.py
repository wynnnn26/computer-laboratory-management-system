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
    role TEXT NOT NULL CHECK(role IN ('student','admin','staff')),
    course TEXT,
    year_level TEXT,
    email TEXT,
    contact TEXT,
    status TEXT DEFAULT 'Active',
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
    cpu_percent REAL DEFAULT 0,
    ram_percent REAL DEFAULT 0,
    last_heartbeat TEXT,
    client_version TEXT,
    is_online INTEGER DEFAULT 0
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
    quantity TEXT,
    condition_status TEXT,
    location TEXT,
    remarks TEXT
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

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT,
    receiver TEXT,
    message TEXT,
    timestamp TEXT,
    is_read TEXT DEFAULT 'No'
);

CREATE TABLE IF NOT EXISTS ojt_interns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    skills TEXT,
    contact TEXT,
    school TEXT,
    status TEXT DEFAULT 'Active'
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intern_name TEXT NOT NULL,
    task_title TEXT NOT NULL,
    description TEXT,
    assigned_date TEXT,
    due_date TEXT,
    status TEXT DEFAULT 'Not Started',
    progress TEXT DEFAULT '0%'
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
    status TEXT DEFAULT 'Active'
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
"""


def _migrate_legacy_schema(cur):
    """Upgrade databases created by earlier (standalone) versions."""
    # 1) users table: older builds had CHECK(role IN ('student','admin'))
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if row and row[0] and "staff" not in row[0]:
        cur.executescript("""
            ALTER TABLE users RENAME TO users_old_v1;
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('student','admin','staff')),
                course TEXT,
                year_level TEXT,
                email TEXT,
                contact TEXT,
                status TEXT DEFAULT 'Active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users (id, student_id, password, full_name, role, course,
                               year_level, email, contact, status)
                SELECT id, student_id, password, full_name, role, course,
                       year_level, email, contact, status FROM users_old_v1;
            DROP TABLE users_old_v1;
        """)
        print("[DB] Migrated users table (added 'staff' role support).")

    # 2) computers table: LAN columns added in v2.0
    cols = [r[1] for r in cur.execute("PRAGMA table_info(computers)").fetchall()]
    for col, decl in [
        ("ip_address", "TEXT"),
        ("mac_address", "TEXT"),
        ("cpu_percent", "REAL DEFAULT 0"),
        ("ram_percent", "REAL DEFAULT 0"),
        ("last_heartbeat", "TEXT"),
        ("client_version", "TEXT"),
        ("is_online", "INTEGER DEFAULT 0"),
    ]:
        if col not in cols:
            cur.execute(f"ALTER TABLE computers ADD COLUMN {col} {decl}")
            print(f"[DB] Added computers.{col}")


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

    # Seed a few sample computers if none exist
    cur.execute("SELECT COUNT(*) FROM computers")
    if cur.fetchone()[0] == 0:
        for i in range(1, 11):
            cur.execute(
                "INSERT INTO computers (pc_name, specs, location, status, assigned_to) VALUES (?,?,?,?,?)",
                (f"PC-{i:02d}", "Intel i5 / 8GB RAM / 256GB SSD", "Computer Lab 1", "Available", ""),
            )

    # Insert default system settings
    default_settings = [
        ("server_port", "8443", "TCP port for secure server communication"),
        ("heartbeat_interval", "5", "Client heartbeat interval in seconds"),
        ("command_timeout", "30", "Command execution timeout in seconds"),
        ("screen_observe_interval", "1.0", "Screen observation capture interval (seconds)"),
        ("screenshot_quality", "50", "JPEG quality for screenshots (1-100)"),
        ("screenshot_scale", "0.5", "Screenshot scale factor (0.1-1.0)"),
        ("max_failed_logins", "5", "Max failed login attempts before lockout"),
        ("lockout_duration", "300", "Lockout duration in seconds"),
    ]
    for key, value, desc in default_settings:
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (key, value, description) VALUES (?,?,?)",
            (key, value, desc)
        )

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
    """Set a system setting value."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?,?)",
        (key, value)
    )
    conn.commit()
    conn.close()