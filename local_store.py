"""
local_store.py - durable client-side storage for the kiosk
(spec items 9, 11, 12, 17).

Everything lives in `lab_client.db` (SQLite, WAL) next to the running app:

  local_logs  Every notable kiosk event with a client-generated UUID
              event_id, severity, category and a sync status
              (PENDING -> SYNCED).  Rows are NEVER deleted before the
              server acknowledges them (spec 17) - they survive
              disconnects, offline runs and client restarts (9/17).

  auth_cache  The last auth roster the server pushed: only accounts that
              were ever synced through the TLS channel can sign in while
              the server is down (spec 12 - no plaintext is ever stored,
              only the server's salted hash; a roster refresh replaces the
              cache wholesale, which is what revokes deleted/disabled
              accounts).

  meta        Small key/value settings that arrived with the roster
              (max_offline_days).

Nothing in this module ever raises into the kiosk: logging failures are
swallowed so the UI can never be disturbed by the log store.
"""

import os
import sqlite3
import threading
import time
import uuid

DB_NAME = "lab_client.db"

# reentrant: init (first call) runs while a caller may already hold it
_lock = threading.RLock()


def db_path():
    """Local DB lives next to the running app (works as .py AND frozen
    .exe, where __file__ points into the temp extraction folder)."""
    import sys
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, DB_NAME)


def _connect_raw():
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_local_connection():
    """Fresh connection; guarantees the tables exist first (item 17: the
    log store works on the very first event of a fresh install)."""
    _ensure()
    return _connect_raw()


def init_local_db():
    global _inited
    with _lock:
        conn = _connect_raw()          # never get_local_connection(): that
        try:                           # would re-enter _ensure() forever
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS local_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'INFO',
                    category TEXT DEFAULT '',
                    message TEXT DEFAULT '',
                    detail TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    pc_name TEXT DEFAULT '',
                    sync_status TEXT NOT NULL DEFAULT 'PENDING'
                );
                CREATE INDEX IF NOT EXISTS idx_local_logs_sync
                    ON local_logs(sync_status, id);
                CREATE TABLE IF NOT EXISTS auth_cache (
                    student_id TEXT PRIMARY KEY,
                    full_name TEXT,
                    role TEXT,
                    password_hash TEXT,
                    status TEXT,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            conn.commit()
            _inited = True
        finally:
            conn.close()


def _now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


_inited = False


def _ensure():
    """Tables must exist before the first read/write; runs init once
    (thread races are harmless - CREATE TABLE IF NOT EXISTS)."""
    global _inited
    if _inited:
        return
    try:
        init_local_db()
    except Exception:
        pass


# ==========================================================================
# Local event log (spec items 9, 11, 17)
# ==========================================================================
def log_event(severity, category, message, detail="", user_id="",
              pc_name=""):
    """Append one event to the durable local log.  Returns its UUID
    event_id, or "" on any failure (never raises)."""
    try:
        event_id = uuid.uuid4().hex
        with _lock:
            conn = get_local_connection()
            try:
                conn.execute(
                    "INSERT INTO local_logs (event_id, created_at, severity,"
                    " category, message, detail, user_id, pc_name, sync_status)"
                    " VALUES (?,?,?,?,?,?,?,?, 'PENDING')",
                    (event_id, _now_iso(), str(severity or "INFO"),
                     str(category or ""), str(message or ""),
                     str(detail or ""), str(user_id or ""),
                     str(pc_name or "")))
                conn.commit()
            finally:
                conn.close()
        return event_id
    except Exception:
        return ""


def pending_logs(limit=100):
    """Next batch of un-acknowledged events (spec 11: flushed in order,
    oldest first, and never dropped)."""
    try:
        with _lock:
            conn = get_local_connection()
            try:
                rows = conn.execute(
                    "SELECT event_id, created_at, severity, category,"
                    " message, detail, user_id, pc_name FROM local_logs"
                    " WHERE sync_status='PENDING' ORDER BY id LIMIT ?",
                    (int(limit),)).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def mark_synced(event_ids):
    """Mark the server-acknowledged events as SYNCED (spec 17: only an
    ack moves a row out of PENDING; the row itself stays for the local
    audit trail)."""
    if not event_ids:
        return 0
    changed = 0
    try:
        with _lock:
            conn = get_local_connection()
            try:
                for i in range(0, len(event_ids), 400):
                    chunk = [str(e) for e in event_ids[i:i + 400]]
                    cur = conn.execute(
                        "UPDATE local_logs SET sync_status='SYNCED'"
                        " WHERE event_id IN (%s)"
                        % ",".join("?" * len(chunk)), chunk)
                    changed += cur.rowcount or 0
                conn.commit()
            finally:
                conn.close()
    except Exception:
        return changed
    return changed


def log_counts():
    """(pending, synced, total) - used by tests and the local log view."""
    try:
        with _lock:
            conn = get_local_connection()
            try:
                row = conn.execute(
                    "SELECT"
                    " SUM(CASE WHEN sync_status='PENDING' THEN 1 ELSE 0 END)"
                    " AS pending,"
                    " SUM(CASE WHEN sync_status='SYNCED' THEN 1 ELSE 0 END)"
                    " AS synced, COUNT(*) AS total"
                    " FROM local_logs").fetchone()
            finally:
                conn.close()
        return (int(row["pending"] or 0), int(row["synced"] or 0),
                int(row["total"] or 0))
    except Exception:
        return (0, 0, 0)


def clear_synced_logs():
    """Housekeeping only: remove ACKNOWLEDGED rows (never PENDING ones -
    spec 17).  Kept for tests/reset flows."""
    try:
        with _lock:
            conn = get_local_connection()
            try:
                conn.execute(
                    "DELETE FROM local_logs WHERE sync_status='SYNCED'")
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


# ==========================================================================
# Auth roster cache (spec item 12)
# ==========================================================================
def cache_auth_roster(users, max_offline_days=7):
    """Replace the offline-login roster with the server's latest push.

    Wholesale replace is what enforces revocation: an account deleted or
    disabled on the server disappears from the cache on the very next
    sync, and can no longer offline-login.  `users` rows carry
    {student_id, full_name, role, password_hash, status,
    must_change_password} - the salted hash only, never plaintext."""
    synced_at = _now_iso()
    try:
        with _lock:
            conn = get_local_connection()
            try:
                conn.execute("DELETE FROM auth_cache")
                for u in users or []:
                    sid = str(u.get("student_id") or "").strip()
                    if not sid:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO auth_cache"
                        " (student_id, full_name, role, password_hash,"
                        "  status, must_change_password, synced_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (sid, str(u.get("full_name") or ""),
                         str(u.get("role") or ""),
                         str(u.get("password_hash") or ""),
                         str(u.get("status") or "Active"),
                         1 if u.get("must_change_password") else 0,
                         synced_at))
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value)"
                    " VALUES ('max_offline_days', ?)",
                    (str(int(max_offline_days or 7)),))
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value)"
                    " VALUES ('roster_synced_at', ?)", (synced_at,))
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception:
        return False


def get_meta(key, default=""):
    try:
        with _lock:
            conn = get_local_connection()
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            finally:
                conn.close()
        return row["value"] if row and row["value"] not in (None, "") \
            else default
    except Exception:
        return default


def roster_size():
    try:
        with _lock:
            conn = get_local_connection()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM auth_cache").fetchone()["n"]
            finally:
                conn.close()
        return int(n or 0)
    except Exception:
        return 0


def verify_offline_login(uid, password):
    """Offline sign-in against the last synced roster (spec items 10/12).

    Returns {"ok": bool, "error": str, "user": dict}.  Every rule is
    fail-closed:
      * the account must exist in the roster (roster-synced only),
      * it must still be Active,
      * accounts flagged must_change_password are online-only,
      * the roster must be younger than max_offline_days,
      * the password is checked against the server's salted hash - the
        plaintext is never stored anywhere on the client.
    """
    try:
        from database import verify_password
    except Exception:
        return {"ok": False,
                "error": "Offline sign-in unavailable (no verifier).",
                "user": {}}
    try:
        uid = str(uid or "").strip()
        with _lock:
            conn = get_local_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM auth_cache WHERE student_id=?",
                    (uid,)).fetchone()
            finally:
                conn.close()
    except Exception as e:
        return {"ok": False,
                "error": f"Offline sign-in unavailable ({e}).", "user": {}}

    if row is None:
        return {"ok": False,
                "error": "No offline access for this account. "
                         "Connect to the server once to sync it.",
                "user": {}}
    if str(row["status"] or "") != "Active":
        return {"ok": False, "error": "Invalid ID or password.", "user": {}}
    if int(row["must_change_password"] or 0):
        return {"ok": False,
                "error": "This password must be changed while connected "
                         "to the server.",
                "user": {}}

    try:
        max_days = int(get_meta("max_offline_days", "7") or 7)
    except (TypeError, ValueError):
        max_days = 7
    try:
        synced = time.mktime(time.strptime(
            str(row["synced_at"] or ""), "%Y-%m-%d %H:%M:%S"))
        age_days = (time.time() - synced) / 86400.0
    except Exception:
        age_days = max_days + 1      # unreadable timestamp -> expired
    if age_days > max_days:
        return {"ok": False,
                "error": f"Offline sign-in expired (last synced "
                         f"{int(age_days)} day(s) ago). Connect to the "
                         f"server once to refresh.",
                "user": {}}

    if not str(row["password_hash"] or "") or \
            not verify_password(str(password or ""), row["password_hash"]):
        return {"ok": False, "error": "Invalid ID or password.", "user": {}}

    return {"ok": True, "error": "", "user": {
        "student_id": row["student_id"],
        "full_name": row["full_name"],
        "role": row["role"],
        "must_change_password": False,
        "offline": True,
    }}


def export_logs(dest=None):
    """Write the whole local audit trail to a UTF-8 CSV file (P1-6).

    Used by `[Uninstall Client]` so removing the kiosk from this PC can
    never silently throw the log away.  Returns the absolute path of the
    export, or "" when it could not be written.  Never raises - a failed
    export must not be able to break the kiosk.
    """
    try:
        import csv
        fields = ("event_id", "created_at", "severity", "category",
                  "message", "detail", "user_id", "pc_name", "sync_status")
        with _lock:
            conn = get_local_connection()
            try:
                rows = conn.execute(
                    "SELECT %s FROM local_logs ORDER BY id ASC"
                    % ",".join(fields)).fetchall()
            finally:
                conn.close()
        if not dest:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(os.path.dirname(db_path()),
                                f"client_log_export_{stamp}.csv")
        with open(dest, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            for row in rows:
                writer.writerow([row[k] for k in fields])
        return os.path.abspath(dest)
    except Exception:
        return ""
